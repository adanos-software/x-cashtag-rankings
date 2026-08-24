# X stock cashtag rankings

Public Top 100 of the most mentioned **stock** cashtags on [X](https://x.com) over a rolling 5-hour window.

**Stocks only** (no crypto coins). Updated **every 5 hours** on `main`.

**Latest:** [`data/latest.json`](data/latest.json)

Built by [Adanos Sentiment](https://adanos.org) (`@adanos_api`).

## Method (dynamic)

X has no global “top cashtags” API. This feed uses a **dynamic scan** over the Adanos [free-ticker-database](https://github.com/adanos-software/free-ticker-database):

1. **Universe** — US `Stock` + `ETF` on NASDAQ / NYSE / NYSE ARCA / BATS / NYSE MKT (~9.4k symbols). OTC and crypto coins excluded.
2. **Core** — ~100 liquid names counted every run.
3. **Discovery** — cashtags spotted in US X trends (matched against the universe).
4. **Chunk rotation** — each run also counts the next ~1/10 of the universe, so every listed symbol is measured about every **~2 days**.

Each run ranks by official X `counts/recent` for `$TICKER` in the trailing 5 hours, then publishes the Top 100 of that measured set.

## JSON shape

```json
{
  "generated_at": "2026-08-24T09:00:00Z",
  "window_hours": 5,
  "window_start": "2026-08-24T04:00:00Z",
  "window_end": "2026-08-24T09:00:00Z",
  "universe": "stocks",
  "method": "core+discovery+chunk",
  "source": {
    "platform": "X",
    "endpoint": "GET /2/tweets/counts/recent",
    "query_pattern": "$TICKER"
  },
  "watchlist_size": 9403,
  "scan_count": 1030,
  "ranked_count": 100,
  "rankings": [
    { "rank": 1, "ticker": "NVDA", "cashtag": "$NVDA", "mentions": 1035 }
  ]
}
```

## Use it

```bash
curl -sL https://raw.githubusercontent.com/adanos-software/x-cashtag-rankings/main/data/latest.json
```

## License

[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). Underlying counts come from X; this repo republishes derived rankings.
