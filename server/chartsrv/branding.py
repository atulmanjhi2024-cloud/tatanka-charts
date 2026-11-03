"""Branding: the app's name and its logo, set in the app (Connections > Branding). Tatanka's by default; a copy run by
someone else renames itself here, not in the code. Chart pictures carry the logo and "Powered by Tatanka Charts" (on
by default; the card turns it off)."""
import base64
import binascii
import hashlib
import re

SETTING, LOGO_SETTING = "branding", "branding_logo"
DEFAULTS = {"name": "Tatanka", "powered": True}
LOGO_MAX = 200_000
LOGO_TYPES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"RIFF", "image/webp"))
_TEXT = re.compile(r"^[^<>\x00-\x1f]*$")


def _logo(v) -> tuple[bytes, str]:
    """A data URL or base64 image: (bytes, its type). ValueError unless a PNG, JPEG or WebP up to 200 KB."""
    if not isinstance(v, str) or len(v) > LOGO_MAX * 2:
        raise ValueError("logo: a PNG, JPEG or WebP image up to 200 KB")
    try:
        data = base64.b64decode(v.split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("logo: a PNG, JPEG or WebP image up to 200 KB") from None
    kind = next((t for sig, t in LOGO_TYPES if data.startswith(sig) and (t != "image/webp" or data[8:12] == b"WEBP")), None)
    if not kind or len(data) > LOGO_MAX:
        raise ValueError("logo: a PNG, JPEG or WebP image up to 200 KB")
    return data, kind


def clean(body: dict, now: dict) -> dict:
    """The text settings as sent over `now` (ValueError if one is wrong); the logo is put separately."""
    if not isinstance(body, dict):
        raise ValueError(f"expected {{{', '.join([*DEFAULTS, 'logo'])}}}")
    out = {k: now[k] for k in DEFAULTS}
    for k, v in body.items():
        if k == "logo":
            continue
        if k not in DEFAULTS:
            raise ValueError(f"no branding setting {k!r}")
        if k == "powered":
            if not isinstance(v, bool):
                raise ValueError("powered: true or false")
        elif not isinstance(v, str) or not _TEXT.match(v.strip()):
            raise ValueError(f"{k}: plain text")
        else:
            v = v.strip()
            if k == "name" and not 1 <= len(v) <= 24:
                raise ValueError("name: 1 to 24 characters")
        out[k] = v
    return out


class Branding:
    def __init__(self, store):
        self.store = store

    def get(self) -> dict:
        """The settings, and whether a logo of its own is set (with its version, for the browsers' caches)."""
        saved = self.store.get_setting(SETTING, {}) or {}
        logo = self.store.get_setting(LOGO_SETTING, None) or {}
        return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}, "logo": bool(logo), "logo_v": logo.get("v", 0)}

    def logo(self) -> tuple[bytes, str] | None:
        logo = self.store.get_setting(LOGO_SETTING, None)
        return (base64.b64decode(logo["data"]), logo["type"]) if logo else None

    def put(self, body: dict) -> dict:
        now = self.get()
        text = clean(body, now)
        if "logo" in body:   # an image: the new logo; null: back to the built-in one
            if body["logo"] is None:
                self.store.set_setting(LOGO_SETTING, None)
            else:
                data, kind = _logo(body["logo"])
                self.store.set_setting(LOGO_SETTING, {"data": base64.b64encode(data).decode(), "type": kind, "v": hashlib.sha1(data).hexdigest()[:12]})   # its own version: never one an old one had
        self.store.set_setting(SETTING, {k: v for k, v in text.items() if v != DEFAULTS[k]})
        return self.get()
