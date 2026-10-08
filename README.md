# X stock cashtag rankings

Ranking of how often US-listed **stock and ETF** cashtags (e.g. `$NVDA`) were mentioned on [X](https://x.com) in a trailing **5-hour** window, based on X's official recent post counts.

**Stocks/ETFs only** (no crypto coins). Updated **every 5 hours** on `main` (runs at 01:14, 06:14, 11:14, 16:14 and 21:14 Europe/Berlin time). All timestamps in the data are UTC.

**Latest:** [`data/latest.json`](data/latest.json) · **History:** [`data/history/`](data/history/)

Built by [Adanos Sentiment](https://adanos.org) (`@adanos_api`).

## What each run measures (honest numbers)

X has no "top cashtags" API, and counting is done **one cashtag per X read**. Each run is capped at ~100 reads, so each run measures **95 tickers**:

| Tier | Tickers per run | What |
|---|---|---|
| `core` | 60 | Hand-curated high-attention names ([`config/core_tickers.txt`](config/core_tickers.txt)), measured **every run**. |
| `hot` | up to 10 | Non-core tickers whose last measurement (≤48 h old) had ≥10 mentions; re-measured while they stay active. |
| `rotation` | the rest (25-35) | Least-recently-measured tickers from the full universe. Never-measured names go first, in [`config/rotation_priority.txt`](config/rotation_priority.txt) order, then alphabetically (stocks before ETFs). |

So the ranking is a ranking **of the 95 tickers measured in that run**, all over the same window. It is **not** an exhaustive top list of every cashtag on X: a ticker outside core/hot is only measured when the rotation reaches it, and the full universe (~10.9k symbols) takes many weeks to rotate through at ~25-35 rotation slots per run. Every ranked entry was measured in that run; nothing is carried over from earlier runs. `data/state.json` keeps each ticker's last measurement only to drive the hot/rotation choice.

## Method

- **Counts:** X API v2 `GET /2/tweets/counts/recent` (via the free public X read tools), one call per ticker, query **`$TICKER -is:retweet`** (original posts, replies and quotes; reposts excluded), `granularity=hour`. The value is `meta.total_tweet_count` over the window. No language filter.
- **Window:** the last 5 full UTC hours before the run (e.g. a run at 07:20 UTC counts 02:00-07:00 UTC). Every ticker in a run uses the identical window. `window_start`/`window_end` are in each file; `measured_from`/`measured_until` say when the X calls were made.
- **Universe:** [adanos-software/free-ticker-database](https://github.com/adanos-software/free-ticker-database) `data/core_listings.csv`, filtered to `Stock` + `ETF` listed on NASDAQ, NYSE, NYSE ARCA, BATS or NYSE MKT, tickers matching `^[A-Z]{1,6}$` (cashtag-able), minus crypto-coin collisions in [`config/exclude_tickers.txt`](config/exclude_tickers.txt) (e.g. `$BTC`, `$ETH`, `$SUI`). OTC excluded. Stored in [`data/universe.csv`](data/universe.csv) with source commit in [`data/universe_meta.json`](data/universe_meta.json). Core tickers are always measured, even if the database lists their primary listing elsewhere (e.g. `SHOP`).
- **Caveats:** some cashtags are ambiguous (e.g. `$AI`, `$GM`, `$NET`, `$OPEN`, `$U`, `$F`) and their counts include unrelated uses. Bot/spam posts are not filtered.
- **Change on 2026-10-08:** the feed was revived after a pause (previous snapshot 2026-08-24). Earlier snapshots counted `$TICKER` *including* reposts over a different watchlist, so counts before and after 2026-10-08 are not directly comparable.

## JSON shape

Existing fields are unchanged; fields marked *(added)* are new and additive. Each ranking entry now also has a `tier`.

```json
{
  "generated_at": "2026-10-08T07:23:15Z",
  "window_hours": 5,
  "window_start": "2026-10-08T02:00:00Z",
  "window_end": "2026-10-08T07:00:00Z",
  "universe": "stocks",
  "method": "core+hot+rotation",
  "source": {
    "platform": "X",
    "endpoint": "GET /2/tweets/counts/recent",
    "query_pattern": "$TICKER -is:retweet",
    "granularity": "hour",
    "metric": "meta.total_tweet_count over the window"
  },
  "watchlist_size": 10898,
  "scan_count": 95,
  "ranked_count": 95,
  "tickers_measured": 95,
  "tiers": { "core": 60, "rotation": 35 },
  "measured_from": "2026-10-08T07:18:33Z",
  "measured_until": "2026-10-08T07:23:15Z",
  "planned_not_measured": [],
  "x_reads_used": 96,
  "x_reads_left_today": 897,
  "universe_source": { "repo": "adanos-software/free-ticker-database", "file": "data/core_listings.csv", "commit": "…", "fetched_at": "…" },
  "cadence": "every 5 hours",
  "note": "Ranks only the tickers measured in this run, all over the same UTC window. …",
  "rankings": [
    { "rank": 1, "ticker": "NET", "cashtag": "$NET", "mentions": 438, "tier": "core" }
  ]
}
```

- `watchlist_size`: tickers in the universe. `scan_count` / `tickers_measured`: tickers measured this run. `ranked_count`: entries in `rankings` (all measured tickers, including zero counts).
- *(added)* `tiers`, `measured_from`, `measured_until`, `planned_not_measured` (planned but not measured, e.g. when a run hit its read budget), `x_reads_used`, `x_reads_left_today`, `universe_source`, `cadence`, `note`, and per-entry `tier`.
- [`data/next_tickers.json`](data/next_tickers.json) lists what the next run is expected to measure.

## Use it

```bash
curl -sL https://raw.githubusercontent.com/adanos-software/x-cashtag-rankings/main/data/latest.json
```

## Refreshing (maintainers)

`scripts/build_rankings.py` (Python 3, stdlib only):

```bash
python3 scripts/build_rankings.py universe   # refresh data/universe.csv (weekly is enough)
python3 scripts/build_rankings.py plan       # window + 95 tickers -> work/plan.json
# for each planned ticker: X counts/recent, query "$TICKER -is:retweet", granularity=hour,
# start_time/end_time from the plan; then record meta.total_tweet_count:
python3 scripts/build_rankings.py record NVDA=352 TSLA=259 ... --reads-left-today 897
python3 scripts/build_rankings.py status     # what is still missing
python3 scripts/build_rankings.py build      # data/latest.json, data/history/<UTC>.json, state, next_tickers
git add data && git commit -m "Rankings <window>" && git push
```

Budget: ≤100 X reads per run, ≤24 calls per clock minute, and stop if the day's remaining X reads would fall below 400 (the reads are shared with other jobs).

## License

[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). Underlying counts come from X; this repo republishes derived rankings.
