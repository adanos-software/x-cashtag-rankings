#!/usr/bin/env python3
"""X stock cashtag rankings: group sweep + adaptive splits + sampling + daily exact counts (stdlib only).

One run (details: README.md and docs/RUNBOOK.md). X calls are made strictly ONE AT A TIME, and the script
generates every query (groups, split parts, tickers, samples); never build one by hand:

  python3 scripts/build_rankings.py plan [--reads-left-today N]   # window, groups, budget, split tree -> work/run.json
  loop:
    python3 scripts/build_rankings.py next-call      # prints the ONE next X call (or the next script step)
    -> make that single x tool call
    python3 scripts/build_rankings.py record KEY=TOTAL:b1,...,b29 --reads-left-today B     (counts; pauses 4s)
    python3 scripts/build_rankings.py record-sample KEY --lines FILE --reads-left-today B  (samples; pauses 4s)
    python3 scripts/build_rankings.py record-error KEY --kind 429|other --reads-left-today B   (on errors;
        prints WAIT 90s / WAIT 240s / RETRY / GIVE UP / STOP)
    script steps when next-call says so: pick-samples (twice), pick-candidates
  python3 scripts/build_rankings.py build          # complete run, or partial if group sweep + megacaps are done
  python3 scripts/build_rankings.py abandon --reason "..."   # archive an unpublishable run
  python3 scripts/build_rankings.py next | check | calls STAGE [--all] | splits-reset

Order: groups (13 stock + 3 ETF) -> 10 megacaps -> adaptive 4-way splits (<= 24 reads, chosen one at a time
from the counts just recorded; the tree persists in data/splits.json, fail-closed validation) -> 6 + 4
samples -> daily exact counts (split leaves, sample discoveries, hot re-reads, core due today) -> build
(latest.json, history, today.json, daily/, hourly.json, splits.json, state.json).
Every count read uses granularity=hour over the last 29 full UTC hours.
"""
import argparse
import csv
import glob
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
SPLITS_JSON = os.path.join(DATA, "splits.json")
HOURLY_JSON = os.path.join(DATA, "hourly.json")
TODAY_JSON = os.path.join(DATA, "today.json")
DAILY_DIR = os.path.join(DATA, "daily")
ARCHIVE = os.path.join(WORK, "archive")

# --- method parameters -------------------------------------------------------
METHOD = "group-sweep+adaptive-splits+sampling+daily-counts"
METHOD_VERSION = 4           # v4 (2026-10-17): query mode (cashtag | bare) + ambiguity rules for bare tickers
QUERY_MODE_JSON = os.path.join(DATA, "query_mode.json")
DOLLAR_NOTE = "$ was removed from query"     # what the x tools print when they strip the $ (since 2026-10-17)
BARE_SINCE = "2026-10-17"
OWN_CLEAN_DEFAULT_PRIOR_BARE = 0.5   # bare mode: prior share of bare matches that are real stock mentions
WINDOW_HOURS = 5             # ranked window: last 5 full UTC hours
HISTORY_HOURS = 29           # every count read covers 29 hourly buckets (window + same hours yesterday)
MAX_QUERY_LEN = 4096         # X rejects longer queries (4099 chars -> invalid request, still costs a read)
RUN_READS = 95               # target X reads per run (everything: groups, samples, counts, retries)
DAILY_CAP = 600              # Alexander's cap per UTC day, shared by all bots
DAILY_QUOTA = 1000           # the x tools report "B of 1000 today"; used today = 1000 - B
FLOOR_LEFT = DAILY_QUOTA - DAILY_CAP   # never let "reads left today" drop below this (400)
MIN_RUN_READS = 20           # skip the run if fewer reads fit
ETF_GROUPS_PER_RUN = 3       # ETF groups rotate, 3 per run (14 groups -> each about once a day)
SAMPLE_GROUPS = 4            # stage-1 samples: the hottest fresh groups / split nodes
COMENTION_SAMPLES = 2        # stage-2 samples: top new tickers from stage 1, unverified split leaves, risers
MEGACAPS_N = 10              # config/megacaps.txt: counted individually every run
SPLIT_READS = 24             # adaptive 4-way splits per run (3 reads with subtraction, 4 if the parent is stale)
DAILY_COUNTS_MAX = 35        # exact 29h counts per run for split leaves, sample candidates, hot re-reads, core
CORE_DAILY_MIN = 10          # of those, at least this many go to core tickers not yet measured this UTC day
LEAF_MAX = 3                 # split nodes with <= 3 tickers are leaves: their members get exact daily counts
LEAF_MIN_24H = 15            # ... if the leaf had >= 15 posts in the last 24h
SPLIT_MIN_24H = 40           # only nodes with >= 40 posts in the last 24h are split
NODE_MAX_AGE_HOURS = 30      # split nodes not re-measured for 30h are pruned
MAX_NODES = 400              # cap on persistent split nodes (coldest pruned first)
HOT_REREAD_5H = 60           # a ticker measured earlier today is re-read if its last 5h count was >= 60
HOT_REREAD_MAX = 8           # ... at most 8 per run before discoveries/core (more only to fill unused slots)
DRILL_CONC = 1.6             # a split part holding >= 1.6x its fair share (>= 40% of a 4-way parent) is drilled
OVERLAP_WARN = 0.05          # subtraction: measured children exceeding the parent by > 5% -> flagged
SAMPLE_MAX_RESULTS = 25
CANDIDATES = 20              # individual counts for discovered + carryover tickers
CARRYOVER_MAX = 8            # of those, at most this many carried over from the last run
CARRYOVER_MIN_MENTIONS = 25  # ... non-core tickers with >= this many mentions last run (or risers)
CARRYOVER_MAX_AGE_HOURS = 12
CORE_INDIVIDUAL = 50         # core tickers counted individually; the weakest rest go into the sweep
CORE_FLOOR = 10              # budget shrink: core is cut first, down to this
SPARE = 4                    # unplanned slack inside RUN_READS (invalid requests, re-asks)
RETRY_RESERVE = 10           # extra reads per run for retries (429 / errors), on top of RUN_READS -> max ~105
MAX_OTHER_ERRORS = 5         # stop the run after this many non-429 errors
PACE_SECONDS = 6             # record commands pause this long, so calls stay strictly sequential (<= ~10/min)
WAIT_429 = (90, 240)         # wait before the 1st and the 2nd (last) retry of a call that got HTTP 429
COOLDOWN_AFTER_GIVE_UP = 300 # after a call gave up on 429s, pause before the next call (shared limit recovers)
SAMPLE_MAX_QUERY_LEN = 4095  # search_posts_all rejects 4096-char queries that counts accept (2026-10-12 smp-S12)
OWN_SAMPLES = 6              # per run: recency samples of a ticker's OWN query -> clean-share estimate
OWN_CLEAN_PRIOR_K = 10       # pseudo-posts of the prior in the smoothed own clean share
OWN_CLEAN_MAX_AGE_HOURS = 72 # own-sample evidence older than this is dropped
OWN_CLEAN_DEFAULT_PRIOR = 0.8  # prior before any own sample (2026-10-13 calibration: 8 big tickers 72-96%, median 0.84)
CRYPTO_SHARE_FLAG = 0.4      # own/sampled posts of a ticker >= 40% crypto (>= 5 posts) -> crypto-collision
CRYPTO_TAGS = {"BTC", "ETH", "XRP", "SOL", "USDT", "USDC", "HBAR", "XDC", "ADA", "DOGE", "BNB", "XLM", "LINK",
               "AVAX", "DOT", "SHIB", "PEPE", "SUI", "TRX", "LTC", "TON", "ALGO", "XMR", "ICP", "FET", "QNT"}
MIN_AUTHORS = 3              # a sampled ticker needs >= 3 distinct authors of CLEAN posts to be a candidate
MIN_CLEAN_SHARE = 0.3        # ... and >= 30% of the sampled posts mentioning it must be clean
CLEAN_SHARE_MIN_POSTS = 5    # a clean share is only reported/used with >= 5 sampled posts mentioning the ticker
LOW_CLEAN_SHARE = 0.3        # core entries below this get the label "low-clean-share" (rank unchanged)
# word-collision guard (bare mode, 2026-10-17; tuned on the 11:17 Berlin run 04-09Z: SHOP 10,901 / BABA 6,669 /
# MARA 1,783 / NIO 353 / CRM 296 / PDD 140 bare were 10-520x their median $cashtag 5h count, while TSLA, AMD,
# NVDA, QQQ, CRWV were 1.2-4.7x; AMD 5.4x and XOM 6.3x in the 07:48 run are the closest real names)
COLLISION_RATIO = 8.0             # bare 5h count > 8x the median $cashtag 5h count before the switch -> suspected
COLLISION_RATIO_MIN_BARE = 50     # ... only for bare counts >= 50 (tiny counts: ratios are noise, rank is low)
COLLISION_NO_HISTORY_MIN = 300    # no $cashtag history at all and bare 5h count > 300 -> suspected
COLLISION_MIN_REAL_SHARE = 0.3    # an own sample with >= 30% real mentions clears a suspected ticker
COLLISION_MIN_OWN_POSTS = 10      # ... from >= 10 own sampled posts (72h)
COLLISION_QUEUE_SHARE = 0.5       # queued suspected tickers take at most this share of the own-sample slots
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


def write_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        else:
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


_mode_cache = {}


def query_mode_info():
    try:
        st = os.stat(QUERY_MODE_JSON)
        mt = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {"mode": "cashtag"}
    if _mode_cache.get("mt") != (QUERY_MODE_JSON, mt):
        _mode_cache["mt"] = (QUERY_MODE_JSON, mt)
        _mode_cache["info"] = load_json(QUERY_MODE_JSON, {}) or {"mode": "cashtag"}
    return _mode_cache["info"]


def query_mode():
    """'cashtag' ($TICKER queries) or 'bare' (TICKER; the x tools strip '$' since 2026-10-17)."""
    m = query_mode_info().get("mode", "cashtag")
    if m not in ("cashtag", "bare"):
        raise SystemExit("data/query_mode.json: unknown mode %r" % m)
    return m


def bare():
    return query_mode() == "bare"


def set_query_mode(mode, reason, now=None):
    info = dict(query_mode_info())
    if info.get("mode", "cashtag") == mode:
        return False
    hist = list(info.get("history", []))
    hist.append({"at": iso(now or utcnow()), "from": info.get("mode", "cashtag"), "to": mode, "reason": reason})
    write_json(QUERY_MODE_JSON, {"mode": mode, "since": iso(now or utcnow()), "reason": reason, "history": hist})
    _mode_cache.clear()
    return True


def qtag(t):
    return t if bare() else "$" + t


def ambiguous_tickers():
    """config/ambiguous_tickers.txt (generated by scripts/gen_ambiguous.py): ticker -> reason."""
    out = {}
    path = os.path.join(CONFIG, "ambiguous_tickers.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    t, _, why = line.partition("#")
                    out[t.strip().upper()] = why.strip()
    return out


def context_tickers():
    """Ambiguous core names counted in bare mode only with a finance-context clause (config/context_tickers.txt)."""
    if not bare():
        return []
    ex = set(read_list("exclude_tickers.txt"))
    return [t for t in read_list("context_tickers.txt") if t not in ex and CASHTAG_RE.match(t)]


def context_terms():
    return config_lines("context_terms.txt")


def context_clause():
    return "(" + " OR ".join(context_terms()) + ")"


def not_measurable():
    """Bare mode: ambiguous tickers that are not counted at all (ambiguous minus context tickers)."""
    if not bare():
        return {}
    ctx = set(context_tickers())
    return {t: why for t, why in ambiguous_tickers().items() if t not in ctx}


def excluded_tickers():
    """Never counted: config/exclude_tickers.txt, plus (bare mode) the not-measurable ambiguous tickers."""
    return set(read_list("exclude_tickers.txt")) | set(not_measurable())


_ctx_re_cache = {}


def context_re():
    terms = tuple(context_terms())
    if terms not in _ctx_re_cache:
        alts = [re.escape(x.strip('"')) for x in terms]
        _ctx_re_cache[terms] = re.compile(r"(?<![\w])(" + "|".join(alts) + r")(?![\w])", re.I)
    return _ctx_re_cache[terms]


def real_mention(text, t, tags):
    """A sampled post is a REAL mention of t if it carries the $t cashtag, or (bare mode) the bare ticker plus
    clear stock context (a finance-context word or another $cashtag). Context tickers (common words) must
    appear in upper case."""
    if t in tags:
        return True
    if not bare():
        return False
    flags = 0 if t in set(context_tickers()) else re.I
    if not re.search(r"(?<![\w$])" + re.escape(t) + r"(?![\w])", text, flags):
        return False
    return bool(tags) or bool(context_re().search(text))


def suffix():
    return " ".join(["-is:retweet"] + spam_terms())


def group_query(tickers):
    return "(" + " OR ".join(qtag(t) for t in tickers) + ") " + suffix()


def ticker_query(t, coll=None):
    """Individual count query: the shared spam suffix is applied to EVERY ticker (core, candidate,
    carryover); collision tickers additionally get their per-ticker terms."""
    coll = collisions() if coll is None else coll
    ctx = context_clause() if t in set(context_tickers()) else ""
    return " ".join(x for x in [qtag(t), ctx, suffix(), coll.get(t, "")] if x)


def sample_ticker_query(t, coll=None):
    return ticker_query(t, coll)


def pack(tickers, maxlen=MAX_QUERY_LEN):
    """Greedy packing of tickers (in order) into OR-groups whose full query is <= maxlen."""
    sfx_len = len(suffix())
    groups, cur, cur_len = [], [], 0
    for t in tickers:
        dl = 0 if bare() else 1
        add = len(t) + dl + (4 if cur else 0)          # "$T" (+ " OR ")
        if cur and 2 + cur_len + add + 1 + sfx_len > maxlen:   # "(" ... ")" + " " + suffix
            groups.append(cur)
            cur, cur_len = [], 0
            add = len(t) + dl
        cur.append(t)
        cur_len += add
    if cur:
        groups.append(cur)
    for g in groups:
        if len(group_query(g)) > maxlen:
            raise SystemExit("packing error: group starting %s is %d chars" % (g[0], len(group_query(g))))
    return groups


# --- persistent split tree ----------------------------------------------------
class SplitStateError(Exception):
    pass


SPLIT_RESET_HINT = ("fail-closed: the persistent split state data/splits.json is inconsistent, so it is NOT used. "
                    "Run `python3 scripts/build_rankings.py splits-reset` (no X reads; loses the refinement "
                    "carried over from earlier runs), then `plan` again, and report it.")


def megacap_list():
    """Counted individually every run: config/megacaps.txt (10), plus in bare mode the context tickers
    (they cannot be swept in a group, so they are counted every run)."""
    exclude = excluded_tickers()
    m = [t for t in read_list("megacaps.txt") if t not in exclude and CASHTAG_RE.match(t)][:MEGACAPS_N]
    return m + [t for t in context_tickers() if t not in m]


def empty_tree():
    return {"version": 1, "query_mode": query_mode(), "nodes": {}}


def validate_tree(tree):
    """Structural checks; raises SplitStateError on any inconsistency (fail closed)."""
    nodes = tree.get("nodes")
    if not isinstance(nodes, dict):
        raise SplitStateError("nodes missing")
    for nid, n in nodes.items():
        tk = n.get("tickers")
        if not tk or not isinstance(tk, list) or len(set(tk)) != len(tk):
            raise SplitStateError("%s: empty or duplicate tickers" % nid)
        par = n.get("parent")
        if par is None:
            if "." in nid:
                raise SplitStateError("%s: has no parent but a child id" % nid)
        else:
            if par not in nodes or nid not in nodes[par].get("children", []):
                raise SplitStateError("%s: parent %s missing or does not list it" % (nid, par))
            if not nid.startswith(par + "."):
                raise SplitStateError("%s: id does not extend parent id %s" % (nid, par))
        ch = n.get("children") or []
        if ch:
            if len(ch) < 2 or len(set(ch)) != len(ch):
                raise SplitStateError("%s: bad children list %s" % (nid, ch))
            union = []
            for c in ch:
                if c not in nodes:
                    raise SplitStateError("%s: child %s missing" % (nid, c))
                if nodes[c].get("parent") != nid:
                    raise SplitStateError("%s: child %s points to another parent" % (nid, c))
                union += nodes[c]["tickers"]
            if sorted(union) != sorted(tk):
                raise SplitStateError("%s: children do not partition its tickers" % nid)
    return True


def load_tree():
    tree = load_json(SPLITS_JSON, None)
    if tree is None or tree.get("query_mode", "cashtag") != query_mode():
        return empty_tree()     # counts of another query mode are not comparable: start a fresh tree
    validate_tree(tree)
    return tree


def subtree(tree, nid):
    out, stack = [], [nid]
    while stack:
        x = stack.pop()
        out.append(x)
        stack += tree["nodes"].get(x, {}).get("children", [])
    return out


def drop_children(tree, nid):
    n = tree["nodes"][nid]
    for c in list(n.get("children", [])):
        for x in subtree(tree, c):
            tree["nodes"].pop(x, None)
    n["children"] = []


def prune_tree(tree, roots, now):
    """roots: {key: (kind, tickers)} = the full current packing (all stock + all ETF groups).
    Benign changes are pruned and logged; returns a list of notes."""
    notes = []
    nodes = tree["nodes"]
    for nid in [k for k, n in nodes.items() if n.get("parent") is None]:
        if nid not in roots or roots[nid][1] != nodes[nid]["tickers"]:
            for x in subtree(tree, nid):
                nodes.pop(x, None)
            notes.append("root %s re-packed or gone: subtree dropped" % nid)
    for key, (kind, tickers) in roots.items():
        if key not in nodes:
            nodes[key] = {"tickers": list(tickers), "parent": None, "children": [], "kind": kind,
                          "count_24h": None, "measured_at": None, "window_end": None, "inferred": False}
    cut = now - timedelta(hours=NODE_MAX_AGE_HOURS)
    for nid in sorted(nodes, key=lambda x: x.count(".")):
        n = nodes.get(nid)
        if n is None or n.get("parent") is None:
            continue
        if not n.get("window_end") or parse_iso(n["window_end"]) < cut:

            drop_children(tree, n["parent"])
            notes.append("%s children dropped (not re-measured for %dh)" % (n["parent"], NODE_MAX_AGE_HOURS))

    # collapse cold splits: all children are leaves and together are quiet
    changed = True
    while changed:
        changed = False
        for nid, n in list(nodes.items()):
            ch = n.get("children") or []
            if ch and all(not nodes[c].get("children") for c in ch):
                tot = sum(nodes[c].get("count_24h") or 0 for c in ch)
                if tot < SPLIT_MIN_24H:
                    drop_children(tree, nid)
                    notes.append("%s collapsed (children only %d posts/24h)" % (nid, tot))
                    changed = True
    while len(nodes) > MAX_NODES:
        cands = [(sum(nodes[c].get("count_24h") or 0 for c in n["children"]), nid) for nid, n in nodes.items()
                 if n.get("children") and all(not nodes[c].get("children") for c in n["children"])]
        if not cands:
            break
        drop_children(tree, min(cands)[1])
        notes.append("node cap: %s collapsed" % min(cands)[1])
    validate_tree(tree)
    return notes


def split_tickers(tickers, k=4):
    n = len(tickers)
    k = min(k, n)
    out, start = [], 0
    for i in range(k):
        size = n // k + (1 if i < n % k else 0)
        out.append(tickers[start:start + size])
        start += size
    return out


def node_count_24h(run, nid):
    c = run["counts"].get(nid)
    if c and c.get("buckets") is not None:
        return sum(c["buckets"][-24:])
    return run["tree"]["nodes"][nid].get("count_24h")


def node_resid_24h(run, nid):
    """Node posts in the last 24h minus the 24h counts of members already counted individually in the last
    30h ("explained away": core tickers inside the groups would otherwise attract every drill). Used only
    to choose splits/leaves, never published."""
    c24 = node_count_24h(run, nid)
    if c24 is None:
        return None
    known = run.get("known_24h") or {}
    return max(0, c24 - sum(known.get(t, 0) for t in run["tree"]["nodes"][nid]["tickers"]))


def node_fresh(run, nid):
    c = run["counts"].get(nid)
    return bool(c) and not c.get("inferred")


def split_reads_planned(run):
    return sum(len(op["measure"]) for op in run["splits"])


def node_conc(run, nid):
    """Concentration of a split part: its share of the parent's posts x number of parts (1.0 = fair share)."""
    n = run["tree"]["nodes"][nid]
    par = n.get("parent")
    if par is None:
        return None
    pc = n.get("parent_resid_24h") or node_resid_24h(run, par)
    c = node_resid_24h(run, nid)
    if not pc or c is None:
        return None
    return c * len(run["tree"]["nodes"][par]["children"]) / pc


def choose_split(run):
    """Pick the next node to split; mutates run (adds the 4 parts and the split op). None when done.
    Heavy-hitter search in drill chains:
      1 continue: a part created in THIS run holding >= DRILL_CONC x its fair share (drill on, 3 reads)
      2 resume:   a part from an earlier run that was concentrated (4 reads: its parent count is stale)
      3 start:    a root group counted in this run and not split yet (3 reads)
      4 refresh:  a root counted in this run whose old parts are all leaves: re-split it fresh (3 reads)
    within a class the node with the most posts in the last 24h first; nodes need >= SPLIT_MIN_24H posts."""
    left = min(run["targets"]["splits"] - split_reads_planned(run), reads_remaining(run))
    nodes = run["tree"]["nodes"]
    created = {c for op in run["splits"] for c in op["children"]}
    cands = []
    for nid, n in nodes.items():
        if len(n["tickers"]) <= LEAF_MAX or nid in run["skipped"]:
            continue
        c24 = node_resid_24h(run, nid)
        if c24 is None or c24 < SPLIT_MIN_24H:
            continue
        fresh = node_fresh(run, nid)
        if n.get("parent") is None:
            if not fresh:
                continue          # roots only when counted in this run (ETF roots rotate)
            if not n.get("children"):
                cls = 3
            elif nid not in {op["node"] for op in run["splits"]} and \
                    all(not nodes[c].get("children") for c in n["children"]) and \
                    not any(c in created for c in n["children"]):
                cls = 4
            else:
                continue
        else:
            if n.get("children"):
                continue
            conc = node_conc(run, nid)
            if conc is None or conc < DRILL_CONC:
                continue
            cls = 1 if nid in created else 2
        cost = 3 if fresh else 4
        if cost <= left:
            cands.append((cls, -c24, cost, nid))
    if not cands:
        return None
    cls, _, cost, nid = min(cands)
    n = nodes[nid]
    if n.get("children"):
        drop_children(run["tree"], nid)       # refresh: old (stale, leaf-only) parts replaced
    parts = split_tickers(n["tickers"])
    ids = ["%s.%d" % (nid, i + 1) for i in range(len(parts))]
    pc = node_count_24h(run, nid)
    pr = node_resid_24h(run, nid)
    for cid, tk in zip(ids, parts):
        if cid in nodes:
            raise SplitStateError("child id %s already exists" % cid)
        nodes[cid] = {"tickers": tk, "parent": nid, "children": [], "kind": n["kind"], "count_24h": None,
                      "measured_at": None, "window_end": None, "inferred": False, "parent_24h": pc,
                      "parent_resid_24h": pr}
    n["children"] = ids
    fresh = cost == 3 and len(ids) == 4
    op = {"node": nid, "class": {1: "continue", 2: "resume", 3: "start", 4: "refresh"}[cls], "children": ids,
          "measure": ids[:3] if fresh else ids, "infer": ids[3] if fresh else None, "parent_24h": pc,
          "parent_resid_24h": pr}
    run["splits"].append(op)
    return op


def finalize_splits(run):
    """Subtraction for completed split ops: inferred child = parent - sum(3 measured), per hour, >= 0."""
    notes = []
    for op in run["splits"]:
        if not op.get("infer") or op.get("inferred_done"):
            continue
        if any(c in run["skipped"] for c in op["measure"]) or op["node"] in run["skipped"]:
            op["inferred_done"] = True
            op["infer_failed"] = "a measured part was skipped"
            continue
        if not all(c in run["counts"] for c in op["measure"]):
            continue
        pb = run["counts"][op["node"]]["buckets"]
        sums = [sum(run["counts"][c]["buckets"][i] for c in op["measure"]) for i in range(len(pb))]
        inf = [max(0, p - s) for p, s in zip(pb, sums)]
        over = sum(max(0, s - p) for p, s in zip(pb, sums))
        run["counts"][op["infer"]] = {"buckets": inf, "recorded_at": iso(utcnow()), "inferred": True}
        op["inferred_done"] = True
        op["overlap_excess"] = over
        op["parent_total"] = sum(pb)
        if sum(pb) and sum(sums) > (1 + OVERLAP_WARN) * sum(pb) and sum(inf) == 0:
            op["overlap_warning"] = True
            notes.append("%s: measured parts exceed parent by %d posts (overlap)" % (op["node"], sum(sums) - sum(pb)))
    return notes


def cmd_splits_reset(args):
    write_json(SPLITS_JSON, empty_tree())
    print("data/splits.json reset to an empty tree (refinement from earlier runs dropped; no X reads)")


# --- hourly store / daily aggregate -------------------------------------------
def hour_key(dt):
    return dt.strftime("%Y-%m-%dT%H")


def load_hourly():
    h = load_json(HOURLY_JSON, None) or {}
    h.setdefault("version", 1)
    h.setdefault("tickers", {})
    h.setdefault("nodes", {})
    h.setdefault("seen", {})
    h.setdefault("windows", [])
    if h.get("query_mode", "cashtag") != query_mode():
        # counts of another query mode ($TICKER vs bare TICKER) are not comparable: drop the per-ticker and
        # per-node series (sample evidence in "seen" is kept; it is read from post texts)
        h["mode_reset"] = {"at": iso(utcnow()), "from": h.get("query_mode", "cashtag"), "to": query_mode(),
                           "dropped_tickers": len(h["tickers"]), "dropped_nodes": len(h["nodes"])}
        h["tickers"], h["nodes"] = {}, {}
        h["query_mode"] = query_mode()
    return h


def buckets_to_hours(run, buckets):
    hs = parse_iso(run["history_start"])
    return {hour_key(hs + timedelta(hours=i)): v for i, v in enumerate(buckets)}


def day_of(window_end_iso):
    return (parse_iso(window_end_iso) - timedelta(minutes=1)).strftime("%Y-%m-%d")


def ticker_nodes_index(hourly):
    idx = {}
    for nid, n in hourly["nodes"].items():
        if n.get("inferred"):
            continue        # an inferred (subtracted) count is not an upper bound
        for t in n["tickers"]:
            idx.setdefault(t, []).append(nid)
    return idx


def daily_entries(hourly, day, through, universe, core, mega):
    """Aggregate for one UTC day over the hours [day 00:00, through)."""
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    hours = []
    h = d0
    while h < min(through, d0 + timedelta(days=1)):
        hours.append(hour_key(h))
        h += timedelta(hours=1)
    idx = ticker_nodes_index(hourly)
    seen = hourly["seen"].get(day, {})
    names = {t for t, v in seen.items() if v.get("authors")}      # sampled with >= 1 clean post
    for t, rec in hourly["tickers"].items():
        if any(k in rec["h"] for k in hours):
            names.add(t)
    out = []
    windows = [w for w in hourly["windows"] if day_of(w) == day]
    for t in sorted(names):
        rec = hourly["tickers"].get(t, {"h": {}})
        exact = {k: rec["h"][k] for k in hours if k in rec["h"]}
        missing = [k for k in hours if k not in exact]
        bound = sum(exact.values())
        for k in missing:
            vals = [hourly["nodes"][nid]["h"][k] for nid in idx.get(t, []) if k in hourly["nodes"][nid]["h"]]
            if not vals:
                bound = None
                break
            bound += min(vals)
        sv = seen.get(t, {})
        tier = "megacap" if t in mega else ("core" if t in core else "other")
        e = {"ticker": t, "cashtag": "$" + t, "tier": tier,
             "asset_type": (universe.get(t) or {}).get("asset_type"),
             "status": ("exact" if exact and not missing else "partial" if exact else
                        "bounded" if bound is not None else "seen"),
             "count": sum(exact.values()) if exact and not missing else None,
             "exact_part": sum(exact.values()) if exact else None,
             "exact_hours": len(exact), "day_hours": len(hours),
             "upper_bound": bound,
             "first_seen": sv.get("first_seen") or rec.get("first_measured"),
             "last_measured": rec.get("last_measured"),
             "sources": sorted(set(sv.get("sources", [])) | set(rec.get("sources", [])))}
        if sv.get("total"):
            e["sample_posts"] = sv["total"]
            e["sample_clean_authors"] = len(sv.get("authors", []))
            e["sample_clean_share"] = round(sv.get("clean", 0) / sv["total"], 2)
        if tier == "other":
            a = {"authors": set(sv.get("authors", [])), "total": sv.get("total", 0),
                 "share": (sv.get("clean", 0) / sv["total"]) if sv.get("total") else 0.0}
            e["held_back"] = not clean_ok(a)
            if e["held_back"]:
                e["held_back_reason"] = ("no clean sample evidence today" if not sv.get("total") else
                                         "sample today: %d posts, %.0f%% clean, %d clean authors"
                                         % (a["total"], 100 * a["share"], len(a["authors"])))
        else:
            e["held_back"] = False
        if t in _COLLISION_HELD:
            e["held_back"] = True
            e["held_back_reason"] = _COLLISION_HELD[t]
        wins = {}
        for w in windows:
            we = parse_iso(w)
            ks = [hour_key(we - timedelta(hours=i)) for i in range(WINDOW_HOURS, 0, -1)]
            if all(k in rec["h"] for k in ks):
                wins[w] = sum(rec["h"][k] for k in ks)
        if wins:
            e["windows_5h"] = wins
        out.append(e)
    out.sort(key=lambda e: (-(e["count"] if e["count"] is not None else
                              (e["exact_part"] or 0) if e["status"] == "partial" else -1),
                            -(e["upper_bound"] or 0), e["ticker"]))
    return out, hours


def collision_guard_rule():
    return {"label": "suspected-word-collision",
            "rule": ("bare mode: a ticker is held back when its bare 5h count is > %gx the median of its $cashtag 5h "
                     "counts before %s (only for bare counts >= %d), or when it has no $cashtag history and the bare "
                     "count is > %d; it is queued for an own recency sample in the next run and ranked (with the "
                     "cleaned estimate, label collision-check-passed) once >= %d own sampled posts in 72h show >= "
                     "%.0f%% real mentions" % (COLLISION_RATIO, BARE_SINCE, COLLISION_RATIO_MIN_BARE,
                                               COLLISION_NO_HISTORY_MIN, COLLISION_MIN_OWN_POSTS,
                                               100 * COLLISION_MIN_REAL_SHARE)),
            "ratio": COLLISION_RATIO, "ratio_min_bare": COLLISION_RATIO_MIN_BARE,
            "no_history_min": COLLISION_NO_HISTORY_MIN, "min_real_share": COLLISION_MIN_REAL_SHARE,
            "min_own_posts": COLLISION_MIN_OWN_POSTS}


def method_note():
    """Human-readable method/version note for latest.json, today.json and daily files."""
    info = query_mode_info()
    if bare():
        return {"method_version": METHOD_VERSION, "query_mode": "bare",
                "counting_changed": ("On %s counting changed from the $TICKER cashtag to the BARE ticker (TICKER "
                                     "without $), because the free x tools now strip '$' from queries ('%s'). "
                                     "Bare counts include plain-text mentions (and company-name mentions where the "
                                     "ticker is the name), so they are NOT directly comparable with counts before "
                                     "%s. Ranking uses mentions_clean_est = bare count x the share of sampled posts "
                                     "that are real stock mentions ($cashtag or ticker + stock context)."
                                     % (BARE_SINCE, DOLLAR_NOTE, BARE_SINCE)),
                "mode_since": info.get("since"),
                "auto_switch_back": ("every bare run starts with a probe call '%s'; if the tool no longer reports "
                                     "that the $ was removed, the script switches back to cashtag queries" % PROBE_QUERY)}
    return {"method_version": METHOD_VERSION, "query_mode": "cashtag",
            "history": info.get("history", [])}


def not_measurable_block(universe):
    nm = not_measurable()
    if not nm:
        return None
    inu = sorted(t for t in nm if t in universe)
    short = [t for t in inu if len(t) <= 2]
    words = [t for t in inu if len(t) > 2]
    return {"count": len(inu), "one_or_two_letters": len(short), "words": len(words),
            "examples": [t for t in ("A", "ALL", "NOW", "ON", "IT", "ARE", "CAN", "FUN", "GO", "CAR", "SO", "OPEN",
                                     "AI", "NET", "APP", "DIA", "BE", "BA") if t in nm],
            "file": "config/ambiguous_tickers.txt",
            "rule": ("bare ticker is a common word (wordfreq zipf >= 3.0 in English or >= 4.0 in de/fr/es/it/pt/nl), "
                     "1-2 letters, or curated slang/acronym; these cannot be counted honestly without the $ and are "
                     "left out of the sweep and the ranking"),
            "context_tickers": context_tickers(),
            "context_rule": ("ambiguous core names counted only together with a finance-context word: "
                             "TICKER %s; label context-required" % context_clause())}


def write_daily(hourly, day, through, universe, core, mega, path, now):
    entries, hours = daily_entries(hourly, day, through, universe, core, mega)
    stat = {}
    for e in entries:
        stat[e["status"]] = stat.get(e["status"], 0) + 1
    write_json(path, {
        "generated_at": iso(now),
        "day": day,
        "day_definition": ("UTC calendar day %s, hours 00:00 up to %s UTC (%d of 24 hours so far)"
                           % (day, iso(min(through, datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                                               + timedelta(days=1))), len(hours))),
        "method": METHOD,
        "method_note": method_note(),
        "status_counts": stat,
        "tickers_seen": len(entries),
        "talked_about_today": sum(1 for e in entries if not e["held_back"] and
                                  ((e["count"] or 0) > 0 or (e["exact_part"] or 0) > 0)),
        "fields": {
            "status": ("exact = every hour of the day so far measured individually (count is exact); partial = "
                       "some hours exact (exact_part), the rest only bounded; bounded = only an upper bound from "
                       "the smallest measured group/split node containing the ticker; seen = mentioned in a "
                       "sample, no count"),
            "upper_bound": ("exact hours + for every other hour the minimum count of a measured (not subtracted) "
                            "group/node that contains the ticker; null if some hour has no such node"),
            "windows_5h": "exact 5h window counts back-filled from hourly buckets (keyed by window end)",
            "held_back": "non-core ticker without clean sample evidence today (>= 3 clean authors, >= 30% clean)",
        },
        "tickers": entries,
    })
    return entries


def update_hourly(run, hourly, agg, now):
    """Store every measured ticker/node series (hourly), the run window, and today's sample evidence."""
    for key, c in run["counts"].items():
        if key == "PROBE":
            continue
        hrs = buckets_to_hours(run, c["buckets"])
        if key in run["tree"]["nodes"]:
            hourly["nodes"][key] = {"tickers": run["tree"]["nodes"][key]["tickers"], "h": hrs,
                                    "inferred": bool(c.get("inferred")), "window_end": run["window_end"]}
        else:
            rec = hourly["tickers"].setdefault(key, {"h": {}, "sources": []})
            rec["h"].update(hrs)
            rec.setdefault("first_measured", c["recorded_at"])
            rec["last_measured"] = c["recorded_at"]
            rec["last_window_end"] = run["window_end"]
            rec["last_5h"] = sum(c["buckets"][-WINDOW_HOURS:])
            src = (run.get("count_sources") or {}).get(key)
            if src and src not in rec["sources"]:
                rec["sources"].append(src)
    if run["window_end"] not in hourly["windows"]:
        hourly["windows"].append(run["window_end"])
    day = day_of(run["window_end"])
    seen = hourly["seen"].setdefault(day, {})
    for t, a in agg.items():
        sv = seen.setdefault(t, {"first_seen": run["window_end"], "sources": ["sample"], "authors": [],
                                 "clean": 0, "total": 0})
        sv["authors"] = sorted(set(sv["authors"]) | set(a["authors"]))
        sv["clean"] += a["posts"]
        sv["total"] += a["total"]
    # keep two UTC days
    keep = (parse_iso(run["window_end"]) - timedelta(hours=1)).replace(hour=0) - timedelta(days=1)
    kk = hour_key(keep)
    for rec in hourly["tickers"].values():
        rec["h"] = {k: v for k, v in rec["h"].items() if k >= kk}
    hourly["tickers"] = {t: r for t, r in hourly["tickers"].items() if r["h"]}
    cut = parse_iso(run["window_end"]) - timedelta(hours=NODE_MAX_AGE_HOURS)
    hourly["nodes"] = {k: n for k, n in hourly["nodes"].items() if parse_iso(n["window_end"]) >= cut}
    hourly["seen"] = {d: v for d, v in hourly["seen"].items() if d >= keep.strftime("%Y-%m-%d")}
    hourly["windows"] = [w for w in hourly["windows"] if parse_iso(w) >= keep]



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


def heat(m, r5=1.0, r1=1.0):
    """Unusual activity of a group: hourly excess over its own baseline (both views), where the
    baseline is scaled by the typical ratio of ALL groups of the same kind in this run (r5, r1).
    Without that scaling every group scored 0.0 whenever the whole market was quieter than
    yesterday / than the previous hours (e.g. Saturday evening vs Friday evening: 2026-10-10 23:41Z
    and 2026-10-11 04:39Z had 0.0 for all 15 groups), so sample groups were picked by tie-break."""
    e1 = max(0.0, m["last_hour"] - r1 * m["prev_9h_avg"])
    e5 = max(0.0, (m["mentions"] - r5 * (m["prev_day_same_hours"] or 0)) / WINDOW_HOURS)
    return round(e1 + e5, 1)


def market_ratios(ms):
    """Median 5h-vs-yesterday and last-hour-vs-9h ratios over a set of group metrics."""
    r5 = [m["mentions"] / m["prev_day_same_hours"] for m in ms if m.get("prev_day_same_hours")]
    r1 = [m["last_hour"] / m["prev_9h_avg"] for m in ms if m.get("prev_9h_avg")]
    return (statistics.median(r5) if r5 else 1.0), (statistics.median(r1) if r1 else 1.0)


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
    exclude = excluded_tickers()
    return [t for t in read_list("core_tickers.txt") if t not in exclude and CASHTAG_RE.match(t)]


def core_priority(state):
    """Core tickers, strongest last measurement first (unmeasured keep config order)."""
    core = core_list()
    st = state["tickers"]
    return sorted(core, key=lambda t: (-(st.get(t, {}).get("mentions", -1)), core.index(t)))


def window_for(now):
    end = now.replace(minute=0, second=0, microsecond=0)
    return end - timedelta(hours=WINDOW_HOURS), end




# --- run file ----------------------------------------------------------------
def load_run():
    run = load_json(RUN_JSON)
    if not run or run.get("method_version") != METHOD_VERSION:
        sys.exit("work/run.json missing or from the old method; run: python3 scripts/build_rankings.py plan")
    log_defaults(run)
    return run


def save_run(run):
    write_json(RUN_JSON, run)


def reconcile_reads(run, n):
    """Retry tally from the X counter: if reads-left fell by more than the reads the run has recorded since
    the last report, the gap was spent on calls nobody recorded (typically a retry after a 429 without
    record-error). It is added to extra_reads (so the retry reserve stays honest) and logged."""
    if n is None:
        return 0
    lg = run["log"]
    mark = lg.get("left_mark")
    gap = 0
    if mark:
        gap = (mark[0] - n) - (reads_used(run) - mark[1])
        if gap > 0:
            lg["extra_reads"] += gap
            lg["unrecorded_reads"] = lg.get("unrecorded_reads", 0) + gap
            lg.setdefault("unrecorded_log", []).append([iso(utcnow()), gap])
            print("WARNING: reads left fell by %d more than the recorded calls; counted as %d unrecorded "
                  "retry/extra read(s). Always run record-error for a failed call before retrying it." % (gap, gap))
    if not mark or n <= mark[0]:
        lg["left_mark"] = [n, reads_used(run)]
    return max(0, gap)


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
    """X reads made for planned calls (subtracted split parts are computed, not read)."""
    return sum(1 for c in run["counts"].values() if not c.get("inferred")) + len(run["samples"])


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
def size_run(budget, n_stock_groups, n_etf_groups_avail, n_mega=MEGACAPS_N, probe=0):
    """Split a read budget into steps. Defaults: stock groups (all), megacaps 10, ETF groups 3, splits 24,
    samples 10, spare 4, daily counts = the rest (<= 35). Shrink order when the budget is smaller:
    daily counts (to 10), splits (to 9), ETF groups (to 1), samples (to 4), spare (to 1), daily counts,
    splits, samples, ETF groups (to 0), megacaps (to 3), spare, megacaps (to 0)."""
    t = {"probe": probe, "stock_groups": n_stock_groups, "megacaps": n_mega,
         "etf_groups": min(ETF_GROUPS_PER_RUN, n_etf_groups_avail), "splits": SPLIT_READS,
         "samples": SAMPLE_GROUPS + COMENTION_SAMPLES + OWN_SAMPLES, "spare": SPARE, "dailies": 0}
    t["dailies"] = max(10, min(DAILY_COUNTS_MAX, budget - sum(t.values())))
    over = lambda: sum(t.values()) - budget
    for key, floor in (("dailies", 10), ("splits", 9), ("etf_groups", 1), ("samples", 4), ("spare", 1),
                       ("dailies", 0), ("splits", 0), ("samples", 0), ("etf_groups", 0), ("megacaps", 3),
                       ("spare", 0), ("megacaps", 0)):
        if over() > 0:
            t[key] = max(floor, t[key] - over())
    return t, over() <= 0


def current_packing(universe):
    exclude = excluded_tickers() | set(context_tickers())    # context tickers: individual counts only
    mega = set(megacap_list())
    stock = sorted(t for t, r in universe.items() if r["asset_type"] == "Stock" and t not in exclude and t not in mega)
    etf = sorted(t for t, r in universe.items() if r["asset_type"] == "ETF" and t not in exclude and t not in mega)
    return pack(stock), pack(etf), stock, etf


def cmd_plan(args):
    now = parse_iso(args.now) if args.now else utcnow()
    start, end = window_for(now)
    hist_start = end - timedelta(hours=HISTORY_HOURS)
    universe = load_universe()
    state = load_state()
    budget = RUN_READS
    if args.reads_left_today is not None:
        budget = min(RUN_READS, args.reads_left_today - FLOOR_LEFT - RETRY_RESERVE)
        if budget < MIN_RUN_READS:
            print("SKIP: only %d reads fit under the daily cap (reads left %d, floor %d); do not run."
                  % (budget, args.reads_left_today, FLOOR_LEFT))
            sys.exit(3)
    mega = megacap_list()
    core = [t for t in core_priority(state) if t not in mega]
    stock_groups, etf_groups, stock_t, etf_t = current_packing(universe)
    probe = 1 if bare() else 0
    targets, ok = size_run(budget, len(stock_groups), len(etf_groups), len(mega), probe)
    if not ok:
        print("SKIP: the stock sweep alone (%d reads) does not fit the budget of %d." % (len(stock_groups), budget))
        sys.exit(3)
    roots = {}
    for i, g in enumerate(stock_groups):
        roots["S%02d" % (i + 1)] = ("stock", g)
    for i, g in enumerate(etf_groups):
        roots["E%02d" % (i + 1)] = ("etf", g)
    try:
        tree = load_tree()
        prune_notes = prune_tree(tree, roots, end)
    except SplitStateError as e:
        print("ERROR: split state inconsistent (%s). %s" % (e, SPLIT_RESET_HINT))
        sys.exit(4)
    hourly = load_hourly()
    known = {}
    for t, rec in hourly["tickers"].items():
        if rec.get("last_window_end") and parse_iso(rec["last_window_end"]) >= end - timedelta(hours=NODE_MAX_AGE_HOURS):
            le = parse_iso(rec["last_window_end"])
            known[t] = sum(rec["h"].get(hour_key(le - timedelta(hours=i)), 0) for i in range(1, 25))
    n = len(etf_groups)
    first = state["etf_rotation_next"] % n if n else 0
    requeue = [int(k[1:]) - 1 for k in state.get("etf_requeue", []) if re.fullmatch(r"E\d+", k) and int(k[1:]) <= n]
    requeue = requeue[:targets["etf_groups"]]
    etf_rotated = []
    i = 0
    while n and len(requeue) + len(etf_rotated) < min(targets["etf_groups"], n) and i < n:
        idx = (first + i) % n
        if idx not in requeue:
            etf_rotated.append(idx)
        i += 1
    etf_pick = requeue + etf_rotated
    groups = []
    for i, g in enumerate(stock_groups):
        groups.append({"key": "S%02d" % (i + 1), "kind": "stock", "tickers": g,
                       "demoted_core": [t for t in g if t in set(core)]})
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
        "query_mode": query_mode(),
        "probe": bool(probe),
        "planned_at": iso(now),
        "window_start": iso(start),
        "window_end": iso(end),
        "history_start": iso(hist_start),
        "budget": budget,
        "targets": targets,
        "megacaps": mega[:targets["megacaps"]],
        "core_order": core,
        "core_individual_planned": mega[:targets["megacaps"]],
        "etf_groups_total": n,
        "etf_tickers_total": len(etf_t),
        "etf_rotation_first": first,
        "stock_tickers_swept": len(stock_t),
        "groups": groups,
        "tree": tree,
        "tree_prune_notes": prune_notes,
        "known_24h": known,
        "splits": [],
        "splits_done": False,
        "counts": {},
        "count_sources": {t: "megacap" for t in mega[:targets["megacaps"]]},
        "samples": {},
        "picks": {"samples1": None, "samples2": None, "individual": None},
        "log": {"reads_left_today": args.reads_left_today, "reads_left_history": [], "extra_reads": 0,
                "retries_429": 0, "errors_other": 0, "attempts": {}},
        "skipped": [],
        "stopped": None,
        "etf_requeued": ["E%02d" % (i + 1) for i in requeue],
        "etf_rotated": ["E%02d" % (i + 1) for i in etf_rotated],
    }
    if args.reads_left_today is not None:
        run["log"]["reads_left_history"].append([iso(now), args.reads_left_today])
        run["log"]["left_mark"] = [args.reads_left_today, 0]
    save_run(run)
    print("window %s -> %s UTC (29h buckets from %s); budget %d reads" % (run["window_start"], run["window_end"],
                                                                          run["history_start"], budget))
    print("targets:", json.dumps(targets), "total", sum(targets.values()))
    print("groups: %d stock (%d tickers incl. %d core) + ETF %s of %d; megacaps: %s"
          % (len(stock_groups), len(stock_t), len(core), ",".join(g["key"] for g in groups if g["kind"] == "etf")
             or "-", n, " ".join(run["megacaps"])))
    print("split state: %d nodes carried over%s" % (len(tree["nodes"]),
                                                     ("; " + "; ".join(prune_notes[:6])) if prune_notes else ""))
    print("next: python3 scripts/build_rankings.py next-call   (ONE X call at a time; see PACING)")


# --- calls (exact tool arguments) ---------------------------------------------
def count_args(run, query):
    return {"query": query, "granularity": "hour", "start_time": run["history_start"], "end_time": run["window_end"]}


def sample_query(tickers):
    """Group query for a sample, trimmed (members dropped from the end) to <= SAMPLE_MAX_QUERY_LEN."""
    tk = list(tickers)
    q = group_query(tk) if len(tk) > 1 else ticker_query(tk[0])
    while len(q) > SAMPLE_MAX_QUERY_LEN and len(tk) > 1:
        tk = tk[:-1]
        q = group_query(tk)
    return q


def sample_args(run, query, sort="relevancy"):
    if len(query) > SAMPLE_MAX_QUERY_LEN:
        raise ValueError("sample query of %d chars exceeds %d" % (len(query), SAMPLE_MAX_QUERY_LEN))
    return {"query": query, "sort_order": sort, "max_results": SAMPLE_MAX_RESULTS,
            "start_time": run["window_start"], "end_time": run["window_end"],
            "post.fields": "author_id,created_at", "expansions": "author_id", "user.fields": "username"}


STAGES = ("probe", "groups", "megacaps", "splits", "samples", "counts")
PROBE_QUERY = "$NVDA -is:retweet"   # bare mode: first call of every run; is the $ still stripped?


def pending_calls(run, stage):
    out = []
    skipped = set(run.get("skipped") or [])
    if stage == "probe":
        if run.get("probe") and "PROBE" not in run["counts"] and "PROBE" not in skipped:
            out.append(("PROBE", "get_posts_counts_recent", count_args(run, PROBE_QUERY)))
    elif stage == "groups":
        for g in run["groups"]:
            if g["key"] not in run["counts"] and g["key"] not in skipped:
                out.append((g["key"], "get_posts_counts_recent", count_args(run, g["query"])))
    elif stage == "megacaps":
        coll = collisions()
        for t in run["megacaps"]:
            if t not in run["counts"] and t not in skipped:
                out.append((t, "get_posts_counts_recent", count_args(run, ticker_query(t, coll))))
    elif stage == "splits":
        for op in run["splits"]:
            for cid in op["measure"]:
                if cid not in run["counts"] and cid not in skipped:
                    out.append((cid, "get_posts_counts_recent",
                                count_args(run, group_query(run["tree"]["nodes"][cid]["tickers"]))))
    elif stage == "samples":
        for p in (run["picks"]["samples1"] or []) + (run["picks"]["samples2"] or []):
            if p["key"] not in run["samples"] and p["key"] not in skipped:
                out.append((p["key"], "search_posts_all", sample_args(run, p["query"], p.get("sort", "relevancy"))))
    elif stage == "counts":
        for p in run["picks"]["individual"] or []:
            if p["ticker"] not in run["counts"] and p["ticker"] not in skipped:
                out.append((p["ticker"], "get_posts_counts_recent", count_args(run, p["query"])))
    return out


def any_pending(run):
    return any(pending_calls(run, st) for st in STAGES)


# --- calls (exact tool arguments) ---------------------------------------------
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
    """(kind, payload): kind in stop/call/pick-samples/pick-candidates/build. May create the next split
    (mutates run; callers save)."""
    if run.get("stopped"):
        return "stop", run["stopped"]
    for stage in ("probe", "groups", "megacaps"):
        calls = pending_calls(run, stage)
        if calls:
            return "call", (stage, calls)
    if not run.get("splits_done"):
        calls = pending_calls(run, "splits")
        if not calls:
            finalize_splits(run)
            op = choose_split(run)
            if op is None:
                run["splits_done"] = True
            else:
                calls = pending_calls(run, "splits")
        if calls:
            return "call", ("splits", calls)
    if run["picks"]["samples1"] is None:
        return "pick-samples", "stage 1"
    calls = pending_calls(run, "samples")
    if calls:
        return "call", ("samples", calls)
    if run["picks"]["samples2"] is None:
        return "pick-samples", "stage 2"
    if run["picks"]["individual"] is None:
        return "pick-candidates", None
    calls = pending_calls(run, "counts")
    if calls:
        return "call", ("counts", calls)
    return "build", None


def record_hint(stage, key):
    if stage == "samples":
        return ("python3 scripts/build_rankings.py record-sample %s --lines work/samples/%s.txt "
                "--reads-left-today <B>" % (key, key))
    if stage == "probe":
        return ("python3 scripts/build_rankings.py record PROBE=<TOTAL>:<29 buckets> --dollar-removed yes|no "
                "--reads-left-today <B>   (yes = the tool output contains '%s')" % DOLLAR_NOTE)
    return "python3 scripts/build_rankings.py record %s=<TOTAL>:<29 buckets> --reads-left-today <B>" % key


def cmd_next_call(args):
    run = load_run()
    try:
        kind, payload = next_action(run)
    except SplitStateError as e:
        save_run(run)
        sys.exit("ERROR: split state inconsistent (%s). Do not hand-build a split; build now if the script "
                 "allows a partial run, else abandon, and report." % e)
    save_run(run)
    if kind != "call":
        cmd_next(args)
        return
    stage, calls = payload
    k, tool, a = calls[0]
    print_call(k, tool, a)
    if stage == "splits":
        node = run["tree"]["nodes"][k]
        print("# split part %s of %s: %d tickers %s..%s (query generated by the script; copy verbatim)"
              % (k, node["parent"], len(node["tickers"]), node["tickers"][0], node["tickers"][-1]))
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
    elif int(lg.get("extra_reads", 0)) >= RETRY_RESERVE and any_pending(run):
        msg = "retry reserve of %d reads used up" % RETRY_RESERVE
    elif left is not None and left <= FLOOR_LEFT:
        msg = "reads left today at/below %d (daily cap)" % FLOOR_LEFT
    if msg and not run.get("stopped"):
        run["stopped"] = msg
    return msg


def partial_ok(run):
    """A run that did not finish may still be built (labelled partial) if every STOCK group and all megacap
    counts are done. Missing ETF groups, split parts and samples are recorded as missing (ETF groups are
    queued for the next run)."""
    miss = [g["key"] for g in run["groups"] if g["kind"] == "stock" and g["key"] not in run["counts"]]
    if miss:
        return False, "stock group sweep incomplete: %s" % ",".join(miss)
    mmiss = [t for t in run["megacaps"] if t not in run["counts"]]
    if mmiss:
        return False, "megacaps not fully counted (%d missing: %s)" % (len(mmiss), ",".join(mmiss))
    return True, None


def missing_calls(run):
    """Planned calls that were not measured, by kind (for honest notes in the outputs)."""
    sk = set(run.get("skipped") or [])
    out = {"etf_groups": [g["key"] for g in run["groups"] if g["kind"] == "etf" and g["key"] not in run["counts"]],
           "split_parts": [c for op in run["splits"] for c in op["measure"] if c not in run["counts"]],
           "samples": [p["key"] for p in (run["picks"]["samples1"] or []) + (run["picks"]["samples2"] or [])
                       if p["key"] not in run["samples"]],
           "individual_counts": [p["ticker"] for p in (run["picks"]["individual"] or [])
                                 if p["ticker"] not in run["counts"]],
           "skipped_after_errors": sorted(sk)}
    return {k: v for k, v in out.items() if v}


def run_complete(run):
    if run.get("skipped") or run.get("stopped"):
        return False
    if run["picks"].get("individual") is None or not run.get("splits_done"):
        return False
    return not any_pending(run)


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
    known = {g["key"] for g in run["groups"]} | set(run["megacaps"]) | ({"PROBE"} if run.get("probe") else set())
    known |= {c for op in run["splits"] for c in op["measure"]}
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
        if key in run["counts"]:
            print("ERROR: %s is already recorded" % key)
            errors += 1
            continue
        if key not in known:
            print("ERROR: %s is not a planned group or picked ticker (run pick-candidates first?)" % key)
            errors += 1
            continue
        if key == "PROBE" and args.dollar_removed is None:
            print("ERROR: PROBE needs --dollar-removed yes|no (yes = the x tool output says '%s')" % DOLLAR_NOTE)
            errors += 1
            continue
        run["counts"][key] = {"buckets": buckets, "recorded_at": iso(utcnow())}
    switched = mode_from_tool_note(run, args)
    for note in finalize_splits(run):
        print("WARNING:", note)
    if args.extra_reads:
        run["log"]["extra_reads"] += args.extra_reads
        run["log"]["errors_other"] += args.extra_reads
    reconcile_reads(run, args.reads_left_today)
    note_reads_left(run, args.reads_left_today)
    msg = stop_checks(run)
    save_run(run)
    print("recorded %d, errors %d; run reads used %d (planned %d + retries/errors %d), remaining %d planned "
          "(+%d retry reserve); reads left today %s"
          % (len(args.pairs) - errors, errors, reads_used(run), planned_used(run), run["log"]["extra_reads"],
             reads_remaining(run), retry_left(run), run["log"]["reads_left_today"]))
    left = run["log"]["reads_left_today"]
    if switched:
        print(switched)
    if left is not None and planned_used(run) <= 2 and left - FLOOR_LEFT - RETRY_RESERVE < MIN_RUN_READS:
        print("SKIP RUN: only %d reads fit under the daily cap (after the %d-read retry reserve); stop now, "
              "run `abandon --reason skip`, do not build or commit." % (left - FLOOR_LEFT - RETRY_RESERVE, RETRY_RESERVE))
    elif msg:
        print("STOP: %s. %s" % (msg, stop_advice(run)))
    elif reads_remaining(run) == 0 and any_pending(run):
        print("STOP: run budget exhausted. " + stop_advice(run))
    if errors:
        sys.exit(1)
    if args.pairs:
        pace(args)


def mode_from_tool_note(run, args):
    """Automatic query-mode switch from the x tools' '$ was removed' note (--dollar-removed on record/
    record-error): bare mode + 'no' -> $ works again -> cashtag; cashtag mode + 'yes' -> bare. The current
    run is stopped (its queries are of the old mode); the runner abandons it and plans again at once."""
    d = getattr(args, "dollar_removed", None)
    if d is None:
        return None
    mode = run.get("query_mode", "cashtag")
    if mode == "bare" and d == "no":
        set_query_mode("cashtag", "probe query %r answered without the '%s' note: cashtag queries work again"
                       % (PROBE_QUERY, DOLLAR_NOTE))
        new = "cashtag"
    elif mode == "cashtag" and d == "yes":
        set_query_mode("bare", "x tool stripped the $ ('%s')" % DOLLAR_NOTE)
        new = "bare"
    else:
        return None
    run["stopped"] = "query mode switched %s -> %s (tool note); this run's queries are stale" % (mode, new)
    return ("MODE SWITCH: %s -> %s written to data/query_mode.json. Do not build this run: "
            "`python3 scripts/build_rankings.py abandon --reason 'query mode switched to %s'`, then run `plan` "
            "again right away (same reads-left) and continue with next-call." % (mode, new, new))


# --- samples -----------------------------------------------------------------
def group_metrics(run):
    out = []
    for g in run["groups"]:
        c = run["counts"].get(g["key"])
        if not c:
            continue
        m = metrics(c["buckets"])
        out.append((g, m))
    for kind in {g["kind"] for g, _ in out}:
        ms = [m for g, m in out if g["kind"] == kind]
        r5, r1 = market_ratios(ms)
        for m in ms:
            m["heat"] = heat(m, r5, r1)
            m["market_ratio_5h"], m["market_ratio_1h"] = round(r5, 3), round(r1, 3)
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


# Crypto/memecoin context, matched locally on sampled posts only (costs no query characters).
# Deliberately memecoin-style words, not bitcoin/btc/eth (normal for $MSTR $COIN $IREN), not 'meme' ($GME
# 'meme stock') and 'NNx' only as a call ('20x profit', '185x up from'), not '30x earnings'.
CRYPTO_PAIR_RE = re.compile(r"\$?[A-Za-z]{2,6}\s*/\s*\$?(usdt|usdc|usd|btc|eth)\b|\b(altcoins?|blockchains?|the coin|this coin|"
                            r"chain[- ]agnostic|on[- ]chain|defi|web3|stablecoins?)\b", re.I)
CRYPTO_CONTEXT_RE = re.compile(
    r"(?<![\w$])(cardano|solana|memecoins?|meme ?coins?|pump ?fun|pumpswap|aped?|degens?|gems?|"
    r"\d{1,5}(?:\.\d+)?x (?:profit|gains?|up|from|on|run|pump|move)|moon(?:ing)?|wagmi|ngmi|lore|cto|dex|presale|onchain|on-chain|airdrop|new coins?|"
    r"contract address|bonding curve)(?![\w])", re.I)
CRYPTO_ADDR_RE = re.compile(r"(?<![\w])(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})(?![\w])")
CRYPTO_HANDLE_RE = re.compile(r"@\w*(sol|gem|crypto|coin|1000x|100x|meme|pump|degen|hodl|airdrop)\w*", re.I)
MIN_REAL_WORDS = 3           # posts with fewer real words ("$hodl gang", "20x on $TM") are not clean context


def classify_post(text, author, bad_accounts, templates, patterns):
    """-> (class, cashtags). Classes: spam_account, template, dump, spam_term, crypto, short, clean."""
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
    ctags = [t for t in tags if t in CRYPTO_TAGS]
    if len(ctags) >= 2 or any(t in ("USDT", "USDC") for t in ctags) or CRYPTO_PAIR_RE.search(text):
        return "crypto", tags
    if CRYPTO_ADDR_RE.search(re.sub(r"https?://\S+", " ", text)) or CRYPTO_HANDLE_RE.search(text) or \
            CRYPTO_CONTEXT_RE.search(re.sub(r"https?://\S+|[@#]\w+", " ", text)):
        return "crypto", tags
    if real_words(text) < MIN_REAL_WORDS:
        return "short", tags
    return "clean", tags


def clean_ok(a):
    """Sample evidence that a non-core ticker is real stock chatter."""
    return len(a["authors"]) >= MIN_AUTHORS and (a["total"] < CLEAN_SHARE_MIN_POSTS or a["share"] >= MIN_CLEAN_SHARE)


def analyze_samples(run, keys=None):
    """Per ticker over all samples of the run: {authors: set of authors of CLEAN posts, posts: clean posts,
    total: all sampled posts mentioning it, share: clean/total, via: sample keys} + per-sample stats."""
    universe = load_universe()
    exclude = excluded_tickers()
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
                                    "dump": 0, "spam_term": 0, "crypto": 0, "short": 0})
        st["posts"] += 1
        cls, tags = classify_post(p["text"], p["author"], bad_accounts, templates, patterns)
        if cls == "clean":
            st["kept"] += 1
        else:
            st[cls] += 1
        if key.startswith("smp-o-"):
            # own sample: EVERY returned post matched the ticker's count query, so all of them form the
            # denominator; real = clean and ($cashtag, or in bare mode the bare ticker with stock context)
            ot = key[len("smp-o-"):]
            if ot not in exclude and ot in universe:
                a = agg.setdefault(ot, {"authors": set(), "posts": 0, "total": 0, "via": set(), "crypto": 0,
                                        "own_total": 0, "own_clean": 0})
                a["own_total"] += 1
                real = cls == "clean" and real_mention(p["text"], ot, tags)
                a["own_clean"] += real
                st["own_real"] = st.get("own_real", 0) + real
        for t in tags:
            if t in exclude or t not in universe:
                continue
            a = agg.setdefault(t, {"authors": set(), "posts": 0, "total": 0, "via": set(), "crypto": 0,
                                   "own_total": 0, "own_clean": 0})
            a["total"] += 1
            if cls == "crypto":
                a["crypto"] += 1
            if cls == "clean":
                a["authors"].add(p["author"].lower())
                a["posts"] += 1
                a["via"].add(key)
    for a in agg.values():
        a["share"] = round(a["posts"] / a["total"], 2) if a["total"] else 0.0
    return agg, stats


def default_prior():
    return OWN_CLEAN_DEFAULT_PRIOR_BARE if bare() else OWN_CLEAN_DEFAULT_PRIOR


def own_mode_ok(state):
    return state.get("own_clean_mode", "cashtag") == query_mode()


_CASHTAG_BASE = None
_COLLISION_HELD = {}   # ticker -> reason, set by build for daily/today files


def cashtag_baseline():
    """ticker -> (median $cashtag 5h count, n windows) from history/ and daily/ files before the bare switch."""
    global _CASHTAG_BASE
    if _CASHTAG_BASE is not None:
        return _CASHTAG_BASE
    wins = {}
    for f in sorted(glob.glob(os.path.join(HISTORY_DIR, "*.json"))):
        if os.path.basename(f) >= BARE_SINCE:
            continue
        d = load_json(f, {}) or {}
        if (d.get("method_version") or 0) >= 4 or d.get("query_mode") == "bare":
            continue
        for e in (d.get("rankings") or []) + (d.get("held_back") or []):
            if e.get("mentions") is not None:
                wins.setdefault(e["ticker"], {})[d.get("window_end")] = e["mentions"]
    for f in sorted(glob.glob(os.path.join(DAILY_DIR, "*.json"))):
        if os.path.basename(f) >= BARE_SINCE:
            continue
        d = load_json(f, {}) or {}
        mn = d.get("method_note")
        if isinstance(mn, dict) and mn.get("query_mode") == "bare":
            continue    # a daily file rewritten from bare hourly data (e.g. 2026-10-16.json) is not $cashtag history
        for e in d.get("tickers") or []:
            for w, v in (e.get("windows_5h") or {}).items():
                wins.setdefault(e["ticker"], {}).setdefault(w, v)
    _CASHTAG_BASE = {t: (statistics.median(v.values()), len(v)) for t, v in wins.items() if v}
    return _CASHTAG_BASE


def collision_trigger(t, mentions):
    """Reason string if a bare count looks like a word collision (vs the $cashtag history), else None."""
    base = cashtag_baseline().get(t)
    if base is None:
        if mentions > COLLISION_NO_HISTORY_MIN:
            return "no $cashtag history and bare count %d > %d" % (mentions, COLLISION_NO_HISTORY_MIN)
        return None
    med, n = base
    if mentions >= COLLISION_RATIO_MIN_BARE and mentions > COLLISION_RATIO * max(med, 1):
        return "bare count %d = %.1fx the median $cashtag 5h count %g (%d windows before %s), > %gx" % (
            mentions, mentions / max(med, 1), med, n, BARE_SINCE, COLLISION_RATIO)
    return None


def collision_decision(state, t, mentions, end):
    """None if not suspected; else {"check", "label", "held" (reason or None), "queue" (needs an own sample)}."""
    why = collision_trigger(t, mentions)
    if not why:
        return None
    on, oc, _ = own_evidence(state, t, end)
    share = oc / on if on else None
    check = {"reason": why, "own_sample_posts_72h": on,
             "own_real_share": round(share, 2) if share is not None else None}
    if on >= COLLISION_MIN_OWN_POSTS and share >= COLLISION_MIN_REAL_SHARE:
        return {"check": check, "label": "collision-check-passed", "held": None, "queue": False}
    held = "suspected-word-collision: %s; %s" % (
        why, ("own sample %d posts, %.0f%% real mentions (< %.0f%%)" % (on, 100 * share, 100 * COLLISION_MIN_REAL_SHARE))
        if on >= COLLISION_MIN_OWN_POSTS else "no own sample yet -> queued for one next run")
    return {"check": check, "label": "suspected-word-collision", "held": held, "queue": on < COLLISION_MIN_OWN_POSTS}


def collision_queue(state):
    return [q["ticker"] for q in state.get("collision_queue", []) if q["ticker"] not in excluded_tickers()]


def own_evidence(state, t, end):
    if not own_mode_ok(state):
        return 0, 0, []
    cut = end - timedelta(hours=OWN_CLEAN_MAX_AGE_HOURS)
    ev = [x for x in state.get("own_clean", {}).get(t, []) if parse_iso(x["at"]) >= cut]
    return sum(x["n"] for x in ev), sum(x["clean"] for x in ev), ev


def update_own_clean(state, run, agg):
    """Store this run's own-sample tallies (smp-o-<T>) and refresh the pooled prior; prune > max age."""
    end = parse_iso(run["window_end"])
    if not own_mode_ok(state):
        # a $cashtag clean share and a bare-ticker real-mention share measure different things: start over
        state["own_clean"] = {}
        state["own_clean_mode"] = query_mode()
        state["own_clean_prior"] = default_prior()
    oc = state.setdefault("own_clean", {})
    for p in run["picks"].get("samples2") or []:
        if p.get("kind") != "own" or p["key"] not in run["samples"]:
            continue
        a = agg.get(p["target"]) or {}
        n = a.get("own_total", 0)
        if n:
            ev = [x for x in oc.get(p["target"], []) if x["at"] != run["window_end"]]
            ev.append({"at": run["window_end"], "n": n, "clean": a.get("own_clean", 0)})
            oc[p["target"]] = ev
    cut = end - timedelta(hours=OWN_CLEAN_MAX_AGE_HOURS)
    for t in list(oc):
        oc[t] = [x for x in oc[t] if parse_iso(x["at"]) >= cut]
        if not oc[t]:
            del oc[t]
    n = sum(x["n"] for v in oc.values() for x in v)
    c = sum(x["clean"] for v in oc.values() for x in v)
    lo = 0.1 if bare() else 0.4
    state["own_clean_prior"] = round(min(0.95, max(lo, c / n)), 3) if n >= 50 else default_prior()


def own_clean_estimate(state, t, end):
    """Smoothed clean share from the ticker's OWN recency samples: (clean + K*prior) / (n + K)."""
    n, c, ev = own_evidence(state, t, end)
    prior = state.get("own_clean_prior", default_prior()) if own_mode_ok(state) else default_prior()
    return round((c + OWN_CLEAN_PRIOR_K * prior) / (n + OWN_CLEAN_PRIOR_K), 3), n


def own_sample_targets(run, hourly, state, n):
    """Tickers most likely ranked high whose clean-share evidence is thinnest/stalest."""
    if n <= 0:
        return []
    end = parse_iso(run["window_end"])
    queued = collision_queue(state)[:max(1, int(n * COLLISION_QUEUE_SHARE))] if bare() else []
    n -= len(queued)
    if n <= 0:
        return queued
    pool = {t: 0 for t in run["megacaps"]}
    for t, rec in hourly["tickers"].items():
        if rec.get("last_window_end") and parse_iso(rec["last_window_end"]) >= end - timedelta(hours=NODE_MAX_AGE_HOURS):
            pool[t] = max(pool.get(t, 0), rec.get("last_5h", 0))
    for t in run["megacaps"]:
        pool[t] = max(pool[t], (state["tickers"].get(t) or {}).get("mentions", 0))
    exclude = excluded_tickers()
    scored = []
    for t, m in pool.items():
        if t in exclude:
            continue
        if t in queued:
            continue
        ev_n, _, _ = own_evidence(state, t, end)
        scored.append((-(m + 1) / (1 + ev_n / 25.0), t))
    return queued + [t for _, t in sorted(scored)[:n]]


def fresh_frontier(run):
    """Nodes counted (or subtracted) in this run that have no children: [(nid, node, metrics)]."""
    out = []
    for nid, n in run["tree"]["nodes"].items():
        if n.get("children") or nid not in run["counts"]:
            continue
        out.append((nid, n, metrics(run["counts"][nid]["buckets"])))
    return out


def measured_today(hourly, t, day):
    rec = hourly["tickers"].get(t)
    return bool(rec) and rec.get("last_window_end") and day_of(rec["last_window_end"]) == day


def cmd_pick_samples(args):
    run = load_run()
    picks = run["picks"]
    if (any(pending_calls(run, st) for st in ("groups", "megacaps", "splits")) or not run.get("splits_done")) \
            and not args.force:
        sys.exit("group/megacap/split counts not finished (follow next-call, or --force to continue without)")
    rem = reads_remaining(run)
    if picks["samples1"] is None:
        ff = [x for x in fresh_frontier(run) if x[2]["adjusted"] > 0]
        scored = sorted(ff, key=lambda x: (-(x[2]["adjusted"] / math.sqrt(len(x[1]["tickers"]))), x[0]))
        n = min(SAMPLE_GROUPS, run["targets"]["samples"], rem)
        picks["samples1"] = [{"key": "smp-" + nid, "kind": "node", "target": nid,
                              "query": sample_query(node["tickers"]), "size": len(node["tickers"]),
                              "heat": round(m["adjusted"] / math.sqrt(len(node["tickers"])), 1)}
                             for nid, node, m in scored[:n]]
        save_run(run)
        print("stage 1: sample %s" % ", ".join("%s (%d tickers, score %.1f)" % (p["target"], p["size"], p["heat"])
                                              for p in picks["samples1"]))
        print("next: python3 scripts/build_rankings.py next-call   (ONE X call at a time; see PACING)")
        return
    pend1 = [p["key"] for p in picks["samples1"] if p["key"] not in run["samples"]]
    if pend1 and not args.force:
        sys.exit("stage-1 samples not recorded yet: %s" % ", ".join(pend1))
    if picks["samples2"] is None:
        n_all = min(max(0, run["targets"]["samples"] - len(picks["samples1"])), rem)
        n = min(COMENTION_SAMPLES, n_all)
        agg, _ = analyze_samples(run)
        mega = set(run["megacaps"])
        hourly = load_hourly()
        day = day_of(run["window_end"])
        seen = hourly["seen"].get(day, {})
        ranked = sorted(((t, a) for t, a in agg.items() if t not in mega and not measured_today(hourly, t, day)),
                        key=lambda x: (-len(x[1]["authors"]), -x[1]["posts"], x[0]))
        chosen = [t for t, a in ranked if clean_ok(a)][:max(0, n - 1)]
        # unverified hot split leaves: which member is it, and is it clean?
        leaves = sorted((x for x in fresh_frontier(run) if len(x[1]["tickers"]) <= LEAF_MAX
                         and (node_resid_24h(run, x[0]) or 0) >= LEAF_MIN_24H),
                        key=lambda x: -(node_resid_24h(run, x[0]) or 0))
        for nid, node, m in leaves:
            for t in node["tickers"]:
                if len(chosen) >= n:
                    break
                if t not in chosen and t not in mega and t not in run["core_order"] and t not in agg \
                        and len(seen.get(t, {}).get("authors", [])) < MIN_AUTHORS:
                    chosen.append(t)
        for r in load_state()["risers"]:
            if len(chosen) >= n:
                break
            if r["ticker"] not in chosen and r["ticker"] not in mega:
                chosen.append(r["ticker"])
        for t, a in ranked:
            if len(chosen) >= n:
                break
            if t not in chosen:
                chosen.append(t)
        coll = collisions()
        chosen = chosen[:n]
        n_own = n_all - len(chosen)
        picks["samples2"] = [{"key": "smp-c-" + t, "kind": "comention", "target": t,
                              "query": sample_ticker_query(t, coll)} for t in chosen]
        own = own_sample_targets(run, hourly, load_state(), n_own)
        picks["samples2"] += [{"key": "smp-o-" + t, "kind": "own", "target": t, "sort": "recency",
                               "query": ticker_query(t, coll)} for t in own]
        chosen = chosen + ["own:" + t for t in own]
        save_run(run)
        print("stage 2 (co-mentions / leaf checks): %s" % (", ".join(chosen[:n]) or "none"))
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
    reconcile_reads(run, args.reads_left_today)
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
    pending = {k for st in STAGES for k, *_ in pending_calls(run, st)}
    if key not in pending:
        sys.exit("%s is not a pending call (already recorded or skipped?)" % key)
    att = lg["attempts"].setdefault(key, [])
    att.append({"kind": args.kind, "at": iso(utcnow()), "detail": (args.detail or "")[:200]})
    lg["extra_reads"] += 1
    if args.kind == "429":
        lg["retries_429"] += 1
    else:
        lg["errors_other"] += 1
    reconcile_reads(run, args.reads_left_today)
    note_reads_left(run, args.reads_left_today)
    n429 = sum(1 for a in att if a["kind"] == "429")
    noth = sum(1 for a in att if a["kind"] != "429")
    give_up = n429 > len(WAIT_429) or noth >= 2
    if give_up:
        run["skipped"].append(key)
    switched = mode_from_tool_note(run, args)
    msg = stop_checks(run)
    save_run(run)
    if switched:
        print(switched)
        return
    print("%s: %s error recorded (attempts: %d x 429, %d other); run retries/errors %d of reserve %d, "
          "non-429 errors %d of %d" % (key, args.kind, n429, noth, lg["extra_reads"], RETRY_RESERVE,
                                       lg["errors_other"], MAX_OTHER_ERRORS))
    if msg:
        print("STOP: %s. %s" % (msg, stop_advice(run)))
    elif give_up:
        print("GIVE UP on %s (no more retries); it is skipped and reported as missing. Continue with next-call "
              "after a %ds pause." % (key, COOLDOWN_AFTER_GIVE_UP if args.kind == "429" else PACE_SECONDS))
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
    """Daily exact counts: split leaves, sample candidates, hot re-reads, then core not yet measured today."""
    run = load_run()
    note_reads_left(run, args.reads_left_today)
    coll = collisions()
    pend = [k for k, *_ in pending_calls(run, "samples")]
    if pend and not args.force:
        sys.exit("samples not recorded yet: %s (or --force)" % ", ".join(pend))
    rem = reads_remaining(run)
    spare = min(SPARE, max(1, rem // 15)) if rem > 0 else 0
    slots = max(0, rem - spare)
    hourly = load_hourly()
    day = day_of(run["window_end"])
    mega = set(run["megacaps"])
    core = run["core_order"]
    taken = set(mega) | set(run["counts"])
    agg, _ = analyze_samples(run)
    leaf, sample, hot = [], [], []
    for nid, node, m in sorted(fresh_frontier(run), key=lambda x: -(node_resid_24h(run, x[0]) or 0)):
        if len(node["tickers"]) <= LEAF_MAX and (node_resid_24h(run, nid) or 0) >= LEAF_MIN_24H:
            for t in node["tickers"]:
                if not measured_today(hourly, t, day):
                    leaf.append((t, nid))
    for t, a in sorted(agg.items(), key=lambda x: (-len(x[1]["authors"]), -x[1]["posts"], x[0])):
        if clean_ok(a) and not measured_today(hourly, t, day):
            sample.append((t, sorted(a["via"])))
    hot_more = []
    for t, rec in sorted(hourly["tickers"].items(), key=lambda x: -x[1].get("last_5h", 0)):
        if t in mega or not measured_today(hourly, t, day) or rec.get("last_window_end") == run["window_end"]:
            continue
        if rec.get("last_5h", 0) >= HOT_REREAD_5H and len(hot) < HOT_REREAD_MAX:
            hot.append((t, None))
        elif rec.get("last_5h", 0) >= HOT_REREAD_5H // 2:
            hot_more.append((t, None))
    core_due = [t for t in core if not measured_today(hourly, t, day)]
    picks = []

    leaf_via = dict(leaf)

    def add(t, source, via):
        if t in taken or len(picks) >= slots:
            return
        if t in leaf_via and source != "split-leaf":
            source, via = "split-leaf", leaf_via[t]
        taken.add(t)
        picks.append({"ticker": t, "tier": "core" if t in core else "discovered", "source": source, "via": via})

    # suspected word collisions that got their own sample in this run are counted again, so the build can
    # rank them as soon as the sample clears them (bare mode)
    if bare():
        queued = set(collision_queue(load_state()))
        for sp in run["picks"].get("samples2") or []:
            if sp.get("kind") == "own" and sp["target"] in queued and sp["key"] in run["samples"]:
                add(sp["target"], "collision-check", None)
    core_min = min(CORE_DAILY_MIN, len(core_due), slots)
    discover = slots - core_min
    lists = [("split-leaf", leaf), ("sample", sample), ("hot-reread", hot)]
    idx = [0, 0, 0]
    while len(picks) < discover and any(idx[i] < len(l) for i, (_, l) in enumerate(lists)):
        for i, (src, l) in enumerate(lists):
            if len(picks) >= discover:
                break
            while idx[i] < len(l) and l[idx[i]][0] in taken:
                idx[i] += 1
            if idx[i] < len(l):
                add(l[idx[i]][0], src, l[idx[i]][1])
                idx[i] += 1
    for t in core_due:
        add(t, "core-daily", None)
    for src, l in lists + [("hot-reread", hot_more)]:
        for t, via in l:
            add(t, src, via)
    for p in picks:
        p["collision"] = p["ticker"] in coll
        p["query"] = ticker_query(p["ticker"], coll)
        run["count_sources"][p["ticker"]] = p["source"]
    run["picks"]["individual"] = picks
    run["spare_reserved"] = spare
    save_run(run)
    by = {}
    for p in picks:
        by[p["source"]] = by.get(p["source"], 0) + 1
    print("daily exact counts: %d %s (remaining budget %d, spare %d)" % (len(picks), json.dumps(by), rem, spare))
    print("split leaves: %s" % (", ".join("%s(%s)" % (t, v) for t, v in leaf[:15]) or "-"))
    print("sample candidates (clean authors, clean share):",
          ", ".join("%s(%d, %.0f%%)" % (t, len(agg[t]["authors"]), 100 * agg[t]["share"]) for t, _ in sample[:20]) or "-")
    rejected = sorted(((t, a) for t, a in agg.items() if not clean_ok(a) and a["total"] >= MIN_AUTHORS
                       and t not in mega and t not in core), key=lambda x: -x[1]["total"])
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
    print("splits: %d ops, %d of %d split reads planned" % (len(run["splits"]), split_reads_planned(run),
                                                            run["targets"]["splits"]))
    kind, payload = next_action(run)
    save_run(run)
    if kind == "stop":
        print("STOPPED: %s. %s" % (run["stopped"], stop_advice(run)))
    elif kind == "call":
        stage, calls = payload
        print("NEXT: %s calls pending (%d), next %s  -> `next-call` prints it; ONE call at a time"
              % (stage, len(calls), calls[0][0]))
    elif kind == "pick-samples":
        print("NEXT: python3 scripts/build_rankings.py pick-samples   (%s)" % payload)
    elif kind == "pick-candidates":
        print("NEXT: python3 scripts/build_rankings.py pick-candidates")
    else:
        print("NEXT: python3 scripts/build_rankings.py build")


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
        missing = [k for st in ("splits", "samples", "counts") for k, *_ in pending_calls(run, st)] + list(run["skipped"])
        partial_reason = "%s; not measured: %s" % (run.get("stopped") or "run ended early",
                                                   ", ".join(missing) or "-")
    coll = collisions()
    picks = [{"ticker": t, "tier": "core", "source": "megacap", "collision": t in coll,
              "query": ticker_query(t, coll), "via": None} for t in run["megacaps"]]
    picks += run["picks"]["individual"] or []
    # a ticker that became not measurable (config/ambiguous_tickers.txt) after it was counted is never ranked
    dropped_nm = sorted({p["ticker"] for p in picks if p["ticker"] in run["counts"]} & excluded_tickers())
    measured = [p for p in picks if p["ticker"] in run["counts"] and p["ticker"] not in dropped_nm]
    if dropped_nm:
        print("not ranked (not measurable / excluded): %s" % ", ".join(dropped_nm))
    if not measured:
        sys.exit("no individual counts recorded")
    universe = load_universe()
    umeta = load_json(UNIVERSE_META, {})
    state = load_state()
    now = utcnow()
    agg, sstats = analyze_samples(run)
    update_own_clean(state, run, agg)
    hourly = load_hourly()
    update_hourly(run, hourly, agg, now)
    day = day_of(run["window_end"])
    pooled = hourly["seen"].get(day, {})

    ctx_set = set(context_tickers())
    entries = []
    for p in measured:
        t = p["ticker"]
        c = run["counts"][t]
        m = metrics(c["buckets"])
        labels = []
        if p["collision"]:
            labels.append("collision-filtered")
        if t in ctx_set:
            labels.append("context-required")
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
            "rank_score_raw": m["adjusted"],
            "hourly": c["buckets"][-WINDOW_HOURS:],
            "last_measured": c["recorded_at"],
        }
        if p["collision"] or t in ctx_set:
            e["query"] = p["query"]
        if p.get("via"):
            e["discovered_via"] = p["via"]
        a = agg.get(t)
        est, est_n = own_clean_estimate(state, t, parse_iso(run["window_end"]))
        e["clean_share_est"] = est
        e["clean_share_own_posts_72h"] = est_n
        e["mentions_clean_est"] = int(round(m["mentions"] * est))
        e["rank_score"] = int(round(m["adjusted"] * est))
        if est_n == 0:
            labels.append("clean-share-prior")
        if est < LOW_CLEAN_SHARE:
            labels.append("low-clean-share")
        if a:
            e["sample_authors"] = len(a["authors"])
            e["sample_posts"] = a["total"]
            if a["total"] >= CLEAN_SHARE_MIN_POSTS:
                # co-mention dominated (list dumps); informational only, biased low for megacaps
                e["sample_clean_share"] = a["share"]
            if a["total"] >= 5 and a.get("crypto", 0) >= CRYPTO_SHARE_FLAG * a["total"]:
                labels.append("crypto-collision")
                if p["tier"] != "core":
                    e["_held"] = "crypto collision: %d of %d sample posts are crypto" % (a["crypto"], a["total"])
        # word-collision guard (bare mode): a bare count far above the $cashtag history (or high without any)
        # is held back until an own sample shows >= 30% real mentions; then it is ranked with the cleaned estimate
        if bare() and "_held" not in e:
            cc = collision_decision(state, t, m["mentions"], parse_iso(run["window_end"]))
            if cc:
                e["collision_check"] = cc["check"]
                labels.append(cc["label"])
                if cc["held"]:
                    e["_held"] = cc["held"]
                    e["_collision_queue"] = cc["queue"]
        # clean gate for non-core tickers (discovered passed it when picked; carryover is re-checked
        # against this run's samples when they mention it, otherwise its last passed check is carried)
        if p["tier"] != "core" and "_held" not in e:
            prev = state.get("clean_checks", {}).get(t)
            if a and a["total"] >= CLEAN_SHARE_MIN_POSTS and not clean_ok(a):
                e["_held"] = "sample: %d posts, %.0f%% clean, %d clean authors" % (a["total"], 100 * a["share"],
                                                                                 len(a["authors"]))
            elif a and clean_ok(a):
                e["_clean_checked_at"] = run["window_end"]
            elif t in pooled and clean_ok({"authors": set(pooled[t]["authors"]), "total": pooled[t]["total"],
                                           "share": pooled[t]["clean"] / max(1, pooled[t]["total"])}):
                e["_clean_checked_at"] = run["window_end"]
                labels.append("clean-check-pooled-today")
            elif prev and parse_iso(prev) >= parse_iso(run["window_end"]) - timedelta(hours=CLEAN_CHECK_MAX_AGE_HOURS):
                e["_clean_checked_at"] = prev
                labels.append("clean-check-carried")
            else:
                e["_held"] = "no sample evidence of clean posts from %d+ authors" % MIN_AUTHORS
        entries.append(e)
    held = [e for e in entries if "_held" in e]
    entries = [e for e in entries if "_held" not in e]
    _COLLISION_HELD.clear()
    _COLLISION_HELD.update({e["ticker"]: e["_held"] for e in held if "collision_check" in e})
    state["collision_queue"] = [{"ticker": e["ticker"], "mentions": e["mentions"], "window_end": run["window_end"],
                                 "reason": e["collision_check"]["reason"]}
                                for e in sorted(held, key=lambda e: -e["mentions"]) if e.get("_collision_queue")]
    # risers
    for e in entries:
        m1 = e["momentum_last_hour_vs_prev_9h"] or 0
        if (e.get("_m1_prev_day") or 0) < RISER_MIN_1H_VS_PREV_DAY:
            m1 = 0   # last hour high only vs the night/pre-open hours, not vs the same hour yesterday
        ratio = max(e["momentum_5h_vs_prev_day"] or 0, m1)
        if e["rank_score"] >= RISER_MIN_MENTIONS and ratio >= RISER_MIN_RATIO:
            e["labels"].append("riser")
            e["_riser_score"] = round(e["rank_score"] * math.log2(ratio), 1)
    entries.sort(key=lambda e: (-e["rank_score"], -e["rank_score_raw"], e["ticker"]))
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
    n_mega = sum(1 for p in measured if p["source"] == "megacap")
    n_split = sum(1 for op in run["splits"] for k in op["measure"] if k in run["counts"])
    n_smp = len(run["samples"])
    n_probe = 1 if "PROBE" in run["counts"] else 0
    extra = int(run["log"].get("extra_reads", 0))
    not_measured = [p["ticker"] for p in picks if p["ticker"] not in run["counts"]]
    split_out = []
    for op in run["splits"]:
        split_out.append({"node": op["node"], "parent_posts_24h": op.get("parent_24h"),
                          "parts": [{"node": k, "size": len(run["tree"]["nodes"][k]["tickers"]),
                                     "first": run["tree"]["nodes"][k]["tickers"][0],
                                     "last": run["tree"]["nodes"][k]["tickers"][-1],
                                     "mentions": (sum(run["counts"][k]["buckets"][-WINDOW_HOURS:])
                                                  if k in run["counts"] else None),
                                     "posts_24h": (sum(run["counts"][k]["buckets"][-24:])
                                                   if k in run["counts"] else None),
                                     "subtracted": bool(run["counts"].get(k, {}).get("inferred"))}
                                    for k in op["children"]],
                          "overlap_excess": op.get("overlap_excess"), "overlap_warning": op.get("overlap_warning", False),
                          "infer_failed": op.get("infer_failed")})
    coll_terms = suffix()
    out = {
        "generated_at": iso(now),
        "window_hours": WINDOW_HOURS,
        "window_start": run["window_start"],
        "window_end": run["window_end"],
        "universe": "stocks",
        "method": METHOD,
        "method_version": METHOD_VERSION,
        "query_mode": query_mode(),
        "method_note": method_note(),
        "not_measurable": not_measurable_block(universe),
        "source": {
            "platform": "X",
            "endpoint": "GET /2/tweets/counts/recent",
            "query_pattern": qtag("TICKER") + " " + coll_terms,
            "collision_query_pattern": qtag("TICKER") + " " + coll_terms + " <per-ticker terms from config/collisions.txt>",
            "context_query_pattern": ("TICKER %s %s" % (context_clause(), coll_terms)) if bare() else None,
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
        "counted_not_ranked": dropped_nm,
        "x_reads_used": n_probe + n_groups + n_split + n_smp + n_ind + len(dropped_nm) + extra,
        "x_reads_breakdown": {"unrecorded_retries": int(run["log"].get("unrecorded_reads", 0)),
                              "mode_probe": n_probe, "group_counts": n_groups, "split_counts": n_split, "samples": n_smp,
                              "individual_counts": n_ind, "megacap_counts": n_mega,
                              "daily_counts": n_ind - n_mega,
                              "counted_not_ranked": len(dropped_nm),
                              "extra_errors_retries": extra,
                              "retries_429": int(run["log"].get("retries_429", 0)),
                              "errors_other": int(run["log"].get("errors_other", 0))},
        "partial": not complete,
        "missing_calls": missing_calls(run),
        "partial_reason": partial_reason,
        "x_reads_left_today": run["log"].get("reads_left_today"),
        "clean_share_rule": ("clean_share_est = (clean + %d x prior) / (posts + %d) over the ticker's own "
                             "recency samples of the last %dh; prior = pooled own-sample clean share (%s); "
                             "mentions_clean_est = mentions x clean_share_est; sample_clean_share is from "
                             "co-mentions (list dumps) and only informational"
                             % (OWN_CLEAN_PRIOR_K, OWN_CLEAN_PRIOR_K, OWN_CLEAN_MAX_AGE_HOURS,
                                state.get("own_clean_prior", default_prior()))),
        "ranking_rule": ("rank_score = round(rank_score_raw x clean_share_est), ties by rank_score_raw. "
                         "rank_score_raw = 5h count, except that a burst hour (an hour > %d x max(29h hourly "
                         "median, %d) whose next hour fell below a third of it) is capped at that limit; "
                         "only differs from mentions for burst-flagged entries; ties by mentions"
                         % (BURST_FACTOR, BURST_MIN_MEDIAN)),
        "group_sweep": {
            "query_pattern": "(%s OR %s OR ...) " % (qtag("T1"), qtag("T2")) + coll_terms,
            "max_query_chars": MAX_QUERY_LEN,
            "stock_groups": len(stock_groups),
            "stock_tickers_screened": sum(g["size"] for g in stock_groups),
            "demoted_core_in_sweep": [t for g in stock_groups for t in g["demoted_core"]],
            "megacaps_counted_individually": run["megacaps"],
            "etf_groups_this_run": [g["key"] for g in etf_groups],
            "etf_groups_total": run["etf_groups_total"],
            "etf_tickers_total": run["etf_tickers_total"],
            "etf_tickers_screened": sum(g["size"] for g in etf_groups),
            "groups_missing": missing_groups,
            "etf_groups_requeued_from_last_run": run.get("etf_requeued", []),
            "etf_groups_queued_for_next_run": [k for k in missing_groups if k.startswith("E")],
            "note": ("A group count is an exact count of posts mentioning ANY ticker of the group (a post with "
                     "several of them counts once), with the spam filter applied. It is an upper bound for "
                     "every member, so a quiet group means all its tickers are quiet."),
            "groups": gsum,
        },
        "adaptive_splits": {
            "rule": ("each run, the unsplit node with the most posts in the last 24h (>= %d) is split into 4 "
                     "contiguous parts; if the node was counted in this run, 3 parts are counted and the 4th is "
                     "parent minus the 3 (per hour, >= 0; an under-estimate when posts mention tickers of "
                     "several parts), else all 4 are counted; nodes with <= %d tickers are leaves whose members "
                     "get exact daily counts; the tree persists in data/splits.json (pruned after %dh without a "
                     "count, cold splits collapsed)" % (SPLIT_MIN_24H, LEAF_MAX, NODE_MAX_AGE_HOURS)),
            "reads": n_split,
            "nodes_after_run": len(run["tree"]["nodes"]),
            "prune_notes": run.get("tree_prune_notes", []),
            "splits": split_out,
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
                       "reason": e["_held"], **({"labels": e["labels"], "collision_check": e["collision_check"]}
                                                if "collision_check" in e else {})}
                      for e in sorted(held, key=lambda e: -e["mentions"])],
        "word_collision_guard": collision_guard_rule() if bare() else None,
        "collision_queue": [q["ticker"] for q in state.get("collision_queue", [])] if bare() else [],
        "universe_source": {
            "repo": umeta.get("source_repo", FTD_REPO),
            "file": umeta.get("source_file", FTD_FILE),
            "commit": umeta.get("source_commit"),
            "fetched_at": umeta.get("fetched_at"),
        },
        "cadence": "every 5 hours",
        "freshness": ("every ranked ticker was counted individually in THIS run (last_measured = time of its "
                      "count; all over the same UTC window). Tickers measured in earlier runs today are in "
                      "data/today.json, not here."),
        "note": (("PARTIAL RUN (%s). " % partial_reason if partial_reason else "") +
                 "Ranks the tickers counted individually in this run: the megacaps (every run) plus this run's "
                 "exact daily counts (split leaves, sample discoveries, hot re-reads, core due today), all over "
                 "the same UTC window. The whole stock universe is screened at group level every run and hot "
                 "groups are split adaptively, but a ticker is only ranked if it was counted individually. "
                 "The daily view of everything seen today is data/today.json."),
        "rankings": rankings,
    }
    # persistent split tree: update counts of every node measured in this run; fail closed
    tree = run["tree"]
    for nid, n in tree["nodes"].items():
        c = run["counts"].get(nid)
        if c:
            n["count_24h"] = sum(c["buckets"][-24:])
            n["count_5h"] = sum(c["buckets"][-WINDOW_HOURS:])
            n["measured_at"] = c["recorded_at"]
            n["window_end"] = run["window_end"]
            n["inferred"] = bool(c.get("inferred"))
    try:
        validate_tree(tree)
        out["adaptive_splits"]["state"] = "saved"
    except SplitStateError as e:
        tree = empty_tree()
        out["adaptive_splits"]["state"] = "RESET (inconsistent: %s)" % e
        print("WARNING: split state inconsistent (%s); data/splits.json reset to an empty tree" % e)
    write_json(SPLITS_JSON, tree)
    write_json(HOURLY_JSON, hourly, compact=True)
    mega_all = set(megacap_list())
    core_all = set(core_list())
    through = parse_iso(run["window_end"])
    today_entries = write_daily(hourly, day, through, universe, core_all, mega_all, TODAY_JSON, now)
    mc = missing_calls(run)
    if mc or not complete:
        td = load_json(TODAY_JSON, {})
        td["notes"] = [("latest run (window end %s) was PARTIAL: %s" % (run["window_end"], partial_reason))
                       if not complete else "latest run (window end %s) complete" % run["window_end"]]
        if mc.get("etf_groups"):
            td["notes"].append("ETF groups not counted in the latest run (no upper bounds from them; queued for the "
                               "next run): %s" % ", ".join(mc["etf_groups"]))
        if mc.get("split_parts") or mc.get("samples"):
            td["notes"].append("missing split parts/samples: %s" % ", ".join(mc.get("split_parts", []) + mc.get("samples", [])))
        write_json(TODAY_JSON, td)
    prev_day = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    if any(day_of(w) == prev_day for w in hourly["windows"]):
        write_daily(hourly, prev_day, through, universe, core_all, mega_all,
                    os.path.join(DAILY_DIR, prev_day + ".json"), now)
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
    rotated = run.get("etf_rotated", [g["key"] for g in etf_groups])
    if rotated and run["etf_groups_total"]:
        last = int(rotated[-1][1:]) - 1
        state["etf_rotation_next"] = (last + 1) % run["etf_groups_total"]
    # ETF groups that could not be counted are queued for the next run
    state["etf_requeue"] = [k for k in missing_groups if k.startswith("E")]
    state["method_version"] = METHOD_VERSION
    state["updated_at"] = iso(now)
    write_json(STATE_JSON, state)
    write_next(state, now)
    if partial_reason:
        print("PARTIAL build: " + partial_reason)
    print("wrote %s and %s: %d ranked, %d risers, reads %s, planned-not-measured %d"
          % (os.path.relpath(LATEST_JSON, ROOT), os.path.relpath(hist, ROOT), len(rankings), len(risers_out),
             json.dumps(out["x_reads_breakdown"]), len(not_measured)))
    print("today.json: %d tickers seen on %s (%d exact, %d partial, %d bounded, %d seen-only)"
          % (len(today_entries), day, *[sum(1 for e in today_entries if e["status"] == k)
                                        for k in ("exact", "partial", "bounded", "seen")]))
    print("top 10:", ", ".join("%s %d%s" % (r["ticker"], r["mentions"], "*" if r["burst"] else "") for r in rankings[:10]))
    print("risers:", ", ".join("%s %d (x%.1f/x%.1f)" % (r["ticker"], r["mentions"], r["momentum_5h_vs_prev_day"] or 0,
                                                       r["momentum_last_hour_vs_prev_9h"]) for r in risers_out[:10]) or "-")


def write_next(state, now):
    mega = megacap_list()
    core = [t for t in core_priority(state) if t not in mega]
    write_json(NEXT_JSON, {
        "generated_at": iso(now),
        "method": METHOD,
        "note": ("Expected individual counts of the next run: the megacaps (every run). Everything else, "
                 "including the rest of the core, is screened in the group sweep and gets one exact daily "
                 "count per UTC day (split leaves, sample discoveries, hot re-reads, core due today), "
                 "chosen by `pick-candidates` at run time."),
        "query_pattern": qtag("TICKER") + " -is:retweet <spam suffix>",
        "method_note": method_note(),
        "count": len(mega),
        "tickers": [{"ticker": t, "tier": "core", "source": "megacap"} for t in mega],
        "core_in_group_sweep": core,
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
    problems = []
    stock_groups, etf_groups, stock_t, etf_t = current_packing(universe)
    for name, tick, groups in (("stock", stock_t, stock_groups), ("etf", etf_t, etf_groups)):
        flat = [t for g in groups for t in g]
        if flat != tick:
            problems.append("%s packing does not reproduce the ticker list" % name)
        for g in groups:
            q = group_query(g)
            inner = q.split(") ", 1)[0].lstrip("(")
            back = [x[1:] if x.startswith("$") else x for x in inner.split(" OR ")]
            if any(x.startswith("$") for x in inner.split(" OR ")) == bare():
                problems.append("%s group %s: query prefix does not match query mode %s" % (name, g[0], query_mode()))
            if back != g:
                problems.append("%s group %s: query does not parse back to its members" % (name, g[0]))
            if len(q) > MAX_QUERY_LEN:
                problems.append("%s group %s too long: %d" % (name, g[0], len(q)))
        print("%s: %d tickers in %d groups (sizes %s..%s, longest query %d chars)"
              % (name, len(tick), len(groups), min(map(len, groups)), max(map(len, groups)),
                 max(len(group_query(g)) for g in groups)))
    mega = megacap_list()
    missing = [t for t in mega if t not in universe]
    if missing:
        problems.append("megacaps not in the universe: %s" % ",".join(missing))
    core = core_list()
    swept = set(stock_t) | set(etf_t)
    lost = [t for t in core if t not in swept and t not in mega and t in universe]
    if lost:
        problems.append("core tickers neither megacap nor swept: %s" % ",".join(lost))
    coll = collisions()
    for t, extra in coll.items():
        q = ticker_query(t, coll)
        if len(q) > MAX_QUERY_LEN or q.count('"') % 2:
            problems.append("collision query for %s is invalid: %s" % (t, q))
    try:
        tree = load_tree()
        print("split state: %d nodes, valid" % len(tree["nodes"]))
    except SplitStateError as e:
        problems.append("split state inconsistent: %s" % e)
    print("suffix (%d chars): %s" % (len(suffix()), suffix()))
    print("collisions: %d (in universe: %d); spam accounts: %d; core: %d; megacaps: %s"
          % (len(coll), sum(1 for t in coll if t in universe), len(spam_accounts()), len(core), " ".join(mega)))
    if bare():
        amb = ambiguous_tickers()
        nm = not_measurable()
        ctx = context_tickers()
        miss_ctx = [t for t in ctx if t not in amb]
        if miss_ctx:
            problems.append("context tickers that are not ambiguous (count them normally): %s" % ",".join(miss_ctx))
        for t in ctx:
            q = ticker_query(t, coll)
            if len(q) > MAX_QUERY_LEN or q.count('"') % 2 or not q.startswith(t + " ("):
                problems.append("context query for %s is invalid: %s" % (t, q))
        leak = [t for g in stock_groups + etf_groups for t in g if t in nm or t in ctx]
        if leak:
            problems.append("ambiguous/context tickers inside sweep groups: %s" % ",".join(leak[:10]))
        print("query mode BARE (since %s): %d ambiguous tickers in config/ambiguous_tickers.txt, %d not measurable "
              "(%d in universe), %d context tickers counted with a finance clause: %s"
              % (query_mode_info().get("since", "?"), len(amb), len(nm), sum(1 for t in nm if t in universe),
                 len(ctx), " ".join(ctx)))
    else:
        print("query mode CASHTAG ($TICKER queries)")
    t, ok = size_run(RUN_READS, len(stock_groups), len(etf_groups), len(mega), 1 if bare() else 0)
    print("default split of %d reads: %s (sum %d)" % (RUN_READS, json.dumps(t), sum(t.values())))
    for p in problems:
        print("PROBLEM:", p)
    if problems:
        sys.exit(1)
    print("check OK")


def cmd_set_mode(args):
    if set_query_mode(args.mode, args.reason):
        print("query mode -> %s (data/query_mode.json). If a run is in progress: abandon it and plan again." % args.mode)
    else:
        print("query mode already %s" % args.mode)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("universe", help="refresh data/universe.csv from free-ticker-database")
    p = sub.add_parser("plan", help="window, budget, groups -> work/run.json")
    p.add_argument("--now", help="override current UTC time (YYYY-MM-DDTHH:MM:SSZ)")
    p.add_argument("--reads-left-today", type=int)
    p = sub.add_parser("calls", help="print exact x tool arguments for pending calls of a stage")
    p.add_argument("stage", choices=list(STAGES))
    p.add_argument("key", nargs="*")
    p.add_argument("--all", action="store_true", help="list all pending calls (review only; never batch them)")
    p = sub.add_parser("record", help="record counts: KEY=TOTAL:b1,b2,...,b29 (29 hourly buckets, oldest first)")
    p.add_argument("pairs", nargs="*")
    p.add_argument("--reads-left-today", type=int)
    p.add_argument("--extra-reads", type=int, default=0, help="(legacy) non-429 error reads; prefer record-error")
    p.add_argument("--dollar-removed", choices=["yes", "no"],
                   help="did the x tool output say '%s'? (required for PROBE; 'yes' on any cashtag-mode call "
                        "switches to bare)" % DOLLAR_NOTE)
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
    p.add_argument("--dollar-removed", choices=["yes", "no"], help="the error output said '%s'" % DOLLAR_NOTE)
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
    sub.add_parser("splits-reset", help="reset data/splits.json to an empty tree (after a fail-closed error)")
    p = sub.add_parser("set-mode", help="set the query mode by hand (cashtag | bare); normally automatic")
    p.add_argument("mode", choices=["cashtag", "bare"])
    p.add_argument("--reason", required=True)
    args = ap.parse_args()
    {"universe": cmd_universe, "plan": cmd_plan, "calls": cmd_calls, "record": cmd_record,
     "pick-samples": cmd_pick_samples, "record-sample": cmd_record_sample,
     "pick-candidates": cmd_pick_candidates, "next": cmd_next, "status": cmd_next,
     "record-error": cmd_record_error, "next-call": cmd_next_call, "abandon": cmd_abandon,
     "build": cmd_build, "check": cmd_check, "migrate": cmd_migrate,
     "splits-reset": cmd_splits_reset, "set-mode": cmd_set_mode}[args.cmd](args)


if __name__ == "__main__":
    main()
