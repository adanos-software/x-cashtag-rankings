#!/usr/bin/env python3
"""X stock cashtag rankings: group-sweep screening + sampling + exact counts (stdlib only).

One run (details: README.md and docs/RUNBOOK.md). X calls are made strictly ONE AT A TIME:

  python3 scripts/build_rankings.py plan [--reads-left-today N]   # window, groups, budget -> work/run.json
  loop:
    python3 scripts/build_rankings.py next-call      # prints the ONE next X call (or the next script step)
    -> make that single x tool call
    python3 scripts/build_rankings.py record KEY=TOTAL:b1,...,b29 --reads-left-today B     (counts; pauses 4s)
    python3 scripts/build_rankings.py record-sample KEY --lines FILE --reads-left-today B  (samples; pauses 4s)
    python3 scripts/build_rankings.py record-error KEY --kind 429|other --reads-left-today B   (on errors;
        prints WAIT 60s / WAIT 120s / RETRY / GIVE UP / STOP)
    script steps when next-call says so: pick-samples (twice), pick-candidates
  python3 scripts/build_rankings.py build          # complete run, or partial if group sweep + core are done
  python3 scripts/build_rankings.py abandon --reason "..."   # archive an unpublishable run
  python3 scripts/build_rankings.py next | check | calls STAGE [--all]

Every count read uses granularity=hour over the last 29 full UTC hours, so one read gives the
5h window (last 5 buckets), the same 5 hours yesterday (first 5 buckets) and the hourly shape.
Only tickers counted individually in THIS run are ranked; nothing is carried over.
"""
import argparse
import csv
import io
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.environ.get("XCR_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
WORK = os.path.join(ROOT, "work")
CONFIG = os.path.join(ROOT, "config")

UNIVERSE_CSV = os.path.join(DATA, "universe.csv")
UNIVERSE_META = os.path.join(DATA, "universe_meta.json")
STATE_JSON = os.path.join(DATA, "state.json")
NEXT_JSON = os.path.join(DATA, "next_tickers.json")
LATEST_JSON = os.path.join(DATA, "latest.json")
HISTORY_DIR = os.path.join(DATA, "history")
RUN_JSON = os.path.join(WORK, "run.json")
ARCHIVE = os.path.join(WORK, "archive")

# --- method parameters -------------------------------------------------------
METHOD = "group-sweep+sampling+counts"
METHOD_VERSION = 2
WINDOW_HOURS = 5             # ranked window: last 5 full UTC hours
HISTORY_HOURS = 29           # every count read covers 29 hourly buckets (window + same hours yesterday)
MAX_QUERY_LEN = 4096         # X rejects longer queries (4099 chars -> invalid request, still costs a read)
RUN_READS = 95               # target X reads per run (everything: groups, samples, counts, retries)
DAILY_CAP = 600              # Alexander's cap per UTC day, shared by all bots
DAILY_QUOTA = 1000           # the x tools report "B of 1000 today"; used today = 1000 - B
FLOOR_LEFT = DAILY_QUOTA - DAILY_CAP   # never let "reads left today" drop below this (400)
MIN_RUN_READS = 20           # skip the run if fewer reads fit
ETF_GROUPS_PER_RUN = 2       # ETF groups rotate, 2 per run
SAMPLE_GROUPS = 3            # stage-1 samples: the hottest groups
COMENTION_SAMPLES = 3        # stage-2 samples: top new tickers from stage 1 / last risers
SAMPLE_MAX_RESULTS = 25
CANDIDATES = 20              # individual counts for discovered + carryover tickers
CARRYOVER_MAX = 8            # of those, at most this many carried over from the last run
CARRYOVER_MIN_MENTIONS = 25  # ... non-core tickers with >= this many mentions last run (or risers)
CARRYOVER_MAX_AGE_HOURS = 12
CORE_INDIVIDUAL = 50         # core tickers counted individually; the weakest rest go into the sweep
CORE_FLOOR = 10              # budget shrink: core is cut first, down to this
SPARE = 5                    # unplanned slack inside RUN_READS (invalid requests, re-asks)
RETRY_RESERVE = 10           # extra reads per run for retries (429 / errors), on top of RUN_READS -> max ~105
MAX_OTHER_ERRORS = 5         # stop the run after this many non-429 errors
PACE_SECONDS = 4             # record commands pause this long, so calls stay strictly sequential (<= ~15/min)
WAIT_429 = (60, 120)         # wait before the 1st and the 2nd (last) retry of a call that got HTTP 429
MIN_AUTHORS = 3              # a sampled ticker needs >= 3 distinct authors of CLEAN posts to be a candidate
MIN_CLEAN_SHARE = 0.3        # ... and >= 30% of the sampled posts mentioning it must be clean
CLEAN_SHARE_MIN_POSTS = 5    # a clean share is only reported/used with >= 5 sampled posts mentioning the ticker
LOW_CLEAN_SHARE = 0.3        # core entries below this get the label "low-clean-share" (rank unchanged)
CLEAN_CHECK_MAX_AGE_HOURS = 24   # a carryover ticker's last passed clean check may be at most this old
MAX_CASHTAGS_PER_POST = 8    # posts with more cashtags are always a ticker dump
DUMP_MIN_CASHTAGS = 4        # posts with >= 4 cashtags ...
DUMP_MIN_WORDS = 8           # ... and fewer than 8 real words (cashtags, links, @, #, emoji, numbers removed) are dumps
TEMPLATE_MIN_AUTHORS = 3     # same normalized text from >= 3 authors -> template spam
BURST_FACTOR = 6             # an hour > 6 x max(median of 29h, 3) is a burst
BURST_MIN_MEDIAN = 3
BURST_DECAY = 3              # ... and the following hour is < spike / 3 (it collapsed)
RISER_MIN_MENTIONS = 20
RISER_MIN_RATIO = 3.0        # momentum (smoothed ratio) needed to be a riser
RISER_MIN_1H_VS_PREV_DAY = 2.0   # a riser via the last-hour ratio also needs last hour >= 2x the same hour yesterday
SPIKE_MIN_RATIO = 2.0        # spike-last-hour needs last hour >= 2x the previous 9h average (smoothed)
BASE_QUERY = "$TICKER -is:retweet"
CASHTAG_RE = re.compile(r"^[A-Z]{1,6}$")
TEXT_CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_$])\$([A-Za-z]{1,6})(?![A-Za-z0-9_])")
RECORD_RE = re.compile(r"^([A-Za-z0-9.\-]+)=(?:(\d+):)?([\d,\s]+)$")

FTD_REPO = "adanos-software/free-ticker-database"
FTD_FILE = "data/core_listings.csv"
US_EXCHANGES = ("NASDAQ", "NYSE", "NYSE ARCA", "BATS", "NYSE MKT")
ASSET_TYPES = ("Stock", "ETF")


# --- helpers -----------------------------------------------------------------
def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def config_lines(name):
    """Non-empty config lines with '#' comments removed (case preserved)."""
    path = os.path.join(CONFIG, name)
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            # '#' starts a comment unless it is inside a quoted phrase
            s, q = [], False
            for ch in line:
                if ch == '"':
                    q = not q
                if ch == "#" and not q:
                    break
                s.append(ch)
            s = "".join(s).strip()
            if s:
                out.append(s)
    return out


def read_list(name):
    out = []
    for s in config_lines(name):
        t = s.split()[0].upper()
        if t not in out:
            out.append(t)
    return out


def spam_terms():
    return config_lines("spam_filter.txt")


def collisions():
    out = {}
    for s in config_lines("collisions.txt"):
        parts = s.split(None, 1)
        out[parts[0].upper()] = parts[1].strip() if len(parts) > 1 else ""
    return out


def spam_accounts():
    return {s.split()[0].lower().lstrip("@") for s in config_lines("spam_accounts.txt")}


def suffix():
    return " ".join(["-is:retweet"] + spam_terms())


def group_query(tickers):
    return "(" + " OR ".join("$" + t for t in tickers) + ") " + suffix()


def ticker_query(t, coll=None):
    """Individual count query: the shared spam suffix is applied to EVERY ticker (core, candidate,
    carryover); collision tickers additionally get their per-ticker terms."""
    coll = collisions() if coll is None else coll
    return " ".join(x for x in ["$" + t, suffix(), coll.get(t, "")] if x)


def sample_ticker_query(t, coll=None):
    coll = collisions() if coll is None else coll
    return " ".join(x for x in ["$" + t, suffix(), coll.get(t, "")] if x)


def pack(tickers, maxlen=MAX_QUERY_LEN):
    """Greedy packing of tickers (in order) into OR-groups whose full query is <= maxlen."""
    sfx_len = len(suffix())
    groups, cur, cur_len = [], [], 0
    for t in tickers:
        add = len(t) + 1 + (4 if cur else 0)          # "$T" (+ " OR ")
        if cur and 2 + cur_len + add + 1 + sfx_len > maxlen:   # "(" ... ")" + " " + suffix
            groups.append(cur)
            cur, cur_len = [], 0
            add = len(t) + 1
        cur.append(t)
        cur_len += add
    if cur:
        groups.append(cur)
    for g in groups:
        if len(group_query(g)) > maxlen:
            raise SystemExit("packing error: group starting %s is %d chars" % (g[0], len(group_query(g))))
    return groups


def metrics(b):
    """Window/momentum/burst metrics from 29 hourly buckets (oldest first)."""
    n = len(b)
    win = b[n - WINDOW_HOURS:]
    m5 = sum(win)
    y5 = sum(b[:WINDOW_HOURS]) if n >= HISTORY_HOURS else None
    last = b[-1]
    prev = b[n - 10:n - 1]
    prev9 = sum(prev) / len(prev) if prev else 0.0
    med = statistics.median(b) if b else 0
    cap = BURST_FACTOR * max(med, BURST_MIN_MEDIAN)
    # A burst is a spike hour (> cap) that already collapsed: the next hour is below
    # spike / BURST_DECAY (typical bot/pump burst, e.g. $NET 374 posts in one hour, ~10/h
    # around it). Only bursts inside the window are down-weighted (hour capped at cap).
    # A spike in the LAST hour cannot be judged yet: it is labelled, not down-weighted,
    # so a genuine breakout (e.g. a premarket gainer) is not penalised.
    # spike-last-hour additionally needs the last hour to be well above the hours just before it
    # (>= SPIKE_MIN_RATIO x the previous 9h average): a high level that is already fading is no spike
    # (bug fixed 2026-10-10: $TM got the label with 40 in the last hour vs a 9h average of 81.7).
    burst_idx, spike_last = [], False
    for i in range(n - WINDOW_HOURS, n):
        if b[i] > cap:
            if i == n - 1:
                spike_last = (last + 1) >= SPIKE_MIN_RATIO * (prev9 + 1)
            elif b[i + 1] * BURST_DECAY < b[i]:
                burst_idx.append(i)
    yl = b[n - 25] if n >= 25 else None
    adjusted = sum(min(b[i], cap) if i in burst_idx else b[i] for i in range(n - WINDOW_HOURS, n))
    return {
        "mentions": m5,
        "prev_day_same_hours": y5,
        "momentum_5h": round((m5 + 1) / ((y5 or 0) + 1), 2) if y5 is not None else None,
        "last_hour": last,
        "prev_9h_avg": round(prev9, 1),
        "momentum_1h": round((last + 1) / (prev9 + 1), 2),
        "last_hour_prev_day": yl,
        "momentum_1h_vs_prev_day": round((last + 1) / (yl + 1), 2) if yl is not None else None,
        "median_29h": med,
        "burst_cap": cap,
        "burst_idx": burst_idx,
        "spike_last_hour": spike_last,
        "adjusted": int(round(adjusted)),
    }


def heat(m):
    """Unusual activity of a group: hourly excess over its own baseline (both views)."""
    e1 = max(0.0, m["last_hour"] - m["prev_9h_avg"])
    e5 = max(0.0, (m["mentions"] - (m["prev_day_same_hours"] or 0)) / WINDOW_HOURS)
    return round(e1 + e5, 1)


# --- universe ----------------------------------------------------------------
def cmd_universe(args):
    url = "https://raw.githubusercontent.com/%s/main/%s" % (FTD_REPO, FTD_FILE)
    with urllib.request.urlopen(url, timeout=120) as r:
        text = r.read().decode("utf-8")
    commit = None
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/%s/commits/main" % FTD_REPO,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "x-cashtag-rankings"})
        with urllib.request.urlopen(req, timeout=60) as r:
            commit = json.load(r).get("sha")
    except Exception as e:
        try:
            import subprocess
            commit = subprocess.run(["gh", "api", "repos/%s/commits/main" % FTD_REPO, "--jq", ".sha"],
                                    capture_output=True, text=True, timeout=60, check=True).stdout.strip() or None
        except Exception:
            print("warning: could not read source commit: %s" % e, file=sys.stderr)
    exclude = set(read_list("exclude_tickers.txt"))
    seen, rows = set(), []
    for row in csv.DictReader(io.StringIO(text)):
        t = (row.get("ticker") or "").strip().upper()
        if row.get("exchange") not in US_EXCHANGES or row.get("asset_type") not in ASSET_TYPES:
            continue
        if not CASHTAG_RE.match(t) or t in exclude or t in seen:
            continue
        seen.add(t)
        rows.append((t, row.get("name", ""), row["exchange"], row["asset_type"]))
    rows.sort()
    with open(UNIVERSE_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "name", "exchange", "asset_type"])
        w.writerows(rows)
    write_json(UNIVERSE_META, {
        "source_repo": FTD_REPO, "source_file": FTD_FILE, "source_commit": commit,
        "fetched_at": iso(utcnow()),
        "filters": {"exchange": list(US_EXCHANGES), "asset_type": list(ASSET_TYPES),
                    "ticker_regex": CASHTAG_RE.pattern,
                    "excluded": "config/exclude_tickers.txt (crypto-coin cashtag collisions)"},
        "count": len(rows),
    })
    print("universe: %d tickers written to %s" % (len(rows), os.path.relpath(UNIVERSE_CSV, ROOT)))


def load_universe():
    if not os.path.exists(UNIVERSE_CSV):
        sys.exit("data/universe.csv missing; run: python3 scripts/build_rankings.py universe")
    with open(UNIVERSE_CSV, encoding="utf-8") as f:
        return {r["ticker"]: r for r in csv.DictReader(f)}


def load_state():
    """data/state.json; method-v1 files (tickers only) are migrated in memory."""
    s = load_json(STATE_JSON, {}) or {}
    s.setdefault("tickers", {})
    s.setdefault("etf_rotation_next", 0)
    s.setdefault("risers", [])
    s.setdefault("last_run_noncore", [])
    return s


def core_list():
    exclude = set(read_list("exclude_tickers.txt"))
    return [t for t in read_list("core_tickers.txt") if t not in exclude and CASHTAG_RE.match(t)]


def core_priority(state):
    """Core tickers, strongest last measurement first (unmeasured keep config order)."""
    core = core_list()
    st = state["tickers"]
    return sorted(core, key=lambda t: (-(st.get(t, {}).get("mentions", -1)), core.index(t)))


def window_for(now):
    end = now.replace(minute=0, second=0, microsecond=0)
    return end - timedelta(hours=WINDOW_HOURS), end


def size_run(budget, n_stock_groups, n_etf_groups_avail):
    """Split a read budget into steps. Shrink order: core (to CORE_FLOOR), ETF groups,
    spare (to 1), samples (to 2), candidates (to 3), then core further."""
    t = {"stock_groups": n_stock_groups, "etf_groups": min(ETF_GROUPS_PER_RUN, n_etf_groups_avail),
         "samples": SAMPLE_GROUPS + COMENTION_SAMPLES, "candidates": CANDIDATES,
         "core": CORE_INDIVIDUAL, "spare": SPARE}
    over = lambda: sum(t.values()) - budget
    for key, floor in (("core", CORE_FLOOR), ("etf_groups", 0), ("spare", 1), ("samples", 2),
                       ("candidates", 3), ("core", 0), ("samples", 0), ("candidates", 0), ("spare", 0)):
        if over() > 0:
            t[key] = max(floor, t[key] - over())
    return t, over() <= 0


# --- run file ----------------------------------------------------------------
def load_run():
    run = load_json(RUN_JSON)
    if not run or run.get("method_version") != METHOD_VERSION:
        sys.exit("work/run.json missing or from the old method; run: python3 scripts/build_rankings.py plan")
    log_defaults(run)
    return run


def save_run(run):
    write_json(RUN_JSON, run)


def note_reads_left(run, n):
    if n is not None:
        prev = run["log"].get("reads_left_today")
        run["log"]["reads_left_today"] = n if prev is None else min(prev, n)
        run["log"]["reads_left_history"].append([iso(utcnow()), n])


def log_defaults(run):
    lg = run["log"]
    lg.setdefault("extra_reads", 0)
    lg.setdefault("retries_429", 0)
    lg.setdefault("errors_other", 0)
    lg.setdefault("attempts", {})
    run.setdefault("skipped", [])
    run.setdefault("stopped", None)
    return lg


def planned_used(run):
    return len(run["counts"]) + len(run["samples"])


def reads_used(run):
    return planned_used(run) + int(run["log"].get("extra_reads", 0))


def retry_left(run):
    return max(0, RETRY_RESERVE - int(run["log"].get("extra_reads", 0)))


def reads_remaining(run):
    """Reads left for PLANNED calls; the retry reserve is kept free under the daily cap too."""
    rem = run["budget"] - planned_used(run)
    left = run["log"].get("reads_left_today")
    if left is not None:
        rem = min(rem, left - FLOOR_LEFT - retry_left(run))
    return max(0, rem)


def pace(args):
    if not getattr(args, "no_sleep", False):
        time.sleep(PACE_SECONDS)
        print("(paused %ds; make the next X call only after this command returned)" % PACE_SECONDS)


def archive_run(tag):
    """Move work/run.json (+ work/samples) to work/archive/<planned_at>-<tag>/ ."""
    if not os.path.exists(RUN_JSON):
        return None
    run = load_json(RUN_JSON, {}) or {}
    stamp = (run.get("planned_at") or iso(utcnow())).replace(":", "").replace("-", "")
    dest = os.path.join(ARCHIVE, "%s-%s" % (stamp, tag))
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(ARCHIVE, "%s-%s-%d" % (stamp, tag, n))
    os.makedirs(dest)
    os.replace(RUN_JSON, os.path.join(dest, "run.json"))
    smp = os.path.join(WORK, "samples")
    if os.path.isdir(smp):
        os.replace(smp, os.path.join(dest, "samples"))
    return dest


# --- plan --------------------------------------------------------------------
def cmd_plan(args):
    now = parse_iso(args.now) if args.now else utcnow()
    start, end = window_for(now)
    hist_start = end - timedelta(hours=HISTORY_HOURS)
    universe = load_universe()
    state = load_state()
    exclude = set(read_list("exclude_tickers.txt"))
    budget = RUN_READS
    if args.reads_left_today is not None:
        budget = min(RUN_READS, args.reads_left_today - FLOOR_LEFT - RETRY_RESERVE)
        if budget < MIN_RUN_READS:
            print("SKIP: only %d reads fit under the daily cap (reads left %d, floor %d); do not run."
                  % (budget, args.reads_left_today, FLOOR_LEFT))
            sys.exit(3)
    core = core_priority(state)
    core_set = set(core)
    etf_tickers = sorted(t for t, r in universe.items()
                         if r["asset_type"] == "ETF" and t not in core_set and t not in exclude)
    etf_groups = pack(etf_tickers)
    stock_nc = [t for t, r in universe.items()
                if r["asset_type"] == "Stock" and t not in core_set and t not in exclude]
    # size with the default core split first, then re-pack with the actual demoted core
    for _ in range(3):
        guess, _ok = size_run(budget, len(pack(sorted(stock_nc + core[CORE_INDIVIDUAL:]))), len(etf_groups))
        demoted = core[min(len(core), max(guess["core"], 0)):]
        stock_groups = pack(sorted(set(stock_nc) | set(demoted)))
        targets, ok = size_run(budget, len(stock_groups), len(etf_groups))
        if targets["core"] == guess["core"]:
            break
    if not ok:
        print("SKIP: the stock sweep alone (%d reads) does not fit the budget of %d." % (len(stock_groups), budget))
        sys.exit(3)
    n = len(etf_groups)
    first = state["etf_rotation_next"] % n if n else 0
    etf_pick = [(first + i) % n for i in range(targets["etf_groups"])] if n else []
    groups = []
    for i, g in enumerate(stock_groups):
        groups.append({"key": "S%02d" % (i + 1), "kind": "stock", "tickers": g,
                       "demoted_core": [t for t in g if t in core_set]})
    for i in etf_pick:
        groups.append({"key": "E%02d" % (i + 1), "kind": "etf", "tickers": etf_groups[i], "demoted_core": []})
    for g in groups:
        g["first"], g["last"], g["size"] = g["tickers"][0], g["tickers"][-1], len(g["tickers"])
        g["query"] = group_query(g["tickers"])
        g["query_len"] = len(g["query"])
    os.makedirs(WORK, exist_ok=True)
    if os.path.exists(RUN_JSON):
        prev = load_json(RUN_JSON, {}) or {}
        dest = archive_run("built" if prev.get("built_at") else "unfinished")
        print("archived previous run to %s" % os.path.relpath(dest, ROOT))
    run = {
        "method_version": METHOD_VERSION,
        "planned_at": iso(now),
        "window_start": iso(start),
        "window_end": iso(end),
        "history_start": iso(hist_start),
        "budget": budget,
        "targets": targets,
        "core_order": core,
        "core_individual_planned": core[:targets["core"]],
        "etf_groups_total": n,
        "etf_tickers_total": len(etf_tickers),
        "etf_rotation_first": first,
        "stock_tickers_swept": sum(len(g) for g in stock_groups),
        "groups": groups,
        "counts": {},
        "samples": {},
        "picks": {"samples1": None, "samples2": None, "individual": None},
        "log": {"reads_left_today": args.reads_left_today, "reads_left_history": [], "extra_reads": 0,
                "retries_429": 0, "errors_other": 0, "attempts": {}},
        "skipped": [],
        "stopped": None,
    }
    if args.reads_left_today is not None:
        run["log"]["reads_left_history"].append([iso(now), args.reads_left_today])
    save_run(run)
    print("window %s -> %s UTC (29h buckets from %s); budget %d reads" % (run["window_start"], run["window_end"],
                                                                          run["history_start"], budget))
    print("targets:", json.dumps(targets), "total", sum(targets.values()))
    print("groups: %d stock (%d tickers incl. %d demoted core) + ETF %s of %d"
          % (len(stock_groups), run["stock_tickers_swept"], len(demoted),
             ",".join(g["key"] for g in groups if g["kind"] == "etf") or "-", n))
    print("next: python3 scripts/build_rankings.py next-call   (ONE X call at a time; see PACING)")


# --- calls (exact tool arguments) ---------------------------------------------
def count_args(run, query):
    return {"query": query, "granularity": "hour", "start_time": run["history_start"], "end_time": run["window_end"]}


def sample_args(run, query):
    return {"query": query, "sort_order": "relevancy", "max_results": SAMPLE_MAX_RESULTS,
            "start_time": run["window_start"], "end_time": run["window_end"],
            "post.fields": "author_id,created_at", "expansions": "author_id", "user.fields": "username"}


def pending_calls(run, stage):
    out = []
    skipped = set(run.get("skipped") or [])
    if stage == "groups":
        for g in run["groups"]:
            if g["key"] not in run["counts"] and g["key"] not in skipped:
                out.append((g["key"], "get_posts_counts_recent", count_args(run, g["query"])))
    elif stage == "samples":
        for p in (run["picks"]["samples1"] or []) + (run["picks"]["samples2"] or []):
            if p["key"] not in run["samples"] and p["key"] not in skipped:
                out.append((p["key"], "search_posts_all", sample_args(run, p["query"])))
    elif stage == "counts":
        for p in run["picks"]["individual"] or []:
            if p["ticker"] not in run["counts"] and p["ticker"] not in skipped:
                out.append((p["ticker"], "get_posts_counts_recent", count_args(run, p["query"])))
    return out


PACING_NOTE = ("PACING: X calls strictly one at a time (never parallel tool calls). After each call, run its "
               "record command (it pauses %ds) before the next call -> at most ~15 calls per minute. "
               "On an error run record-error and follow its instruction." % PACE_SECONDS)


def print_call(k, tool, a):
    print("## %s  -> namespace x, tool %s" % (k, tool))
    print(json.dumps(a, ensure_ascii=False))


def cmd_calls(args):
    run = load_run()
    calls = pending_calls(run, args.stage)
    if args.key:
        calls = [c for c in calls if c[0] in args.key]
    if args.all:
        print("# LIST FOR REVIEW ONLY - do not batch these. Use `next-call` and make ONE call at a time.")
        for k, tool, a in calls:
            print_call(k, tool, a)
    elif calls:
        print_call(*calls[0])
    print("# %d pending %s calls; remaining run budget %d planned reads (+%d retry reserve)"
          % (len(calls), args.stage, reads_remaining(run), retry_left(run)))
    print("# " + PACING_NOTE)


def next_action(run):
    """(kind, payload): kind in stop/call/pick-samples/pick-candidates/build."""
    if run.get("stopped"):
        return "stop", run["stopped"]
    for stage, gate in (("groups", None), ("samples", "samples1"), ("counts", "individual")):
        if stage == "samples" and run["picks"]["samples1"] is None:
            return "pick-samples", "stage 1"
        if stage == "counts":
            if run["picks"]["samples2"] is None:
                return "pick-samples", "stage 2"
            if run["picks"]["individual"] is None:
                return "pick-candidates", None
        calls = pending_calls(run, stage)
        if calls:
            return "call", (stage, calls)
    return "build", None


def record_hint(stage, key):
    if stage == "samples":
        return ("python3 scripts/build_rankings.py record-sample %s --lines work/samples/%s.txt "
                "--reads-left-today <B>" % (key, key))
    return "python3 scripts/build_rankings.py record %s=<TOTAL>:<29 buckets> --reads-left-today <B>" % key


def cmd_next_call(args):
    run = load_run()
    kind, payload = next_action(run)
    if kind != "call":
        cmd_next(args)
        return
    stage, calls = payload
    k, tool, a = calls[0]
    print_call(k, tool, a)
    print("# then: " + record_hint(stage, k))
    print("# on an error/429: python3 scripts/build_rankings.py record-error %s --kind 429|other --reads-left-today <B>" % k)
    print("# %d %s calls pending incl. this one; remaining %d planned reads (+%d retry reserve)"
          % (len(calls), stage, reads_remaining(run), retry_left(run)))
    print("# " + PACING_NOTE)


def stop_checks(run):
    """Set run['stopped'] when a stop rule fires; returns the message or None."""
    lg = run["log"]
    left = lg.get("reads_left_today")
    msg = None
    if lg.get("errors_other", 0) >= MAX_OTHER_ERRORS:
        msg = "%d non-429 errors (limit %d)" % (lg["errors_other"], MAX_OTHER_ERRORS)
    elif int(lg.get("extra_reads", 0)) >= RETRY_RESERVE and any(
            pending_calls(run, st) for st in ("groups", "samples", "counts")):
        msg = "retry reserve of %d reads used up" % RETRY_RESERVE
    elif left is not None and left <= FLOOR_LEFT:
        msg = "reads left today at/below %d (daily cap)" % FLOOR_LEFT
    if msg and not run.get("stopped"):
        run["stopped"] = msg
    return msg


def partial_ok(run):
    """A run that did not finish may still be built (labelled partial) if the whole group sweep
    and all planned core counts are done."""
    miss = [g["key"] for g in run["groups"] if g["key"] not in run["counts"]]
    if miss:
        return False, "group sweep incomplete: %s" % ",".join(miss)
    picks = run["picks"].get("individual")
    if not picks:
        return False, "candidates/core not picked yet, core not counted"
    core_miss = [p["ticker"] for p in picks if p["source"] == "core" and p["ticker"] not in run["counts"]]
    if core_miss:
        return False, "core not fully counted (%d missing: %s)" % (len(core_miss), ",".join(core_miss[:10]))
    return True, None


def run_complete(run):
    if run.get("skipped") or run.get("stopped"):
        return False
    if run["picks"].get("individual") is None:
        return False
    return not any(pending_calls(run, st) for st in ("groups", "samples", "counts"))


def stop_advice(run):
    ok, why = partial_ok(run)
    if ok:
        return "build now (it is labelled partial): python3 scripts/build_rankings.py build"
    return ("cannot build (%s); archive the run: python3 scripts/build_rankings.py abandon --reason '<why>' "
            "and report the failure" % why)


# --- record counts -----------------------------------------------------------
def parse_record(item):
    m = RECORD_RE.match(item.strip())
    if not m:
        raise ValueError("bad record %r; expected KEY=TOTAL:b1,b2,...,b29" % item)
    key, total, vals = m.group(1), m.group(2), m.group(3)
    buckets = [int(x) for x in re.split(r"[,\s]+", vals.strip()) if x != ""]
    if len(buckets) != HISTORY_HOURS:
        raise ValueError("%s: %d buckets, expected %d (oldest first)" % (key, len(buckets), HISTORY_HOURS))
    if total is not None and int(total) != sum(buckets):
        raise ValueError("%s: buckets sum to %d but meta.total_tweet_count is %s (transcription error?)"
                         % (key, sum(buckets), total))
    return key, buckets


def cmd_record(args):
    run = load_run()
    known = {g["key"] for g in run["groups"]}
    known |= {p["ticker"] for p in (run["picks"]["individual"] or [])}
    errors = 0
    for item in args.pairs:
        try:
            key, buckets = parse_record(item)
        except ValueError as e:
            print("ERROR:", e)
            errors += 1
            continue
        key = key.upper()
        if key not in known:
            print("ERROR: %s is not a planned group or picked ticker (run pick-candidates first?)" % key)
            errors += 1
            continue
        run["counts"][key] = {"buckets": buckets, "recorded_at": iso(utcnow())}
    if args.extra_reads:
        run["log"]["extra_reads"] += args.extra_reads
        run["log"]["errors_other"] += args.extra_reads
    note_reads_left(run, args.reads_left_today)
    msg = stop_checks(run)
    save_run(run)
    print("recorded %d, errors %d; run reads used %d (planned %d + retries/errors %d), remaining %d planned "
          "(+%d retry reserve); reads left today %s"
          % (len(args.pairs) - errors, errors, reads_used(run), planned_used(run), run["log"]["extra_reads"],
             reads_remaining(run), retry_left(run), run["log"]["reads_left_today"]))
    left = run["log"]["reads_left_today"]
    if left is not None and planned_used(run) <= 2 and left - FLOOR_LEFT - RETRY_RESERVE < MIN_RUN_READS:
        print("SKIP RUN: only %d reads fit under the daily cap (after the %d-read retry reserve); stop now, "
              "run `abandon --reason skip`, do not build or commit." % (left - FLOOR_LEFT - RETRY_RESERVE, RETRY_RESERVE))
    elif msg:
        print("STOP: %s. %s" % (msg, stop_advice(run)))
    elif reads_remaining(run) == 0 and any(pending_calls(run, st) for st in ("groups", "samples", "counts")):
        print("STOP: run budget exhausted. " + stop_advice(run))
    if errors:
        sys.exit(1)
    if args.pairs:
        pace(args)


# --- samples -----------------------------------------------------------------
def group_metrics(run):
    out = []
    for g in run["groups"]:
        c = run["counts"].get(g["key"])
        if not c:
            continue
        m = metrics(c["buckets"])
        m["heat"] = heat(m)
        out.append((g, m))
    return out


def norm_text(text):
    t = re.sub(r"https?://\S+", " ", text.lower())
    t = TEXT_CASHTAG_RE.sub(" ", t)
    t = re.sub(r"[@#]\w+", " ", t)
    t = re.sub(r"[\d\W_]+", " ", t).strip()
    return t[:60]


CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def real_words(text):
    """Words of real text: cashtags, links, @mentions, #hashtags, numbers and emoji removed.
    CJK/Hangul characters count as half a word each (no spaces between words)."""
    t = re.sub(r"https?://\S+", " ", text)
    t = TEXT_CASHTAG_RE.sub(" ", t)
    t = re.sub(r"[@#]\w+", " ", t)
    cjk = len(CJK_RE.findall(t))
    t = CJK_RE.sub(" ", t)
    words = [w for w in re.findall(r"[^\W\d_]{2,}", t)]
    return len(words) + cjk // 2


def term_patterns(terms=None):
    """Spam-filter terms as word-boundary regexes for local post classification (X matches whole
    tokens too, so -ca must not hit 'can' and -tg must not hit 'tgt')."""
    terms = spam_terms() if terms is None else terms
    out = []
    for t in terms:
        if not t.startswith("-") or t.startswith("-is:"):
            continue
        w = t[1:].strip('"').lower()
        if not w:
            continue
        words = [re.escape(x) for x in re.findall(r"[\w.]+", w)]
        if words:
            out.append(re.compile(r"(?<![\w$])" + r"\W+".join(words) + r"(?![\w])"))
    return out


def classify_post(text, author, bad_accounts, templates, patterns):
    """-> (class, cashtags). Classes: spam_account, template, dump, spam_term, clean."""
    tags = []
    for t in TEXT_CASHTAG_RE.findall(text):
        t = t.upper()
        if t not in tags:
            tags.append(t)
    if author.lower() in bad_accounts:
        return "spam_account", tags
    if norm_text(text) in templates:
        return "template", tags
    if len(tags) > MAX_CASHTAGS_PER_POST or (len(tags) >= DUMP_MIN_CASHTAGS and real_words(text) < DUMP_MIN_WORDS):
        return "dump", tags
    low = text.lower()
    if any(rx.search(low) for rx in patterns):
        return "spam_term", tags
    return "clean", tags


def clean_ok(a):
    """Sample evidence that a non-core ticker is real stock chatter."""
    return len(a["authors"]) >= MIN_AUTHORS and (a["total"] < CLEAN_SHARE_MIN_POSTS or a["share"] >= MIN_CLEAN_SHARE)


def analyze_samples(run, keys=None):
    """Per ticker over all samples of the run: {authors: set of authors of CLEAN posts, posts: clean posts,
    total: all sampled posts mentioning it, share: clean/total, via: sample keys} + per-sample stats."""
    universe = load_universe()
    exclude = set(read_list("exclude_tickers.txt"))
    bad_accounts = spam_accounts()
    patterns = term_patterns()
    posts = []
    for key, s in run["samples"].items():
        if keys is not None and key not in keys:
            continue
        for p in s.get("posts", []):
            posts.append((key, p))
    # template detection across all samples of the run
    tmpl = {}
    for key, p in posts:
        k = norm_text(p["text"])
        if len(k) >= 20:
            tmpl.setdefault(k, set()).add(p["author"].lower())
    templates = {k for k, a in tmpl.items() if len(a) >= TEMPLATE_MIN_AUTHORS}
    agg, stats = {}, {}
    for key, p in posts:
        st = stats.setdefault(key, {"posts": 0, "kept": 0, "spam_account": 0, "template": 0,
                                    "dump": 0, "spam_term": 0})
        st["posts"] += 1
        cls, tags = classify_post(p["text"], p["author"], bad_accounts, templates, patterns)
        if cls == "clean":
            st["kept"] += 1
        else:
            st[cls] += 1
        for t in tags:
            if t in exclude or t not in universe:
                continue
            a = agg.setdefault(t, {"authors": set(), "posts": 0, "total": 0, "via": set()})
            a["total"] += 1
            if cls == "clean":
                a["authors"].add(p["author"].lower())
                a["posts"] += 1
                a["via"].add(key)
    for a in agg.values():
        a["share"] = round(a["posts"] / a["total"], 2) if a["total"] else 0.0
    return agg, stats


def cmd_pick_samples(args):
    run = load_run()
    picks = run["picks"]
    missing = [g["key"] for g in run["groups"] if g["key"] not in run["counts"]]
    if missing and not args.force:
        sys.exit("group counts missing for %s (record them, or use --force to continue without)" % ",".join(missing))
    rem = reads_remaining(run)
    if picks["samples1"] is None:
        gm = sorted(group_metrics(run), key=lambda x: (-x[1]["heat"], x[0]["kind"] != "stock",
                                                        -x[1]["mentions"], x[0]["key"]))
        n = min(SAMPLE_GROUPS, run["targets"]["samples"], rem)
        picks["samples1"] = [{"key": "smp-" + g["key"], "kind": "group", "target": g["key"], "query": g["query"],
                              "heat": m["heat"]} for g, m in gm[:n]]
        save_run(run)
        print("stage 1: sample %s" % ", ".join("%s (heat %.1f)" % (p["target"], p["heat"]) for p in picks["samples1"]))
        print("group heat ranking:", ", ".join("%s %.1f" % (g["key"], m["heat"]) for g, m in gm))
        print("next: python3 scripts/build_rankings.py next-call   (ONE X call at a time; see PACING)")
        return
    pend1 = [p["key"] for p in picks["samples1"] if p["key"] not in run["samples"]]
    if pend1 and not args.force:
        sys.exit("stage-1 samples not recorded yet: %s" % ", ".join(pend1))
    if picks["samples2"] is None:
        n = min(COMENTION_SAMPLES, max(0, run["targets"]["samples"] - len(picks["samples1"])), rem)
        agg, _ = analyze_samples(run)
        core_ind = set(run["core_individual_planned"])
        ranked = sorted(((t, a) for t, a in agg.items() if t not in core_ind),
                        key=lambda x: (-len(x[1]["authors"]), -x[1]["posts"], x[0]))
        chosen = [t for t, a in ranked if clean_ok(a)][:n]
        st = load_state()
        end = parse_iso(run["window_end"])
        # then last run's risers / strong non-core tickers, those without a fresh clean check first
        # (a carryover ticker is only counted again if it passed a clean check <= 24h ago)
        prev = [r for r in st.get("last_run_noncore", []) if r.get("riser") or r["mentions"] >= CARRYOVER_MIN_MENTIONS]
        prev.sort(key=lambda r: (clean_check_fresh(r, end), not r.get("riser"), -r["mentions"]))
        for r in prev + st["risers"]:
            if len(chosen) >= n:
                break
            if r["ticker"] not in chosen and r["ticker"] not in core_ind:
                chosen.append(r["ticker"])
        coll = collisions()
        picks["samples2"] = [{"key": "smp-c-" + t, "kind": "comention", "target": t,
                              "query": sample_ticker_query(t, coll)} for t in chosen]
        save_run(run)
        print("stage 2 (co-mentions): %s" % (", ".join(chosen) or "none"))
        print("next: python3 scripts/build_rankings.py next-call   (ONE X call at a time; see PACING)")
        return
    print("samples already picked; next: record pending samples, then pick-candidates")


def parse_sample_json(obj):
    users = {u.get("id"): u.get("username") for u in (obj.get("includes") or {}).get("users", [])}
    posts = []
    for d in obj.get("data") or []:
        aid = d.get("author_id")
        posts.append({"author": users.get(aid) or (aid or "unknown"), "text": d.get("text", ""),
                      "created_at": d.get("created_at")})
    return posts


def cmd_record_sample(args):
    run = load_run()
    planned = {p["key"]: p for p in (run["picks"]["samples1"] or []) + (run["picks"]["samples2"] or [])}
    if args.key not in planned:
        sys.exit("%s is not a picked sample (%s)" % (args.key, ", ".join(planned) or "none picked"))
    if args.failed:
        posts = None
    elif args.json:
        with open(args.json, encoding="utf-8") as f:
            raw = f.read()
        posts = parse_sample_json(json.loads(raw[raw.index("{"):raw.rindex("}") + 1]))
    elif args.lines:
        posts = []
        with open(args.lines, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                sep = "\t" if "\t" in line else (" | " if " | " in line else None)
                if line.strip() and sep:
                    a, t = line.split(sep, 1)
                    posts.append({"author": a.strip().lstrip("@"), "text": t})
    else:
        sys.exit("give --json FILE, --lines FILE or --failed")
    run["samples"][args.key] = {"posts": posts or [], "failed": posts is None, "recorded_at": iso(utcnow())}
    note_reads_left(run, args.reads_left_today)
    msg = stop_checks(run)
    save_run(run)
    print("%s: %s posts; run reads used %d, remaining %d planned (+%d retry reserve)"
          % (args.key, "failed" if posts is None else len(posts), reads_used(run), reads_remaining(run), retry_left(run)))
    if msg:
        print("STOP: %s. %s" % (msg, stop_advice(run)))
    pace(args)


def cmd_record_error(args):
    """One X read that gave no usable result. Decides retry / wait / give up and applies the stop rules."""
    run = load_run()
    lg = run["log"]
    key = args.key if args.key.startswith("smp-") else args.key.upper()
    pending = {k for st in ("groups", "samples", "counts") for k, *_ in pending_calls(run, st)}
    if key not in pending:
        sys.exit("%s is not a pending call (already recorded or skipped?)" % key)
    att = lg["attempts"].setdefault(key, [])
    att.append({"kind": args.kind, "at": iso(utcnow()), "detail": (args.detail or "")[:200]})
    lg["extra_reads"] += 1
    if args.kind == "429":
        lg["retries_429"] += 1
    else:
        lg["errors_other"] += 1
    note_reads_left(run, args.reads_left_today)
    n429 = sum(1 for a in att if a["kind"] == "429")
    noth = sum(1 for a in att if a["kind"] != "429")
    give_up = n429 > len(WAIT_429) or noth >= 2
    if give_up:
        run["skipped"].append(key)
    msg = stop_checks(run)
    save_run(run)
    print("%s: %s error recorded (attempts: %d x 429, %d other); run retries/errors %d of reserve %d, "
          "non-429 errors %d of %d" % (key, args.kind, n429, noth, lg["extra_reads"], RETRY_RESERVE,
                                       lg["errors_other"], MAX_OTHER_ERRORS))
    if msg:
        print("STOP: %s. %s" % (msg, stop_advice(run)))
    elif give_up:
        print("GIVE UP on %s (no more retries); it is skipped and reported as missing. Continue with next-call "
              "after a %ds pause." % (key, PACE_SECONDS))
    elif args.kind == "429":
        w = WAIT_429[n429 - 1]
        print("WAIT %ds, then retry the SAME call (%s). Shell: sleep %d  (set block_until_ms >= %d)"
              % (w, "1st retry" if n429 == 1 else "last try", w, (w + 15) * 1000))
    else:
        print("RETRY the same call once after the normal %ds pause." % PACE_SECONDS)


def cmd_abandon(args):
    if not os.path.exists(RUN_JSON):
        sys.exit("no work/run.json to abandon")
    run = load_json(RUN_JSON, {})
    run["abandoned"] = {"at": iso(utcnow()), "reason": args.reason}
    write_json(RUN_JSON, run)
    dest = archive_run("abandoned")
    print("archived to %s (nothing published). Reason: %s" % (os.path.relpath(dest, ROOT), args.reason))


# --- candidates --------------------------------------------------------------
def clean_check_fresh(r, end):
    c = r.get("clean_checked_at")
    return bool(c) and parse_iso(c) >= end - timedelta(hours=CLEAN_CHECK_MAX_AGE_HOURS)


def cmd_pick_candidates(args):
    run = load_run()
    note_reads_left(run, args.reads_left_today)
    state = load_state()
    universe = load_universe()
    coll = collisions()
    pend = [k for k, *_ in pending_calls(run, "samples")]
    if pend and not args.force:
        sys.exit("samples not recorded yet: %s (or --force)" % ", ".join(pend))
    rem = reads_remaining(run)
    spare = min(SPARE, max(1, rem // 15)) if rem > 0 else 0
    slots = max(0, rem - spare)
    core = run["core_order"]
    core_ind = run["core_individual_planned"]
    # carryover: last run's strong non-core tickers and risers
    end = parse_iso(run["window_end"])
    carry = []
    for r in state.get("last_run_noncore", []):
        t = r["ticker"]
        if t in core or t not in universe:
            continue
        if parse_iso(r["window_end"]) < end - timedelta(hours=CARRYOVER_MAX_AGE_HOURS):
            continue
        if not clean_check_fresh(r, end):
            continue   # no recent sample evidence of clean stock chatter -> must be re-discovered
        if r.get("riser") or r["mentions"] >= CARRYOVER_MIN_MENTIONS:
            carry.append((-(1 if r.get("riser") else 0), -r["mentions"], t))
    carry = [t for *_, t in sorted(carry)][:CARRYOVER_MAX]
    agg, _ = analyze_samples(run)
    disc = sorted(((t, a) for t, a in agg.items()
                   if t not in core_ind and t not in carry and clean_ok(a)),
                  key=lambda x: (-len(x[1]["authors"]), -x[1]["posts"], x[0]))
    pool = [(t, "carryover", None) for t in carry] + \
           [(t, "core" if t in core else "discovered", sorted(a["via"])) for t, a in disc]
    # demoted core in the hottest groups come back first among the remaining core
    hot_groups = {p["target"] for p in (run["picks"]["samples1"] or [])}
    demoted = [t for t in core if t not in core_ind]
    in_hot = {t for g in run["groups"] if g["key"] in hot_groups for t in g["demoted_core"]}
    core_rest = sorted(demoted, key=lambda t: (t not in in_hot, core.index(t)))
    cand_n = min(CANDIDATES, len(pool))
    core_n = slots - cand_n
    if core_n < CORE_FLOOR:
        core_n = min(CORE_FLOOR, slots)
        cand_n = max(0, min(len(pool), slots - core_n))
    core_full = core_ind + core_rest
    core_n = min(core_n, len(core_full))
    chosen_core = core_full[:core_n]
    picks, seen = [], set()
    for t in chosen_core:
        picks.append({"ticker": t, "tier": "core", "source": "core" if t in core_ind else "core-returned",
                      "via": None})
        seen.add(t)
    for t, tier, via in pool:
        if len([p for p in picks if p["tier"] != "core" or p["source"] == "discovered"]) >= cand_n:
            break
        if t in seen:
            continue
        picks.append({"ticker": t, "tier": "core" if tier == "core" else tier,
                      "source": "discovered" if tier in ("discovered", "core") else tier, "via": via})
        seen.add(t)
    # call order = priority if the budget runs out mid-way: top core, then candidates, then the rest of core
    head = [p for p in picks if p["tier"] == "core" and p["source"] != "discovered"]
    rest = [p for p in picks if not (p["tier"] == "core" and p["source"] != "discovered")]
    picks = head[:20] + rest + head[20:]
    for p in picks:
        p["collision"] = p["ticker"] in coll
        p["query"] = ticker_query(p["ticker"], coll)
    run["picks"]["individual"] = picks
    run["spare_reserved"] = spare
    save_run(run)
    by = {}
    for p in picks:
        by[p["source"]] = by.get(p["source"], 0) + 1
    print("individual counts: %d %s (remaining budget %d, spare %d)" % (len(picks), json.dumps(by), rem, spare))
    print("discovered (clean authors, clean share):",
          ", ".join("%s(%d, %.0f%%)" % (t, len(a["authors"]), 100 * a["share"]) for t, a in disc[:30]) or "-")
    rejected = sorted(((t, a) for t, a in agg.items() if t not in core_ind and t not in carry and not clean_ok(a)
                       and a["total"] >= MIN_AUTHORS), key=lambda x: -x[1]["total"])
    if rejected:
        print("rejected by the clean check:", ", ".join("%s(%d posts, %.0f%% clean, %d authors)"
                                                       % (t, a["total"], 100 * a["share"], len(a["authors"]))
                                                       for t, a in rejected[:15]))
    print("next: python3 scripts/build_rankings.py next-call   (ONE X call at a time; see PACING)")


# --- next / status -----------------------------------------------------------
def cmd_next(args):
    run = load_run()
    left = run["log"].get("reads_left_today")
    print("window %s -> %s UTC; run reads used %d of budget %d, remaining %d; reads left today %s (floor %d)"
          % (run["window_start"], run["window_end"], reads_used(run), run["budget"], reads_remaining(run),
             left, FLOOR_LEFT))
    lg = run["log"]
    print("retries/errors %d of reserve %d (429: %d, other: %d of max %d); skipped: %s"
          % (lg["extra_reads"], RETRY_RESERVE, lg["retries_429"], lg["errors_other"], MAX_OTHER_ERRORS,
             ", ".join(run["skipped"]) or "-"))
    if run.get("stopped"):
        print("STOPPED: %s. %s" % (run["stopped"], stop_advice(run)))
        return
    g = pending_calls(run, "groups")
    if g:
        print("NEXT: group counts pending (%d): %s  -> next-call / record, ONE call at a time"
              % (len(g), " ".join(k for k, *_ in g)))
        return
    if run["picks"]["samples1"] is None:
        print("NEXT: pick-samples (stage 1)")
        return
    s = pending_calls(run, "samples")
    if s:
        print("NEXT: samples pending: %s  -> next-call / record-sample, ONE call at a time" % " ".join(k for k, *_ in s))
        return
    if run["picks"]["samples2"] is None:
        print("NEXT: pick-samples (stage 2)")
        return
    if run["picks"]["individual"] is None:
        print("NEXT: pick-candidates")
        return
    c = pending_calls(run, "counts")
    if c:
        print("NEXT: individual counts pending (%d): %s  -> next-call / record, ONE call at a time"
              % (len(c), " ".join(k for k, *_ in c)))
        return
    print("NEXT: build")


# --- build -------------------------------------------------------------------
def hour_iso(run, idx):
    return iso(parse_iso(run["history_start"]) + timedelta(hours=idx))


def cmd_build(args):
    run = load_run()
    if args.extra_reads:
        run["log"]["extra_reads"] += args.extra_reads
        run["log"]["errors_other"] += args.extra_reads
    complete = run_complete(run)
    partial_reason = None
    if not complete:
        ok, why = partial_ok(run)
        if not ok:
            sys.exit("cannot build: run incomplete and not publishable as partial (%s). "
                     "Archive it with: python3 scripts/build_rankings.py abandon --reason '...'" % why)
        missing = [k for st in ("samples", "counts") for k, *_ in pending_calls(run, st)] + list(run["skipped"])
        partial_reason = "%s; not measured: %s" % (run.get("stopped") or "run ended early",
                                                   ", ".join(missing) or "-")
    picks = run["picks"]["individual"] or []
    measured = [p for p in picks if p["ticker"] in run["counts"]]
    if not measured:
        sys.exit("no individual counts recorded")
    universe = load_universe()
    umeta = load_json(UNIVERSE_META, {})
    state = load_state()
    now = utcnow()
    agg, sstats = analyze_samples(run)

    entries = []
    for p in measured:
        t = p["ticker"]
        c = run["counts"][t]
        m = metrics(c["buckets"])
        labels = []
        if p["collision"]:
            labels.append("collision-filtered")
        if m["burst_idx"]:
            labels.append("burst")
        if m["spike_last_hour"]:
            labels.append("spike-last-hour")
        e = {
            "ticker": t,
            "cashtag": "$" + t,
            "mentions": m["mentions"],
            "tier": p["tier"],
            "source": p["source"],
            "labels": labels,
            "mentions_prev_day_same_hours": m["prev_day_same_hours"],
            "momentum_5h_vs_prev_day": m["momentum_5h"],
            "mentions_last_hour": m["last_hour"],
            "prev_9h_avg_per_hour": m["prev_9h_avg"],
            "momentum_last_hour_vs_prev_9h": m["momentum_1h"],
            "mentions_last_hour_prev_day": m["last_hour_prev_day"],
            "_m1_prev_day": m["momentum_1h_vs_prev_day"],
            "burst": bool(m["burst_idx"]),
            "burst_hours": [hour_iso(run, i) for i in m["burst_idx"]],
            "rank_score": m["adjusted"],
            "hourly": c["buckets"][-WINDOW_HOURS:],
        }
        if p["collision"]:
            e["query"] = p["query"]
        if p.get("via"):
            e["discovered_via"] = p["via"]
        a = agg.get(t)
        if a:
            e["sample_authors"] = len(a["authors"])
            e["sample_posts"] = a["total"]
            if a["total"] >= CLEAN_SHARE_MIN_POSTS:
                e["sample_clean_share"] = a["share"]
                if p["tier"] == "core" and a["share"] < LOW_CLEAN_SHARE:
                    labels.append("low-clean-share")
        # clean gate for non-core tickers (discovered passed it when picked; carryover is re-checked
        # against this run's samples when they mention it, otherwise its last passed check is carried)
        if p["tier"] != "core":
            prev = state.get("clean_checks", {}).get(t)
            if a and a["total"] >= CLEAN_SHARE_MIN_POSTS and not clean_ok(a):
                e["_held"] = "sample: %d posts, %.0f%% clean, %d clean authors" % (a["total"], 100 * a["share"],
                                                                                 len(a["authors"]))
            elif a and clean_ok(a):
                e["_clean_checked_at"] = run["window_end"]
            elif prev and parse_iso(prev) >= parse_iso(run["window_end"]) - timedelta(hours=CLEAN_CHECK_MAX_AGE_HOURS):
                e["_clean_checked_at"] = prev
                labels.append("clean-check-carried")
            else:
                e["_held"] = "no sample evidence of clean posts from %d+ authors" % MIN_AUTHORS
        entries.append(e)
    held = [e for e in entries if "_held" in e]
    entries = [e for e in entries if "_held" not in e]
    # risers
    for e in entries:
        m1 = e["momentum_last_hour_vs_prev_9h"] or 0
        if (e.get("_m1_prev_day") or 0) < RISER_MIN_1H_VS_PREV_DAY:
            m1 = 0   # last hour high only vs the night/pre-open hours, not vs the same hour yesterday
        ratio = max(e["momentum_5h_vs_prev_day"] or 0, m1)
        if e["rank_score"] >= RISER_MIN_MENTIONS and ratio >= RISER_MIN_RATIO:
            e["labels"].append("riser")
            e["_riser_score"] = round(e["rank_score"] * math.log2(ratio), 1)
    entries.sort(key=lambda e: (-e["rank_score"], -e["mentions"], e["ticker"]))
    for i, e in enumerate(entries):
        e["rank"] = i + 1
    order = ["rank", "ticker", "cashtag", "mentions", "tier"]
    rankings = [{**{k: e[k] for k in order}, **{k: v for k, v in e.items() if k not in order and not k.startswith("_")}}
                for e in entries]
    risers = sorted((e for e in entries if "_riser_score" in e), key=lambda e: (-e["_riser_score"], e["ticker"]))
    risers_out = [{"ticker": e["ticker"], "cashtag": e["cashtag"], "mentions": e["mentions"], "rank": e["rank"],
                   "tier": e["tier"], "source": e["source"],
                   "mentions_prev_day_same_hours": e["mentions_prev_day_same_hours"],
                   "momentum_5h_vs_prev_day": e["momentum_5h_vs_prev_day"],
                   "mentions_last_hour": e["mentions_last_hour"],
                   "momentum_last_hour_vs_prev_9h": e["momentum_last_hour_vs_prev_9h"],
                   "burst": e["burst"], "riser_score": e["_riser_score"]} for e in risers]

    gsum = []
    for g, m in group_metrics(run):
        gsum.append({"key": g["key"], "kind": g["kind"], "first": g["first"], "last": g["last"],
                     "size": g["size"], "mentions": m["mentions"],
                     "mentions_prev_day_same_hours": m["prev_day_same_hours"],
                     "mentions_last_hour": m["last_hour"], "prev_9h_avg_per_hour": m["prev_9h_avg"],
                     "momentum_5h_vs_prev_day": m["momentum_5h"], "momentum_last_hour_vs_prev_9h": m["momentum_1h"],
                     "heat": m["heat"], "burst": bool(m["burst_idx"]),
                     "sampled": any(p["target"] == g["key"] for p in (run["picks"]["samples1"] or []))})
    missing_groups = [g["key"] for g in run["groups"] if g["key"] not in run["counts"]]
    stock_groups = [g for g in run["groups"] if g["kind"] == "stock"]
    etf_groups = [g for g in run["groups"] if g["kind"] == "etf"]
    tiers, sources = {}, {}
    for e in entries:
        tiers[e["tier"]] = tiers.get(e["tier"], 0) + 1
        sources[e["source"]] = sources.get(e["source"], 0) + 1
    times = sorted(c["recorded_at"] for c in run["counts"].values())
    n_groups = sum(1 for g in run["groups"] if g["key"] in run["counts"])
    n_ind = len(measured)
    n_smp = len(run["samples"])
    extra = int(run["log"].get("extra_reads", 0))
    not_measured = [p["ticker"] for p in picks if p["ticker"] not in run["counts"]]
    not_measured += [t for t in run["core_order"] if t not in {p["ticker"] for p in picks}
                     and not any(t in g["tickers"] for g in stock_groups)]
    coll_terms = suffix()
    out = {
        "generated_at": iso(now),
        "window_hours": WINDOW_HOURS,
        "window_start": run["window_start"],
        "window_end": run["window_end"],
        "universe": "stocks",
        "method": METHOD,
        "source": {
            "platform": "X",
            "endpoint": "GET /2/tweets/counts/recent",
            "query_pattern": "$TICKER " + coll_terms,
            "collision_query_pattern": "$TICKER " + coll_terms + " <per-ticker terms from config/collisions.txt>",
            "granularity": "hour",
            "history_hours": HISTORY_HOURS,
            "metric": "sum of the last 5 hourly buckets (= meta.total_tweet_count over the window)",
        },
        "watchlist_size": len(universe),
        "scan_count": n_ind,
        "ranked_count": len(rankings),
        "tickers_measured": n_ind,
        "screened_count": sum(len(g["tickers"]) for g in run["groups"] if g["key"] in run["counts"]),
        "tiers": tiers,
        "sources": sources,
        "measured_from": times[0],
        "measured_until": times[-1],
        "planned_not_measured": not_measured,
        "x_reads_used": n_groups + n_smp + n_ind + extra,
        "x_reads_breakdown": {"group_counts": n_groups, "samples": n_smp, "individual_counts": n_ind,
                              "extra_errors_retries": extra,
                              "retries_429": int(run["log"].get("retries_429", 0)),
                              "errors_other": int(run["log"].get("errors_other", 0))},
        "partial": not complete,
        "partial_reason": partial_reason,
        "x_reads_left_today": run["log"].get("reads_left_today"),
        "ranking_rule": ("rank_score = 5h count, except that a burst hour (an hour > %d x max(29h hourly "
                         "median, %d) whose next hour fell below a third of it) is capped at that limit; "
                         "only differs from mentions for burst-flagged entries; ties by mentions"
                         % (BURST_FACTOR, BURST_MIN_MEDIAN)),
        "group_sweep": {
            "query_pattern": "($T1 OR $T2 OR ...) " + coll_terms,
            "max_query_chars": MAX_QUERY_LEN,
            "stock_groups": len(stock_groups),
            "stock_tickers_screened": sum(g["size"] for g in stock_groups),
            "demoted_core_in_sweep": [t for g in stock_groups for t in g["demoted_core"]],
            "etf_groups_this_run": [g["key"] for g in etf_groups],
            "etf_groups_total": run["etf_groups_total"],
            "etf_tickers_total": run["etf_tickers_total"],
            "etf_tickers_screened": sum(g["size"] for g in etf_groups),
            "groups_missing": missing_groups,
            "note": ("A group count is an exact count of posts mentioning ANY ticker of the group (a post with "
                     "several of them counts once), with the spam filter applied. It is an upper bound for "
                     "every member, so a quiet group means all its tickers are quiet."),
            "groups": gsum,
        },
        "sampling": {
            "sort_order": "relevancy", "max_results": SAMPLE_MAX_RESULTS,
            "samples": [{"key": k, "kind": p["kind"], "target": p["target"], "failed": run["samples"].get(k, {}).get("failed"),
                         **sstats.get(k, {"posts": 0})}
                        for p in (run["picks"]["samples1"] or []) + (run["picks"]["samples2"] or [])
                        for k in [p["key"]] if k in run["samples"]],
            "min_distinct_clean_authors": MIN_AUTHORS,
            "min_clean_share": MIN_CLEAN_SHARE,
            "candidates_found": sorted(t for t, a in agg.items() if clean_ok(a)),
            "rejected_by_clean_check": sorted(t for t, a in agg.items() if not clean_ok(a) and a["total"] >= MIN_AUTHORS),
        },
        "risers_rule": ("rank_score >= %d and max(momentum_5h_vs_prev_day, momentum_last_hour_vs_prev_9h) >= %.1f, "
                        "where the last-hour ratio only counts if the last hour is also >= %.0fx the same hour "
                        "yesterday; momentum = (now+1)/(baseline+1)"
                        % (RISER_MIN_MENTIONS, RISER_MIN_RATIO, RISER_MIN_1H_VS_PREV_DAY)),
        "risers": risers_out,
        "spam_handling": {
            "filter": "every X query (group sweep, every individual count, every sample) carries the spam suffix",
            "suffix_chars": len(suffix()),
            "post_rules": ("sampled posts are dropped for discovery if the author is in config/spam_accounts.txt, "
                           "the text is a template (same text from >= %d authors), a ticker dump (> %d cashtags, "
                           "or >= %d cashtags with < %d real words), or contains a filter term"
                           % (TEMPLATE_MIN_AUTHORS, MAX_CASHTAGS_PER_POST, DUMP_MIN_CASHTAGS, DUMP_MIN_WORDS)),
            "clean_gate": ("a discovered or carryover ticker is only ranked if a sample of this run (or a passed "
                           "check <= %dh old) shows clean posts from >= %d distinct authors and, with >= %d sampled "
                           "posts, a clean share >= %.0f%%; others are listed in held_back with their count"
                           % (CLEAN_CHECK_MAX_AGE_HOURS, MIN_AUTHORS, CLEAN_SHARE_MIN_POSTS, 100 * MIN_CLEAN_SHARE)),
            "sample_clean_share": ("sample_clean_share = clean / all sampled posts mentioning the ticker (relevancy "
                                   "samples, small n); informational, the rank stays the exact filtered count; core "
                                   "entries below %.0f%% get the label low-clean-share" % (100 * LOW_CLEAN_SHARE)),
        },
        "held_back": [{"ticker": e["ticker"], "mentions": e["mentions"], "tier": e["tier"], "source": e["source"],
                       "reason": e["_held"]} for e in sorted(held, key=lambda e: -e["mentions"])],
        "universe_source": {
            "repo": umeta.get("source_repo", FTD_REPO),
            "file": umeta.get("source_file", FTD_FILE),
            "commit": umeta.get("source_commit"),
            "fetched_at": umeta.get("fetched_at"),
        },
        "cadence": "every 5 hours",
        "note": (("PARTIAL RUN (%s). " % partial_reason if partial_reason else "") +
                 "Ranks the tickers counted individually in this run (core + carryover + tickers discovered "
                 "via the group sweep and samples), all over the same UTC window. The whole non-core stock "
                 "universe is screened at group level every run, but a ticker is only ranked if it was "
                 "counted individually; discovery is sampled, so a busy ticker can still be missed."),
        "rankings": rankings,
    }
    write_json(LATEST_JSON, out)
    hist = os.path.join(HISTORY_DIR, now.strftime("%Y-%m-%dT%H%MZ") + ".json")
    write_json(hist, out)
    run["built_at"] = iso(now)
    run["built_partial"] = not complete
    save_run(run)

    for e in entries:
        state["tickers"][e["ticker"]] = {"mentions": e["mentions"], "window_end": run["window_end"],
                                         "measured_at": run["counts"][e["ticker"]]["recorded_at"],
                                         "tier": e["tier"], "source": e["source"]}
    state["tickers"] = dict(sorted(state["tickers"].items()))
    state["risers"] = [{"ticker": r["ticker"], "mentions": r["mentions"], "window_end": run["window_end"]}
                       for r in risers_out]
    state["last_run_noncore"] = [{"ticker": e["ticker"], "mentions": e["mentions"], "window_end": run["window_end"],
                                  "riser": "riser" in e["labels"], "clean_checked_at": e.get("_clean_checked_at")}
                                 for e in entries if e["tier"] != "core"]
    cc = state.setdefault("clean_checks", {})
    for e in entries:
        if e.get("_clean_checked_at"):
            cc[e["ticker"]] = e["_clean_checked_at"]
    for t, a in agg.items():
        if clean_ok(a):
            cc[t] = run["window_end"]
    cut = parse_iso(run["window_end"]) - timedelta(hours=CLEAN_CHECK_MAX_AGE_HOURS)
    state["clean_checks"] = {t: v for t, v in sorted(cc.items()) if parse_iso(v) >= cut}
    if etf_groups and run["etf_groups_total"]:
        last = int(etf_groups[-1]["key"][1:]) - 1
        state["etf_rotation_next"] = (last + 1) % run["etf_groups_total"]
    state["method_version"] = METHOD_VERSION
    state["updated_at"] = iso(now)
    write_json(STATE_JSON, state)
    write_next(state, now)
    if partial_reason:
        print("PARTIAL build: " + partial_reason)
    print("wrote %s and %s: %d ranked, %d risers, reads %s, planned-not-measured %d"
          % (os.path.relpath(LATEST_JSON, ROOT), os.path.relpath(hist, ROOT), len(rankings), len(risers_out),
             json.dumps(out["x_reads_breakdown"]), len(not_measured)))
    print("top 10:", ", ".join("%s %d%s" % (r["ticker"], r["mentions"], "*" if r["burst"] else "") for r in rankings[:10]))
    print("risers:", ", ".join("%s %d (x%.1f/x%.1f)" % (r["ticker"], r["mentions"], r["momentum_5h_vs_prev_day"] or 0,
                                                       r["momentum_last_hour_vs_prev_9h"]) for r in risers_out[:10]) or "-")


def write_next(state, now):
    core = core_priority(state)
    carry = [r["ticker"] for r in state.get("last_run_noncore", [])
             if (r.get("riser") or r["mentions"] >= CARRYOVER_MIN_MENTIONS) and r.get("clean_checked_at")][:CARRYOVER_MAX]
    write_json(NEXT_JSON, {
        "generated_at": iso(now),
        "method": METHOD,
        "note": ("Expected individual counts of the next run: core (strongest first; the weakest go into the "
                 "group sweep) and carryover. Discovered tickers are only known during the run. "
                 "Recomputed by `plan`/`pick-candidates` at run time."),
        "query_pattern": BASE_QUERY,
        "count": len(core[:CORE_INDIVIDUAL]) + len(carry),
        "tickers": [{"ticker": t, "tier": "core"} for t in core[:CORE_INDIVIDUAL]] +
                   [{"ticker": t, "tier": "carryover"} for t in carry],
        "core_in_group_sweep": core[CORE_INDIVIDUAL:],
        "etf_rotation_next": state.get("etf_rotation_next", 0),
    })


def cmd_migrate(args):
    """Bring method-v1 data files up to the v2 shape (idempotent, no X reads)."""
    state = load_state()
    latest = load_json(LATEST_JSON, {}) or {}
    if not state["last_run_noncore"] and latest.get("rankings"):
        # seed carryover from the last v1 run's non-core ("hot"/"rotation") tickers
        state["last_run_noncore"] = [{"ticker": r["ticker"], "mentions": r["mentions"],
                                      "window_end": latest["window_end"], "riser": False}
                                     for r in latest["rankings"] if r.get("tier", "core") != "core"]
    state["method_version"] = METHOD_VERSION
    write_json(STATE_JSON, state)
    write_next(state, utcnow())
    print("state.json and next_tickers.json migrated to method v%d (latest.json/history untouched)" % METHOD_VERSION)


# --- offline checks ----------------------------------------------------------
def cmd_check(args):
    universe = load_universe()
    exclude = set(read_list("exclude_tickers.txt"))
    state = load_state()
    core = core_priority(state)
    cs = set(core)
    stock_nc = sorted(t for t, r in universe.items() if r["asset_type"] == "Stock" and t not in cs and t not in exclude)
    etf_nc = sorted(t for t, r in universe.items() if r["asset_type"] == "ETF" and t not in cs and t not in exclude)
    problems = []
    for name, tick in (("stock", sorted(set(stock_nc) | set(core[CORE_INDIVIDUAL:]))), ("etf", etf_nc)):
        groups = pack(tick)
        flat = [t for g in groups for t in g]
        if flat != tick:
            problems.append("%s packing does not reproduce the ticker list" % name)
        for g in groups:
            q = group_query(g)
            back = re.findall(r"\$([A-Z]{1,6})\b", q.split(") ", 1)[0])
            if back != g:
                problems.append("%s group %s: query does not parse back to its members" % (name, g[0]))
            if len(q) > MAX_QUERY_LEN:
                problems.append("%s group %s too long: %d" % (name, g[0], len(q)))
        print("%s: %d tickers in %d groups (sizes %s..%s, longest query %d chars)"
              % (name, len(tick), len(groups), min(map(len, groups)), max(map(len, groups)),
                 max(len(group_query(g)) for g in groups)))
    if not set(stock_nc) <= set(t for g in pack(sorted(set(stock_nc) | set(core[CORE_INDIVIDUAL:]))) for t in g):
        problems.append("a non-core stock is missing from the sweep")
    coll = collisions()
    for t, extra in coll.items():
        q = ticker_query(t, coll)
        if len(q) > MAX_QUERY_LEN or q.count('"') % 2:
            problems.append("collision query for %s is invalid: %s" % (t, q))
    print("suffix (%d chars): %s" % (len(suffix()), suffix()))
    print("collisions: %d (in universe: %d); spam accounts: %d; core: %d"
          % (len(coll), sum(1 for t in coll if t in universe), len(spam_accounts()), len(core)))
    t, ok = size_run(RUN_READS, len(pack(sorted(set(stock_nc) | set(core[CORE_INDIVIDUAL:])))), len(pack(etf_nc)))
    print("default split of %d reads: %s (sum %d)" % (RUN_READS, json.dumps(t), sum(t.values())))
    for p in problems:
        print("PROBLEM:", p)
    if problems:
        sys.exit(1)
    print("check OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("universe", help="refresh data/universe.csv from free-ticker-database")
    p = sub.add_parser("plan", help="window, budget, groups -> work/run.json")
    p.add_argument("--now", help="override current UTC time (YYYY-MM-DDTHH:MM:SSZ)")
    p.add_argument("--reads-left-today", type=int)
    p = sub.add_parser("calls", help="print exact x tool arguments for pending calls of a stage")
    p.add_argument("stage", choices=["groups", "samples", "counts"])
    p.add_argument("key", nargs="*")
    p.add_argument("--all", action="store_true", help="list all pending calls (review only; never batch them)")
    p = sub.add_parser("record", help="record counts: KEY=TOTAL:b1,b2,...,b29 (29 hourly buckets, oldest first)")
    p.add_argument("pairs", nargs="*")
    p.add_argument("--reads-left-today", type=int)
    p.add_argument("--extra-reads", type=int, default=0, help="(legacy) non-429 error reads; prefer record-error")
    p.add_argument("--no-sleep", action="store_true", help="skip the pacing pause (tests only)")
    p = sub.add_parser("pick-samples", help="choose the next sample batch (stage 1 groups, stage 2 co-mentions)")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("record-sample", help="record the posts of one sample read")
    p.add_argument("key")
    p.add_argument("--json", help="file with the search_posts_all JSON result")
    p.add_argument("--lines", help="file with one 'username | text' (or username<TAB>text) line per post")
    p.add_argument("--failed", action="store_true", help="the read returned, but with no usable posts")
    p.add_argument("--reads-left-today", type=int)
    p.add_argument("--no-sleep", action="store_true", help="skip the pacing pause (tests only)")
    p = sub.add_parser("record-error", help="record a read that failed (HTTP 429 or other) and get retry advice")
    p.add_argument("key")
    p.add_argument("--kind", choices=["429", "other"], required=True)
    p.add_argument("--detail", help="short error text")
    p.add_argument("--reads-left-today", type=int)
    sub.add_parser("next-call", help="print the ONE next X call to make (and how to record it)")
    p = sub.add_parser("abandon", help="archive an unfinished run without publishing")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("pick-candidates", help="final list of individual counts (core + carryover + discovered)")
    p.add_argument("--reads-left-today", type=int)
    p.add_argument("--force", action="store_true")
    sub.add_parser("next", help="show the next step and the budget")
    sub.add_parser("status", help="alias of next")
    p = sub.add_parser("build", help="write latest.json, history, state, next_tickers")
    p.add_argument("--extra-reads", type=int, default=0)
    sub.add_parser("check", help="offline self-checks")
    sub.add_parser("migrate", help="migrate state.json/next_tickers.json from the old method")
    args = ap.parse_args()
    {"universe": cmd_universe, "plan": cmd_plan, "calls": cmd_calls, "record": cmd_record,
     "pick-samples": cmd_pick_samples, "record-sample": cmd_record_sample,
     "pick-candidates": cmd_pick_candidates, "next": cmd_next, "status": cmd_next,
     "record-error": cmd_record_error, "next-call": cmd_next_call, "abandon": cmd_abandon,
     "build": cmd_build, "check": cmd_check, "migrate": cmd_migrate}[args.cmd](args)


if __name__ == "__main__":
    main()
