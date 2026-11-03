"""App notifications: Web Push to the installed app on an iPhone (iOS 16.4+, added to the Home Screen), Android, or a
PC (the app installed from Edge or Chrome), with no other app or account. Each device subscribes from the app
(Connections > App notifications > Turn on for this device); alerts then reach it like Discord or ntfy do.

The server signs with its own key pair (VAPID, made once and kept in the database) and encrypts each message for the
device (RFC 8291, aes128gcm), so Apple's, Google's or Microsoft's push service only carries it. Device addresses are
secrets: never logged or sent back to a browser."""
import base64
import json
import logging
import os
import sqlite3
import struct
import time
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)
SETTING = "push_vapid"
SUBJECT = os.environ.get("CHART_PUSH_SUBJECT", "").strip() or "https://github.com/tatankatrading/tatanka-charts"   # the push services' contact
TTL_S = 4 * 3600   # an alert older than this is no news
SERVICES = {   # push service hosts a subscription may point at: the browsers' own, nothing else
    "push.apple.com": "Apple", "fcm.googleapis.com": "Google", "push.services.mozilla.com": "Firefox",
    "notify.windows.com": "Windows",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subs (id INTEGER PRIMARY KEY, endpoint TEXT NOT NULL UNIQUE, p256dh TEXT NOT NULL,
    auth TEXT NOT NULL, label TEXT NOT NULL, created INTEGER NOT NULL, last_ok INTEGER, last_error TEXT NOT NULL DEFAULT '');
"""


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


def _point(key) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(plain: bytes, p256dh: str, auth: str, salt: bytes | None = None, sender=None) -> bytes:
    """RFC 8291 message encryption for one device: the aes128gcm body, header included."""
    ua_pub = unb64u(p256dh)
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    as_pub = _point(sender.public_key())
    salt = salt or os.urandom(16)
    ikm = _hkdf(unb64u(auth), sender.exchange(ec.ECDH(), receiver), b"WebPush: info\x00" + ua_pub + as_pub, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, plain + b"\x02", None)   # one record, the last
    return salt + struct.pack("!IB", 4096, len(as_pub)) + as_pub + body


def service_of(endpoint: str) -> str | None:
    u = urlsplit(endpoint)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
        return None
    return next((name for h, name in SERVICES.items() if host == h or host.endswith("." + h)), None)


class Push:
    def __init__(self, store, http: httpx.AsyncClient | None):
        self.store, self.http = store, http
        store.db.executescript(SCHEMA)
        keys = store.get_setting(SETTING, None)
        if keys:
            self.key = serialization.load_pem_private_key(keys["private"].encode(), None)
        else:
            self.key = ec.generate_private_key(ec.SECP256R1())
            pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            store.set_setting(SETTING, {"private": pem.decode()})
        self.public = b64u(_point(self.key.public_key()))   # the app's applicationServerKey
        self._jwt: dict[str, tuple[float, str]] = {}

    # -- devices --
    def count(self) -> int:
        return self.store.db.execute("SELECT COUNT(*) FROM push_subs").fetchone()[0]

    def devices(self) -> list[dict]:
        rows = self.store.db.execute("SELECT id, endpoint, label, created, last_ok, last_error FROM push_subs ORDER BY id")
        return [{"id": i, "service": service_of(e) or "", "label": lb, "created": c, "last_ok": ok, "last_error": err}
                for i, e, lb, c, ok, err in rows]

    def subscribe(self, sub: dict, label: str) -> int:
        """Keeps a device's subscription (the browser's PushSubscription as JSON); ValueError if it isn't one."""
        if not isinstance(sub, dict) or not isinstance(sub.get("endpoint"), str) or not isinstance(sub.get("keys"), dict):
            raise ValueError("expected the browser's push subscription")
        endpoint, keys = sub["endpoint"], sub["keys"]
        if len(endpoint) > 1000 or not service_of(endpoint):
            raise ValueError("not a known push service")
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(str(keys.get("p256dh", ""))))
            if len(unb64u(str(keys.get("auth", "")))) != 16:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("the subscription's keys are not valid") from None
        label = (label if isinstance(label, str) else "")[:60].strip() or service_of(endpoint)
        with self.store.db:
            self.store.db.execute("INSERT INTO push_subs (endpoint, p256dh, auth, label, created) VALUES (?,?,?,?,?) "
                                  "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, label=excluded.label",
                                  (endpoint, keys["p256dh"], keys["auth"], label, int(time.time() * 1000)))
        return self.store.db.execute("SELECT id FROM push_subs WHERE endpoint=?", (endpoint,)).fetchone()[0]

    def remove(self, *, id: int | None = None, endpoint: str | None = None) -> bool:
        with self.store.db:
            cur = self.store.db.execute("DELETE FROM push_subs WHERE id=?", (id,)) if id is not None else \
                self.store.db.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))
        return cur.rowcount > 0

    # -- sending --
    def _vapid(self, endpoint: str) -> str:
        u = urlsplit(endpoint)
        aud = f"{u.scheme}://{u.netloc}"
        hit = self._jwt.get(aud)
        if hit and hit[0] > time.time() + 600:
            return hit[1]
        exp = int(time.time()) + 12 * 3600
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        claims = b64u(json.dumps({"aud": aud, "exp": exp, "sub": SUBJECT}, separators=(",", ":")).encode())
        r, s = decode_dss_signature(self.key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
        token = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        header = f"vapid t={token}, k={self.public}"
        self._jwt[aud] = (exp, header)   # the whole header: a reused one must be as complete as the first
        return header

    async def _one(self, row, payload: bytes) -> bool:
        sid, endpoint, p256dh, auth = row
        try:
            r = await self.http.post(endpoint, content=encrypt(payload, p256dh, auth), timeout=15, headers={
                "Authorization": self._vapid(endpoint), "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream", "TTL": str(TTL_S), "Urgency": "high"})
            ok, err = r.is_success, "" if r.is_success else f"HTTP {r.status_code}"
            if r.status_code in (404, 410):   # the device unsubscribed or the app was removed
                try:
                    self.remove(id=sid)
                    log.info("push: a %s device is gone; removed", service_of(endpoint))
                except sqlite3.Error as e:   # not written (a full disk): removed after a later alert
                    log.warning("push: a %s device is gone, not removed: %s", service_of(endpoint), e)
                return False
        except (httpx.HTTPError, ValueError) as e:
            ok, err = False, type(e).__name__
        try:
            with self.store.db:
                self.store.db.execute("UPDATE push_subs SET last_ok=CASE WHEN ? THEN ? ELSE last_ok END, last_error=? WHERE id=?",
                                      (ok, int(time.time() * 1000), err, sid))
        except sqlite3.Error as e:   # the database can't be written (a full disk): the other devices still get it
            log.warning("push: a device's answer not saved: %s", e)
        if not ok:
            log.warning("push to a %s device failed (%s)", service_of(endpoint), err)   # never the endpoint: a secret
        return ok

    async def send(self, title: str, message: str, tag: str = "") -> int:
        """Sends to every device; returns how many took it."""
        rows = self.store.db.execute("SELECT id, endpoint, p256dh, auth FROM push_subs").fetchall()
        if not rows or self.http is None:
            return 0
        payload = json.dumps({"title": title[:120], "body": message[:1000], "tag": tag[:60], "ts": int(time.time() * 1000)},
                             separators=(",", ":")).encode()
        n = 0
        for row in rows:
            n += await self._one(row, payload)
        return n
