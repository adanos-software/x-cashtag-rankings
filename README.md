# X stock cashtag rankings

How often US-listed **stock and ETF** cashtags (e.g. `$NVDA`) were mentioned on [X](https://x.com) in a trailing **5-hour** window, plus which ones are **rising**. Everything comes straight from X: official recent post counts and a few post samples. No third-party "trending" lists are used.

**Stocks/ETFs only** (no crypto coins). Updated **every 5 hours** on `main`, with runs at 01:14, 06:14, 11:14, 16:14 and 21:14 Europe/Berlin time. All timestamps in the data are UTC.

**Latest:** [`data/latest.json`](data/latest.json) · **History:** [`data/history/`](data/history/)

Built by [Adanos Sentiment](https://adanos.org) (`@adanos_api`).

## Method (since 2026-10-08, method v2: `group-sweep+sampling+counts`)

X has no "top cashtags" API, and each X read gives one count or one small sample of posts. A run spends about **95 reads**, in five steps:

| Step | Reads per run | What it does | Exact or sampled |
|---|---|---|---|
| 1. Group sweep (stocks) | 12 | **Every non-core US stock (~5,370 tickers)** is packed into OR-groups: `($A OR $B OR ...) -is:retweet <spam filter>`. Each query is at most 4096 characters, about 450 tickers per group, and each group is counted with 29 hourly buckets. | Exact group counts |
| 1b. Group sweep (ETFs) | 2 | The ~5,470 non-core ETFs are packed the same way into 13 groups, and **2 groups rotate per run**, so each ETF group is screened about every 6–7 runs (~1.5 days). | Exact group counts |
| 2. Sampling | up to 6 | 3 relevancy-sorted samples (25 posts each) from the groups with the most unusual activity. Then 3 "co-mention" samples on the strongest new tickers from those samples, or on the last run's risers. | **Sampled** |
| 3. Candidates | about 20 | Tickers found in the samples (at least 2 distinct non-spam authors), plus carryover from the last run (risers and non-core tickers with at least 25 mentions). | Exact individual counts |
| 4. Core | about 50 | The strongest 50 of the 60 hand-curated core tickers in [`config/core_tickers.txt`](config/core_tickers.txt). The 10 weakest are screened inside the stock groups instead, and are counted again when candidate slots are free or their group is among the hottest. | Exact individual counts |
| Spare | about 5 | Retries and invalid requests. | |

**What is exact:** every ranked number is an exact X count, `GET /2/tweets/counts/recent` over the identical 5-hour window. A group count is the exact number of posts mentioning *any* ticker of the group; a post with several of them counts once. That makes it an upper bound for every member, so a quiet group proves all its tickers are quiet.

**What is sampled:** *which* non-core tickers get counted individually. Samples are 25 posts each, so a ticker that is busy but rarely co-mentioned, sitting in a group without unusual activity, can be missed in a run. The ranking therefore covers **the tickers counted individually in that run**: core, carryover and discovered. It is not a proven exhaustive top list of every cashtag on X. The group sweep does mean that every non-core stock is *screened* every run.

### Counting details

- **Individual query:** `$TICKER -is:retweet` (original posts, replies and quotes; reposts excluded). No language filter. For **collision tickers** (below), the query is the filtered one.
- **Hourly series:** `granularity=hour`, from `window_end − 29h` to `window_end`. One read gives the 5h window (last 5 buckets), the same 5 hours yesterday (first 5 buckets) and the hourly shape.
- **Window:** the last 5 full UTC hours before the run. For example, a run at 14:14 UTC counts 09:00–14:00 UTC.
- **Ranking:** by the exact 5h count, `mentions`. The only exception is a **burst** (see below), which is ranked by `rank_score`. `mentions` always stays the exact count.
- **Momentum:**
  - `momentum_5h_vs_prev_day` = (5h + 1) / (same 5h yesterday + 1)
  - `momentum_last_hour_vs_prev_9h` = (last hour + 1) / (average of the 9 hours before + 1)
  - The +1 keeps tickers with no posts yesterday from producing infinite ratios.
- **Risers:** `rank_score` ≥ 20 and one of the two momentum values ≥ 3. They are listed separately in `risers` and get the label `riser`.

### Spam, bots and collisions

- **Spam filter:** [`config/spam_filter.txt`](config/spam_filter.txt) holds terms such as `-vote -leaderboard -airdrop -memecoin -token -crypto -"pump.fun"`. They are appended to every group, sample and collision query. Measured on 2026-10-08:
  - removed ~52% of a 457-stock group's posts (vote/listing templates, memecoin shills)
  - removed ~10% on `$NVDA $TSLA $MU $SPY`
- **Collisions:** [`config/collisions.txt`](config/collisions.txt) covers tickers whose cashtag is also a coin or token, a greeting or something else, such as GM, AI, U, STX, SKY, PUMP, S, W, SI and SM. These are counted as `$T -is:retweet <spam filter> <per-ticker terms>`, and that filtered count is ranked (label `collision-filtered`; the entry carries its `query`). Example from 2026-10-08: `$GM` went from 245 to 30, because ~88% of its posts were CoinMarketCap "vote" templates and a BNB memecoin. Tickers fully dominated by a coin (e.g. `$BTC`, `$ETH`, `$SUI`) are excluded from the universe ([`config/exclude_tickers.txt`](config/exclude_tickers.txt)).
- **Sample hygiene:** these posts are dropped before candidates are picked:
  - posts from known template accounts ([`config/spam_accounts.txt`](config/spam_accounts.txt))
  - identical normalized text from at least 3 authors (templates)
  - posts with more than 8 cashtags (lists)
  - posts that still contain a spam term
- **Bursts:** an hour above 6 × max(29h hourly median, 3) that collapses in the next hour (to less than a third) is a bot or pump burst. It is labelled `burst`, listed in `burst_hours`, and that hour is capped at the limit in `rank_score`. A spike in the **last** hour can't be judged yet: it gets the label `spike-last-hour` but is not down-weighted, so real breakouts are not penalised.

### Universe

[adanos-software/free-ticker-database](https://github.com/adanos-software/free-ticker-database) `data/core_listings.csv`, filtered as follows:

- `Stock` + `ETF` on NASDAQ, NYSE, NYSE ARCA, BATS or NYSE MKT
- tickers matching `^[A-Z]{1,6}$`
- minus [`config/exclude_tickers.txt`](config/exclude_tickers.txt)

It is stored in [`data/universe.csv`](data/universe.csv) (10,898 symbols on 2026-10-08), with the source commit in [`data/universe_meta.json`](data/universe_meta.json).

### Read budget

- About 95 planned X reads per run, plus a reserve of at most 10 retry reads (≤105 per run). 5 runs a day uses ≤525 reads per UTC day, inside a 600-reads-per-day cap shared with other jobs.
- X calls are made strictly one at a time, with a ~4 s pause after each one (≤15 calls per minute; in practice ~4–6). A run takes about 30–40 minutes.
- On HTTP 429 (rate limited), the same call is retried after 60 s, and once more after 120 s. A call that fails 3 times is skipped.
- A run stops after 5 non-429 errors, or when the retry reserve is used up.
- If a run ends early but the whole group sweep and all planned core counts are done, it is published with `"partial": true` and a `partial_reason`. Otherwise nothing is published.
- If fewer reads are left, the script shrinks the core first, then ETF groups, spare, samples and candidates.
- A run is skipped if fewer than ~20 reads fit.

### Known risks and limits

- Discovery is sampled (25 posts per read). A busy ticker in a group without unusual activity can be missed in a run. Relevancy order with few results can bunch near the end of the window.
- The spam filter and collision terms need maintenance as spam templates change. The filter also removes some real posts (~10% on large caps); this applies only to groups, samples and collision tickers.
- Coordinated but "real" chatter (pump groups, screener bots, signal channels) is hard to tell apart from organic interest, especially on small caps.
- Group queries are 4096-character strings that the operator passes to X verbatim. A copy error silently drops tickers from that group's screen.
- ETFs outside the core are only screened about every 1.5 days.
- Comparability:
  - Snapshots before 2026-10-08 counted `$TICKER` including reposts over a different watchlist.
  - Method v1 runs (2026-10-08 morning) ranked core + hot + rotation tickers with unfiltered counts.
  - Collision tickers such as `$AI` and `$OPEN` are filtered from method v2 on.

## JSON shape

Existing fields keep their meaning; everything else is additive. Excerpt (illustrative values):

```json
{
  "generated_at": "2026-10-08T14:20:00Z",
  "window_hours": 5,
  "window_start": "2026-10-08T09:00:00Z",
  "window_end": "2026-10-08T14:00:00Z",
  "universe": "stocks",
  "method": "group-sweep+sampling+counts",
  "source": { "platform": "X", "endpoint": "GET /2/tweets/counts/recent", "query_pattern": "$TICKER -is:retweet",
              "collision_query_pattern": "$TICKER -is:retweet <spam filter> <per-ticker terms>", "granularity": "hour",
              "history_hours": 29, "metric": "sum of the last 5 hourly buckets (= meta.total_tweet_count over the window)" },
  "watchlist_size": 10898,
  "scan_count": 70, "ranked_count": 70, "tickers_measured": 70,
  "screened_count": 6265,
  "tiers": { "core": 60, "discovered": 10 },
  "sources": { "core": 50, "core-returned": 5, "carryover": 5, "discovered": 10 },
  "measured_from": "…", "measured_until": "…",
  "planned_not_measured": [],
  "x_reads_used": 90,
  "x_reads_breakdown": { "group_counts": 14, "samples": 6, "individual_counts": 70, "extra_errors_retries": 0,
                         "retries_429": 0, "errors_other": 0 },
  "partial": false, "partial_reason": null,
  "x_reads_left_today": 628,
  "ranking_rule": "…", "risers_rule": "…",
  "group_sweep": { "stock_groups": 12, "stock_tickers_screened": 5378, "etf_groups_this_run": ["E01", "E02"],
                   "etf_groups_total": 13, "groups": [ { "key": "S01", "first": "A", "last": "ATRA", "size": 453,
                   "mentions": 1471, "mentions_prev_day_same_hours": 1541, "heat": 9.3, "sampled": false, "…": "…" } ] },
  "sampling": { "samples": [ { "key": "smp-S11", "kind": "group", "posts": 25, "kept": 19, "template": 3, "…": "…" } ],
                "candidates_found": ["DKI", "IPW", "…"] },
  "risers": [ { "ticker": "IPW", "mentions": 72, "momentum_5h_vs_prev_day": 36.5, "momentum_last_hour_vs_prev_9h": 7.5, "…": "…" } ],
  "universe_source": { "…": "…" },
  "cadence": "every 5 hours",
  "note": "…",
  "rankings": [
    { "rank": 1, "ticker": "NVDA", "cashtag": "$NVDA", "mentions": 343, "tier": "core", "source": "core",
      "labels": [], "mentions_prev_day_same_hours": 270, "momentum_5h_vs_prev_day": 1.27,
      "mentions_last_hour": 93, "prev_9h_avg_per_hour": 70.1, "momentum_last_hour_vs_prev_9h": 1.31,
      "burst": false, "burst_hours": [], "rank_score": 343, "hourly": [48, 64, 60, 78, 93] }
  ]
}
```

These fields have the same meaning as before:

- `watchlist_size`: the size of the universe.
- `scan_count`, `tickers_measured`: tickers counted individually this run.
- `ranked_count`: entries in `rankings`.

Added fields:

- **Run-level:**
  - `screened_count`: tickers covered by the group sweep this run.
  - `sources`, `x_reads_breakdown`, `ranking_rule`, `risers_rule`, `group_sweep`, `sampling` and `risers`.
  - `partial` / `partial_reason`: `true` when a run ended early but published, because the full group sweep and all planned core counts were done. The missing tickers are in `planned_not_measured`, and the `note` starts with `PARTIAL RUN`.
- **Per entry:**
  - `tier`: `core`, `carryover` or `discovered`.
  - `source`: `core`, `core-returned`, `carryover` or `discovered`.
  - `labels`: `riser`, `burst`, `spike-last-hour` or `collision-filtered`.
  - the momentum and burst fields and `rank_score`.
  - `hourly`: the 5 window buckets.
  - `discovered_via` and `sample_authors` for discovered tickers.
  - `query` for collision tickers.

[`data/next_tickers.json`](data/next_tickers.json) lists the expected individual counts of the next run: core and carryover. Older snapshots in `data/history/` keep their original shape.

## Use it

```bash
curl -sL https://raw.githubusercontent.com/adanos-software/x-cashtag-rankings/main/data/latest.json
```

## Refreshing (maintainers)

`scripts/build_rankings.py` (Python 3, stdlib only) does all selection logic. The operator makes **one X call at a time**, exactly as printed, and records each result (full procedure: [`docs/RUNBOOK.md`](docs/RUNBOOK.md)):

```bash
python3 scripts/build_rankings.py plan            # window, budget, groups -> work/run.json
python3 scripts/build_rankings.py next-call       # the ONE next X call (or the next script step: pick-samples,
                                                  # pick-candidates, build)
python3 scripts/build_rankings.py record S01=1471:364,344,...   # KEY=TOTAL:29 hourly buckets (oldest first); pauses 4 s
python3 scripts/build_rankings.py record-sample smp-S07 --lines work/samples/smp-S07.txt
python3 scripts/build_rankings.py record-error S05 --kind 429   # prints WAIT 60s / WAIT 120s / RETRY / GIVE UP / STOP
python3 scripts/build_rankings.py build           # complete, or partial if group sweep + core are done
python3 scripts/build_rankings.py abandon --reason "..."   # archive an unpublishable run (nothing published)
python3 scripts/build_rankings.py next            # status: next step, budget, retries, skipped calls
python3 scripts/build_rankings.py check           # offline self-checks; tests: python3 -m unittest discover tests
```

`TOTAL` is `meta.total_tweet_count`. The script checks that the buckets add up to it, which catches transcription errors. Tunables are the constants at the top of the script; spam, collision and account lists are in `config/`.

## License

[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). Underlying counts come from X; this repo republishes derived rankings.
