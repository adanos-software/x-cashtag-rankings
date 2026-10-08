#!/usr/bin/env python3
"""Build the X stock cashtag ranking from per-ticker X counts (stdlib only).

Workflow for one refresh (see README.md):

  python3 scripts/build_rankings.py universe        # optional, refresh ticker universe (weekly)
  python3 scripts/build_rankings.py plan            # pick ~95 tickers + the UTC window -> work/plan.json
  # for every ticker in work/plan.json call X counts/recent with query "$TICKER -is:retweet",
  # granularity=hour, start_time/end_time = the plan window, and record meta.total_tweet_count:
  python3 scripts/build_rankings.py record AAPL=110 MSFT=95 ... [--reads-left-today N]
  python3 scripts/build_rankings.py status          # which planned tickers are still missing
  python3 scripts/build_rankings.py build [--extra-reads N]
  # -> data/latest.json, data/history/<UTC>.json, data/state.json, data/next_tickers.json

Only tickers measured in THIS run (same window for all) are ranked. Nothing is
carried over from earlier runs into the ranking.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
WORK = os.path.join(ROOT, "work")
CONFIG = os.path.join(ROOT, "config")

UNIVERSE_CSV = os.path.join(DATA, "universe.csv")
UNIVERSE_META = os.path.join(DATA, "universe_meta.json")
STATE_JSON = os.path.join(DATA, "state.json")
NEXT_JSON = os.path.join(DATA, "next_tickers.json")
LATEST_JSON = os.path.join(DATA, "latest.json")
HISTORY_DIR = os.path.join(DATA, "history")
PLAN_JSON = os.path.join(WORK, "plan.json")
COUNTS_CSV = os.path.join(WORK, "counts.csv")
RUNLOG_JSON = os.path.join(WORK, "run_log.json")

# --- method parameters -------------------------------------------------------
WINDOW_HOURS = 5            # trailing window, aligned to full UTC hours
RUN_SIZE = 95               # tickers measured per run (= X reads per run, +<=5 spare)
HOT_MAX = 10                # max non-core "hot" re-measurements per run
HOT_MIN_MENTIONS = 10       # a non-core ticker is "hot" if its last count was >= this
HOT_MAX_AGE_HOURS = 48      # ... and that count is not older than this
QUERY_PATTERN = "$TICKER -is:retweet"
CASHTAG_RE = re.compile(r"^[A-Z]{1,6}$")

FTD_REPO = "adanos-software/free-ticker-database"
FTD_FILE = "data/core_listings.csv"
US_EXCHANGES = ("NASDAQ", "NYSE", "NYSE ARCA", "BATS", "NYSE MKT")
ASSET_TYPES = ("Stock", "ETF")


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


def read_list(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            t = line.split("#", 1)[0].strip().upper()
            if t and t not in out:
                out.append(t)
    return out


def query_for(ticker):
    return QUERY_PATTERN.replace("TICKER", ticker)


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
    except Exception as e:  # metadata only; fall back to the gh CLI if installed
        try:
            import subprocess
            commit = subprocess.run(["gh", "api", "repos/%s/commits/main" % FTD_REPO, "--jq", ".sha"],
                                    capture_output=True, text=True, timeout=60, check=True).stdout.strip() or None
        except Exception:
            print("warning: could not read source commit: %s" % e, file=sys.stderr)
    exclude = set(read_list(os.path.join(CONFIG, "exclude_tickers.txt")))
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
    meta = {
        "source_repo": FTD_REPO,
        "source_file": FTD_FILE,
        "source_commit": commit,
        "fetched_at": iso(utcnow()),
        "filters": {
            "exchange": list(US_EXCHANGES),
            "asset_type": list(ASSET_TYPES),
            "ticker_regex": CASHTAG_RE.pattern,
            "excluded": "config/exclude_tickers.txt (crypto-coin cashtag collisions)",
        },
        "count": len(rows),
    }
    write_json(UNIVERSE_META, meta)
    print("universe: %d tickers written to %s" % (len(rows), os.path.relpath(UNIVERSE_CSV, ROOT)))


def load_universe():
    if not os.path.exists(UNIVERSE_CSV):
        sys.exit("data/universe.csv missing; run: python3 scripts/build_rankings.py universe")
    with open(UNIVERSE_CSV, encoding="utf-8") as f:
        return {r["ticker"]: r for r in csv.DictReader(f)}


# --- selection ---------------------------------------------------------------
def select_tickers(now):
    """Return [(ticker, tier)] for one run: core + hot + rotation (RUN_SIZE total)."""
    universe = load_universe()
    exclude = set(read_list(os.path.join(CONFIG, "exclude_tickers.txt")))
    state = load_json(STATE_JSON, {"tickers": {}})["tickers"]
    # core tickers are always measured, even if the database lacks them (e.g. SHOP)
    core = [t for t in read_list(os.path.join(CONFIG, "core_tickers.txt"))
            if t not in exclude and CASHTAG_RE.match(t)]
    chosen = [(t, "core") for t in core]
    taken = set(core)

    cutoff = now - timedelta(hours=HOT_MAX_AGE_HOURS)
    hot = []
    for t, s in state.items():
        if t in taken or t in exclude or t not in universe:
            continue
        if s.get("mentions", 0) >= HOT_MIN_MENTIONS and parse_iso(s["window_end"]) >= cutoff:
            hot.append((-s["mentions"], t))
    for _, t in sorted(hot)[:HOT_MAX]:
        chosen.append((t, "hot"))
        taken.add(t)

    # rotation: least recently measured first ("" = never measured); ties broken by
    # config/rotation_priority.txt order, then stocks before ETFs, then alphabetically
    prio = {t: i for i, t in enumerate(read_list(os.path.join(CONFIG, "rotation_priority.txt")))}
    need = max(0, RUN_SIZE - len(chosen))
    pool = []
    for t, row in universe.items():
        if t in taken or t in exclude:
            continue
        last = state.get(t, {}).get("measured_at", "")
        pool.append((last, prio.get(t, len(prio)), 0 if row["asset_type"] == "Stock" else 1, t))
    for *_, t in sorted(pool)[:need]:
        chosen.append((t, "rotation"))
    return chosen


def window_for(now):
    end = now.replace(minute=0, second=0, microsecond=0)
    return end - timedelta(hours=WINDOW_HOURS), end


# --- commands ----------------------------------------------------------------
def cmd_plan(args):
    now = parse_iso(args.now) if args.now else utcnow()
    start, end = window_for(now)
    tickers = select_tickers(now)
    os.makedirs(WORK, exist_ok=True)
    if os.path.exists(COUNTS_CSV):  # archive leftovers from an unfinished run
        os.replace(COUNTS_CSV, COUNTS_CSV + ".prev-" + now.strftime("%Y%m%dT%H%M%SZ"))
    plan = {
        "planned_at": iso(now),
        "window_start": iso(start),
        "window_end": iso(end),
        "granularity": "hour",
        "query_pattern": QUERY_PATTERN,
        "tickers": [{"ticker": t, "tier": tier, "query": query_for(t)} for t, tier in tickers],
    }
    write_json(PLAN_JSON, plan)
    write_json(RUNLOG_JSON, {"reads_left_today": None, "extra_reads": 0})
    tiers = {}
    for _, tier in tickers:
        tiers[tier] = tiers.get(tier, 0) + 1
    print("window %s -> %s (UTC), %d tickers %s" % (plan["window_start"], plan["window_end"], len(tickers), tiers))
    print("tool: x get_posts_counts_recent  args: query=<query> granularity=hour start_time=%s end_time=%s"
          % (plan["window_start"], plan["window_end"]))
    print(" ".join(t for t, _ in tickers))


def load_plan():
    plan = load_json(PLAN_JSON)
    if not plan:
        sys.exit("work/plan.json missing; run plan first")
    return plan


def load_counts():
    out = {}
    if os.path.exists(COUNTS_CSV):
        with open(COUNTS_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[r["ticker"]] = r  # last write wins (corrections)
    return out


def cmd_record(args):
    plan = load_plan()
    planned = {p["ticker"] for p in plan["tickers"]}
    new = not os.path.exists(COUNTS_CSV)
    now = iso(utcnow())
    rows = []
    for item in args.pairs:
        if "=" not in item:
            sys.exit("bad pair %r, expected TICKER=COUNT" % item)
        t, c = item.split("=", 1)
        t = t.strip().lstrip("$").upper()
        if t not in planned:
            sys.exit("%s is not in work/plan.json" % t)
        rows.append((t, int(c), now))
    with open(COUNTS_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ticker", "mentions", "measured_at"])
        w.writerows(rows)
    log = load_json(RUNLOG_JSON, {"reads_left_today": None, "extra_reads": 0})
    if args.reads_left_today is not None:
        log["reads_left_today"] = args.reads_left_today
    if args.extra_reads:
        log["extra_reads"] = log.get("extra_reads", 0) + args.extra_reads
    write_json(RUNLOG_JSON, log)
    done = load_counts()
    print("recorded %d; %d/%d planned tickers measured" % (len(rows), len(done), len(planned)))


def cmd_status(args):
    plan = load_plan()
    done = load_counts()
    missing = [p["ticker"] for p in plan["tickers"] if p["ticker"] not in done]
    print("window %s -> %s; measured %d/%d" % (plan["window_start"], plan["window_end"],
                                                len(done), len(plan["tickers"])))
    print("missing:", " ".join(missing) if missing else "(none)")


def cmd_build(args):
    plan = load_plan()
    counts = load_counts()
    if not counts:
        sys.exit("no counts recorded in work/counts.csv")
    tier_of = {p["ticker"]: p["tier"] for p in plan["tickers"]}
    universe = load_universe()
    umeta = load_json(UNIVERSE_META, {})
    log = load_json(RUNLOG_JSON, {"reads_left_today": None, "extra_reads": 0})
    now = utcnow()

    measured = sorted(counts.values(), key=lambda r: (-int(r["mentions"]), r["ticker"]))
    rankings = [{
        "rank": i + 1,
        "ticker": r["ticker"],
        "cashtag": "$" + r["ticker"],
        "mentions": int(r["mentions"]),
        "tier": tier_of.get(r["ticker"], "extra"),
    } for i, r in enumerate(measured)]
    tiers = {}
    for r in rankings:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    times = sorted(r["measured_at"] for r in measured)
    reads_used = len(measured) + int(log.get("extra_reads", 0)) + int(args.extra_reads or 0)
    not_measured = [p["ticker"] for p in plan["tickers"] if p["ticker"] not in counts]

    out = {
        "generated_at": iso(now),
        "window_hours": WINDOW_HOURS,
        "window_start": plan["window_start"],
        "window_end": plan["window_end"],
        "universe": "stocks",
        "method": "core+hot+rotation",
        "source": {
            "platform": "X",
            "endpoint": "GET /2/tweets/counts/recent",
            "query_pattern": plan["query_pattern"],
            "granularity": plan["granularity"],
            "metric": "meta.total_tweet_count over the window",
        },
        "watchlist_size": len(universe),
        "scan_count": len(measured),
        "ranked_count": len(rankings),
        "tickers_measured": len(measured),
        "tiers": tiers,
        "measured_from": times[0],
        "measured_until": times[-1],
        "planned_not_measured": not_measured,
        "x_reads_used": reads_used,
        "x_reads_left_today": log.get("reads_left_today"),
        "universe_source": {
            "repo": umeta.get("source_repo", FTD_REPO),
            "file": umeta.get("source_file", FTD_FILE),
            "commit": umeta.get("source_commit"),
            "fetched_at": umeta.get("fetched_at"),
        },
        "cadence": "every 5 hours",
        "note": ("Ranks only the tickers measured in this run, all over the same UTC window. "
                 "Not an exhaustive ranking of every cashtag on X."),
        "rankings": rankings,
    }
    write_json(LATEST_JSON, out)
    hist = os.path.join(HISTORY_DIR, now.strftime("%Y-%m-%dT%H%MZ") + ".json")
    write_json(hist, out)

    state = load_json(STATE_JSON, {"tickers": {}})
    for r in measured:
        state["tickers"][r["ticker"]] = {
            "mentions": int(r["mentions"]),
            "window_end": plan["window_end"],
            "measured_at": r["measured_at"],
        }
    state["updated_at"] = iso(now)
    state["tickers"] = dict(sorted(state["tickers"].items()))
    write_json(STATE_JSON, state)

    nxt = select_tickers(now + timedelta(hours=5))
    write_json(NEXT_JSON, {
        "generated_at": iso(now),
        "note": "Tickers the next run will measure (recomputed by `plan` at run time).",
        "query_pattern": QUERY_PATTERN,
        "count": len(nxt),
        "tickers": [{"ticker": t, "tier": tier} for t, tier in nxt],
    })
    print("wrote %s and %s: %d ranked, %d reads used, %d planned-not-measured"
          % (os.path.relpath(LATEST_JSON, ROOT), os.path.relpath(hist, ROOT),
             len(rankings), reads_used, len(not_measured)))
    print("top 10:", ", ".join("%s %d" % (r["ticker"], r["mentions"]) for r in rankings[:10]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("universe", help="refresh data/universe.csv from free-ticker-database")
    p = sub.add_parser("plan", help="choose tickers + window for this run -> work/plan.json")
    p.add_argument("--now", help="override current UTC time (YYYY-MM-DDTHH:MM:SSZ)")
    p = sub.add_parser("record", help="append TICKER=COUNT results to work/counts.csv")
    p.add_argument("pairs", nargs="*")
    p.add_argument("--reads-left-today", type=int, help="'reads left today' from the last x tool result")
    p.add_argument("--extra-reads", type=int, default=0, help="x reads spent without a usable count (errors, retries)")
    sub.add_parser("status", help="show planned tickers not yet recorded")
    p = sub.add_parser("build", help="write latest.json, history, state, next_tickers")
    p.add_argument("--extra-reads", type=int, default=0)
    args = ap.parse_args()
    {"universe": cmd_universe, "plan": cmd_plan, "record": cmd_record,
     "status": cmd_status, "build": cmd_build}[args.cmd](args)


if __name__ == "__main__":
    main()
