"""Offline checks for scripts/build_rankings.py (no X reads). Run: python3 -m unittest discover tests"""
import importlib.util
import json
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
        t, ok = br.size_run(95, 12, 13)
        self.assertTrue(ok)
        self.assertEqual(t, {"stock_groups": 12, "etf_groups": 2, "samples": 6, "candidates": 20, "core": 50, "spare": 5})

    def test_core_shrinks_first(self):
        t, ok = br.size_run(60, 12, 13)
        self.assertEqual((t["core"], t["etf_groups"], t["candidates"], t["samples"]), (15, 2, 20, 6))
        t, ok = br.size_run(40, 12, 13)
        self.assertEqual(t["core"], 10)
        self.assertEqual(sum(t.values()), 40)

    def test_minimum(self):
        t, ok = br.size_run(20, 12, 13)
        self.assertTrue(ok)
        self.assertEqual(sum(t.values()), 20)
        self.assertEqual(t["stock_groups"], 12)
        t, ok = br.size_run(11, 12, 13)
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
        for t in plan["core_order"][plan["targets"]["core"]:]:
            self.assertIn(t, swept)          # demoted core stays screened


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
            {"author": "a3", "text": "$DKI gap up"},
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
        r = run("migrate")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.load(open(p))
        self.assertEqual(s["method_version"], 2)
        self.assertTrue(s["last_run_noncore"])   # seeded from the v1 latest.json

    def test_check_command(self):
        r = run("check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("check OK", r.stdout)


Z29 = ",".join(["10"] * 29)


def runjson():
    return json.load(open(os.path.join(TMP, "work", "run.json")))


class RunFlow(unittest.TestCase):
    """Sequential-run mechanics: one call at a time, 429 handling, stop rules, partial build, archive."""

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
        self.assertIn("WAIT 60s", r.stdout)
        r = run("record-error", "S01", "--kind", "429")
        self.assertIn("WAIT 120s", r.stdout)
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
        self.assertIn("STOP: 5 non-429 errors", r.stdout)
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

    def test_partial_build_and_refusal(self):
        self.plan()
        self.record_groups(skip=("S12",))
        r = run("build")
        self.assertNotEqual(r.returncode, 0)          # nothing picked yet
        self.record_groups()
        self.do_samples()
        r = run("pick-candidates")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        picks = runjson()["picks"]["individual"]
        core = [p["ticker"] for p in picks if p["source"] == "core"]
        rest = [p["ticker"] for p in picks if p["source"] != "core"]
        self.assertTrue(rest)
        run("record", *["%s=%s" % (t, Z29) for t in core[:-1]], "--no-sleep")
        r = run("build")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("core not fully counted", r.stderr)
        run("record", "%s=%s" % (core[-1], Z29), "--no-sleep")
        r = run("build")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PARTIAL build", r.stdout)
        out = json.load(open(os.path.join(TMP, "data", "latest.json")))
        self.assertTrue(out["partial"])
        self.assertTrue(out["note"].startswith("PARTIAL RUN"))
        self.assertIn(rest[0], out["partial_reason"])
        self.assertIn(rest[0], out["planned_not_measured"])
        self.assertEqual(out["ranked_count"], len(core))
        # next plan archives the built run
        self.plan()
        arch = os.listdir(os.path.join(TMP, "work", "archive"))
        self.assertTrue(any(a.endswith("-built") for a in arch))

    def test_complete_build_not_partial(self):
        self.plan()
        self.record_groups()
        self.do_samples()
        run("pick-candidates")
        picks = runjson()["picks"]["individual"]
        run("record", *["%s=%s" % (p["ticker"], Z29) for p in picks], "--no-sleep")
        r = run("build")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.load(open(os.path.join(TMP, "data", "latest.json")))
        self.assertFalse(out["partial"])
        self.assertIsNone(out["partial_reason"])

    def test_clean_gate_in_flow(self):
        """TM-style memecoin/template chatter is not discovered; a real mover with clean posts is;
        a carryover ticker whose sample is spam is held back instead of ranked."""
        st = json.load(open(os.path.join(TMP, "data", "state.json")))
        st["last_run_noncore"] = [{"ticker": "DKI", "mentions": 300, "window_end": "2026-10-09T04:00:00Z",
                                   "riser": True, "clean_checked_at": "2026-10-09T04:00:00Z"}]
        json.dump(st, open(os.path.join(TMP, "data", "state.json"), "w"))
        self.plan()
        self.record_groups()
        run("pick-samples")
        lines = os.path.join(TMP, "lines.txt")
        posts = ["memeguru%d\t%dx profit from my call on $TM https://t.co/x" % (i, 20 + i) for i in range(6)]
        posts += ["solmaster%d\tDecent %dx profit pump up on $TM private TG friends printing" % (i, i) for i in range(4)]
        posts += ["trader0\t$IPW breaking out on volume after the contract news",
                  "trader1\tadded some $IPW here, the chart finally cleared resistance today",
                  "trader2\t$IPW premarket looks strong, curious whether it holds the open",
                  "trader3\tnot chasing $IPW at these levels but the story is real"]
        posts += ["dumper%d\t$DKI $AAA $BBB $CCC top gainers" % i for i in range(5)]
        open(lines, "w").write("\n".join(posts) + "\n")
        for p in runjson()["picks"]["samples1"]:
            run("record-sample", p["key"], "--lines", lines, "--no-sleep")
        run("pick-samples")
        for p in runjson()["picks"]["samples2"] or []:
            run("record-sample", p["key"], "--failed", "--no-sleep")
        r = run("pick-candidates")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        picks = {p["ticker"]: p for p in runjson()["picks"]["individual"]}
        self.assertNotIn("TM", picks)
        self.assertEqual(picks["IPW"]["source"], "discovered")
        self.assertEqual(picks["DKI"]["source"], "carryover")
        self.assertTrue(picks["IPW"]["query"].endswith(br.suffix()))
        run("record", *["%s=%s" % (t, Z29) for t in picks], "--no-sleep")
        r = run("build")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.load(open(os.path.join(TMP, "data", "latest.json")))
        ranked = {e["ticker"] for e in out["rankings"]}
        self.assertIn("IPW", ranked)
        self.assertNotIn("DKI", ranked)
        self.assertEqual([h["ticker"] for h in out["held_back"]], ["DKI"])
        self.assertIn("TM", out["sampling"]["rejected_by_clean_check"])

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


if __name__ == "__main__":
    unittest.main()
