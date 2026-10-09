# Runbook: refresh adanos-software/x-cashtag-rankings (one run, method v2)

Owner: Alexander Schneider (Adanos Software GmbH). The repo is public; publish straight to `main` (the owner approved this).
Schedule: every 5 hours at **01:14, 06:14, 11:14, 16:14 and 21:14 Europe/Berlin**. Data timestamps stay UTC.
A run is about **95 planned X reads, plus at most 10 retry reads (max ~105)**. The script plans everything. You make **one X call at
a time**, exactly as the script prints it, record the result, and publish at the end.
**Expected duration: about 30–40 minutes.** That is ~95 sequential calls at ~15–20 s each, including the pauses, plus a few minutes to
write up the 6 samples. Each HTTP 429 adds 1–2 minutes of waiting.

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
    - `record-error KEY --kind 429` prints `WAIT 60s`. Run Shell `sleep 60` (with `block_until_ms` ≥ 75000), then retry the SAME call.
    - If it gets a second 429, the script prints `WAIT 120s`. Run `sleep 120` (`block_until_ms` ≥ 135000), then make one last try.
    - A third 429 means `GIVE UP`: the call is skipped. Never make a 4th attempt.
  - **Any other error** (invalid request, 5xx, timeout, malformed result): `record-error KEY --kind other` says `RETRY` once.
    A second failure means `GIVE UP`.
  - **Do what the script says:** `WAIT`, `RETRY`, `GIVE UP` or `STOP`. Never retry a call the script has skipped.
- **Stop rules** (the script prints `STOP` and refuses further calls; `next-call` shows `STOPPED`):
  - **5 non-429 errors** in the run, or
  - the **retry reserve of 10 extra reads is used up** (429s and other errors together), or
  - reads left today ≤ 400.
  - After a STOP, the script tells you whether a **partial build** is allowed (group sweep and all planned core counts done) or
    whether to **`abandon`**.
- **Daily budget:** Alexander's cap is **600 X reads per UTC day, shared with all bots**.
  - Every x result ends with `X reads left for this user: A of 30 this minute ..., B of 1000 today ...`.
  - Reads used today = 1000 − B. **B must never drop below 400.**
  - Pass the lowest B you have seen with `--reads-left-today B` on every record command.
  - The script keeps the 10-read retry reserve free under the cap, and shrinks the run if needed: core first, then ETF groups,
    spare, samples and candidates.
  - It prints `SKIP RUN` if fewer than ~20 planned reads fit.
  - 5 runs × ≤105 = ≤525 reads per day.
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
- Config (editable): `config/core_tickers.txt` (60), `config/spam_filter.txt`, `config/collisions.txt`,
  `config/spam_accounts.txt`, `config/exclude_tickers.txt`.
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
It prints the window (e.g. `2026-10-09T04:00:00Z -> 2026-10-09T09:00:00Z`; counts cover the 29h ending at `window_end`) and the
split, normally `stock_groups 12, etf_groups 2, samples 6, candidates 20, core 50, spare 5` = 95 planned, plus the 10 retry reserve.

### 2. The call loop (repeat until `next-call` says `build`)
```bash
python3 scripts/build_rankings.py next-call
```
It prints EITHER one X call (`## KEY -> namespace x, tool ...` plus the exact JSON arguments and how to record it), OR a script
step to run. Script steps:
- `pick-samples` (stage 1: the 3 groups with the most unusual activity; stage 2: co-mention samples)
- `pick-candidates --reads-left-today <B>` (the individual-count list: core, carryover, discovered)
- `build`
- `STOPPED`: follow its advice.

For an X call:
1. **Make that one call.**
   - Copy the arguments verbatim. Group queries are ~4096 characters, and a typo silently drops tickers, so never retype them.
     If needed, Read `work/run.json` and copy `groups[i].query`.
   - Formats:
     - groups: `{"query": "($A OR $AA OR ...) -is:retweet -vote ... -\"pump.fun\"", "granularity": "hour", "start_time": "<window_end-29h>", "end_time": "<window_end>"}`
     - tickers: `{"query": "$NVDA -is:retweet", ...same...}`
     - collision tickers: the longer printed query, e.g. `$GM -is:retweet -vote ... -coinmarketcap -"listing id" -bnb -wallet`
     - samples (`search_posts_all`): `{"query": ..., "sort_order": "relevancy", "max_results": 25, "start_time": <window_start>, "end_time": <window_end>, "post.fields": "author_id,created_at", "expansions": "author_id", "user.fields": "username"}`
2. **Record the result** (this pauses 4 s):
   - **Count:** `data` has 29 hourly buckets (oldest first) and `meta.total_tweet_count`:
     ```bash
     python3 scripts/build_rankings.py record S01=8571:364,344,...,316 --reads-left-today <B>
     ```
     The script rejects a list that is not 29 long or doesn't add up to TOTAL. Fix the transcription; don't call X again.
   - **Sample:** one line per post, `username | text`. Take the username from `includes.users` by `author_id`. You may shorten the
     text, but keep every `$CASHTAG` and roughly the first 100 characters.
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
     Then do exactly what it prints: `WAIT 60s` → `sleep 60`, then the same call; `WAIT 120s` → `sleep 120`, then the same call one
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
- **Stopped or ended early:** `build` works only if **all group counts and all planned core counts** are recorded. The JSON then
  has `"partial": true`, a `partial_reason`, the missing tickers in `planned_not_measured`, and a note starting with `PARTIAL RUN`.
- **Otherwise** `build` refuses. Then run:
  ```bash
  python3 scripts/build_rankings.py abandon --reason "<what happened>"
  ```
  This archives the run to `work/archive/` and publishes nothing. Report it as a failure.

`build` writes:
- `data/latest.json`
- `data/history/<YYYY-MM-DDTHHMMZ>.json`
- `data/state.json`
- `data/next_tickers.json`

Check the summary: ranked count, reads breakdown (incl. `retries_429`, `errors_other`), top 10 and risers.

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
- burst / spike-last-hour / collision notes, skipped calls, planned_not_measured

## What counts as a failure (report it, don't work around it)
- gh not authenticated, no push access, or the push is refused other than non-fast-forward
- `check` fails or the script crashes. Do not edit code mid-run; report the traceback and `abandon`.
- `SKIP RUN`, or B would go below 400
- `STOP` followed by a refused build (`abandon`), i.e. the group sweep or the core is incomplete
- a published PARTIAL run: still report it as degraded, with the reason
- X tools unavailable, or the `x` namespace not ready. Never switch to `user-X`.
- a run still unfinished when the next scheduled run starts. Stop; `build` if the script allows partial, otherwise `abandon`.

## Method summary (do not change silently)
- Constants are at the top of `scripts/build_rankings.py`:
  - `RUN_READS=95`, `RETRY_RESERVE=10`, `MAX_OTHER_ERRORS=5`, `PACE_SECONDS=4`, `WAIT_429=(60, 120)`
  - `ETF_GROUPS_PER_RUN=2`, `SAMPLE_GROUPS=3`, `COMENTION_SAMPLES=3`, `CANDIDATES=20`, `CORE_INDIVIDUAL=50`, `SPARE=5`
  - `MIN_AUTHORS=2`, `BURST_FACTOR=6`, `BURST_DECAY=3`, `RISER_MIN_MENTIONS=20`, `RISER_MIN_RATIO=3`
  - `DAILY_CAP=600`, `MIN_RUN_READS=20`
- Groups are packed by the script: every non-core stock plus the demoted core, OR'ed, ≤4096 characters including the spam
  suffix (12 stock groups). 2 of 13 ETF groups rotate per run.
- Ranking: exact 5h count. Collision tickers use their filtered count. Bursts that collapsed are capped in `rank_score`.
  `risers` is a separate list.
- Change config only between runs. Run `check` and `python3 -m unittest discover tests` before committing such a change.
