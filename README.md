# X stock cashtag rankings

Public, machine-readable ranking of the most mentioned **stock** cashtags on [X](https://x.com) over a rolling 5-hour window.

**Stocks only** — crypto coins (`$BTC`, `$ETH`, `$SOL`, …) are excluded. Crypto-related equities (e.g. `$COIN`, `$MSTR`, miners) may still appear.

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
  "universe": "stocks",
  "source": {
    "platform": "X",
    "endpoint": "GET /2/tweets/counts/recent",
    "query_pattern": "$TICKER"
  },
  "watchlist_size": 101,
  "ranked_count": 100,
  "rankings": [
    { "rank": 1, "ticker": "NVDA", "cashtag": "$NVDA", "mentions": 1035 }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `universe` | Always `stocks` |
| `generated_at` | UTC timestamp when this file was written |
| `window_start` / `window_end` | Count window in UTC |
| `rankings[].mentions` | Official X recent-search post count for that cashtag in the window |

## How counts are produced

X does not expose a global “top cashtags” endpoint. Rankings are **counts of a fixed liquid stock watchlist**, not a crawl of every `$` token on the platform.

1. For each equity ticker in the watchlist, call X’s recent post-counts API with query `$TICKER` and `start_time` = now − 5 hours.
2. Sort by `total_tweet_count` descending.
3. Publish the top 100.

A sudden jump of an order of magnitude (typical of cashtag spam / bot bursts) is still counted — the number is what X reported, not a quality score.

## Cadence

- Refresh: every 5 hours, 24/7.
- Source of truth: `data/latest.json` on `main`.
- History files: `data/history/YYYY-MM-DDTHHMMZ.json`.

## Use it

```bash
curl -sL https://raw.githubusercontent.com/adanos-software/x-cashtag-rankings/main/data/latest.json
```

## License

Data and docs in this repo are dedicated to the public domain under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). X remains the source of the underlying post counts; this repo only republishes derived rankings.
