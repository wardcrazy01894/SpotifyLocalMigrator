"""Offline tests for the logic that decides what goes into the user's real playlist.

Run: python3 -m unittest discover -s tests
No network: anything that would call the Spotify API is monkeypatched.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import migrate as mg  # noqa: E402


def local(pos, title, artist, secs, album=""):
    return {
        "pos": pos, "is_local": True,
        "uri": f"spotify:local:{artist}:{album}:{title}:{secs}".replace(" ", "+"),
        "id": None, "title": title, "artist": artist, "album": album, "duration_ms": secs * 1000,
    }


def remote(pos, uri):
    return {"pos": pos, "is_local": False, "uri": uri, "id": uri.split(":")[-1],
            "title": "x", "artist": "y", "album": "", "duration_ms": 1000}


def cand(name, artists, ms, uri="spotify:track:cand"):
    return {"name": name, "artists": [{"name": a} for a in artists], "duration_ms": ms, "uri": uri,
            "album": {"name": "A"}}


class Normalising(unittest.TestCase):
    def test_norm_strips_accents_case_punctuation(self):
        self.assertEqual(mg.norm("Déjà Vu!  (Live)"), "deja vu live")
        self.assertEqual(mg.norm("Rock & Roll"), "rock and roll")

    def test_norm_title_drops_leading_track_number(self):
        self.assertEqual(mg.norm_title("03 - Mr. Brightside"), "mr brightside")
        self.assertEqual(mg.norm_title("3x5"), "3x5")

    def test_core_title_drops_feat_and_suffixes(self):
        self.assertEqual(mg.core_title("Problem (feat. Iggy Azalea)"), "problem")
        self.assertEqual(mg.core_title("Come Together - Remastered 2015"), "come together")
        # "with" is not a feat marker: it is part of many real titles
        self.assertEqual(mg.core_title("Dancing With Myself"), "dancing with myself")

    def test_artists_match(self):
        self.assertTrue(mg.artists_match("One Republic & Timbaland", ["Timbaland", "OneRepublic"]) is False)
        self.assertTrue(mg.artists_match("Kanye West", ["Kanye West", "Big Sean"]))
        self.assertTrue(mg.artists_match("Usher Feat. Lil' Jon", ["USHER", "Lil Jon", "Ludacris"]))
        self.assertTrue(mg.artists_match("Bob Marley & The Wailers", ["Bob Marley & The Wailers"]))
        self.assertFalse(mg.artists_match("", ["Anyone"]))


class Evaluate(unittest.TestCase):
    src = local(0, "Lego House", "Ed Sheeran", 187)

    def test_exact_match(self):
        r = mg.evaluate(self.src, cand("Lego House", ["Ed Sheeran"], 185093))
        self.assertEqual(r["verdict"], "match")

    def test_duration_mismatch_beyond_tolerance(self):
        r = mg.evaluate(self.src, cand("Lego House", ["Ed Sheeran"], 180000))
        self.assertEqual(r["verdict"], "duration_mismatch")

    def test_title_variant(self):
        r = mg.evaluate(self.src, cand("Lego House - Acoustic", ["Ed Sheeran"], 187000))
        self.assertEqual(r["verdict"], "title_variant")

    def test_artist_mismatch_and_no_match(self):
        self.assertEqual(mg.evaluate(self.src, cand("Lego House", ["Someone"], 187000))["verdict"], "artist_mismatch")
        self.assertEqual(mg.evaluate(self.src, cand("Other", ["Someone"], 187000))["verdict"], "no_match")

    def test_no_duration(self):
        src = dict(self.src, duration_ms=0)
        self.assertEqual(mg.evaluate(src, cand("Lego House", ["Ed Sheeran"], 187000))["verdict"], "no_duration")


class Wanted(unittest.TestCase):
    def test_only_match_or_approved_with_uri(self):
        self.assertTrue(mg.wanted({"verdict": "match", "uri": "spotify:track:a"}))
        self.assertTrue(mg.wanted({"verdict": "title_variant", "uri": "spotify:track:a", "approved": True}))
        self.assertFalse(mg.wanted({"verdict": "title_variant", "uri": "spotify:track:a"}))
        self.assertFalse(mg.wanted({"verdict": "match", "uri": None}))
        self.assertFalse(mg.wanted(None))


SRC = {"name": "T", "id": "p", "items": [
    remote(0, "spotify:track:R0"),
    local(1, "A", "a", 100),
    local(2, "B", "b", 100),
    local(3, "A", "a", 100),  # duplicate of #1
    local(4, "L", "l", 100),  # keep local
    local(5, "U", "u", 100),  # undecided
]}
CACHE = {
    "1": {"verdict": "match", "uri": "spotify:track:A"},
    "2": {"verdict": "title_variant", "uri": "spotify:track:B", "approved": True},
    "3": {"verdict": "match", "uri": "spotify:track:A"},
    "4": {"verdict": "no_match", "uri": "spotify:track:X", "keep_local": True, "approved": False},
    "5": {"verdict": "no_match", "uri": "spotify:track:Y"},
}


class DesiredAndOrder(unittest.TestCase):
    def test_desired_uris_collapses_duplicates(self):
        uris, dupes = mg.desired_uris(SRC, CACHE, False)
        self.assertEqual(uris, ["spotify:track:R0", "spotify:track:A", "spotify:track:B"])
        self.assertEqual(dupes, 1)
        uris, dupes = mg.desired_uris(SRC, CACHE, True)
        self.assertEqual(uris.count("spotify:track:A"), 2)
        self.assertEqual(dupes, 0)

    def test_full_order_includes_local_files(self):
        order = mg.full_order(SRC, CACHE)
        self.assertEqual(order, ["spotify:track:R0", "spotify:track:A", "spotify:track:B",
                                 SRC["items"][4]["uri"], SRC["items"][5]["uri"]])


class PlanMerge(unittest.TestCase):
    def test_nothing_missing(self):
        self.assertEqual(mg.plan_merge(["spotify:track:A", "spotify:track:B"], ["spotify:track:A", "spotify:track:B"]), [])

    def test_inserts_before_next_present_including_local_anchor(self):
        L1 = "spotify:local:x:y:L1:100"
        order = ["spotify:track:A", "spotify:track:B", L1, "spotify:track:C"]
        live = ["spotify:track:A", L1, "spotify:track:C"]
        self.assertEqual(mg.plan_merge(order, live), [(1, ["spotify:track:B"])])

    def test_runs_and_positions_account_for_earlier_inserts(self):
        order = ["spotify:track:A", "spotify:track:B", "spotify:track:C", "spotify:track:D", "spotify:track:E", "spotify:track:F"]
        live = ["spotify:track:C", "spotify:track:F"]
        plan = mg.plan_merge(order, live)
        self.assertEqual(plan, [(0, ["spotify:track:A", "spotify:track:B"]), (3, ["spotify:track:D", "spotify:track:E"])])
        sim = list(live)
        for pos, run in plan:
            sim[pos:pos] = run
        self.assertEqual(sim, order)

    def test_missing_at_end_appends(self):
        self.assertEqual(mg.plan_merge(["spotify:track:A", "spotify:track:B"], ["spotify:track:A"]), [(1, ["spotify:track:B"])])

    def test_runs_split_at_100(self):
        order = [f"spotify:track:{i:03d}" for i in range(250)] + ["spotify:track:END"]
        plan = mg.plan_merge(order, ["spotify:track:END"])
        self.assertEqual([len(r) for _, r in plan], [100, 100, 50])
        self.assertEqual([p for p, _ in plan], [0, 100, 200])

    def test_local_files_never_inserted(self):
        self.assertEqual(mg.plan_merge(["spotify:local:a:b:c:1", "spotify:track:A"], ["spotify:track:A"]), [])


class MergePlaylistCalls(unittest.TestCase):
    def test_posts_each_run_with_position(self):
        calls = []
        orig = mg.api_post
        mg.api_post = lambda path, body: calls.append((path, body)) or {}
        try:
            mg.merge_playlist("pid", ["spotify:track:A", "spotify:track:B", "spotify:local:z:z:z:1"],
                              ["spotify:track:A", "spotify:local:z:z:z:1", "spotify:track:OLD"])
        finally:
            mg.api_post = orig
        self.assertEqual(calls, [("/playlists/pid/items", {"uris": ["spotify:track:B"], "position": 1})])


class VerifySummary(unittest.TestCase):
    def test_complete_accounts_for_duplicates_and_local_files(self):
        cache = {k: dict(v) for k, v in CACHE.items()}
        cache["5"] = {"verdict": "no_match", "uri": None, "keep_local": True, "approved": False}
        live = ["spotify:track:R0", "spotify:track:A", "spotify:track:B", SRC["items"][4]["uri"], SRC["items"][5]["uri"]]
        v = mg.verify_summary(SRC, cache, live)
        self.assertTrue(v["complete"])
        self.assertEqual(v["dupes"], 1)
        self.assertEqual(v["expected"] + len(v["keep_local"]) + v["dupes"], v["source_total"])

    def test_not_complete_while_undecided_or_local_missing(self):
        live = ["spotify:track:R0", "spotify:track:A", "spotify:track:B"]
        v = mg.verify_summary(SRC, CACHE, live)
        self.assertFalse(v["complete"])
        self.assertEqual([it["pos"] for it in v["unresolved"]], [5])
        self.assertEqual([it["pos"] for it in v["missing_local"]], [4])
        self.assertEqual(v["missing_tracks"], [])

    def test_reports_missing_and_extra_tracks(self):
        v = mg.verify_summary(SRC, CACHE, ["spotify:track:R0", "spotify:track:B", "spotify:track:ZZZ"])
        self.assertEqual(v["missing_tracks"], ["spotify:track:A"])
        self.assertEqual(v["extra_tracks"], ["spotify:track:ZZZ"])


class CacheAlignment(unittest.TestCase):
    def test_backfills_keys(self):
        cache = {"1": {"verdict": "match", "uri": "spotify:track:A"}}
        self.assertEqual(mg.align_cache(SRC, cache), (1, 0, 0))
        self.assertEqual(cache["1"]["key"], mg.song_key(SRC["items"][1]))

    def test_two_songs_sharing_a_key_both_keep_their_entries(self):
        a1, a2 = local(1, "Royals", "Lorde", 190), local(3, "Royals", "Lorde", 190)
        src = {"name": "T", "items": [remote(0, "spotify:track:R0"), a1, local(2, "B", "b", 5), a2]}
        cache = {"1": {"verdict": "match", "uri": "spotify:track:A", "key": mg.song_key(a1)},
                 "3": {"verdict": "match", "uri": "spotify:track:A", "key": mg.song_key(a2)}}
        self.assertEqual(mg.align_cache(src, cache), (0, 0, 0))
        src2 = {"name": "T", "items": [remote(0, "spotify:track:R0"), local(1, "NEW", "n", 1), dict(a1, pos=2),
                                        local(3, "B", "b", 5), dict(a2, pos=4)]}
        orphans = []
        self.assertEqual(mg.align_cache(src2, cache, orphans), (0, 2, 0))
        self.assertEqual(sorted(cache), ["2", "4"])
        self.assertEqual(orphans, [])

    def test_dropped_entries_are_handed_back(self):
        src = {"name": "T", "items": [local(0, "A", "a", 1)]}
        cache = {"0": {"verdict": "match", "uri": "spotify:track:Z", "key": "gone|gone|1"}}
        orphans = []
        self.assertEqual(mg.align_cache(src, cache, orphans), (0, 0, 1))
        self.assertEqual(orphans[0]["former_pos"], "0")

    def test_rekeys_after_insert_and_drops_vanished(self):
        src = {"name": "T", "items": [remote(0, "spotify:track:R0"), local(1, "NEW", "n", 50), local(2, "A", "a", 100), local(3, "B", "b", 100)]}
        cache = {
            "1": {"verdict": "match", "uri": "spotify:track:A", "key": mg.song_key(local(0, "A", "a", 100))},
            "2": {"verdict": "title_variant", "uri": "spotify:track:B", "approved": True, "key": mg.song_key(local(0, "B", "b", 100))},
            "3": {"verdict": "no_match", "uri": None, "key": mg.song_key(local(0, "GONE", "g", 1))},
        }
        backfilled, rekeyed, dropped = mg.align_cache(src, cache)
        self.assertEqual((backfilled, rekeyed, dropped), (0, 2, 1))
        self.assertEqual(cache["2"]["uri"], "spotify:track:A")
        self.assertTrue(cache["3"]["approved"])
        self.assertNotIn("1", cache)


class GlobalDecisions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = mg.GLOBAL_APPROVALS
        mg.GLOBAL_APPROVALS = Path(self.tmp.name) / "approvals.json"

    def tearDown(self):
        mg.GLOBAL_APPROVALS = self.orig
        self.tmp.cleanup()

    def test_carry_unsearched_skips_search_for_decided_songs(self):
        it_a, it_b = local(1, "A", "a", 100), local(2, "B", "b", 100)
        mg.record_global(it_a, {"uri": "spotify:track:A"}, "approve", "Other")
        mg.record_global(it_b, {"uri": "spotify:track:X"}, "keep_local", "Other")
        cache = {}
        rest = mg.carry_unsearched({"items": [it_a, it_b]}, cache, [it_a, it_b, local(3, "C", "c", 1)], "2026-01-01")
        self.assertEqual([i["pos"] for i in rest], [3])
        self.assertTrue(mg.wanted(cache["1"]))
        self.assertTrue(cache["2"]["keep_local"])
        self.assertFalse(mg.wanted(cache["2"]))

    def test_keep_local_elsewhere_overrides_auto_match(self):
        it = local(1, "A", "a", 100)
        mg.record_global(it, {"uri": None}, "keep_local", "Other")
        cache = {"1": {"verdict": "match", "uri": "spotify:track:A"}}
        self.assertEqual(mg.apply_global({"items": [it]}, cache), 1)
        self.assertTrue(cache["1"]["keep_local"])
        self.assertFalse(mg.wanted(cache["1"]))

    def test_legacy_identity_less_key_is_not_applied(self):
        it = local(1, "John Blend", "", 314)
        mg.GLOBAL_APPROVALS.write_text(json.dumps({mg.song_key(it): {"decision": "keep_local", "uri": None, "from_playlist": "P"}}))
        self.assertEqual(mg.carry_unsearched({"items": [it]}, {}, [it], "d"), [it])
        self.assertEqual(mg.apply_global({"items": [it]}, {"1": {"verdict": "no_match", "uri": None}}), 0)

    def test_approve_elsewhere_does_not_touch_a_match(self):
        it = local(1, "A", "a", 100)
        mg.record_global(it, {"uri": "spotify:track:OTHER"}, "approve", "Other")
        cache = {"1": {"verdict": "match", "uri": "spotify:track:A"}}
        self.assertEqual(mg.apply_global({"items": [it]}, cache), 0)
        self.assertEqual(cache["1"]["uri"], "spotify:track:A")


class CallRetries(unittest.TestCase):
    """_call: 429 is retried for every method (the request was rejected, not processed);
    5xx/network errors are retried only for GET."""

    class _Resp:
        def __init__(self, body=b"{}"):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def _run(self, method, outcomes):
        import urllib.error
        calls = []

        def fake_urlopen(req, data=None, timeout=None):
            calls.append(req.get_method())
            o = outcomes.pop(0)
            if o == 429:
                raise urllib.error.HTTPError(req.full_url, 429, "", {"Retry-After": "0"}, self._Resp(b""))
            if isinstance(o, int) and o != 200:
                raise urllib.error.HTTPError(req.full_url, o, "", {}, self._Resp(b"boom"))
            return self._Resp(b'{"ok": true}')

        orig_open, orig_tok, orig_sleep = mg.urllib.request.urlopen, mg.access_token, mg.time.sleep
        mg.urllib.request.urlopen, mg.access_token, mg.time.sleep = fake_urlopen, lambda: "t", lambda s: None
        try:
            return mg._call(method, "/x", body={} if method != "GET" else None), calls
        finally:
            mg.urllib.request.urlopen, mg.access_token, mg.time.sleep = orig_open, orig_tok, orig_sleep

    def test_post_retries_after_429(self):
        res, calls = self._run("POST", [429, 200])
        self.assertEqual(res, {"ok": True})
        self.assertEqual(calls, ["POST", "POST"])

    def test_post_does_not_retry_5xx(self):
        with self.assertRaises(RuntimeError) as cm:
            self._run("POST", [502, 200])
        self.assertIn("502", str(cm.exception))

    def test_get_retries_5xx(self):
        res, calls = self._run("GET", [503, 200])
        self.assertEqual(res, {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_long_retry_after_raises_rate_limited(self):
        import urllib.error

        def fake_urlopen(req, data=None, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "", {"Retry-After": "77000"}, self._Resp(b""))

        orig_open, orig_tok = mg.urllib.request.urlopen, mg.access_token
        mg.urllib.request.urlopen, mg.access_token = fake_urlopen, lambda: "t"
        try:
            with self.assertRaises(mg.RateLimited):
                mg._call("GET", "/x")
        finally:
            mg.urllib.request.urlopen, mg.access_token = orig_open, orig_tok


class PlanMergeFuzz(unittest.TestCase):
    def test_random_cases_place_every_missing_track_once_in_order(self):
        import random
        rng = random.Random(7)
        for _ in range(300):
            n = rng.randint(1, 60)
            order = [f"spotify:local:a:b:{i}:1" if rng.random() < 0.2 else f"spotify:track:{i:03d}" for i in range(n)]
            live = [u for u in order if rng.random() < 0.6]
            extra = [f"spotify:track:X{i}" for i in range(rng.randint(0, 2))]
            for e in extra:
                live.insert(rng.randint(0, len(live)), e)
            plan = mg.plan_merge(order, live)
            sim = list(live)
            for pos, run in plan:
                self.assertTrue(0 <= pos <= len(sim))
                self.assertTrue(1 <= len(run) <= 100)
                sim[pos:pos] = run
            want = [u for u in order if u.startswith("spotify:track:") and u not in set(live)]
            self.assertEqual(sorted(u for _, run in plan for u in run), sorted(want))
            kept = [u for u in sim if u in set(order)]
            self.assertEqual(kept, [u for u in order if u in set(kept)])


class RetryAfter(unittest.TestCase):
    def test_parses_int_float_and_garbage(self):
        self.assertEqual(mg._retry_after({"Retry-After": "5"}), 6)
        self.assertEqual(mg._retry_after({"Retry-After": "77000.5"}), 77001)
        self.assertEqual(mg._retry_after({"Retry-After": "Wed, 21 Oct"}), 3)
        self.assertEqual(mg._retry_after({}), 3)


if __name__ == "__main__":
    unittest.main()
