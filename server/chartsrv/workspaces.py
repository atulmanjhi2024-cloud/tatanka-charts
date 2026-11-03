"""Workspaces, as TradingView's saved layouts: a name and what the app had open (the chart layout, each chart's
symbol, timeframe, tabs, studies and settings, the panel), stored on the server so every device can open them.
The app saves the one it has open as it changes; the last save wins."""
import json
import re
import secrets

KEY = "workspaces"
MAX_WORKSPACES, MAX_NAME, MAX_BYTES = 50, 60, 512 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _clean_data(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    keys = data.get("keys", {})
    if not isinstance(keys, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in keys.items()):
        raise ValueError("keys must map names to text")
    try:
        size = len(json.dumps(data, allow_nan=False))   # NaN would be stored, then fail every read
    except ValueError:
        raise ValueError("numbers must be finite") from None
    if size > MAX_BYTES:
        raise ValueError("workspace too large")
    return data


class Workspaces:
    def __init__(self, store):
        self.store = store
        self.items: list[dict] = list(store.get_setting(KEY, []) or [])

    def summary(self) -> list[dict]:
        return [{k: w[k] for k in ("id", "name", "updated", "by")} for w in self.items]

    def get(self, wid: str) -> dict | None:
        return next((w for w in self.items if w["id"] == wid), None)

    def put(self, wid: str, name: str | None, data, by: str, now_ms: int) -> dict:
        if not ID_RE.fullmatch(wid or ""):
            raise ValueError("bad workspace id")
        w = self.get(wid)
        name = (name if name is not None else (w or {}).get("name", "")).strip()
        if not name or len(name) > MAX_NAME:
            raise ValueError(f"a workspace needs a name of 1-{MAX_NAME} characters")
        if w is None and len(self.items) >= MAX_WORKSPACES:
            raise ValueError(f"{MAX_WORKSPACES} workspaces at most")
        data = _clean_data(data if data is not None else (w or {}).get("data", {}))
        new = {"id": wid, "name": name, "updated": now_ms, "by": str(by or "")[:40], "data": data}
        if w is None:
            self.items.append(new)
        else:
            self.items[self.items.index(w)] = new
        self._save()
        return new

    def delete(self, wid: str) -> bool:
        w = self.get(wid)
        if w is None:
            return False
        self.items.remove(w)
        self._save()
        return True

    def _save(self):
        self.store.set_setting(KEY, self.items)


def new_id() -> str:
    return secrets.token_urlsafe(9)
