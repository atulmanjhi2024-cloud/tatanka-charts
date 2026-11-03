"""The AMT Toolkit's daily levels by NinjaTrader's order (levels file or link, else the pasted line), when a levels
link is asked, the indicator settings API, and the NinjaTrader settings seed."""
import json
import os

import pytest

from chartsrv import daily_levels as dl
from chartsrv import indicators as ind
from test_review_fixes import build_market

LINE = "2026-01-15, 5000.00, 5020.00, 5040.00, 5060.00, 4980.00, 4960.00, 4940.00"   # invented


def test_paths_and_roots(tmp_path):
    assert dl.levels_root("MES") == "ES" and dl.levels_root("CL") == "CL"
    assert dl.local_path(r"C:\Users\you\Documents\Tatanka\levels\ES.txt", tmp_path) == tmp_path / "Tatanka" / "levels" / "ES.txt"
    assert dl.local_path(r"%USERPROFILE%\Documents\Tatanka\levels\NQ.txt", tmp_path) == tmp_path / "Tatanka" / "levels" / "NQ.txt"
    assert dl.local_path(r"D:\levels\ES.txt", tmp_path) is None


def test_file_beats_paste(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    (docs / "Tatanka" / "levels").mkdir(parents=True)
    monkeypatch.setenv("CHART_DOCUMENTS_DIR", str(docs))
    m, _ = build_market(tmp_path, monkeypatch)
    levels, date, src = m.daily.current("ES")
    assert src.origin == "none" and levels == []
    m.daily.set_paste("ES", LINE)
    levels, date, src = m.daily.current("ES")
    assert src.origin == "paste" and [x.Name for x in levels][:3] == ["PIVOT", "RES1", "RES2"]
    assert next(x for x in levels if x.Name == "SUPP1").Price == 4980.0
    with pytest.raises(ValueError):
        m.daily.set_paste("ES", "2026-01-15, 5000, 50,200")   # a thousands separator
    # the file in Documents\Tatanka\levels\{root}.txt wins; micros read their parent's
    (docs / "Tatanka" / "levels" / "ES.txt").write_text("2026-10-16, 7700.00, 7720.00, 7680.00\n")
    import asyncio
    asyncio.run(m.daily.poll())
    levels, date, src = m.daily.current("MES")
    assert src.origin == "file" and levels[0].Price == 7700.0 and date.day == 16
    st = m.daily.status("ES")
    assert st["origin"] == "file" and st["count"] == 3 and st["paste"] == LINE
    # a bad rewrite keeps the last good levels and says why
    p = docs / "Tatanka" / "levels" / "ES.txt"
    p.write_text("garbage")
    os.utime(p, (1, 1))
    asyncio.run(m.daily.poll())
    levels, _, src = m.daily.current("ES")
    assert levels[0].Price == 7700.0 and "Keeping the last good levels" in m.daily.src["ES"].error


def test_indicator_settings_and_symbols(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    assert m.ind.put("tpo", {"BlockMinutes": 15, "StartColor": "#0000FF", "ShowLive": False}) == {"BlockMinutes": 15}
    assert m.ind.value("tpo", "BlockMinutes") == 15 and m.ind.value("tpo", "TargetRows") == 80
    for bad in [{"BlockMinutes": 3}, {"Nope": 1}, {"StartColor": "blue"}, {"SessionTime": "8:30-15"}]:
        with pytest.raises(ValueError):
            m.ind.put("tpo", bad)
    assert m.ind.put("hema", {"PlotHTFEMA": {"color": "#FF0000", "dash": "Dash", "width": 1.5}})["PlotHTFEMA"]["dash"] == "Dash"
    with pytest.raises(ValueError):
        m.ind.put("hema", {"HtfPeriodType": "Week"})   # this app builds Minute and Day sources
    m.ind.put_symbol("lines", "ES", {"RangeTop": 7800.0, "CustomLevels": "7845.75|VAH"})
    assert m.ind.value("lines", "RangeTop", "ES") == 7800.0 and m.ind.value("lines", "RangeTop", "NQ") == 0.0
    m.ind.put_symbol("ifthen", "ES", {"RowsBlock": "U|1|2|x"}, 5)
    assert m.ind.value("ifthen", "RowsBlock", "ES") == "U|1|2|x" and m.store.get_setting("ifthen")["ES"]["rows"] == "U|1|2|x"
    with pytest.raises(ValueError):
        m.ind.put_symbol("tpo", "ES", {"BlockMinutes": 5})


def test_seed_from_your_ninjatrader_settings(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)   # the repo's config/nt_settings.json
    seed = json.loads((m.s.config_dir / "nt_settings.json").read_text())
    assert m.amt.overrides["lvl_bull_color"] == seed["amt"]["lvl_bull_color"] == 0xFFFF3333
    assert m.ind.value("gap", "TablePosition") == "BottomRight"
    assert m.store.get_setting("nt_seed") == seed["version"]


def test_seed_reset_puts_a_setting_back(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    m.store.set_setting("amt_settings", {**m.amt.overrides, "mvp_align_right": False, "mvp_offset": 30})
    m.store.set_setting("nt_seed", 1)   # a server seeded before the reset existed
    m2, _ = build_market(tmp_path, monkeypatch)
    assert "mvp_align_right" not in m2.amt.overrides and m2.amt.overrides["mvp_offset"] == 30   # only the reset one
    assert m2.amt._settings.mvp_align_right is True


class _Link:
    """A levels link (or several) answering by the clock; `asked` lists when and which."""
    def __init__(self, answer):
        self.answer, self.asked, self.now = answer, [], 0

    async def get(self, url, timeout=None, follow_redirects=None):
        from types import SimpleNamespace
        self.asked.append((self.now, url))
        status, text = self.answer(url, self.now)
        return SimpleNamespace(status_code=status, text=text, raise_for_status=lambda: None)


def _link_levels(setting, http):
    from types import SimpleNamespace
    from chartsrv.config import Instrument
    store = SimpleNamespace(get_setting=lambda k, d=None: d, set_setting=lambda k, v: None)
    m = SimpleNamespace(store=store, http=http, broadcast=lambda msg: None, levels_changed=lambda: None,
                        amt=SimpleNamespace(settings_value=lambda k: setting["v"], levels_changed=lambda s: None),
                        instruments={"ES": Instrument("ES", "E-mini", 0.25, 50)})
    return dl.DailyLevels(m)


async def _poll_until(d, http, start, end, step=5000, each=None):
    t = start
    while t < end:
        http.now = t
        if each:
            each(t)
        await d.poll(t)
        t += step


def _ct(y, mo, day, h, mi, s=0):
    from datetime import datetime
    from chartsrv import sessions
    return int(datetime(y, mo, day, h, mi, s, tzinfo=sessions.CT).timestamp() * 1000)


def test_a_new_levels_link_is_asked_at_once():
    """The setting changed to another link (the old one refused): asked on the next check, not when the old link's
    next ask was due, even while the market is shut."""
    import asyncio
    setting = {"v": "https://levels.example/OLD/{root}.txt"}
    http = _Link(lambda url, now: (403, "") if "OLD" in url else (200, LINE))
    d = _link_levels(setting, http)
    wed = lambda h, mi, s=0: _ct(2026, 10, 21, h, mi, s)   # noqa: E731
    asyncio.run(_poll_until(d, http, wed(10, 0), wed(10, 1)))
    assert d.status("ES")["error"] == "The levels link was refused (HTTP 403): check the link."
    setting["v"] = "https://levels.example/NEW/{root}.txt"
    asyncio.run(_poll_until(d, http, wed(10, 1), wed(10, 1, 10)))
    assert http.asked == [(wed(10, 0), "https://levels.example/OLD/ES.txt"), (wed(10, 1), "https://levels.example/NEW/ES.txt")]
    assert d.status("ES")["origin"] == "link" and d.status("ES")["error"] == "" and d.status("ES")["count"] == 7
    setting["v"] = "https://levels.example/NEWER/{root}.txt"   # on a Saturday, with levels: asked too
    sat = _ct(2026, 10, 24, 11, 0)
    asyncio.run(_poll_until(d, http, sat, sat + 10_000))
    assert http.asked[-1] == (sat, "https://levels.example/NEWER/ES.txt")


def test_a_levels_link_is_asked_every_2_minutes_while_the_market_is_open():
    """At once when set (and at a start), then every 2 minutes while the market is open; not in the 16:00-17:00 CT
    break, a weekend or a closed holiday, and at the first check once it opens again. No publish windows."""
    import asyncio
    from chartsrv import sessions
    later = "2026-10-21, 5100.00, 5120.00, 5080.00\n"
    http = _Link(lambda url, now: (200, later if now >= _ct(2026, 10, 20, 17, 3) else LINE))
    d = _link_levels({"v": "https://levels.example/key/{root}.txt"}, http)
    asyncio.run(_poll_until(d, http, _ct(2026, 10, 20, 15, 51), _ct(2026, 10, 20, 17, 7)))   # a Tuesday
    every2 = lambda h, mi, n: [_ct(2026, 10, 20, h, mi + 2 * i) for i in range(n)]   # noqa: E731
    assert [t for t, _ in http.asked] == every2(15, 51, 5) + every2(17, 0, 4)
    assert d.src["ES"].read_ms == _ct(2026, 10, 20, 17, 4) and d.current("ES")[0][0].Price == 5100.0
    # set on a Friday in the last minutes: asked then, not over the weekend, again at Sunday's open
    http = _Link(lambda url, now: (200, LINE))
    d = _link_levels({"v": "https://levels.example/key/{root}.txt"}, http)
    asyncio.run(_poll_until(d, http, _ct(2026, 10, 23, 15, 57), _ct(2026, 10, 25, 17, 3), step=60_000))
    assert [t for t, _ in http.asked] == [_ct(2026, 10, 23, 15, 57), _ct(2026, 10, 23, 15, 59),
                                          _ct(2026, 10, 25, 17, 0), _ct(2026, 10, 25, 17, 2)]
    # a closed day (here an invented Wednesday): set that day, asked once; none until the next open
    sessions.set_calendar({"days": [{"date": "2026-11-25", "name": "Closed", "closed": True}]})
    try:
        http = _Link(lambda url, now: (200, LINE))
        d = _link_levels({"v": "https://levels.example/key/{root}.txt"}, http)
        asyncio.run(_poll_until(d, http, _ct(2026, 11, 25, 9, 0), _ct(2026, 11, 25, 17, 3), step=60_000))
        assert [t for t, _ in http.asked] == [_ct(2026, 11, 25, 9, 0), _ct(2026, 11, 25, 17, 0), _ct(2026, 11, 25, 17, 2)]
    finally:
        sessions.set_calendar({"days": []})


def test_paths_under_onedrive_and_without_docker(tmp_path, monkeypatch):
    """Documents moved to OneDrive (Windows 11's default, and NinjaTrader's folder with it) is the folder Docker
    mounts too; the server run on Windows itself, without Docker, opens a Windows path as it is."""
    from pathlib import Path
    want = tmp_path / "Tatanka" / "levels" / "ES.txt"
    assert dl.local_path(r"C:\Users\you\OneDrive\Documents\Tatanka\levels\ES.txt", tmp_path) == want
    assert dl.local_path(r"C:\Users\you\OneDrive - Contoso\Documents\Tatanka\levels\ES.txt", tmp_path) == want
    assert dl.local_path(r"C:\Users\you\OneDrive\Pictures\ES.txt", tmp_path) is None
    assert dl.local_path(r"D:\levels\ES.txt", None) is None   # in Docker, Documents not mounted
    monkeypatch.setattr(dl, "WINDOWS", True)
    monkeypatch.setenv("USERPROFILE", r"C:\Users\you")
    assert dl.local_path(r"D:\levels\ES.txt", None) == Path(r"D:\levels\ES.txt")
    assert dl.local_path(dl.DEFAULT_FILE.replace("{root}", "ES"), None) == Path(r"C:\Users\you\Documents\Tatanka\levels\ES.txt")
    assert dl.local_path(r"C:\Users\you\Documents\ES.txt", tmp_path) == tmp_path / "ES.txt"   # CHART_DOCUMENTS_DIR set


def test_a_file_missing_from_the_mounted_documents_says_which_folder(tmp_path, monkeypatch):
    """A path under Documents (OneDrive's too) is read from the Documents folder the server is given, which may not
    be the one the path names (OneDrive without its Documents moved there): the error says which folder it looked in,
    not only that the file the user can see isn't there."""
    import asyncio
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setenv("CHART_DOCUMENTS_DIR", str(docs))
    m, _ = build_market(tmp_path, monkeypatch)
    spec = r"C:\Users\you\OneDrive\Documents\Tatanka\levels\{root}.txt"
    monkeypatch.setattr(m.daily, "spec", lambda: spec)
    asyncio.run(m.daily.poll())
    assert m.daily.src["ES"].error == (r"No levels file at C:\Users\you\OneDrive\Documents\Tatanka\levels\ES.txt in the "
                                       "Documents folder the server sees (DOCUMENTS_DIR).")
    spec = "/no/such/levels/{root}.txt"   # a path of its own: just not there
    asyncio.run(m.daily.poll())
    assert m.daily.src["ES"].error == "No levels file at /no/such/levels/ES.txt."


def test_back_from_a_link_to_an_unchanged_file_reads_the_file(tmp_path, monkeypatch):
    """The setting goes from a file to a link and back to the same file, not edited meanwhile: the file's levels come
    back, not the link's labelled as the file's (the file's time is unchanged, but its levels are not the ones held).
    The blank setting (Documents\\Tatanka\\levels\\{root}.txt) does the same."""
    import asyncio
    docs = tmp_path / "docs"
    for folder in ("lv", "Tatanka/levels"):
        (docs / folder).mkdir(parents=True)
        (docs / folder / "ES.txt").write_text("2026-10-21, 7700.00, 7720.00, 7680.00\n")
    monkeypatch.setenv("CHART_DOCUMENTS_DIR", str(docs))
    for file_spec in (r"C:\Users\you\Documents\lv\{root}.txt", ""):
        setting = {"v": file_spec}
        http = _Link(lambda url, now: (200, LINE))
        d = _link_levels(setting, http)
        wed = _ct(2026, 10, 21, 10, 0)
        seen = []
        for i, spec in enumerate([file_spec, "https://levels.example/key/{root}.txt", file_spec]):
            setting["v"] = spec
            asyncio.run(d.poll(wed + i * 5000))
            levels, _, src = d.current("ES")
            seen.append((src.origin, levels[0].Price))
        assert seen == [("file", 7700.0), ("link", 5000.0), ("file", 7700.0)], file_spec
        assert d.status("ES")["read_ms"] == wed + 10_000
