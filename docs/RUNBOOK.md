# Runbook: refresh adanos-software/x-cashtag-rankings (one run, method v3)

Owner: Alexander Schneider (Adanos Software GmbH). The repo is public; publish straight to `main` (the owner approved this).
Schedule: every 5 hours at **01:14, 06:14, 11:14, 16:14 and 21:14 Europe/Berlin**. Data timestamps stay UTC.
A run is about **95 planned X reads, plus at most 10 retry reads (max ~105)**. The script plans everything. You make **one X call at
a time**, exactly as the script prints it, record the result, and publish at the end.
**Expected duration: about 35–45 minutes.** That is ~95 sequential calls at ~15–20 s each, including the pauses, plus a few minutes to
write up the 10 samples. Each HTTP 429 adds 1–2 minutes of waiting.
**The script generates every query, including every group and every split part. Never build, shorten, merge or edit a query
yourself; copy what `next-call` prints. If something looks wrong, stop and report.**

## Hard rules
- **X tools:** use ONLY the free public X read tools in the **`x`** namespace: `GetDynamicTools` with namespace `"x"`, then
  `CallDynamicTool` with namespace `"x"` and tool `get_posts_counts_recent` or `search_posts_all`.
  - NEVER use the `user-X` namespace; it uses paid credits.
  - Never post, like, repost or DM anything on X.
- **Strictly sequential X calls.**
  - Make **exactly one X tool call per message/turn**. NEVER put several x calls in parallel or in one batch.
  - After every call, run its `record` / `record-sample` / `record-error` command. Each of these pauses **4 seconds** before it
    returns; that is the pacing, so don't skip it.
  - Only then make the next call. This keeps a run at **≤15 calls per minute** (in practice ~4–6).
  - Background: on 2026-10-09 06:14, up to 20 parallel calls got HTTP 429 (rate limited) on the shared free access, even though
    950 of the day's reads were left.
- **Retries (every retry costs a read and MUST be recorded with `record-error`):**
  - **HTTP 429 / "Too Many Requests" / rate limit:**
    - `record-error KEY --kind 429` prints `WAIT 90s`. Run Shell `sleep 90` (with `block_until_ms` ≥ 105000), then retry the SAME call.
    - If it gets a second 429, the script prints `WAIT 240s`. Run `sleep 240` (`block_until_ms` ≥ 255000), then make one last try. After a `GIVE UP` on 429s, `sleep 300` before the next call.
    - A third 429 means `GIVE UP`: the call is skipped. Never make a 4th attempt.
  - **Any other error** (invalid request, 5xx, timeout, malformed result): `record-error KEY --kind other` says `RETRY` once.
    A second failure means `GIVE UP`.
  - **Never retry without `record-error` first.** The script also compares reads-left (B) with the recorded calls; a gap
    is counted as unrecorded retries (`unrecorded_retries`) against the retry reserve and printed as a WARNING
    (2026-10-12 11:14: two BA retries were not recorded). Reads spent by other bots in the same minutes show up the same way.
  - **Do what the script says:** `WAIT`, `RETRY`, `GIVE UP` or `STOP`. Never retry a call the script has skipped.
- **Stop rules** (the script prints `STOP` and refuses further calls; `next-call` shows `STOPPED`):
  - **5 non-429 errors** in the run, or
  - the **retry reserve of 10 extra reads is used up** (429s and other errors together), or
  - reads left today ≤ 400.
  - After a STOP, the script tells you whether a **partial build** is allowed (all 13 STOCK groups and all 10 megacap counts done; a missing ETF group, split part or sample is only recorded as missing) or
    whether to **`abandon`**.
- **Daily budget:** Alexander's cap is **600 X reads per UTC day, shared with all bots**.
  - Every x result ends with `X reads left for this user: A of 30 this minute ..., B of 1000 today ...`.
  - Reads used today = 1000 − B. **B must never drop below 400.**
  - Pass the lowest B you have seen with `--reads-left-today B` on every record command.
  - The script keeps the 10-read retry reserve free under the cap, and shrinks the run if needed: daily counts first (to 10),
    then splits (to 9), ETF groups, samples, spare, then the rest; megacaps last.
  - It prints `SKIP RUN` if fewer than ~20 planned reads fit.
  - 5 runs × ≤105 = ≤525 reads per day.
- **Fail closed on split state:** if `plan` (or `check`) says `split state inconsistent ... fail-closed`, do NOT edit
  `data/splits.json` by hand. Run `python3 scripts/build_rankings.py splits-reset` (no X reads; it only drops the refinement
  carried over from earlier runs), then `plan` again, and mention it in the report. If `next-call` itself reports an
  inconsistent split state mid-run, `build` if the script allows a partial run, otherwise `abandon`, and report.
- **Honest data:** never invent, estimate or carry over a number. Only individually counted tickers are ranked. Never publish
  a snapshot that did not come from a real run. A partial build is allowed only through the script; it labels it
  `"partial": true`.

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
- Config (editable): `config/megacaps.txt` (10, counted every run), `config/core_tickers.txt` (60; the non-megacaps are
  swept in the groups and counted once per UTC day), `config/spam_filter.txt`, `config/collisions.txt`,
  `config/spam_accounts.txt`, `config/exclude_tickers.txt`.
- Persistent state committed with every run: `data/state.json`, `data/splits.json` (split tree), `data/hourly.json` (hourly
  series of everything measured in the last ~2 UTC days), `data/today.json`, `data/daily/<day>.json`.
- Run state: `work/run.json` (gitignored). Write sample files to `work/samples/`.
- `plan`, `build` and `abandon` move old runs to `work/archive/<planned_at>-built|unfinished|abandoned/`.
  Don't leave other scratch files in `work/`.
- `python3 scripts/build_rankings.py next` shows the next step, reads used and remaining, the retry reserve, the error counts and
  skipped calls.

## Steps

### 0. Prepare (no X reads)
```bash
cd /workspace/x-cashtag-rankings
gh auth status                 # must be logged in with push access, otherwise STOP and report (failure)
git pull --ff-only origin main
python3 scripts/build_rankings.py check   # must end with "check OK"
ls work/                        # if a work/run.json from an earlier run is there, `plan` archives it automatically
```
If `data/universe_meta.json` `fetched_at` is more than 7 days old, run `python3 scripts/build_rankings.py universe` (no X reads).

### 1. Plan (no X reads)
```bash
python3 scripts/build_rankings.py plan
```
It prints the window (e.g. `2026-10-09T04:00:00Z -> 2026-10-09T09:00:00Z`; counts cover the 29h ending at `window_end`), the
split, normally `stock_groups 13, megacaps 10, etf_groups 3, splits 24, samples 10, spare 4, dailies 31` = 95 planned, plus the
10 retry reserve, and how many split nodes were carried over (plus any pruning notes).

Order of the calls (all chosen by the script): 13 stock groups + 3 ETF groups → 10 megacaps → adaptive splits (up to 24 reads;
the script picks each next split from the counts just recorded, so you only ever see the next part) → stage-1 samples (6) →
stage-2 samples (4) → daily exact counts (~31) → build.

### 2. The call loop (repeat until `next-call` says `build`)
```bash
python3 scripts/build_rankings.py next-call
```
It prints EITHER one X call (`## KEY -> namespace x, tool ...` plus the exact JSON arguments and how to record it), OR a script
step to run. Script steps:
- `pick-samples` (stage 1: the 6 hottest groups/split parts per ticker; stage 2: co-mentions and unverified split leaves)
- `pick-candidates --reads-left-today <B>` (the daily exact counts: split leaves, sample discoveries, hot re-reads, core due today)
- `build`
- `STOPPED`: follow its advice.

For an X call:
1. **Make that one call.**
   - Copy the arguments verbatim. Group queries are ~4096 characters, and a typo silently drops tickers, so never retype them.
     Split parts (keys like `S07.1`, `S07.2.3`) are groups too: `next-call` prints the full query. Never derive a part's
     ticker list yourself. If you need the text again, re-run `next-call` (it prints the same pending call).
   - Formats:
     - groups: `{"query": "($A OR $AA OR ...) -is:retweet -vote ... -\"pump.fun\"", "granularity": "hour", "start_time": "<window_end-29h>", "end_time": "<window_end>"}`
     - tickers: `{"query": "$NVDA -is:retweet -whatsapp -telegram ... -bnb", ...same...}`: EVERY ticker carries the full spam
       suffix (~450 characters). Copy the printed query verbatim; never shorten it to `$NVDA -is:retweet`.
     - collision tickers: the suffix plus per-ticker terms, e.g. `$TM -is:retweet -whatsapp ... -bnb -telemoney -"telegram money" -sats`
     - samples (`search_posts_all`): `{"query": ..., "sort_order": "relevancy", "max_results": 25, "start_time": <window_start>, "end_time": <window_end>, "post.fields": "author_id,created_at", "expansions": "author_id", "user.fields": "username"}`
2. **Record the result** (this pauses 4 s):
   - **Count:** `data` has 29 hourly buckets (oldest first) and `meta.total_tweet_count`:
     ```bash
     python3 scripts/build_rankings.py record S01=8571:364,344,...,316 --reads-left-today <B>
     python3 scripts/build_rankings.py record S07.2=1210:40,38,...,77 --reads-left-today <B>     # a split part
     ```
     After the 3rd part of a split the script computes the 4th by subtraction (no read); don't call X for it.
     The script rejects a list that is not 29 long or doesn't add up to TOTAL. Fix the transcription; don't call X again.
   - **Sample:** one line per post, `username | text`. Take the username from `includes.users` by `author_id`. Keep the **full
     text** on one line (replace line breaks with spaces); do not shorten it: the ticker-dump rule counts the real words around
     the cashtags, and the template rule compares texts across authors. Every post of the result goes in, spam included.
     ```bash
     mkdir -p work/samples
     cat > work/samples/smp-S07.txt <<'TXT'
     sahmamreky | Pre-market Top Gainers • $DKI +82.0% • $SBFM +47.1% • $IPW +38.4%
     rts_screener | Realtime Stock Screener #premarket 10/08 $DKI $AIXI $MRNO $IPW
     TXT
     python3 scripts/build_rankings.py record-sample smp-S07 --lines work/samples/smp-S07.txt --reads-left-today <B>
     ```
     If the result came back but had no posts, use `record-sample KEY --failed`.
   - **Error** (HTTP 429, any other error, or no usable `data`):
     ```bash
     python3 scripts/build_rankings.py record-error S05 --kind 429 --detail "429 Too Many Requests" --reads-left-today <B>
     ```
     Then do exactly what it prints: `WAIT 90s` → `sleep 90`, then the same call; `WAIT 240s` → `sleep 240`, then the same call one
     last time; `RETRY` → the same call once more; `GIVE UP` → continue with `next-call`; `STOP` → step 3.
3. **First call of the run:** record it right away with its B. If the script prints `SKIP RUN`, run
   `abandon --reason "budget skip"` and report a skip. Don't build or commit.
4. Then run `next-call` again.

### 3. Build (no X reads)
```bash
python3 scripts/build_rankings.py next      # "NEXT: build", or STOPPED with advice
python3 scripts/build_rankings.py build
```
- **Complete run:** `"partial": false`.
- **Stopped or ended early:** `build` works only if **all stock group counts and all 10 megacap counts** are recorded (missing ETF groups, split parts or samples are listed in `missing_calls` and the ETF group is queued for the next run). The JSON then
  has `"partial": true`, a `partial_reason`, the missing tickers in `planned_not_measured`, and a note starting with `PARTIAL RUN`.
- **Otherwise** `build` refuses. Then run:
  ```bash
  python3 scripts/build_rankings.py abandon --reason "<what happened>"
  ```
  This archives the run to `work/archive/` and publishes nothing. Report it as a failure.

`build` writes:
- `data/latest.json`
- `data/history/<YYYY-MM-DDTHHMMZ>.json`
- `data/state.json`, `data/splits.json`, `data/hourly.json`
- `data/today.json` (every ticker seen this UTC day) and `data/daily/<previous day>.json`
- `data/next_tickers.json`

Check the summary: ranked count, reads breakdown (groups, split_counts, samples, megacap/daily counts, `retries_429`,
`errors_other`), the today.json line, top 10 and risers. If it prints `split state inconsistent ... reset`, report it.

### 4. Commit and push to main
```bash
git add data
git commit -m "Rankings <window_start>-<window_end> UTC: <N> tickers counted, <G> groups screened, <R> X reads[, PARTIAL]"
git push origin main
```
- If the push is rejected as non-fast-forward: `git pull --rebase origin main`, then push again.
- **Never force-push.**
- If the push is refused for another reason (auth, branch protection), STOP and report. If push asks for a username, use
  `git -c 'credential.helper=!gh auth git-credential' push origin main`.
- Do not commit `work/` (it is gitignored), and do not change code or config during a run.

### 5. Report
- commit URL `https://github.com/adanos-software/x-cashtag-rankings/commit/<sha>`
- window (UTC), run start and end in Berlin time
- complete or PARTIAL (with the reason)
- X reads used (`x_reads_breakdown`, incl. 429 retries and other errors) and the last B
- top 5 by mentions, and the top 5 risers
- splits: which nodes were split (`adaptive_splits.splits`), any `overlap_warning`, and the split-leaf tickers found
- today.json: tickers seen today and the top 5 by daily count
- burst / spike-last-hour / collision notes, skipped calls, planned_not_measured
- `held_back` tickers and `sampling.rejected_by_clean_check` (non-core tickers kept out by the clean check), and any core entry
  with `low-clean-share`

## What counts as a failure (report it, don't work around it)
- gh not authenticated, no push access, or the push is refused other than non-fast-forward
- `check` fails or the script crashes. Do not edit code mid-run; report the traceback and `abandon`.
- `SKIP RUN`, or B would go below 400
- `STOP` followed by a refused build (`abandon`), i.e. a stock group or a megacap is missing
- a fail-closed split state (`splits-reset` needed); the run itself may still be fine after the reset
- a published PARTIAL run: still report it as degraded, with the reason
- X tools unavailable, or the `x` namespace not ready. Never switch to `user-X`.
- a run still unfinished when the next scheduled run starts. Stop; `build` if the script allows partial, otherwise `abandon`.

## Method summary (do not change silently)
- Constants are at the top of `scripts/build_rankings.py`:
  - `RUN_READS=95`, `RETRY_RESERVE=10`, `MAX_OTHER_ERRORS=5`, `PACE_SECONDS=6`, `WAIT_429=(90, 240)`, `COOLDOWN_AFTER_GIVE_UP=300`, `SAMPLE_MAX_QUERY_LEN=4095`, `OWN_SAMPLES=4`
  - `MEGACAPS_N=10`, `ETF_GROUPS_PER_RUN=3`, `SPLIT_READS=24`, `SAMPLE_GROUPS=6`, `COMENTION_SAMPLES=4`, `SPARE=4`,
    `DAILY_COUNTS_MAX=35`, `CORE_DAILY_MIN=10`
  - splits: `LEAF_MAX=3`, `LEAF_MIN_24H=15`, `SPLIT_MIN_24H=40`, `DRILL_CONC=1.6`, `NODE_MAX_AGE_HOURS=30`, `MAX_NODES=400`,
    `OVERLAP_WARN=0.05`; re-reads: `HOT_REREAD_5H=60`, `HOT_REREAD_MAX=8`
  - `MIN_AUTHORS=3` (distinct authors of clean posts), `MIN_CLEAN_SHARE=0.3`, `DUMP_MIN_CASHTAGS=4`, `DUMP_MIN_WORDS=8`,
    `CLEAN_CHECK_MAX_AGE_HOURS=24`, `SPIKE_MIN_RATIO=2`, `RISER_MIN_1H_VS_PREV_DAY=2`, `BURST_FACTOR=6`, `BURST_DECAY=3`, `RISER_MIN_MENTIONS=20`, `RISER_MIN_RATIO=3`
  - `DAILY_CAP=600`, `MIN_RUN_READS=20`
- Groups are packed by the script: every stock except the 10 megacaps (so the rest of the core is swept too), OR'ed, ≤4096
  characters including the spam suffix (13 stock groups). 3 of 14 ETF groups rotate per run.
- Adaptive splits: a split cuts a node into 4 contiguous parts; if the node was counted in this run, 3 parts are counted and the
  4th = node − 3 parts (per hour, ≥ 0), else all 4 are counted. Drill chains follow a part holding ≥ 1.6× its fair share
  (counts of tickers measured individually in the last 30h are subtracted first); nodes with ≤ 3 tickers are leaves whose members
  get exact counts. The tree persists in `data/splits.json`, is validated on every plan/build (fail closed), pruned after 30h
  without a count, and cold splits collapse.
- Daily exact counts: every ticker gets at most one 29h count per UTC day, re-read only if its last 5h count was ≥ 60 (max 8 per
  run; more only to fill unused slots). Megacaps are counted every run. ≥ 10 per run go to core tickers not yet counted today.
- Spam: `config/spam_filter.txt` goes on every query (groups, split parts, every individual count, samples). Sampled posts from
  `config/spam_accounts.txt`, templates, ticker dumps (> 8 cashtags, or >= 4 with < 8 real words), filter-term posts, crypto/memecoin
  context and posts with < 3 real words are not clean. Non-core tickers (split leaves included) are only ranked after a clean
  check (>= 3 clean authors, >= 30% clean, from this run's samples, today's pooled samples, or a check <= 24h old); failures go to
  `held_back` (and `held_back: true` in today.json).
  Maintain the config lists only between runs, never mid-run; run `check` after editing.
- Ranking: exact 5h filtered count of the tickers counted in this run. Collision tickers additionally use their per-ticker terms.
  Ranked by `rank_score` (clean-adjusted: `rank_score_raw` × `clean_share_est`); bursts that collapsed are capped in `rank_score_raw`. `risers` is a separate list. `data/today.json` is the daily view.
- Change config only between runs. Run `check` and `python3 -m unittest discover tests` before committing such a change.
