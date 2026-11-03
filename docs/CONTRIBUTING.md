# Contributing

How the code is laid out, tests and checks, and what is not built yet.

## Layout

```
config/            instruments.json (the default instrument list), watchlists.json (first-run watchlist)
server/chartsrv/   app.py (HTTP + websocket), market.py (hub), bars.py, sessions.py, store.py (SQLite),
                   levels.py, alerts.py, notify.py, backfill.py, databento.py, feeds/ (sim, ninjatrader, ibkr),
                   amt.py (Toolkit engine port), amt_runner.py, amt_hub.py, tpo.py (TPO port), tpo_service.py,
                   gaps.py (Gap Analysis port), gaps_service.py, htfema.py (HTF EMA), dsma.py (Daily SMA x4),
                   ifthen.py (If/Then),
                   indicators.py + nt_schema.json (indicator settings), daily_levels.py (levels file/link/paste),
                   drawings.py (drawings and their alerts), watchlists.py, symbols.py + symbol_data.py (symbol
                   database), ext.py (free-data charts), cot.py, econ_calendar.py, earnings.py, connections.py, webpush.py,
                   auth.py (login), backup.py, check.py, workers.py (history loads in worker processes),
                   feeds/ (also alpaca, coinbase, yahoo, fred, eia), feeds/sdk.py (the feed plugin interface),
                   feeds/plugins/ (built-in plugins: IBKR futures, Databento live, TCP lines), plugins.py (loads them),
                   branding.py, setup.py (first run, leaving the simulator)
feeds/             your own feed plugins (git ignores them): docs/FEEDS.md
ninjatrader/       TatankaChartFeed.cs, the NinjaTrader 8 add-on
web/               the app: index.html, app.js, ta.js (standard indicator formulas; server/chartsrv/ta.py is its twin), studies.js (indicator drawing), drawings.js, watchlist.js, style.css, sw.js,
                   vendored Lightweight Charts 5.1.0
```

Storage is SQLite in WAL mode (`/data/chart.db` in the container): raw ticks with aggressor side (kept for
footprint and delta later), 1m bars, alerts and alert history.

Speed: the heavy history work (the AMT engine over 90 days, charts built from trades, volume profiles, an indicator
alert's first look on long settings, a seconds chart's HTF EMA seed) runs in worker processes (`CHART_WORKERS`,
default one fewer than the cores, up to 4), so other charts and the API stay quick while it runs. Charts and AMT runs
nobody is looking at stay loaded for 6 hours (the 24 charts and 12 AMT runs used last), and after a restart the server
loads the 8 charts used last in the background. Responses are gzipped and the live connection is compressed.

## Tests and checks

From `server/`, with the test tools installed as CI does (`pip install -r requirements.txt pytest==9.1.1 ruff==0.16.8`):
`python -m pytest -q` runs every test (about a minute). Tests that chart the simulator's last minutes
use the `in_session` fixture (tests/conftest.py): a clock inside a trading day, so they pass at any hour.

GitHub Actions (`.github/workflows/checks.yml`) runs on every push and pull request: the tests, `ruff check --select
E9,F63,F7,F82 server` (syntax errors and undefined names) and `node --check` on every web file. Run the same before
pushing.

A feed of your own: [FEEDS.md](FEEDS.md). A web change needs nothing more: `WEB_VERSION` (a hash of `web/`) names the
page's files (`app.js?v=<version>`, cached for good), the service worker's cache (`/sw.js`) and the page's
`app-version` meta, so installed apps take it and open pages reload themselves. A new file the page loads goes in
`FILES` in `web/sw.js`. A change to the NinjaTrader add-on: raise `Version` in `TatankaChartFeed.cs` and
`ADDON_VERSION` in `feeds/ninjatrader.py` together, so older copies are flagged.

## Not built yet

| Item | Blocked on |
|---|---|
| A direct Rithmic futures feed (use the NinjaTrader feed meanwhile, or your own program: FEEDS.md) | A Rithmic account, dev kit, test credentials |
| IBKR as the main watchlist quote feed and chart source (`feeds/ibkr.py` written, never run against a Gateway or TWS; Yahoo covers meanwhile) | An IBKR account and TWS or IB Gateway |
| Alpaca quotes and stock charts, Coinbase quotes, App notifications on a real iPhone/PC (written and tested offline against recorded answers and RFC 8291's example; never run against the live services from here) | Nothing: turn them on under Connections and check the dot and Test |
| Databento live and Finnhub plugins (written and tested against stand-ins for their client libraries; never run against a live Databento key or a Finnhub key) | A first run with a Databento key and a Finnhub key |
| Back-adjusting history across rolls (the live feed already rolls itself) | Open: keep unadjusted, or shift older bars by the roll spread |
