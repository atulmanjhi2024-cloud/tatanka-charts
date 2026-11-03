"""Alert delivery: a private Discord webhook, ntfy phone push, and the app's own notifications (webpush.py). URLs are
never logged."""
import asyncio
import json
import logging
import time

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, client: httpx.AsyncClient, discord_webhook: str | None, ntfy_url: str | None):
        self.client = client
        self.discord = discord_webhook   # Connections changes these while running
        self.ntfy = ntfy_url
        self.last: dict[str, tuple[bool, int]] = {}   # channel -> (delivered, when): the Connections status
        self.push = None   # webpush.Push: the app on phones and PCs that turned notifications on
        self.extra: list[tuple[str, str, str]] = []   # more Discord channels and ntfy topics: (id, kind, url)

    @property
    def channels(self) -> list[str]:
        out = [n for n, url in (("discord", self.discord), ("ntfy", self.ntfy)) if url]
        out += [k for k in ("discord", "ntfy") if k not in out and any(kind == k for _, kind, _ in self.extra)]
        return out + ["app"] if self.push is not None and self.push.count() else out

    async def send(self, title: str, message: str) -> str:
        """Returns the channels that accepted the message, e.g. 'discord,ntfy'."""
        jobs = []
        if self.discord:
            jobs.append(("discord", self._discord(title, message)))
        if self.ntfy:
            jobs.append(("ntfy", self._ntfy(title, message)))
        for cid, kind, url in self.extra:
            if kind in ("discord", "ntfy"):
                jobs.append((cid, (self._discord if kind == "discord" else self._ntfy)(title, message, url)))
        if self.push is not None and self.push.count():
            jobs.append(("app", self._push(title, message)))
        results = await asyncio.gather(*(j for _, j in jobs), return_exceptions=True)
        ok = []
        for (name, _), r in zip(jobs, results):
            self.last[name] = (r is True, int(time.time() * 1000))
            kind = name.rsplit("-", 1)[0]   # discord-2 is Discord
            if r is True:
                if kind not in ok:
                    ok.append(kind)
            else:
                log.warning("%s delivery failed: %s", name, type(r).__name__ if isinstance(r, Exception) else r)
        return ",".join(ok)

    async def _discord(self, title, message, url=None) -> bool:
        body = {"username": "Chart Alerts", "content": f"**{title}**\n{message}"}
        for _ in range(2):
            r = await self.client.post(url or self.discord, json=body)
            if r.status_code == 429:
                await asyncio.sleep(min(float(r.json().get("retry_after", 1)), 5))
                continue
            return r.is_success
        return False

    async def post_chart(self, url: str, caption: str, png: bytes | None, username: str = "Tatanka Charts",
                         channel: str = "discord_charts") -> bool:
        """A chart picture (or just the text) to a Discord webhook: Post to Discord, as the app's name (Branding).
        Mentions are never pinged."""
        payload = json.dumps({"username": username, "content": caption, "allowed_mentions": {"parse": []}})
        ok = False
        for _ in range(2):
            if png is None:
                r = await self.client.post(url, json=json.loads(payload), timeout=30)
            else:
                r = await self.client.post(url, data={"payload_json": payload}, files={"files[0]": ("chart.png", png, "image/png")}, timeout=30)
            if r.status_code == 429:
                await asyncio.sleep(min(float(r.json().get("retry_after", 1)), 5))
                continue
            ok = r.is_success
            break
        self.last[channel] = (ok, int(time.time() * 1000))
        return ok

    async def _push(self, title, message) -> bool:
        return await self.push.send(title, message) > 0

    async def _ntfy(self, title, message, url=None) -> bool:
        r = await self.client.post(url or self.ntfy, content=message.encode(),
                                   headers={"Title": title, "Priority": "high", "Tags": "chart_with_upwards_trend"})
        return r.is_success
