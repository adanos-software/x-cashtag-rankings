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
        self.assertEqual(plan["budget"], 60)
        self.assertEqual(sum(plan["targets"].values()), 60)
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
        self.assertEqual(br.ticker_query("NVDA", coll), "$NVDA -is:retweet")
        q = br.ticker_query("GM", coll)
        self.assertTrue(q.startswith("$GM -is:retweet -vote"))
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
        self.assertNotIn("MI", agg)
        self.assertNotIn("GM", agg)
        s = st["smp-X"]
        self.assertEqual((s["spam_account"], s["list_spam"], s["template"], s["kept"]), (1, 1, 4, 3))

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


if __name__ == "__main__":
    unittest.main()
