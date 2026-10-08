# Runbook: refresh adanos-software/x-cashtag-rankings (one run, method v2)

Owner: Alexander Schneider (Adanos Software GmbH). The repo is public; publish straight to `main` (the owner approved this).
Schedule: every 5 hours at **01:14, 06:14, 11:14, 16:14 and 21:14 Europe/Berlin**. Data timestamps stay UTC.
A run is about **95 X reads**. The script plans everything; you make the X calls it prints, record the results and publish.
Expect about 15–25 minutes per run because of the pacing.

## Hard rules
- **X tools:** use ONLY the free public X read tools in the **`x`** namespace: `GetDynamicTools` with namespace `"x"`,
  then `CallDynamicTool` with namespace `"x"` and tool `get_posts_counts_recent` or `search_posts_all`.
  - NEVER use the `user-X` namespace; it uses paid credits.
  - Never post, like, repost or DM anything on X.
- **Daily budget:** Alexander's cap is **600 X reads per UTC day, shared with all bots**.
  - Every x result ends with `X reads left for this user: A of 30 this minute ..., B of 1000 today ...`.
  - Reads used today = 1000 − B. So **B must never drop below 400**.
  - Pass the lowest B you have seen to the script with `--reads-left-today B` whenever you record. The script then sizes the
    rest of the run: it shrinks the core first, then ETF groups, spare, samples and candidates.
  - It prints `SKIP RUN` if fewer than ~20 reads fit, and `STOP` when the budget is used up.
- **Pacing:** **≤24 calls in any clock minute**. Do batches of up to 20 calls, then wait for the next minute with Shell:
  `sleep $((61 - $(date +%-S)))`. Calls in a batch may run in parallel.
- **Cost:** every call costs a read, including invalid requests and errors. Retry a failed call at most once, and record the
  wasted read (see step 7).
- **Honest data:** never invent, estimate or carry over a number. Only individually counted tickers are ranked, and the script
  handles that. Never publish a data snapshot that did not come from a real run.

## Paths and setup
- Working clone on the box: `/workspace/x-cashtag-rankings`. The git remote is https and the clone is gh-authenticated with a local
  `credential.helper=!gh auth git-credential`.
- If the clone is missing:
  ```bash
  gh repo clone adanos-software/x-cashtag-rankings /workspace/x-cashtag-rankings
  cd /workspace/x-cashtag-rankings
  git config user.name "Alexander Schneider"
  git config user.email "alexanderschneider@me.com"
  git config credential.helper '!gh auth git-credential'
  ```
- Script: `scripts/build_rankings.py` (Python 3, stdlib only). Tests live in `tests/`.
- Config (editable):
  - `config/core_tickers.txt` (60)
  - `config/spam_filter.txt`
  - `config/collisions.txt`
  - `config/spam_accounts.txt`
  - `config/exclude_tickers.txt`
- Run state: `work/run.json` (gitignored). Save sample results in `work/samples/`.
- `python3 scripts/build_rankings.py next` always tells you the next step, the run's reads used and remaining, and reads left today.

## Steps (in this order)

### 0. Prepare (no X reads)
```bash
cd /workspace/x-cashtag-rankings
gh auth status                 # must be logged in with push access, otherwise STOP and report (failure)
git pull --ff-only origin main
python3 scripts/build_rankings.py check   # must end with "check OK"
```
If `data/universe_meta.json` `fetched_at` is more than 7 days old, run `python3 scripts/build_rankings.py universe` (no X reads).

### 1. Plan (no X reads)
```bash
python3 scripts/build_rankings.py plan
```
This prints:
- the window, e.g. `2026-10-08T09:00:00Z -> 2026-10-08T14:00:00Z`; the counts cover 29h, starting at `window_end − 29h`
- the read split, normally `stock_groups 12, etf_groups 2, samples 6, candidates 20, core 50, spare 5` = 95
- the groups, S01–S12 plus 2 rotating ETF groups

If you already know B from a call made seconds ago, add `--reads-left-today B`.

### 2. Group counts (14 reads)
Print the exact arguments one group at a time and pass them **verbatim**:
```bash
python3 scripts/build_rankings.py calls groups S01
```
Copy the printed JSON exactly. Queries are ~4096 characters; a typo silently drops tickers, so never retype them by hand.
If needed, open `work/run.json` with Read and copy `groups[i].query`.

Call format, as printed:
```json
{"query": "($A OR $AA OR ... ) -is:retweet -vote -leaderboard -\"stock analyst\" -whatsapp -airdrop -memecoin -solana -dexscreener -gmgn -mcap -token -crypto -\"pump.fun\"",
 "granularity": "hour", "start_time": "<window_end-29h>", "end_time": "<window_end>"}
```
Tool: namespace `x`, `get_posts_counts_recent`.

The result has `data` with 29 hourly buckets (oldest first) and `meta.total_tweet_count`. Record each result as
`KEY=TOTAL:b1,b2,...,b29`:
```bash
python3 scripts/build_rankings.py record S01=8571:364,344,267,...,316 --reads-left-today <lowest B so far>
```
- **First call of the run:** record it right away with its B. If the script prints `SKIP RUN`, stop and report a skip:
  do not build and do not commit.
- **Errors:** the script rejects a bucket list that is not 29 long or doesn't add up to TOTAL. Fix your transcription; don't call X again.
- Then do the remaining groups in batches of ≤20 calls per clock minute, recording after each batch.

### 3. Samples, stage 1 (normally 3 reads)
```bash
python3 scripts/build_rankings.py pick-samples      # picks the 3 groups with the most unusual activity ("heat")
python3 scripts/build_rankings.py calls samples     # exact search_posts_all arguments
```
Call format: namespace `x`, `search_posts_all`, with
`{"query": <group query>, "sort_order": "relevancy", "max_results": 25, "start_time": <window_start>, "end_time": <window_end>, "post.fields": "author_id,created_at", "expansions": "author_id", "user.fields": "username"}`.

Record each sample in ONE of these ways:
- **Compact (preferred):** write one line per post, `username | text`.
  - Take the username from `includes.users`, matched by `author_id`; fall back to the author_id.
  - The text may be shortened, but keep every `$CASHTAG` and roughly the first 100 characters.
  ```bash
  mkdir -p work/samples
  cat > work/samples/smp-S07.txt <<'TXT'
  sahmamreky | Pre-market Top Gainers • $DKI +82.0% • $SBFM +47.1% • $IPW +38.4% • $FKWL +24.0% • $OLB +22.3%
  rts_screener | Realtime Stock Screener #premarket 10/08 $DKI $AIXI $MRNO $IPW
  TXT
  python3 scripts/build_rankings.py record-sample smp-S07 --lines work/samples/smp-S07.txt --reads-left-today <B>
  ```
- **Raw:** save the result JSON (`{"data": [...], "includes": {...}}`) to a file and use `--json FILE`.
- **Failed:** if the read gave nothing usable (error), use `record-sample KEY --failed`.

### 4. Samples, stage 2 (normally 3 reads)
```bash
python3 scripts/build_rankings.py pick-samples      # co-mention samples: $TICKER + spam filter, on the top new tickers
python3 scripts/build_rankings.py calls samples
```
Record them as in step 3. If stage 2 picks none, continue.

### 5. Pick candidates (no X reads)
```bash
python3 scripts/build_rankings.py pick-candidates --reads-left-today <B>
```
This builds the individual-count list:
- core: the strongest 50; more if few candidates were found, fewer if the budget is tight
- carryover: the last run's risers and strong non-core tickers
- discovered: sampled tickers with at least 2 distinct non-spam authors

Call order is priority order.

### 6. Individual counts (normally ~70 reads)
```bash
python3 scripts/build_rankings.py calls counts      # or: calls counts --out work/calls_counts.jsonl
```
- **Normal tickers:** `{"query": "$NVDA -is:retweet", "granularity": "hour", "start_time": "<window_end-29h>", "end_time": "<window_end>"}`
- **Collision tickers** (GM, AI, U, OPEN, STX, ...): the printed query is longer, e.g.
  `$GM -is:retweet -vote ... -"pump.fun" -coinmarketcap -"listing id" -bnb -wallet`. Use it exactly as printed.

Record after each batch of ≤20, wait for the next minute, and repeat:
```bash
python3 scripts/build_rankings.py record NVDA=343:<29 buckets> MU=310:<29 buckets> ... --reads-left-today <B>
```
If the script prints `STOP`, stop calling and go to step 8. Unmeasured tickers end up in `planned_not_measured`.

### 7. Errors and retries
- If a call returns an error or no `data`, retry it once.
- Count every read that gave no usable result: `python3 scripts/build_rankings.py record --extra-reads 1`
  (or `build --extra-reads N`).
- Skip a ticker or group that fails twice. The build lists it as missing (`group_sweep.groups_missing` / `planned_not_measured`).

### 8. Build (no X reads)
```bash
python3 scripts/build_rankings.py next      # should say "NEXT: build"
python3 scripts/build_rankings.py build
```
This writes:
- `data/latest.json`
- `data/history/<YYYY-MM-DDTHHMMZ>.json`
- `data/state.json` (last counts, risers, carryover, ETF rotation pointer)
- `data/next_tickers.json`

Check the printed summary: ranked count, reads breakdown, top 10 (`*` = burst) and risers.

### 9. Commit and push to main
```bash
git add data
git commit -m "Rankings <window_start>-<window_end> UTC: <N> tickers counted, <G> groups screened, <R> X reads"
git push origin main
```
- If the push is rejected as non-fast-forward: `git pull --rebase origin main`, then push again.
- **Never force-push.**
- If the push is refused for any other reason (auth, branch protection), STOP and report. If push asks for a username, use
  `git -c 'credential.helper=!gh auth git-credential' push origin main`.
- Do not commit `work/` (it is gitignored), and do not change code or config during a run.

### 10. Report
- commit URL `https://github.com/adanos-software/x-cashtag-rankings/commit/<sha>`
- window (UTC) and run start in Berlin time
- X reads used, with the `x_reads_breakdown`, and the last B (reads left today)
- top 5 by mentions, and the top 5 risers
- any burst / spike-last-hour / collision notes
- groups missing, planned_not_measured, extra reads

## What counts as a failure (report it, don't work around it)
- gh not authenticated, no push access, or the push is refused other than non-fast-forward
- `check` fails, or the script crashes. Do not edit code mid-run; report the traceback.
- `SKIP RUN` (fewer than ~20 reads fit) or B would go below 400: skip or stop, and report it as a budget skip
- any group count missing after one retry (part of the universe went unscreened), more than 5 extra reads, or more than 100 reads in the run
- X tools unavailable, or the `x` namespace not ready. Never switch to `user-X`.
- a run still unfinished when the next scheduled run starts. Stop, build with what you have only if all groups and at least the
  core were counted, otherwise discard (`work/run.json` gets archived by the next `plan`).

## Method summary (do not change silently)
- Constants are at the top of `scripts/build_rankings.py`:
  - `RUN_READS=95`, `ETF_GROUPS_PER_RUN=2`, `SAMPLE_GROUPS=3`, `COMENTION_SAMPLES=3`, `CANDIDATES=20`, `CORE_INDIVIDUAL=50`, `SPARE=5`
  - `MIN_AUTHORS=2`, `BURST_FACTOR=6`, `BURST_DECAY=3`, `RISER_MIN_MENTIONS=20`, `RISER_MIN_RATIO=3`
  - `DAILY_CAP=600`, `MIN_RUN_READS=20`
- Groups are packed by the script: every non-core stock plus the demoted core, sorted, OR'ed, ≤4096 characters including the
  spam suffix (12 stock groups as of 2026-10-08).
  - Adding spam terms or many tickers can create a 13th group. The script then takes the read from the core automatically.
  - Check with `python3 scripts/build_rankings.py check`.
- Ranking: exact 5h count. Collision tickers use their filtered count. Bursts that collapsed are capped in `rank_score`.
  `risers` is a separate list.
- Daily load: 5 runs × ~95 reads ≈ 475 reads per UTC day.
- Change config (spam terms, collisions, spam accounts) only between runs. Run `check` and the tests
  (`python3 -m unittest discover tests`) before committing such a change.
