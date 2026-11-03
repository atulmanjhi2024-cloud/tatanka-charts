"""Login: one password for the app, set under Connections > Login (or `python -m chartsrv.auth set`). Until one is set
the app is open to whoever reaches it, with Tailscale the only lock. Each device signs in once and stays signed in
(SESSION_DAYS, renewed as it is used, the iPhone app too); a device can be signed out from any other, or all at once.

The password is kept as a scrypt hash; a session is a random token in an HttpOnly cookie, kept here as its SHA-256.
Forgot it: `python -m chartsrv.auth off` on the PC (Docker: `docker compose exec chart python -m chartsrv.auth off`)
turns the login off, then set a new one in the app.

    python -m chartsrv.auth set     # asks for a new password
    python -m chartsrv.auth off     # no password, every device signed out
"""
import getpass
import hashlib
import hmac
import secrets
import sys
import time

SETTING = "auth"
COOKIE = "tk_session"
SESSION_DAYS = 180
RENEW_S = 86400                 # a session in use is renewed (and its cookie re-sent) once a day
MIN_LEN, MAX_LEN = 8, 200
FAIL_MAX, FAIL_WINDOW_S = 10, 600   # wrong passwords from one address before the login waits
FAIL_ALL = 30                       # from all addresses together (someone trying from many), before every login waits
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, created INTEGER NOT NULL, seen INTEGER NOT NULL,
    label TEXT NOT NULL) WITHOUT ROWID;
"""


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def make_hash(password: str) -> dict:
    if not isinstance(password, str) or not MIN_LEN <= len(password) <= MAX_LEN:
        raise ValueError(f"a password of {MIN_LEN}-{MAX_LEN} characters")
    salt = secrets.token_bytes(16)
    return {"hash": _hash(password, salt).hex(), "salt": salt.hex(), "set": int(time.time() * 1000)}


def verify(password, cfg: dict) -> bool:
    if not cfg.get("hash") or not isinstance(password, str) or len(password) > MAX_LEN:
        return False
    return hmac.compare_digest(_hash(password, bytes.fromhex(cfg["salt"])).hex(), cfg["hash"])


class Auth:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript(SCHEMA)
        self.fails: dict[str, list[float]] = {}

    @property
    def enabled(self) -> bool:
        return bool((self.store.get_setting(SETTING, {}) or {}).get("hash"))

    # -- the password (hashing is slow on purpose: callers on the event loop run make_hash/verify in a thread) --
    def cfg(self) -> dict:
        return self.store.get_setting(SETTING, {}) or {}

    def save(self, cfg: dict):
        self.store.set_setting(SETTING, cfg)

    def set_password(self, password: str):
        self.save(make_hash(password))

    def check(self, password) -> bool:
        return verify(password, self.cfg())

    def turn_off(self):
        self.store.set_setting(SETTING, {})
        self.end_all()

    def locked(self, who: str) -> int:
        """Seconds until `who` may try again after too many wrong passwords (0: now)."""
        now = time.time()
        for k in list(self.fails):   # forget the old ones, and addresses with none left
            recent = [t for t in self.fails[k] if now - t < FAIL_WINDOW_S]
            if recent:
                self.fails[k] = recent
            else:
                del self.fails[k]
        mine = self.fails.get(who, [])
        if len(mine) >= FAIL_MAX:
            return int(FAIL_WINDOW_S - (now - mine[0])) + 1
        every = sorted(t for v in self.fails.values() for t in v)
        return int(FAIL_WINDOW_S - (now - every[0])) + 1 if len(every) >= FAIL_ALL else 0

    def failed(self, who: str):
        self.fails.setdefault(who, []).append(time.time())

    def attempt(self, who: str) -> int:
        """Before checking a password: the seconds to wait (0: go ahead). A try is counted as wrong before the
        (slow) check, so many sent at once can't all slip in under the limit; ok() takes it back."""
        wait = self.locked(who)
        if not wait:
            self.failed(who)
        return wait

    def ok(self, who: str):
        self.fails.pop(who, None)

    # -- sessions --
    def start(self, label: str) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time() * 1000)
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (_digest(token), now, now, (label or "a device")[:60]))
        return token

    def session(self, token: str | None, touch: bool = True) -> dict | None:
        """The session the cookie names, if it is still good: {id, created, seen, label, renew}. renew: the day's
        renewal was made, so the caller sends the cookie again; touch=False only checks it (the live connection,
        which can't send a cookie)."""
        if not token or len(token) > 100:
            return None
        d = _digest(token)
        r = self.db.execute("SELECT created, seen, label FROM sessions WHERE token=?", (d,)).fetchone()
        now = int(time.time() * 1000)
        if not r or now - r[1] > SESSION_DAYS * 86_400_000:
            if r:
                self.end(token)
            return None
        renew = touch and now - r[1] > RENEW_S * 1000
        if renew:
            with self.db:
                self.db.execute("UPDATE sessions SET seen=? WHERE token=?", (now, d))
        return {"id": d[:12], "created": r[0], "seen": now if renew else r[1], "label": r[2], "renew": renew}

    def sessions(self) -> list[dict]:
        """The signed-in devices. One not used for SESSION_DAYS has signed out by itself: it goes here, as it does
        when its cookie comes back (session())."""
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE seen<?", (int(time.time() * 1000) - SESSION_DAYS * 86_400_000,))
        return [{"id": t[:12], "created": c, "seen": s, "label": lb}
                for t, c, s, lb in self.db.execute("SELECT token, created, seen, label FROM sessions ORDER BY seen DESC")]

    def end(self, token: str) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM sessions WHERE token=?", (_digest(token),)).rowcount > 0

    def end_id(self, sid: str) -> bool:
        if not sid or len(sid) != 12 or not all(c in "0123456789abcdef" for c in sid):
            return False
        with self.db:
            return self.db.execute("DELETE FROM sessions WHERE substr(token, 1, 12)=?", (sid,)).rowcount > 0

    def end_all(self, keep: str | None = None) -> int:
        with self.db:
            return self.db.execute("DELETE FROM sessions WHERE token != ?", (_digest(keep) if keep else "",)).rowcount


def main(argv=None):
    from .config import load_settings
    from .store import Store
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] not in (["set"], ["off"]):
        print(__doc__.split("\n\n")[-1])
        return 2
    store = Store(load_settings().db_path)
    a = Auth(store)
    if argv[0] == "off":
        a.turn_off()
        print("login off; every device signed out. Set a new password in the app: Connections > Login.")
    else:
        pw = getpass.getpass("new password: ")
        if pw != getpass.getpass("again: "):
            print("they differ; nothing changed")
            return 1
        a.set_password(pw)
        a.end_all()
        print("password set; every device signs in again")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
