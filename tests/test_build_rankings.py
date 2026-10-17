"""Offline checks for scripts/build_rankings.py (no X reads). Run: python3 -m unittest discover tests"""
import importlib.util
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="xcr-test-")
for d in ("scripts", "config", "data"):
    shutil.copytree(os.path.join(REPO, d), os.path.join(TMP, d))
os.environ["XCR_ROOT"] = TMP
MODE_FILE = os.path.join(TMP, "data", "query_mode.json")


def set_mode(mode):
    """The method-v3 tests run in cashtag mode; the bare-ticker tests switch to bare and back."""
    with open(MODE_FILE, "w") as f:
        json.dump({"mode": mode, "since": "2026-10-17T04:23:20Z", "history": []}, f)


set_mode("cashtag")
spec = importlib.util.spec_from_file_location("br", os.path.join(TMP, "scripts", "build_rankings.py"))
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)
CLI = [sys.executable, os.path.join(TMP, "scripts", "build_rankings.py")]

# real 29h series measured 2026-10-07T04Z..2026-10-08T09Z (oldest first)
NET = [31, 11, 12, 10, 21, 154, 267, 23, 17, 15, 70, 19, 14, 27, 15, 11, 16, 6, 15, 5, 18, 161, 374, 44, 9, 6, 5, 7, 13]
IPW = [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 7, 5, 10, 2, 3, 4, 2, 1, 4, 5, 16, 46]
NVDA_FLAT = [60] * 24 + [48, 64, 60, 78, 93]


def run(*args):
    return subprocess.run(CLI + list(args), capture_output=True, text=True, env=dict(os.environ, XCR_ROOT=TMP))


class Packing(unittest.TestCase):
    def setUp(self):
        self.u = br.load_universe()
        self.ex = set(br.read_list("exclude_tickers.txt"))
        self.core = set(br.core_list())

    def test_every_noncore_stock_in_exactly_one_group(self):
        stocks = sorted(t for t, r in self.u.items() if r["asset_type"] == "Stock" and t not in self.core and t not in self.ex)
        groups = br.pack(stocks)
        flat = [t for g in groups for t in g]
        self.assertEqual(flat, stocks)
        self.assertEqual(len(flat), len(set(flat)))
        for t in ("DLTR", "DLX"):          # dropped by a hand-copy typo in the R&D run
            self.assertIn(t, flat)

    def test_queries_within_limit_and_parse_back(self):
        etfs = sorted(t for t, r in self.u.items() if r["asset_type"] == "ETF" and t not in self.core and t not in self.ex)
        for g in br.pack(etfs):
            q = br.group_query(g)
            self.assertLessEqual(len(q), br.MAX_QUERY_LEN)
            self.assertTrue(q.endswith(br.suffix()))
            body = q[1:q.index(") ")]
            self.assertEqual([x[1:] for x in body.split(" OR ")], g)

    def test_pack_is_tight(self):
        # adding the next ticker to any full group would exceed the limit
        tick = sorted(self.u)[:3000]
        groups = br.pack(tick)
        for a, b in zip(groups, groups[1:]):
            self.assertGreater(len(br.group_query(a + [b[0]])), br.MAX_QUERY_LEN)

    def test_boundary_exact_limit(self):
        g = br.pack(sorted(self.u), maxlen=600)
        self.assertTrue(all(len(br.group_query(x)) <= 600 for x in g))


class Budget(unittest.TestCase):
    def test_default_split(self):
        t, ok = br.size_run(90, 13, 14)
        self.assertTrue(ok)
        self.assertEqual(t, {"probe": 0, "stock_groups": 13, "megacaps": 10, "etf_groups": 3, "splits": 18,
                             "samples": 10, "spare": 3, "dailies": 33})
        # bare mode since 2026-10-17 17:00: groups <= 3000 chars (14 stock), probe + 10 megacaps + 8 context
        t, ok = br.size_run(br.RUN_READS, 14, 16, 18, 1)
        self.assertEqual(t, {"probe": 1, "stock_groups": 14, "megacaps": 18, "etf_groups": 3, "splits": 18,
                             "samples": 10, "spare": 3, "dailies": 23})
        self.assertLessEqual(sum(t.values()) + br.RETRY_RESERVE, 100)

    def test_dailies_then_splits_shrink_first(self):
        t, ok = br.size_run(70, 13, 14)
        self.assertEqual((t["dailies"], t["splits"], t["megacaps"], t["samples"]), (13, 18, 10, 10))
        self.assertEqual(sum(t.values()), 70)
        t, ok = br.size_run(60, 13, 14)
        self.assertEqual((t["dailies"], t["splits"], t["megacaps"], t["samples"]), (10, 11, 10, 10))
        t, ok = br.size_run(40, 13, 14)
        self.assertEqual(sum(t.values()), 40)
        self.assertEqual(t["megacaps"], 10)

    def test_minimum(self):
        t, ok = br.size_run(20, 13, 14)
        self.assertTrue(ok)
        self.assertEqual(sum(t.values()), 20)
        self.assertEqual(t["stock_groups"], 13)
        t, ok = br.size_run(12, 13, 14)
        self.assertFalse(ok)

    def test_plan_skips_when_under_daily_cap(self):
        r = run("plan", "--now", "2026-10-08T14:14:00Z", "--reads-left-today", "415")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("SKIP", r.stdout)

    def test_plan_shrinks_with_reads_left(self):
        r = run("plan", "--now", "2026-10-08T14:14:00Z", "--reads-left-today", "460")
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.load(open(os.path.join(TMP, "work", "run.json")))
        self.assertEqual(plan["budget"], 50)          # 460 - 400 floor - 10 retry reserve
        self.assertEqual(sum(plan["targets"].values()), 50)
        self.assertEqual(plan["window_start"], "2026-10-08T09:00:00Z")
        self.assertEqual(plan["history_start"], "2026-10-07T09:00:00Z")
        swept = {t for g in plan["groups"] if g["kind"] == "stock" for t in g["tickers"]}
        for t in plan["core_order"]:
            if br.load_universe().get(t, {}).get("asset_type") == "Stock":
                self.assertIn(t, swept)          # the core (minus megacaps) is folded into the sweep
        for t in plan["megacaps"]:
            self.assertNotIn(t, swept)


class Metrics(unittest.TestCase):
    def test_momentum(self):
        m = br.metrics(IPW)
        self.assertEqual(m["mentions"], 72)
        self.assertEqual(m["prev_day_same_hours"], 1)
        self.assertEqual(m["momentum_5h"], 36.5)
        self.assertEqual(m["last_hour"], 46)
        self.assertTrue(m["spike_last_hour"])
        self.assertEqual(m["burst_idx"], [])      # breakout in the last hour is not down-weighted
        self.assertEqual(m["adjusted"], 72)

    def test_collapsed_burst_is_downweighted(self):
        shifted = [12, 12, 12] + NET[:26]   # puts the 374 spike (then 44) inside the 5h window
        m = br.metrics(shifted)
        self.assertEqual(len(m["burst_idx"]), 1)
        self.assertLess(m["adjusted"], m["mentions"])
        self.assertEqual(m["adjusted"], m["mentions"] - 374 + m["burst_cap"])

    def test_spike_last_hour_needs_rise_vs_prev_9h(self):
        # $TM 2026-10-10: quiet yesterday, ~80/h for 9h, last hour 40 -> above the burst cap, but fading
        tm = [0] * 19 + [60, 90, 110, 95, 80, 85, 75, 70, 70, 40]
        m = br.metrics(tm)
        self.assertLess(m["momentum_1h"], 1)
        self.assertFalse(m["spike_last_hour"])
        rising = [2] * 28 + [60]
        self.assertTrue(br.metrics(rising)["spike_last_hour"])

    def test_last_hour_vs_same_hour_yesterday(self):
        b = [5] * 29
        b[4], b[-1] = 30, 40        # same hour yesterday was also busy (market open)
        m = br.metrics(b)
        self.assertEqual(m["last_hour_prev_day"], 30)
        self.assertAlmostEqual(m["momentum_1h_vs_prev_day"], round(41 / 31, 2))

    def test_flat_has_no_flags(self):
        m = br.metrics(NVDA_FLAT)
        self.assertEqual((m["burst_idx"], m["spike_last_hour"], m["mentions"]), ([], False, 343))

    def test_heat(self):
        self.assertGreater(br.heat(br.metrics(IPW)), br.heat(br.metrics([10] * 29)))


class Records(unittest.TestCase):
    def test_parse_ok_and_total_check(self):
        b = ",".join(map(str, IPW))
        self.assertEqual(br.parse_record("IPW=%d:%s" % (sum(IPW), b)), ("IPW", IPW))
        self.assertEqual(br.parse_record("IPW=" + b)[1], IPW)
        with self.assertRaises(ValueError):
            br.parse_record("IPW=%d:%s" % (sum(IPW) + 1, b))
        with self.assertRaises(ValueError):
            br.parse_record("IPW=1,2,3")

    def test_queries(self):
        coll = br.collisions()
        # the spam suffix goes on EVERY individual count, not only on collision tickers
        self.assertEqual(br.ticker_query("NVDA", coll), "$NVDA " + br.suffix())
        self.assertEqual(br.ticker_query("TM", coll), "$TM " + br.suffix() + " " + coll["TM"])
        for term in ("-whatsapp", "-telegram", '-"stock analyst"', '-"hot stocks"', '-"discussion group"',
                     '-"Ms. Victoria"', '-"investment advisor"', '-"join for free"', "-challenge", "-signals"):
            self.assertIn(term, br.suffix())
        q = br.ticker_query("GM", coll)
        self.assertTrue(q.startswith("$GM " + br.suffix()))
        self.assertIn('-"listing id"', q)
        for t in ("GM", "AI", "U", "STX", "SKY", "PUMP", "S", "W", "SI", "SM"):
            self.assertIn(t, coll)
        self.assertNotIn("#", br.suffix())
        self.assertEqual(br.suffix().count('"') % 2, 0)


class Samples(unittest.TestCase):
    def test_spam_handling(self):
        run_obj = {"samples": {"smp-X": {"posts": [
            {"author": "a1", "text": "$IPW breaking out premarket"},
            {"author": "a2", "text": "watching $IPW and $DKI today"},
            {"author": "a3", "text": "$DKI gap up this morning"},
            {"author": "Yuki_rodds", "text": "$MI great company"},
            {"author": "camila", "text": "$A $B $C $D $E $F $G $H $IPW"},
        ] + [{"author": "t%d" % i, "text": "Heads up $GM Family! time to support the listing, fewer than %d left! go" % i}
             for i in range(4)]}}}
        agg, st = br.analyze_samples(run_obj)
        self.assertEqual(len(agg["IPW"]["authors"]), 2)
        self.assertEqual(len(agg["DKI"]["authors"]), 2)
        self.assertEqual(agg["MI"]["authors"], set())      # spam account: seen, but no clean author
        self.assertEqual(agg["GM"]["authors"], set())
        self.assertEqual((agg["GM"]["total"], agg["GM"]["share"]), (4, 0.0))
        s = st["smp-X"]
        self.assertEqual((s["spam_account"], s["dump"], s["template"], s["kept"]), (1, 1, 4, 3))

    def test_ticker_dump_rule(self):
        cls = lambda text: br.classify_post(text, "x", set(), set(), br.term_patterns())[0]
        self.assertEqual(cls("$NVDA $AMD $TSLA $AAPL #FinTwit #AI https://t.co/x"), "dump")
        self.assertEqual(cls("Have a phenomenal weekend bulls !! $ZYBT $WBUY $WFF $YMAT $NCPL"), "dump")
        self.assertEqual(cls("Top Gainers $WFF $JZ $ZYBT $VEEA $QETA"), "dump")
        # 4 cashtags but real context around them -> clean
        self.assertEqual(cls("Has anyone else looked under the hood of their portfolio lately? 4 of 9 stocks "
                             "carry my returns $AMD $NVDA $NBIS $PLTR"), "clean")
        self.assertEqual(cls("$PLTR closed my option position here off this monster move"), "clean")
        # filter terms as whole words: -ca / -tg must not hit 'can' / 'tgt'
        self.assertEqual(cls("$TGT can still run, cash flow looks fine"), "clean")
        self.assertEqual(cls("I'm your stock analyst! send me your WhatsApp number $TM"), "spam_term")
        self.assertEqual(cls("185x up from my call on $TM https://t.co/x"), "spam_term")
        self.assertEqual(cls("Reply \"Hot Stocks\" to join $TM $AAPL"), "spam_term")

    def test_crypto_context_and_short_posts(self):
        cls = lambda text: br.classify_post(text, "x", set(), set(), br.term_patterns())[0]
        # HODL (VanEck Bitcoin ETF) in the 2026-10-11 06:14 run: memecoin chatter passed as clean
        self.assertEqual(cls("@SolGemsAlert Certainly only a yet unknown one. Or $HODL if you can"), "crypto")
        self.assertEqual(cls("top 3 meme projects on Cardano so far stay together choose $HODL"), "crypto")
        self.assertEqual(cls("13 years of lore. One timeless message. $HODL."), "crypto")
        self.assertEqual(cls("@hodl2013 $hodl gang"), "crypto")
        self.assertEqual(cls("$HODL gang"), "short")
        self.assertEqual(cls("20x on $TM"), "crypto")
        self.assertEqual(cls("$PLTR :-)"), "short")
        self.assertEqual(cls("185x up from the entry on $TM, what a run for everyone"), "crypto")
        # no false positives on normal stock talk
        self.assertEqual(cls("$GME meme stock rally again, volume is huge today and shorts are covering"), "clean")
        self.assertEqual(cls("$NVDA trades at 30x earnings, bitcoin miners like $IREN pivoting to AI"), "clean")

    def test_crypto_detection(self):
        cls = lambda text: br.classify_post(text, "x", set(), set(), br.term_patterns())[0]
        self.assertEqual(cls("$QNT and $XRP are the only ones that matter for banks, accumulating both"), "crypto")
        self.assertEqual(cls("$SOL/USDT long setup here, stop below the range low, target the highs"), "crypto")
        self.assertEqual(cls("best altcoin of the cycle is $QNT, overledger is chain-agnostic tech"), "crypto")
        self.assertEqual(cls("paid out in $USDC, buying more $COIN on this dip because volume is up"), "crypto")
        self.assertEqual(cls("$AAPL services revenue keeps growing, margins look strong into earnings"), "clean")
        self.assertEqual(cls("$JPM chain of branches is expanding, deposits up again this quarter"), "clean")
        self.assertIn("QNT", br.read_list("exclude_tickers.txt"))

    def test_own_sample_tallies(self):
        run_obj = {"samples": {
            "smp-o-NVDA": {"posts": [{"author": "a%d" % i, "text": "$NVDA holding into earnings, %s" % w}
                                     for i, w in enumerate(["guidance matters", "margins first", "datacenter demand",
                                                            "watching China", "options are pricey", "adding a bit",
                                                            "trimmed some", "long term hold"])] + [{"author": "d", "text": "$NVDA $AMD $TSLA $AAPL $META"}]},
            "smp-c-NVDA": {"posts": [{"author": "z", "text": "$NVDA $AMD $TSLA $AAPL $MSFT"}]}}}
        agg, _ = br.analyze_samples(run_obj)
        self.assertEqual((agg["NVDA"]["own_total"], agg["NVDA"]["own_clean"], agg["NVDA"]["total"]), (9, 8, 10))
        self.assertEqual(agg["AMD"]["own_total"], 0)

    def test_clean_share_estimate(self):
        from datetime import datetime, timezone
        end = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
        state = {"own_clean": {"NVDA": [{"at": "2026-10-12T23:00:00Z", "n": 25, "clean": 24},
                                        {"at": "2026-10-09T00:00:00Z", "n": 25, "clean": 0}]}}
        est, n = br.own_clean_estimate(state, "NVDA", end)     # 3+ day old evidence ignored
        self.assertEqual(n, 25)
        self.assertAlmostEqual(est, (24 + 10 * 0.8) / 35, places=3)
        self.assertEqual(br.own_clean_estimate(state, "XYZ", end), (0.8, 0))
        run_obj = {"window_end": "2026-10-13T04:00:00Z",
                   "picks": {"samples2": [{"key": "smp-o-MU", "kind": "own", "target": "MU"},
                                          {"key": "smp-o-GME", "kind": "own", "target": "GME"}]},
                   "samples": {"smp-o-MU": {}}}
        br.update_own_clean(state, run_obj, {"MU": {"own_total": 20, "own_clean": 10}})
        self.assertEqual(state["own_clean"]["MU"], [{"at": "2026-10-13T04:00:00Z", "n": 20, "clean": 10}])
        self.assertNotIn("GME", state["own_clean"])           # not recorded -> no evidence
        self.assertEqual(len(state["own_clean"]["NVDA"]), 1)  # pruned
        self.assertEqual(state["own_clean_prior"], 0.8)       # < 50 own posts pooled -> default

    def test_sample_query_length_cap(self):
        u = sorted(br.load_universe())[:400]
        q = br.sample_query(u)
        self.assertLessEqual(len(q), br.SAMPLE_MAX_QUERY_LEN)
        self.assertLess(br.SAMPLE_MAX_QUERY_LEN, 4096)
        with self.assertRaises(ValueError):
            br.sample_args({"window_end": "2026-10-13T04:00:00Z"}, "x" * 4096)

    def test_group_heat_is_relative_to_market(self):
        # every group quieter than yesterday (weekend evening): old heat was 0.0 for all
        ms = [br.metrics([200] * 5 + [100] * 19 + [b] * 5) for b in (50, 60, 90)]
        r5, r1 = br.market_ratios(ms)
        heats = [br.heat(m, r5, r1) for m in ms]
        self.assertEqual([br.heat(m) for m in ms], [0.0, 0.0, 0.0])
        self.assertEqual(heats[0], 0.0)
        self.assertGreater(heats[2], heats[1])
        self.assertGreater(heats[2], 0)

    def test_clean_gate(self):
        ok = {"authors": {"a", "b", "c"}, "posts": 3, "total": 4, "share": 0.75}
        self.assertTrue(br.clean_ok(ok))
        self.assertFalse(br.clean_ok({**ok, "authors": {"a", "b"}}))            # < 3 distinct clean authors
        self.assertFalse(br.clean_ok({"authors": {"a", "b", "c"}, "posts": 3, "total": 25, "share": 0.12}))
        self.assertTrue(br.clean_ok({"authors": {"a", "b", "c"}, "posts": 3, "total": 3, "share": 1.0}))

    def test_parse_sample_json(self):
        obj = {"data": [{"author_id": "1", "text": "$IPW go"}], "includes": {"users": [{"id": "1", "username": "bob"}]}}
        self.assertEqual(br.parse_sample_json(obj)[0]["author"], "bob")


class Migration(unittest.TestCase):
    def test_v1_state_loads(self):
        p = os.path.join(TMP, "data", "state.json")
        json.dump({"tickers": {"AAPL": {"mentions": 1, "window_end": "2026-10-08T09:00:00Z",
                                        "measured_at": "2026-10-08T09:18:54Z"}}}, open(p, "w"))
        s = br.load_state()
        self.assertEqual((s["etf_rotation_next"], s["risers"], s["last_run_noncore"]), (0, [], []))
        lp = os.path.join(TMP, "data", "latest.json")
        keep = open(lp).read()
        self.addCleanup(lambda: open(lp, "w").write(keep))
        json.dump({"window_end": "2026-10-08T09:00:00Z",          # a v1 latest.json with a non-core ticker
                   "rankings": [{"ticker": "AAPL", "mentions": 1, "tier": "core"},
                                {"ticker": "RGTI", "mentions": 40, "tier": "hot"}]}, open(lp, "w"))
        r = run("migrate")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.load(open(p))
        self.assertEqual(s["method_version"], br.METHOD_VERSION)
        self.assertTrue(s["last_run_noncore"])   # seeded from the v1 latest.json

    def test_check_command(self):
        r = run("check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("check OK", r.stdout)


Z29 = ",".join(["10"] * 29)


def runjson():
    return json.load(open(os.path.join(TMP, "work", "run.json")))


def reset_data():
    for f in ("splits.json", "hourly.json", "today.json"):
        p = os.path.join(TMP, "data", f)
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(os.path.join(TMP, "data", "daily"), ignore_errors=True)
    shutil.copy(os.path.join(REPO, "data", "state.json"), os.path.join(TMP, "data", "state.json"))


class RunFlow(unittest.TestCase):
    """Sequential-run mechanics: one call at a time, 429 handling, stop rules, partial build, archive."""

    def setUp(self):
        reset_data()

    def plan(self, left="900"):
        r = run("plan", "--now", "2026-10-09T09:14:00Z", "--reads-left-today", left)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def record_groups(self, skip=()):
        keys = [g["key"] for g in runjson()["groups"] if g["key"] not in skip]
        r = run("record", *["%s=290:%s" % (k, Z29) for k in keys], "--reads-left-today", "880", "--no-sleep")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def do_samples(self):
        for _ in range(2):
            run("pick-samples")
            for p in (runjson()["picks"]["samples1"] or []) + (runjson()["picks"]["samples2"] or []):
                if p["key"] not in runjson()["samples"]:
                    run("record-sample", p["key"], "--failed", "--no-sleep")

    def test_calls_prints_one_call(self):
        self.plan()
        r = run("calls", "groups")
        self.assertEqual(r.stdout.count("## "), 1)
        self.assertIn("one at a time", r.stdout)
        r = run("next-call")
        self.assertEqual(r.stdout.count("## "), 1)
        self.assertIn("record S01=", r.stdout)
        r = run("calls", "groups", "--all")
        self.assertIn("REVIEW ONLY", r.stdout)

    def test_429_sequence_and_skip(self):
        self.plan()
        r = run("record-error", "S01", "--kind", "429", "--reads-left-today", "899")
        self.assertIn("WAIT 90s", r.stdout)
        r = run("record-error", "S01", "--kind", "429")
        self.assertIn("WAIT 240s", r.stdout)
        r = run("record-error", "S01", "--kind", "429")
        self.assertIn("GIVE UP", r.stdout)
        rj = runjson()
        self.assertIn("S01", rj["skipped"])
        self.assertEqual((rj["log"]["retries_429"], rj["log"]["errors_other"], rj["log"]["extra_reads"]), (3, 0, 3))
        self.assertNotIn("S01", run("next-call").stdout.split("\n")[0])
        r = run("record-error", "S01", "--kind", "429")
        self.assertNotEqual(r.returncode, 0)          # no 4th attempt on a skipped call

    def test_other_errors_retry_once_and_stop_at_5(self):
        self.plan()
        r = run("record-error", "S01", "--kind", "other")
        self.assertIn("RETRY", r.stdout)
        r = run("record-error", "S01", "--kind", "other")
        self.assertIn("GIVE UP", r.stdout)
        for k in ("S02", "S03"):
            run("record-error", k, "--kind", "other")
        r = run("record-error", "S04", "--kind", "other")
        self.assertIn("STOP: 5 errors other than 429/5xx", r.stdout)
        self.assertIn("cannot build", r.stdout)
        self.assertIn("STOPPED", run("next-call").stdout)

    def test_429s_do_not_count_as_errors_but_reserve_stops(self):
        self.plan()
        keys = [g["key"] for g in runjson()["groups"]]
        out = ""
        for k in keys[:5]:
            out = run("record-error", k, "--kind", "429").stdout
            self.assertNotIn("STOP", out)
            out = run("record-error", k, "--kind", "429").stdout
        self.assertIn("STOP: retry reserve of 10 reads used up", out)

    def test_budget_keeps_retry_reserve(self):
        r = run("plan", "--now", "2026-10-09T09:14:00Z", "--reads-left-today", "470")
        self.assertEqual(runjson()["budget"], 60)    # 470 - 400 floor - 10 retry reserve
        r = run("plan", "--now", "2026-10-09T09:14:00Z", "--reads-left-today", "425")
        self.assertEqual(r.returncode, 3)

    def test_abandon_archives(self):
        self.plan()
        os.makedirs(os.path.join(TMP, "work", "samples"), exist_ok=True)
        r = run("abandon", "--reason", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(TMP, "work", "run.json")))
        arch = [a for a in os.listdir(os.path.join(TMP, "work", "archive")) if a.endswith("-abandoned")]
        self.assertTrue(arch)
        a = json.load(open(os.path.join(TMP, "work", "archive", arch[-1], "run.json")))
        self.assertEqual(a["abandoned"]["reason"], "test")


class NS(dict):
    __getattr__ = dict.get


class Sim:
    """Fake X: per-ticker hourly series; a group/node count is the sum over its members (overlap: posts
    that mention two given tickers at once, counted once in a union)."""

    def __init__(self, series, clean_posts=(), spam_posts=(), overlap=None):
        self.series = series
        self.clean = set(clean_posts)
        self.spam = set(spam_posts)
        self.overlap = overlap or {}      # (A, B) -> per-hour posts mentioning both
        self.reads = 0
        self.dollar_removed = "yes"       # bare mode probe answer

    def tickers(self, q):
        q0 = q.split(" -is:retweet")[0]
        if q0.startswith("("):
            return [x.lstrip("$") for x in q0[1:q0.index(")")].split(" OR ")]
        return [q0.split()[0].lstrip("$")]          # single ticker (bare context query: "META (stock OR ...)")

    def count(self, q):
        self.reads += 1
        tk = self.tickers(q)
        b = [0] * 29
        for t in tk:
            for i, v in enumerate(self.series.get(t, [0] * 29)):
                b[i] += v
        for (x, y), per_h in self.overlap.items():
            if x in tk and y in tk:
                b = [max(0, v - per_h) for v in b]
        return b

    def posts(self, q):
        self.reads += 1
        out = []
        for t in self.tickers(q):
            if t in self.clean:
                notes = ["the chart finally cleared resistance on volume", "earnings call tomorrow, holding into it",
                         "guidance raise looks real, adding on dips", "short interest is high, watching the open",
                         "new contract announced this morning, big if true"]
                out += ["trader%s%d\tadded more $%s today, %s" % (t, i, t, notes[i]) for i in range(5)]
            if t in self.spam:
                out += ["memeguru%d\t%dx profit from my call on $%s https://t.co/x" % (i, 20 + i, t) for i in range(6)]
        return out[:25]


def drive(sim, now, left=900, until=None, max_steps=500):
    import contextlib
    import io as _io
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        br.cmd_plan(NS(now=now, reads_left_today=left))
        for _ in range(max_steps):
            r = br.load_run()
            kind, payload = br.next_action(r)
            br.save_run(r)
            if kind == until:
                return kind
            if kind == "call":
                stage, calls = payload
                key, tool, a = calls[0]
                if tool == "get_posts_counts_recent":
                    b = sim.count(a["query"])
                    br.cmd_record(NS(pairs=["%s=%d:%s" % (key, sum(b), ",".join(map(str, b)))], no_sleep=True,
                                     extra_reads=0, dollar_removed=sim.dollar_removed if key == "PROBE" else None))
                else:
                    lines = sim.posts(a["query"])
                    f = os.path.join(TMP, "lines.txt")
                    open(f, "w").write("\n".join(lines) + "\n")
                    br.cmd_record_sample(NS(key=key, lines=f if lines else None, failed=not lines, no_sleep=True))
            elif kind == "pick-samples":
                br.cmd_pick_samples(NS())
            elif kind == "pick-candidates":
                br.cmd_pick_candidates(NS())
            elif kind == "build":
                br.cmd_build(NS(extra_reads=0))
                return "built"
            else:
                return kind
    raise AssertionError("run did not finish")


def latest():
    return json.load(open(os.path.join(TMP, "data", "latest.json")))


def today():
    return json.load(open(os.path.join(TMP, "data", "today.json")))


class SplitFlow(unittest.TestCase):
    def setUp(self):
        reset_data()
        u = br.load_universe()
        core = set(br.core_list())
        stocks = sorted(t for t, r in u.items() if r["asset_type"] == "Stock" and t not in core
                        and t not in set(br.read_list("exclude_tickers.txt")) and t not in br.collisions())
        self.hot = stocks[len(stocks) // 3]          # a non-core stock in the middle of some group
        self.warm = stocks[2 * len(stocks) // 3]
        self.series = {self.hot: [2] * 24 + [10, 20, 30, 40, 50], self.warm: [3] * 29, "NVDA": [60] * 29}

    def test_splits_drill_down_and_subtract(self):
        sim = Sim(self.series, clean_posts=[self.hot])
        self.assertEqual(drive(sim, "2026-10-09T09:14:00Z"), "built")
        rj = runjson()
        out = latest()
        self.assertLessEqual(sim.reads, 95)
        self.assertEqual(out["x_reads_used"], sim.reads)
        self.assertEqual(out["x_reads_breakdown"]["split_counts"], br.split_reads_planned(rj))
        self.assertLessEqual(br.split_reads_planned(rj), 24)
        # drilled down to a leaf containing the hot ticker, which got an exact count and is ranked
        picks = {p["ticker"]: p for p in rj["picks"]["individual"]}
        self.assertEqual(picks[self.hot]["source"], "split-leaf")
        ranked = {e["ticker"]: e for e in out["rankings"]}
        self.assertEqual(ranked[self.hot]["mentions"], 150)
        self.assertIn("last_measured", ranked[self.hot])
        # subtraction: inferred parts equal the truth when posts do not overlap
        for op in rj["splits"]:
            if op.get("infer") and op.get("inferred_done") and not op.get("infer_failed"):
                inf = rj["counts"][op["infer"]]["buckets"]
                self.assertEqual(inf, sim.count(br.group_query(rj["tree"]["nodes"][op["infer"]]["tickers"])))
        # persistent tree saved and valid
        tree = json.load(open(os.path.join(TMP, "data", "splits.json")))
        br.validate_tree(tree)
        self.assertGreater(len(tree["nodes"]), 27)
        # daily aggregate
        td = {e["ticker"]: e for e in today()["tickers"]}
        self.assertEqual(td[self.hot]["status"], "exact")
        self.assertEqual(td[self.hot]["count"], sum(self.series[self.hot][-9:]))   # 00:00-09:00 UTC
        self.assertFalse(td[self.hot]["held_back"])
        self.assertIn("2026-10-09T09:00:00Z", td[self.hot]["windows_5h"])
        self.assertEqual(td["NVDA"]["tier"], "megacap")

    def test_tree_persists_then_prunes(self):
        sim = Sim(self.series, clean_posts=[self.hot])
        drive(sim, "2026-10-09T09:14:00Z")
        n1 = len(json.load(open(os.path.join(TMP, "data", "splits.json")))["nodes"])
        sim2 = Sim(self.series, clean_posts=[self.hot])
        drive(sim2, "2026-10-09T14:14:00Z", until="pick-samples")
        rj = runjson()
        # carried-over nodes are stale: splitting one costs 4 reads, a fresh root 3
        self.assertGreaterEqual(len(rj["tree"]["nodes"]), n1)
        self.assertLessEqual(br.split_reads_planned(rj), 24)
        for op in rj["splits"]:
            fresh = op["node"] in rj["counts"] and not rj["counts"][op["node"]].get("inferred")
            self.assertEqual(len(op["measure"]), 3 if fresh else 4)
        br.archive_run("test")
        # 2 days later nothing was re-measured -> pruned back to the roots
        import contextlib
        import io as _io
        with contextlib.redirect_stdout(_io.StringIO()):
            br.cmd_plan(NS(now="2026-10-11T14:14:00Z", reads_left_today=900))
        rj = runjson()
        self.assertTrue(all(n["parent"] is None for n in rj["tree"]["nodes"].values()))
        self.assertTrue(rj["tree_prune_notes"])

    def test_next_and_next_call_name_the_split_part(self):
        run("plan", "--now", "2026-10-09T09:14:00Z", "--reads-left-today", "900")
        hot = ",".join(["10"] * 24 + ["200"] * 5)
        for g in runjson()["groups"]:
            run("record", "%s=%s" % (g["key"], hot if g["key"] == "S03" else Z29), "--no-sleep")
        for t in runjson()["megacaps"]:
            run("record", "%s=%s" % (t, Z29), "--no-sleep")
        r = run("next-call")
        self.assertEqual(r.stdout.count("## "), 1)
        self.assertIn("## S03.1 ", r.stdout)
        self.assertIn("record S03.1=", r.stdout)
        run("record", "S03.1=" + hot, "--no-sleep")
        self.assertIn("splits calls pending (2), next S03.2", run("next").stdout)
        self.assertNotEqual(run("record", "S03.1=" + hot, "--no-sleep").returncode, 0)   # no double record
        self.assertNotEqual(run("record", "S09.1=" + hot, "--no-sleep").returncode, 0)   # not a planned part

    def test_fail_closed_on_inconsistent_split_state(self):
        drive(Sim(self.series, clean_posts=[self.hot]), "2026-10-09T09:14:00Z")
        p = os.path.join(TMP, "data", "splits.json")
        tree = json.load(open(p))
        child = next(k for k, n in tree["nodes"].items() if n["parent"])
        tree["nodes"][child]["tickers"] = tree["nodes"][child]["tickers"][1:]    # breaks the partition
        json.dump(tree, open(p, "w"))
        r = run("plan", "--now", "2026-10-09T14:14:00Z", "--reads-left-today", "900")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("fail-closed", r.stdout)
        self.assertEqual(run("check").returncode, 1)
        self.assertEqual(run("splits-reset").returncode, 0)
        r = run("plan", "--now", "2026-10-09T14:14:00Z", "--reads-left-today", "900")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_hot_reread_and_partial_day_bounds(self):
        drive(Sim(self.series, clean_posts=[self.hot]), "2026-10-09T09:14:00Z")
        first = {p["ticker"] for p in runjson()["picks"]["individual"]}
        later = dict(self.series)
        later[self.hot] = [2] * 19 + [10, 20, 30, 40, 50] + [50] * 5
        drive(Sim(later, clean_posts=[self.hot]), "2026-10-09T14:14:00Z")
        picks = {p["ticker"]: p for p in runjson()["picks"]["individual"]}
        self.assertEqual(picks[self.hot]["source"], "hot-reread")
        quiet = [t for t in first if t != self.hot and t not in picks]
        self.assertTrue(quiet)                         # measured once today, quiet -> not re-read
        td = {e["ticker"]: e for e in today()["tickers"]}
        self.assertEqual(td[self.hot]["status"], "exact")
        for t in sorted(quiet):
            self.assertEqual(td[t]["status"], "partial")
            self.assertEqual(td[t]["exact_hours"], 9)
        bounded = [td[t] for t in sorted(quiet) if td[t]["upper_bound"] is not None]
        self.assertTrue(bounded)                       # remaining hours bounded by a group/node
        for e in bounded:
            self.assertGreaterEqual(e["upper_bound"], e["exact_part"])

    def test_clean_adjusted_ranking_and_own_samples(self):
        drive(Sim(self.series, clean_posts=[self.hot]), "2026-10-09T09:14:00Z")
        rj = runjson()
        own = [p for p in rj["picks"]["samples2"] if p["kind"] == "own"]
        self.assertGreaterEqual(len(own), br.OWN_SAMPLES)
        self.assertTrue(all(p["key"] == "smp-o-" + p["target"] and p["sort"] == "recency" for p in own))
        lt = latest()
        rows = lt["tickers"] if "tickers" in lt else lt["rankings"]
        for e in rows:
            self.assertIn("clean_share_est", e)
            self.assertEqual(e["rank_score"], int(round(e["rank_score_raw"] * e["clean_share_est"])))
        self.assertEqual([e["rank_score"] for e in rows], sorted((e["rank_score"] for e in rows), reverse=True))

    def test_partial_build_needs_sweep_and_megacaps(self):
        sim = Sim(self.series)
        self.assertEqual(drive(sim, "2026-10-09T09:14:00Z", until="pick-samples"), "pick-samples")
        r = run("build")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PARTIAL build", r.stdout)
        out = latest()
        self.assertTrue(out["partial"])
        self.assertEqual({e["ticker"] for e in out["rankings"]} - set(br.megacap_list()), set())
        run("plan", "--now", "2026-10-09T14:14:00Z", "--reads-left-today", "900")
        rj = runjson()
        r = run("record", *["%s=%s" % (g["key"], Z29) for g in rj["groups"]], "--no-sleep")
        r = run("build")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("megacaps not fully counted", r.stderr)

    def test_clean_gate_in_flow(self):
        """Memecoin/template chatter is not discovered; a split leaf without clean sample evidence is held back."""
        series = dict(self.series)
        series["TM"] = [40] * 29
        sim = Sim(series, clean_posts=[], spam_posts=["TM", self.hot])
        drive(sim, "2026-10-09T09:14:00Z")
        out = latest()
        ranked = {e["ticker"] for e in out["rankings"]}
        self.assertNotIn("TM", ranked)
        self.assertNotIn(self.hot, ranked)
        self.assertIn("TM", [h["ticker"] for h in out["held_back"]])     # found by splits, sample is spam
        td = {e["ticker"]: e for e in today()["tickers"]}
        self.assertTrue(td["TM"]["held_back"])
        self.assertIn("clean", td["TM"]["held_back_reason"])
        for p in runjson()["picks"]["individual"]:
            self.assertTrue(p["query"].startswith("$%s %s" % (p["ticker"], br.suffix())))


class PartialAndTally(unittest.TestCase):
    def setUp(self):
        reset_data()

    def test_missing_etf_group_still_publishes_and_is_requeued(self):
        sim = Sim({"NVDA": [60] * 29})
        self.assertEqual(drive(sim, "2026-10-12T09:14:00Z", until="pick-samples"), "pick-samples")
        rj = runjson()
        etf = [g["key"] for g in rj["groups"] if g["kind"] == "etf"]
        # pretend the last ETF group failed 3x 429: rebuild a run where it is skipped
        run("plan", "--now", "2026-10-12T09:14:00Z", "--reads-left-today", "900")
        rj = runjson()
        etf = [g["key"] for g in rj["groups"] if g["kind"] == "etf"]
        for g in rj["groups"]:
            if g["key"] != etf[1]:
                run("record", "%s=%s" % (g["key"], Z29), "--no-sleep")
        for k in ("429", "429", "429"):
            run("record-error", etf[1], "--kind", k)
        self.assertIn(etf[1], runjson()["skipped"])
        for t in runjson()["megacaps"]:
            run("record", "%s=%s" % (t, Z29), "--no-sleep")
        r = run("build")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = latest()
        self.assertTrue(out["partial"])
        self.assertIn(etf[1], out["partial_reason"])
        self.assertEqual(out["missing_calls"]["etf_groups"], [etf[1]])
        self.assertEqual(out["group_sweep"]["etf_groups_queued_for_next_run"], [etf[1]])
        self.assertTrue(any(etf[1] in n for n in today()["notes"]))
        run("plan", "--now", "2026-10-12T14:14:00Z", "--reads-left-today", "800")
        rj = runjson()
        nxt = [g["key"] for g in rj["groups"] if g["kind"] == "etf"]
        self.assertEqual(nxt[0], etf[1])                 # requeued first
        self.assertEqual(len(nxt), 3)
        self.assertEqual(len(set(nxt)), 3)

    def test_missing_stock_group_refuses(self):
        run("plan", "--now", "2026-10-12T09:14:00Z", "--reads-left-today", "900")
        rj = runjson()
        for g in rj["groups"]:
            if g["key"] != "S02":
                run("record", "%s=%s" % (g["key"], Z29), "--no-sleep")
        for t in rj["megacaps"]:
            run("record", "%s=%s" % (t, Z29), "--no-sleep")
        r = run("build")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stock group sweep incomplete: S02", r.stderr)

    def test_unrecorded_retries_are_tallied(self):
        run("plan", "--now", "2026-10-12T09:14:00Z", "--reads-left-today", "900")
        run("record", "S01=" + Z29, "--reads-left-today", "899", "--no-sleep")
        r = run("record", "S02=" + Z29, "--reads-left-today", "896", "--no-sleep")   # 2 retries not recorded
        self.assertIn("2 unrecorded", r.stdout)
        lg = runjson()["log"]
        self.assertEqual((lg["extra_reads"], lg["unrecorded_reads"]), (2, 2))
        run("record-error", "S03", "--kind", "429", "--reads-left-today", "895")
        run("record", "S03=" + Z29, "--reads-left-today", "894", "--no-sleep")
        lg = runjson()["log"]
        self.assertEqual((lg["extra_reads"], lg["unrecorded_reads"], lg["retries_429"]), (3, 2, 1))


class Subtraction(unittest.TestCase):
    def test_overlap_flag_and_clamp(self):
        r = {"counts": {"P": {"buckets": [100] * 29}, "P.1": {"buckets": [40] * 29}, "P.2": {"buckets": [40] * 29},
                        "P.3": {"buckets": [30] * 29}},
             "splits": [{"node": "P", "children": ["P.1", "P.2", "P.3", "P.4"], "measure": ["P.1", "P.2", "P.3"],
                         "infer": "P.4"}], "skipped": []}
        notes = br.finalize_splits(r)
        self.assertEqual(r["counts"]["P.4"]["buckets"], [0] * 29)
        self.assertTrue(r["counts"]["P.4"]["inferred"])
        self.assertTrue(r["splits"][0]["overlap_warning"])
        self.assertTrue(notes)

    def test_validate_tree(self):
        t = {"nodes": {"S01": {"tickers": ["A", "B", "C", "D"], "parent": None, "children": ["S01.1", "S01.2"]},
                       "S01.1": {"tickers": ["A", "B"], "parent": "S01", "children": []},
                       "S01.2": {"tickers": ["C", "D"], "parent": "S01", "children": []}}}
        self.assertTrue(br.validate_tree(t))
        t["nodes"]["S01.2"]["tickers"] = ["C", "E"]
        with self.assertRaises(br.SplitStateError):
            br.validate_tree(t)
        t["nodes"]["S01.2"]["tickers"] = ["C", "D"]
        t["nodes"]["S01.2"]["parent"] = "S02"
        with self.assertRaises(br.SplitStateError):
            br.validate_tree(t)

    def test_split_tickers(self):
        self.assertEqual([len(x) for x in br.split_tickers(list("ABCDEFGHIJ"))], [3, 3, 2, 2])
        self.assertEqual(br.split_tickers(list("AB")), [["A"], ["B"]])

class BareMode(unittest.TestCase):
    """Method v4: the x tools strip '$' since 2026-10-17 -> bare-ticker queries, ambiguity rules, probe."""

    def setUp(self):
        reset_data()
        set_mode("bare")

    def tearDown(self):
        set_mode("cashtag")
        shutil.copy(os.path.join(REPO, "data", "latest.json"), os.path.join(TMP, "data", "latest.json"))

    def test_queries_have_no_dollar_and_skip_ambiguous(self):
        self.assertTrue(br.ticker_query("NVDA").startswith("NVDA -is:retweet"))
        q = br.ticker_query("META")
        self.assertTrue(q.startswith("META (stock OR stocks OR shares"), q)
        self.assertNotIn("$", br.group_query(["AAPL", "NVDA"]).split(" -is:")[0])
        nm = br.not_measurable()
        for t in ("A", "ALL", "NOW", "ON", "IT", "ARE", "CAN", "FUN", "GO", "CAR", "SO", "OPEN", "AI"):
            self.assertIn(t, nm, t)
        for t in ("NVDA", "AAPL", "TSLA", "AMD", "PLTR", "IREN"):
            self.assertNotIn(t, br.ambiguous_tickers(), t)
        stock, etf, st, et = br.current_packing(br.load_universe())
        swept = set(st) | set(et)
        self.assertFalse(swept & set(nm))
        self.assertFalse(swept & set(br.context_tickers()))
        for g in stock + etf:
            self.assertLessEqual(len(br.group_query(g)), br.MAX_QUERY_LEN)
        mega = br.megacap_list()
        self.assertEqual(mega[:10], br.read_list("megacaps.txt")[:10])
        self.assertIn("MU", mega)                       # context ticker: counted individually every run
        self.assertEqual(run("check").returncode, 0)

    def test_real_mention_rule(self):
        self.assertTrue(br.real_mention("$META to 800 next week", "META", ["META"]))
        self.assertTrue(br.real_mention("META calls ripping into earnings", "META", []))
        self.assertFalse(br.real_mention("meta calls ripping into earnings", "META", []))   # context: upper case
        self.assertFalse(br.real_mention("the META is broken in this game", "META", []))
        self.assertTrue(br.real_mention("nvda earnings next week, holding shares", "NVDA", []))
        self.assertTrue(br.real_mention("NVDA and $AMD both up big today", "NVDA", ["AMD"]))
        self.assertFalse(br.real_mention("NVDA keynote was fun to watch tonight", "NVDA", []))

    def test_own_sample_denominator_is_every_post(self):
        posts = ["META stock is up again after earnings, adding shares", "the meta in this game is so boring now",
                 "Meta should fix my instagram account already please", "$META breaking out over 760 resistance"]
        run_obj = {"samples": {"smp-o-META": {"posts": [{"author": "u%d" % i, "text": t}
                                                         for i, t in enumerate(posts)]}}}
        agg, st = br.analyze_samples(run_obj)
        self.assertEqual((agg["META"]["own_total"], agg["META"]["own_clean"]), (4, 2))
        self.assertEqual(st["smp-o-META"]["own_real"], 2)

    def test_probe_switches_back_to_cashtag_when_dollar_works(self):
        self.assertEqual(run("plan", "--now", "2026-10-17T09:14:00Z", "--reads-left-today", "900").returncode, 0)
        r = run("next-call")
        self.assertIn("## PROBE", r.stdout)
        self.assertIn('"query": "$NVDA -is:retweet"', r.stdout)
        b = ",".join(["5"] * 29)
        r = run("record", "PROBE=145:" + b, "--no-sleep")
        self.assertNotEqual(r.returncode, 0)            # the answer to "was the $ removed?" is required
        r = run("record", "PROBE=145:" + b, "--dollar-removed", "yes", "--no-sleep")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(br.query_mode(), "bare")
        self.assertIn("## S01", run("next-call").stdout)
        # next run: the tool no longer says the $ was removed -> cashtag again, automatically
        run("plan", "--now", "2026-10-17T14:14:00Z", "--reads-left-today", "800")
        r = run("record", "PROBE=145:" + b, "--dollar-removed", "no", "--no-sleep")
        self.assertIn("MODE SWITCH: bare -> cashtag", r.stdout)
        self.assertEqual(br.query_mode(), "cashtag")
        self.assertIsNotNone(runjson()["stopped"])
        run("abandon", "--reason", "query mode switched to cashtag")
        run("plan", "--now", "2026-10-17T14:14:00Z", "--reads-left-today", "799")
        rj = runjson()
        self.assertFalse(rj["probe"])
        self.assertTrue(rj["groups"][0]["query"].startswith("($"))
        # and if the $ is stripped again while in cashtag mode -> bare
        r = run("record-error", "S01", "--kind", "other", "--dollar-removed", "yes", "--detail", "$ was removed")
        self.assertIn("MODE SWITCH: cashtag -> bare", r.stdout)
        self.assertEqual(br.query_mode(), "bare")

    def test_bare_flow_publishes_with_method_note(self):
        series = {"NVDA": [60] * 29, "META": [40] * 29, "MU": [30] * 29, "AAPL": [20] * 29}
        self.assertEqual(drive(Sim(series, clean_posts=["NVDA", "META"]), "2026-10-17T09:14:00Z"), "built")
        lt = latest()
        self.assertEqual(lt["query_mode"], "bare")
        self.assertEqual(lt["method_version"], 4)
        self.assertIn("2026-10-17", lt["method_note"]["counting_changed"])
        self.assertIn("not directly comparable", lt["method_note"]["counting_changed"].lower())
        self.assertGreater(lt["not_measurable"]["count"], 1000)
        self.assertEqual(lt["x_reads_breakdown"]["mode_probe"], 1)
        rows = {e["ticker"]: e for e in lt["rankings"]}
        self.assertIn("context-required", rows["META"]["labels"])
        self.assertFalse(set(rows) & set(br.not_measurable()))
        self.assertEqual(today()["method_note"]["query_mode"], "bare")
        st = json.load(open(os.path.join(TMP, "data", "state.json")))
        self.assertEqual(st["own_clean_mode"], "bare")
        self.assertEqual(json.load(open(os.path.join(TMP, "data", "splits.json")))["query_mode"], "bare")


class CollisionGuard(unittest.TestCase):
    """Bare mode: bare counts far above the $cashtag history are held back as suspected word collisions."""

    def setUp(self):
        reset_data()
        set_mode("bare")
        br._CASHTAG_BASE = None

    def tearDown(self):
        set_mode("cashtag")
        br._CASHTAG_BASE = None

    def test_thresholds_on_the_1117_run(self):
        # 2026-10-17 11:17 Berlin run (04-09Z), bare 5h counts; baseline = repo $cashtag history before 10-17
        held = {"SHOP": 10901, "BABA": 6669, "MARA": 1783, "NIO": 353, "CRM": 296, "PDD": 140, "SOFI": 1760}
        kept = {"TSLA": 1153, "AMD": 965, "NVDA": 801, "QQQ": 596, "CRWV": 368, "IREN": 329, "NBIS": 302,
                "AAPL": 301, "MSTR": 271, "ASML": 246, "XOM": 126}
        for t, m in held.items():
            self.assertIsNotNone(br.collision_trigger(t, m), t)
        for t, m in kept.items():
            self.assertIsNone(br.collision_trigger(t, m), t)
        self.assertIn("no $cashtag history", br.collision_trigger("HAIN", 8028))
        self.assertIsNone(br.collision_trigger("HASI", 152))
        for t in ("SHOP", "BABA", "MARA", "NIO", "CRM", "PDD", "SOFI"):
            self.assertIn(t, br.not_measurable(), t)

    def test_small_counts_and_bare_history_are_ignored(self):
        br._CASHTAG_BASE = {"TINY": (2, 10)}
        self.assertIsNone(br.collision_trigger("TINY", 40))          # 20x, but below the minimum bare count
        self.assertIsNotNone(br.collision_trigger("TINY", 60))
        br._CASHTAG_BASE = None
        base = br.cashtag_baseline()
        # the bare-rewritten daily/2026-10-16.json and the bare history files never count as $cashtag history
        self.assertLess(base["SOFI"][0], 1000)

    def test_own_sample_clears_or_confirms(self):
        end = br.parse_iso("2026-10-17T14:00:00Z")
        br._CASHTAG_BASE = {"ZZZQ": (10, 20)}
        st = {"own_clean_mode": "bare", "own_clean": {}}
        d = br.collision_decision(st, "ZZZQ", 500, end)
        self.assertEqual((d["label"], d["queue"]), ("suspected-word-collision", True))
        st["own_clean"]["ZZZQ"] = [{"at": "2026-10-17T09:00:00Z", "n": 25, "clean": 4}]
        d = br.collision_decision(st, "ZZZQ", 500, end)
        self.assertEqual((d["label"], d["queue"]), ("suspected-word-collision", False))
        self.assertIn("16% real", d["held"])
        st["own_clean"]["ZZZQ"] = [{"at": "2026-10-17T09:00:00Z", "n": 25, "clean": 9}]
        d = br.collision_decision(st, "ZZZQ", 500, end)
        self.assertEqual(d["label"], "collision-check-passed")
        self.assertIsNone(d["held"])
        self.assertIsNone(br.collision_decision(st, "ZZZQ", 70, end))

    def test_queue_gets_own_samples_first(self):
        run_obj = {"window_end": "2026-10-17T09:00:00Z", "megacaps": ["NVDA", "TSLA", "QQQ"]}
        hourly = {"tickers": {}}
        st = {"tickers": {}, "own_clean_mode": "bare", "own_clean": {},
              "collision_queue": [{"ticker": t} for t in ("HASI", "HAPN", "HAYW", "SHOP")]}
        own = br.own_sample_targets(run_obj, hourly, st, 6)
        self.assertEqual(own[:3], ["HASI", "HAPN", "HAYW"])          # SHOP: not measurable, never sampled
        self.assertEqual(len(own), 6)
        self.assertNotIn("SHOP", own)


class Oct17Fixes(unittest.TestCase):
    """2026-10-17 16:21 run: SOFI leak (not-measurable ticker still counted/published), 503 handling, shorter groups."""

    def setUp(self):
        reset_data()

    def tearDown(self):
        set_mode("cashtag")
        shutil.copy(os.path.join(REPO, "data", "latest.json"), os.path.join(TMP, "data", "latest.json"))

    def plan(self):
        r = run("plan", "--now", "2026-10-09T09:14:00Z", "--reads-left-today", "900")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_503_waits_20s_retries_once(self):
        self.plan()
        r = run("record-error", "S01", "--kind", "other", "--detail", "503 Service Unavailable")
        self.assertIn("recorded as --kind 5xx", r.stdout)
        self.assertIn("WAIT 20s", r.stdout)
        r = run("record-error", "S01", "--kind", "5xx")
        self.assertIn("GIVE UP", r.stdout)
        lg = runjson()["log"]
        self.assertEqual((lg["errors_5xx"], lg["errors_other"], lg["retries_429"]), (2, 0, 0))
        self.assertIn("S01", runjson()["skipped"])

    def test_five_503s_no_longer_stop_eight_do(self):
        self.plan()
        keys = [g["key"] for g in runjson()["groups"]]
        for k in keys[:7]:
            out = run("record-error", k, "--kind", "5xx").stdout
            self.assertNotIn("STOP", out, k)               # the 16:21 run stopped at the 5th 503
        out = run("record-error", keys[7], "--kind", "5xx").stdout
        self.assertIn("STOP: 8 5xx errors (limit 8)", out)

    def test_429_rules_unchanged(self):
        self.plan()
        self.assertIn("WAIT 90s", run("record-error", "S01", "--kind", "429").stdout)
        self.assertIn("WAIT 240s", run("record-error", "S01", "--kind", "429").stdout)
        self.assertIn("GIVE UP", run("record-error", "S01", "--kind", "429").stdout)
        self.assertEqual(runjson()["log"]["errors_5xx"], 0)

    def test_group_queries_at_most_3000_chars(self):
        for mode in ("cashtag", "bare"):
            set_mode(mode)
            stock, etf, st, et = br.current_packing(br.load_universe())
            for g in stock + etf:
                self.assertLessEqual(len(br.group_query(g)), br.MAX_GROUP_QUERY_LEN)
            self.assertEqual([t for g in stock for t in g], st)
        set_mode("bare")
        stock, etf, _, _ = br.current_packing(br.load_universe())
        self.assertGreaterEqual(len(stock), 14)

    def test_not_measurable_never_stored_or_published(self):
        set_mode("bare")
        self.assertIn("SOFI", br.not_measurable())
        for t in ("HAIN", "HAO", "HAVA"):
            self.assertIn(t, br.not_measurable(), t)
        hp = os.path.join(TMP, "data", "hourly.json")
        hours = {"2026-10-17T%02d" % h: 300 for h in range(0, 9)}
        rec = lambda: {"h": dict(hours), "sources": ["core-daily"], "last_5h": 1500,
                       "last_window_end": "2026-10-17T09:00:00Z", "last_measured": "2026-10-17T09:40:00Z"}
        json.dump({"version": 1, "query_mode": "bare", "tickers": {"SOFI": rec(), "NVDA": rec()}, "nodes": {},
                   "seen": {"2026-10-17": {"SOFI": {"authors": ["a"], "clean": 1, "total": 1}}},
                   "windows": ["2026-10-17T09:00:00Z"]}, open(hp, "w"))
        h = br.load_hourly()                          # hot re-reads and daily files read this store
        self.assertNotIn("SOFI", h["tickers"])
        self.assertNotIn("SOFI", h["seen"]["2026-10-17"])
        ent, _ = br.daily_entries(h, "2026-10-17", br.parse_iso("2026-10-17T09:00:00Z"), {}, set(), {"NVDA"})
        self.assertEqual([e["ticker"] for e in ent], ["NVDA"])
        # a count recorded for a not-measurable ticker is never stored
        run_obj = {"counts": {"SOFI": {"buckets": [10] * 29, "recorded_at": "2026-10-17T14:30:00Z"}},
                   "tree": {"nodes": {}}, "history_start": "2026-10-16T09:00:00Z",
                   "window_end": "2026-10-17T14:00:00Z", "count_sources": {}}
        br.update_hourly(run_obj, h, {}, br.utcnow())
        self.assertNotIn("SOFI", h["tickers"])
        # published files: check flags a leak, purge-not-measurable removes it
        json.dump({"version": 1, "query_mode": "bare", "tickers": {"SOFI": rec()}, "nodes": {}, "seen": {},
                   "windows": []}, open(hp, "w"))
        tp = os.path.join(TMP, "data", "today.json")
        json.dump({"method_note": {"query_mode": "bare"}, "status_counts": {"partial": 2},
                   "tickers": [{"ticker": "SOFI", "status": "partial", "exact_part": 4433, "held_back": True},
                               {"ticker": "NVDA", "status": "partial", "exact_part": 900, "held_back": False}]},
                  open(tp, "w"))
        lp = os.path.join(TMP, "data", "latest.json")
        lt = json.load(open(lp))
        lt["held_back"] = (lt.get("held_back") or []) + [{"ticker": "HAIN", "mentions": 8028}]
        lt["collision_queue"] = ["HAIN", "HAO"]
        json.dump(lt, open(lp, "w"))
        r = run("check")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not-measurable ticker published in data/today.json", r.stdout)
        r = run("purge-not-measurable")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        td = json.load(open(tp))
        self.assertEqual([e["ticker"] for e in td["tickers"]], ["NVDA"])
        self.assertEqual((td["tickers_seen"], td["talked_about_today"], td["status_counts"]), (1, 1, {"partial": 1}))
        lt = json.load(open(lp))
        self.assertNotIn("HAIN", json.dumps(lt["held_back"]))
        self.assertEqual(lt["collision_queue"], [])
        self.assertIn("A", lt["not_measurable"]["examples"])          # the documented examples stay
        self.assertNotIn("SOFI", json.load(open(hp))["tickers"])
        r = run("check")
        self.assertEqual(r.returncode, 0, r.stdout)
        os.remove(tp)


if __name__ == "__main__":
    unittest.main()
