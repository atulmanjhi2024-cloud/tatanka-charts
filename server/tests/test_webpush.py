"""App notifications: Web Push encryption (RFC 8291), VAPID signing, subscriptions, delivery."""
import asyncio
import json
import os
import struct

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from chartsrv import webpush as wp
from test_review_fixes import build_market

APPLE = "https://web.push.apple.com/QGuQyavXutnMOLpwg7f6cE"


def test_rfc8291_example():
    """RFC 8291, Appendix A: the same keys, salt and text give the RFC's message, byte for byte."""
    sender = ec.derive_private_key(int.from_bytes(wp.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    out = wp.encrypt(b"When I grow up, I want to be a watermelon",
                     "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                     "BTBZMqHH6r4Tts7J_aSIgg", salt=wp.unb64u("DGv6ra1nlYgDCS1FRnbzlw"), sender=sender)
    assert wp.b64u(out) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6Tlz"
                            "AC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def _decrypt(body: bytes, device, auth: bytes) -> bytes:   # what the browser does
    salt, (rs, idlen) = body[:16], struct.unpack("!IB", body[16:21])
    as_pub, ct = body[21:21 + idlen], body[21 + idlen:]
    ua_pub = wp._point(device.public_key())
    shared = device.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = wp._hkdf(auth, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    plain = AESGCM(wp._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)).decrypt(
        wp._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12), ct, None)
    assert plain.endswith(b"\x02") and rs == 4096
    return plain[:-1]


def _device():
    key, auth = ec.generate_private_key(ec.SECP256R1()), os.urandom(16)
    return key, auth, {"endpoint": APPLE, "keys": {"p256dh": wp.b64u(wp._point(key.public_key())), "auth": wp.b64u(auth)}}


def test_subscribe_send_and_forget(tmp_path, monkeypatch):
    got = []

    def answer(req):
        got.append(req)
        return httpx.Response(410 if "gone" in req.url.path else 201)
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    p = m.push
    key, auth, sub = _device()
    for bad in ({"endpoint": "https://evil.example.com/x", "keys": sub["keys"]}, {"endpoint": "http://web.push.apple.com/x", "keys": sub["keys"]},
                {"endpoint": APPLE, "keys": {"p256dh": "AAAA", "auth": sub["keys"]["auth"]}}, {"endpoint": APPLE}, "nope",
                {"endpoint": "https://web.push.apple.com.evil.com/x", "keys": sub["keys"]}):
        with pytest.raises(ValueError):
            p.subscribe(bad, "x")
    did = p.subscribe(sub, "iPhone · app")
    assert p.subscribe(sub, "iPhone · app") == did and p.count() == 1   # the same device again: one
    k2, a2, sub2 = _device()
    sub2["endpoint"] = "https://fcm.googleapis.com/fcm/send/gone-token"
    p.subscribe(sub2, "")
    assert [d["service"] for d in p.devices()] == ["Apple", "Google"] and "endpoint" not in json.dumps(p.devices())
    assert m.notifier.push is p

    async def go():
        return await p.send("ES alert", "ES crossing 6900.25")
    assert asyncio.run(go()) == 1 and p.count() == 1   # the Google one is gone (410): removed
    req = got[0]
    assert req.headers["Content-Encoding"] == "aes128gcm" and int(req.headers["TTL"]) > 0
    msg = json.loads(_decrypt(req.content, key, auth))
    assert (msg["title"], msg["body"]) == ("ES alert", "ES crossing 6900.25")
    # VAPID: a JWT for Apple's origin, signed with the key the app subscribed with
    t, k = (part.split("=", 1)[1] for part in req.headers["Authorization"][len("vapid "):].split(", "))
    assert k == p.public
    head, claims, sig = t.split(".")
    assert json.loads(wp.unb64u(claims))["aud"] == "https://web.push.apple.com"
    raw = wp.unb64u(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wp.unb64u(p.public))
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    # a second alert reuses the signed token, in a complete header (it once went out bare and Apple refused it)
    assert asyncio.run(go()) == 1
    assert got[-1].headers["Authorization"] == req.headers["Authorization"] and got[-1].headers["Authorization"].startswith("vapid t=")
    # the key pair is kept: devices stay subscribed across restarts
    m2, _ = build_market(tmp_path, monkeypatch)
    assert m2.push.public == p.public and m2.push.count() == 1
    assert p.remove(id=did) and p.count() == 0


def test_alerts_reach_devices(tmp_path, monkeypatch):
    from chartsrv.notify import Notifier
    sent = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(201)))
    m, _ = build_market(tmp_path, monkeypatch, http=http)
    n = Notifier(http, None, None)
    n.push = m.push
    assert n.channels == []
    m.push.subscribe(_device()[2], "PC")
    assert n.channels == ["app"]
    assert asyncio.run(n.send("NQ alert", "NQ above 25000")) == "app" and len(sent) == 1 and n.last["app"][0]


def test_a_database_that_cannot_be_written_does_not_stop_the_other_devices(tmp_path, monkeypatch):
    """Each device's answer is noted in the database. With that write failing (a full disk, another program holding
    the lock), the alert still goes to every device, a gone one among them."""
    import sqlite3
    sent = []

    def answer(req):
        sent.append(req)
        return httpx.Response(410 if "gone" in req.url.path else 201)
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    for i, end in enumerate((f"{APPLE}0", "https://fcm.googleapis.com/fcm/send/gone-token", f"{APPLE}1", f"{APPLE}2")):
        sub = _device()[2]
        sub["endpoint"] = end
        m.push.subscribe(sub, f"device {i}")
    m.store.db.execute("PRAGMA busy_timeout=50")   # the real wait is 5 s
    other = sqlite3.connect(m.store.path)
    other.execute("BEGIN IMMEDIATE")               # the database can't be written for now
    try:
        n = asyncio.run(m.push.send("ES alert", "ES crossed above 7000.00"))
    finally:
        other.rollback()
        other.close()
    assert n == 3 and len(sent) == 4
