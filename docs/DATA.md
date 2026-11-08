# Market data: history and holidays

Filling history (backfill, Databento import) and CME holiday hours.

Neither import runs on the simulator (a fresh install starts on it): leaving it deletes every stored trade and bar,
imported ones too, and until then imported history would mix with its invented trades. Leave it first: **Leave the
simulator** is in the setup guide and on Connections > NinjaTrader. A Databento `--dry-run` still runs.

## Exchange holidays

`config/holidays.json` lists CME holiday hours (CT) through 2027, per product group: `equity` (ES, NQ, micros),
`energy` (CL), `metals` (GC); the group is **Holidays** in Connections > Instruments (`calendar` in `instruments.json`).

- `halt`: trades until the time, then shut until 17:00 with no settlement (MLK, Presidents, Memorial,
  Juneteenth, July 4th, Labor Day, Thanksgiving). That session counts toward the next trade date, as CME's daily
  bars show it, so the daily ATRs (TPO, If/Then) see one day, not two.
- `close`: early close with a settlement (day after Thanksgiving, Christmas Eve).
- `closed`: no session (New Year's, Good Friday, Christmas).

A time can differ by group: `"halt": {"default": "12:00", "energy": "13:30"}`. The file uses the earliest
time CME usually sets, so the stale-feed alert never pages during a real closure. **CME publishes final hours
about two weeks before each holiday at [cmegroup.com/trading-hours](https://www.cmegroup.com/trading-hours.html):
check the line, edit it, then `docker compose up -d --build` (config is built into the image).** A year the file
doesn't list follows CME's usual pattern by itself; to correct a day in one, add that whole year, since a year in the
file is used as it is (one line alone would leave that year with only that holiday). This prints a year's lines, each
ending in a comma, to paste under the `"days": [` line and edit:
```
docker compose exec chart python -c "import json; from chartsrv.config import rule_holidays; [print(json.dumps(d) + ',') for d in rule_holidays(2028)]"
```
A file the server can't read (a typo, a trailing comma) doesn't stop it: every year follows CME's usual pattern until
it is fixed, and `docker compose logs chart` says why. `/api/health` shows `session_open` per symbol and today's
`holiday`.
On D/W/M charts a halt day's short session counts toward the next trade date, as CME's daily bars and
TradingView: MLK Day's trading is in Tuesday's 1D bar (and the RTH 1D bar), the July 3 halt's in the next week's bar.

## Backfill

With the NinjaTrader add-on (20+), 1-minute history fills itself: the last year in the background and older as a
chart is scrolled back ([NINJATRADER.md](NINJATRADER.md), Older history). A backfill is for what your data provider
doesn't have.

Imports 1-minute history from a file, one symbol at a time. A minute already stored is never overwritten, so the
first file imported for a minute wins; live bars from the feed always replace imported ones. The server can keep
running: it restarts by itself a minute after the import ends, so open charts and studies load the history.

Compose mounts the folder `DOCUMENTS_DIR` in `.env` names (your Documents folder) read-only at `/import/documents`.
Put the file there, then, for ES:
```
docker compose exec chart python -m chartsrv.backfill ES /import/documents/ES_1min.csv
```
It reads CSVs with `Datetime,Open,High,Low,Close,Volume` (bar open; naive times in `--tz`, America/Chicago by
default) or `date,time,open,...` (naive; `--tz`), IBKR exports (`date` with its UTC offset), and parquet files with
`time` (the bar's close, time-zone aware, as NinjaTrader stamps its bars), `open`, `high`, `low`, `close`, `volume`.
Prices are put on the instrument's tick. It prints how many bars it read, inserted and found already stored, and the
row count before and after. Without Docker, run from the `server` folder, e.g.
`python -m chartsrv.backfill ES C:\Users\<you>\Documents\ES_1min.csv`.

To check the stored data against NinjaTrader's, day by day: `docker compose exec chart python -m chartsrv.dayvol NQ 15`
prints each trading day's volume, 1-minute bar count (a full Globex day is about 1380), high and low, to compare with
a NinjaTrader Daily chart's Data Box.

## Databento import (ticks)

`chartsrv.databento` imports Databento CME trades (GLBX.MDP3, schema trades) as stored ticks with the buyer/seller
side, plus 1-minute bars built from them. That gives footprints, delta, tick/volume/range charts and bar replay on
that history, and fills minutes missing while NinjaTrader or the PC was off. Compose mounts the folders
`DATABENTO_DIR` and `DOWNLOADS_DIR` in `.env` name (where your Databento downloads are) at `/import/databento` and
`/import/downloads`, read-only.

1. Rebuild once so the image has the new mounts and the DBN reader: `docker compose up -d --build`
2. See what it would do, writing nothing:
   `docker compose exec chart python -m chartsrv.databento /import/databento /import/downloads --dry-run`
3. Import (the server keeps running; about 20 minutes and 7-8 GB per symbol-year):
   `docker compose exec chart python -m chartsrv.databento /import/databento /import/downloads`
4. Nothing more: the server restarts by itself a minute after it ends, so open charts load it.

- Reads `*.trades.csv.zst` and `*.trades.csv` (an unzipped copy of a day is skipped), `*.trades.dbn.zst`, Databento
  batch `.zip` downloads holding either (with their `symbology.json`), and NinjaTrader `NQ 03-24.Last.txt` tick
  exports (no buyer/seller side unless they carry bid and ask; their time zone is found by lining them up with the
  stored 1-minute bars, or give `--last-tz America/New_York`). Without Docker on Python 3.15, DBN files wait for a
  databento-dbn made for it (0.72.0 stops at 3.14).
- The contract for each session: the outright whose prices match the 1-minute bars already stored (your NinjaTrader
  history, so it rolls on the same day), else the most traded. Spreads are left out. Prices are unadjusted.
- Never doubles: a session that already has ticks (live feed or an earlier run) is skipped, so running it again is
  safe. Each day file's part of a session is checked on its own, so the rest of a session an earlier run or the live
  feed began is still filled. 1-minute bars only fill missing minutes; a stored minute with the same volume and no
  buy/sell split (imported 1-minute bars have none) gets the split. It prints each session and the tick and bar counts
  before and after.
- `--symbols ES NQ`, `--since 2025-06-01`, `--until ...` narrow it; `--bars-only` skips the ticks (a few MB);
  `--micros` also gives MES and MNQ the ES and NQ trades.
