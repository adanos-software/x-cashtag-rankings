# X cashtag rankings

Public, machine-readable ranking of the most mentioned stock and crypto cashtags on [X](https://x.com) over a rolling 5-hour window.

Updated **every 5 hours** and pushed to `main`.

**Latest list:** [`data/latest.json`](data/latest.json)

Built by [Adanos Sentiment](https://adanos.org) (`@adanos_api`).

## What you get

Each update overwrites `data/latest.json` with the current Top 100. Snapshots of every run are kept under [`data/history/`](data/history/).

```json
{
  "generated_at": "2026-08-24T09:00:00Z",
  "window_hours": 5,
  "window_start": "2026-08-24T04:00:00Z",
  "window_end": "2026-08-24T09:00:00Z",
  "source": {
    "platform": "X",
    "endpoint": "GET /2/tweets/counts/recent",
    "query_pattern": "$TICKER"
  },
  "watchlist_size": 111,
  "ranked_count": 100,
  "rankings": [
    { "rank": 1, "ticker": "BTC", "cashtag": "$BTC", "mentions": 2012 }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `generated_at` | UTC timestamp when this file was written |
| `window_start` / `window_end` | Count window in UTC |
| `rankings[].mentions` | Official X recent-search post count for that cashtag in the window |
| `rankings[].ticker` | Symbol without `$` |
| `rankings[].cashtag` | Symbol with `$` |

## How counts are produced

X does not expose a global “top cashtags” endpoint. Rankings are **counts of a fixed liquid watchlist**, not a crawl of every `$` token on the platform.

1. For each ticker in the watchlist, call X’s recent post-counts API with query `$TICKER` and `start_time` = now − 5 hours.
2. Sort by `total_tweet_count` descending.
3. Publish the top 100.

A sudden jump of an order of magnitude (typical of cashtag spam / bot bursts) is still counted — the number is what X reported, not a quality score.

Watchlist mix: mega-cap stocks, liquid ETFs (`SPY`, `QQQ`, …), popular single names and meme tickers, crypto-equity proxies (`MSTR`, `COIN`, miners), and major crypto cashtags (`BTC`, `ETH`, `SOL`, …).

## Cadence

- Refresh: every 5 hours, 24/7 (markets do not gate social volume).
- Source of truth: `data/latest.json` on `main`.
- History files are named `data/history/YYYY-MM-DDTHHMMZ.json`.

## Use it

```bash
curl -sL https://raw.githubusercontent.com/adanos-software/x-cashtag-rankings/main/data/latest.json
```

```js
const data = await fetch(
  "https://raw.githubusercontent.com/adanos-software/x-cashtag-rankings/main/data/latest.json"
).then((r) => r.json());
```

No auth. Treat it as a public snapshot, not a commercial feed SLA.

## License

Data and docs in this repo are dedicated to the public domain under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). X remains the source of the underlying post counts; this repo only republishes derived rankings.
