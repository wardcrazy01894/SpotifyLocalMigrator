"""Offline tests for the logic that decides what goes into the user's real playlist.

Run: python3 -m unittest discover -s tests
No network: anything that would call the Spotify API is monkeypatched.
"""
import argparse
import contextlib
import csv
import io
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

    def test_recheck_is_open_not_wanted(self):
        self.assertFalse(mg.wanted({"verdict": "match", "uri": "spotify:track:a", "recheck": True}))
        self.assertFalse(mg.wanted({"verdict": "title_variant", "uri": "spotify:track:a", "approved": True, "recheck": True}))


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


class PlanReorder(unittest.TestCase):
    L = [f"spotify:local:x:y:L{i}:100" for i in range(4)]

    def _apply(self, live, moves):
        """Simulate the Spotify reorder endpoint: indices are taken before the move."""
        sim = list(live)
        for start, before in moves:
            self.assertTrue(0 <= start < len(sim) and 0 <= before <= len(sim), (start, before, sim))
            self.assertNotEqual(start, before)
            u = sim.pop(start)
            sim.insert(before - 1 if start < before else before, u)
        return sim

    def _ordered_part(self, order, final):
        s = set(order)
        seen = set()
        out = []
        for u in final:
            if u in s and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def test_already_in_order(self):
        self.assertEqual(mg.plan_reorder(["spotify:track:A", self.L[0], "spotify:track:B"], ["spotify:track:A", self.L[0], "spotify:track:B"]), [])

    def test_empty_inputs(self):
        self.assertEqual(mg.plan_reorder([], ["spotify:track:A"]), [])
        self.assertEqual(mg.plan_reorder(["spotify:track:A"], []), [])

    def test_appended_local_files_move_to_source_positions(self):
        order = [self.L[0], "spotify:track:A", self.L[1], "spotify:track:B", "spotify:track:C", self.L[2]]
        live = ["spotify:track:A", "spotify:track:B", "spotify:track:C", self.L[0], self.L[1], self.L[2]]
        moves = mg.plan_reorder(order, live)
        self.assertEqual(moves, [(3, 0), (4, 2)])
        self.assertEqual(self._apply(live, moves), order)

    def test_full_reversal_ends_in_order(self):
        order = ["spotify:track:A", "spotify:track:B", "spotify:track:C", "spotify:track:D"]
        live = list(reversed(order))
        moves = mg.plan_reorder(order, live)
        self.assertEqual(len(moves), 3)
        self.assertEqual(self._apply(live, moves), order)

    def test_one_displaced_item_costs_one_move(self):
        order = ["spotify:track:A", "spotify:track:B", "spotify:track:C", self.L[0]]
        live = [self.L[0], "spotify:track:A", "spotify:track:B", "spotify:track:C"]
        moves = mg.plan_reorder(order, live)
        self.assertEqual(moves, [(0, 4)])
        self.assertEqual(self._apply(live, moves), order)

    def test_stray_at_front_costs_nothing(self):
        order = ["spotify:track:A", "spotify:track:B", "spotify:track:C"]
        live = ["spotify:track:EXTRA", "spotify:track:A", "spotify:track:B", "", "spotify:track:C"]
        self.assertEqual(mg.plan_reorder(order, live), [])

    def test_extras_and_absent_items_are_left_alone(self):
        order = ["spotify:track:A", "spotify:track:MISSING", self.L[0], "spotify:track:B"]
        live = ["spotify:track:EXTRA", "spotify:track:B", "spotify:track:A", self.L[0]]
        moves = mg.plan_reorder(order, live)
        final = self._apply(live, moves)
        self.assertEqual(self._ordered_part(order, final), ["spotify:track:A", self.L[0], "spotify:track:B"])
        self.assertIn("spotify:track:EXTRA", final)

    def test_duplicate_live_entries_position_the_first_only(self):
        order = ["spotify:track:A", self.L[0], "spotify:track:B"]
        live = ["spotify:track:B", self.L[0], "spotify:track:A", self.L[0], "spotify:track:B"]
        moves = mg.plan_reorder(order, live)
        final = self._apply(live, moves)
        self.assertEqual(self._ordered_part(order, final), order)
        self.assertEqual(sorted(final), sorted(live))

    def test_duplicate_copy_does_not_confuse_predecessor_lookup(self):
        order = ["spotify:track:A", "spotify:track:E", "spotify:track:F", "spotify:track:B", "spotify:track:G"]
        live = ["spotify:track:B", "spotify:track:B", "spotify:track:G", "spotify:track:A", "spotify:track:E", "spotify:track:F"]
        moves = mg.plan_reorder(order, live)
        self.assertEqual(moves, [(0, 6), (1, 6)])
        # The tracked (first) copy of B is placed; the stray second copy stays where it was.
        self.assertEqual(self._apply(live, moves), ["spotify:track:B", "spotify:track:A", "spotify:track:E", "spotify:track:F", "spotify:track:B", "spotify:track:G"])

    def test_large_pasted_tail_is_linear_in_displaced_items(self):
        tracks = [f"spotify:track:{i:04d}" for i in range(2000)]
        locals_ = [f"spotify:local:x:y:L{i}:1" for i in range(150)]
        order = []
        for i, u in enumerate(tracks):
            order.append(u)
            if i % 13 == 0 and i // 13 < len(locals_):
                order.append(locals_[i // 13])
        live = tracks + locals_
        moves = mg.plan_reorder(order, live)
        self.assertEqual(len(moves), len(locals_))
        self.assertEqual(self._apply(live, moves), order)


class ReorderPlaylistCalls(unittest.TestCase):
    def test_puts_each_move_and_chains_snapshot_ids(self):
        calls = []
        orig = mg._call

        def fake(method, path, params=None, body=None):
            calls.append((method, path, dict(body or {})))
            return {"snapshot_id": f"snap{len(calls)}"}

        mg._call = fake
        try:
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                n = mg.reorder_playlist("PID", ["spotify:track:A", "spotify:track:B", "spotify:track:C"], ["spotify:track:C", "spotify:track:B", "spotify:track:A"])
        finally:
            mg._call = orig
        self.assertEqual(n, 2)
        # the current snapshot is read first so even the first move is rejected if the playlist changed
        self.assertEqual(calls[0][0:2], ("GET", "/playlists/PID"))
        self.assertEqual([c[0:2] for c in calls[1:]], [("PUT", "/playlists/PID/items")] * 2)
        self.assertEqual(calls[1][2]["snapshot_id"], "snap1")
        self.assertEqual(calls[2][2]["snapshot_id"], "snap2")

    def test_no_moves_means_no_calls(self):
        calls = []
        orig = mg._call
        mg._call = lambda *a, **k: calls.append(a) or {}
        try:
            n = mg.reorder_playlist("PID", ["spotify:track:A", "spotify:track:B"], ["spotify:track:A", "spotify:track:B"])
        finally:
            mg._call = orig
        self.assertEqual((n, calls), (0, []))


class MergePlaylistCalls(unittest.TestCase):
    def test_posts_each_run_with_position(self):
        calls = []
        orig = mg.api_post
        import contextlib
        import io
        mg.api_post = lambda path, body: calls.append((path, body)) or {}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
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

    def test_local_file_of_a_built_song_is_extra_not_complete(self):
        # #2 was dragged in as a local file earlier, then approved: the song is now in the playlist twice
        cache = {k: dict(v) for k, v in CACHE.items()}
        cache["5"] = {"verdict": "no_match", "uri": None, "keep_local": True, "approved": False}
        live = ["spotify:track:R0", "spotify:track:A", "spotify:track:B", SRC["items"][4]["uri"], SRC["items"][5]["uri"],
                SRC["items"][2]["uri"]]
        v = mg.verify_summary(SRC, cache, live)
        self.assertEqual(v["extra_local"], [SRC["items"][2]["uri"]])
        self.assertFalse(v["complete"])

    def test_rechecked_song_is_unresolved(self):
        cache = {k: dict(v) for k, v in CACHE.items()}
        cache["1"]["recheck"] = True
        v = mg.verify_summary(SRC, cache, ["spotify:track:R0", "spotify:track:A", "spotify:track:B"])
        self.assertIn(1, [it["pos"] for it in v["unresolved"]])
        self.assertEqual(v["extra_tracks"], [])  # #3 still wants track A; the duplicate is not collapsed now
        self.assertEqual(v["dupes"], 0)


class VerifyCommandAdvice(unittest.TestCase):
    """The closing line of `verify` must name a step that can actually finish the job."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE, mg.live_uris)
        mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE = root, root / "approvals.json", "T"
        wd = root / "t"
        wd.mkdir()
        self.items = [local(0, "A", "a", 100), local(1, "L", "l", 100)]
        (wd / "source.json").write_text(json.dumps({"name": "T", "id": "p", "items": self.items}))
        (wd / "matches.json").write_text(json.dumps({
            "0": {"verdict": "match", "uri": "spotify:track:A", "key": mg.song_key(self.items[0])},
            "1": {"verdict": "no_match", "uri": None, "keep_local": True, "approved": False, "key": mg.song_key(self.items[1])},
        }))
        (wd / "result.json").write_text(json.dumps({"playlist_id": "PID", "url": "u", "count": 1}))

    def tearDown(self):
        mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE, mg.live_uris = self.saved
        self.tmp.cleanup()

    def _verify(self, live):
        mg.live_uris = lambda pid: live
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mg.cmd_verify(argparse.Namespace())
        return out.getvalue().strip().splitlines()[-1]

    def test_unwanted_track_with_local_files_present_is_a_by_hand_step(self):
        # merge mode never removes tracks, so "run build" would loop forever
        last = self._verify(["spotify:track:A", "spotify:track:OLD", self.items[1]["uri"]])
        self.assertEqual(last, "All decided; remove the 1 unwanted Spotify track(s) above by hand.")
        last = self._verify(["spotify:track:OLD", self.items[1]["uri"]])
        self.assertEqual(last, "All decided; remove the 1 unwanted Spotify track(s) above by hand, then run `build`.")  # track A missing

    def test_unwanted_track_plus_extra_and_missing_local_files_are_all_named(self):
        last = self._verify(["spotify:track:A", "spotify:track:OLD", "spotify:local:x:x:stray:1"])
        self.assertEqual(last, "All decided; remove the 1 unwanted Spotify track(s) above by hand and the 1 extra local file(s); paste the 1 missing local file(s) in.")
        last = self._verify(["spotify:track:OLD", "spotify:local:x:x:stray:1"])  # track A missing too
        self.assertEqual(last, "All decided; remove the 1 unwanted Spotify track(s) above by hand and the 1 extra local file(s); paste the 1 missing local file(s) in, then run `build`.")

    def test_unwanted_track_without_local_files_is_fixed_by_build(self):
        last = self._verify(["spotify:track:A", "spotify:track:OLD"])
        self.assertEqual(last, "All decided, but the playlist does not match the decisions; run `build`.")

    def test_extra_and_missing_local_files_are_both_named(self):
        last = self._verify(["spotify:track:A", "spotify:local:x:x:stray:1"])
        self.assertEqual(last, "All decided; remove the 1 extra local file(s) above by hand and paste the 1 missing one(s) in to finish (`build` never removes local files).")

    def test_complete(self):
        self.assertEqual(self._verify(["spotify:track:A", self.items[1]["uri"]]), "COMPLETE: every source song is accounted for in the new playlist.")


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
        self.orig = (mg.GLOBAL_APPROVALS, mg.WORK_ROOT)  # WORK_ROOT too: carry-over reads other work dirs
        mg.GLOBAL_APPROVALS = Path(self.tmp.name) / "approvals.json"
        mg.WORK_ROOT = Path(self.tmp.name)

    def tearDown(self):
        mg.GLOBAL_APPROVALS, mg.WORK_ROOT = self.orig
        self.tmp.cleanup()

    def test_carry_unsearched_skips_search_for_decided_songs(self):
        it_a, it_b = local(1, "A", "a", 100), local(2, "B", "b", 100)
        mg.record_global(it_a, {"uri": "spotify:track:A"}, "approve", "Other")
        mg.record_global(it_b, {"uri": "spotify:track:X"}, "keep_local", "Other")
        cache = {}
        rest, decided, matched = mg.carry_unsearched({"items": [it_a, it_b]}, cache, [it_a, it_b, local(3, "C", "c", 1)], "2026-01-01")
        self.assertEqual((decided, matched), (2, 0))
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
        self.assertEqual(mg.carry_unsearched({"items": [it]}, {}, [it], "d"), ([it], 0, 0))
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

        def close(self):
            pass

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


class GlobalIdentityGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = (mg.GLOBAL_APPROVALS, mg.WORK_ROOT)  # WORK_ROOT too: carry-over reads other work dirs
        mg.GLOBAL_APPROVALS = Path(self.tmp.name) / "approvals.json"
        mg.WORK_ROOT = Path(self.tmp.name)

    def tearDown(self):
        mg.GLOBAL_APPROVALS, mg.WORK_ROOT = self.orig
        self.tmp.cleanup()

    def test_metadata_less_files_get_no_standing_decision(self):
        mg.record_global(local(1, "", "", 100), {"uri": None}, "keep_local", "P")
        mg.record_global(local(2, "Title only", "", 100), {"uri": None}, "keep_local", "P")
        self.assertFalse(mg.GLOBAL_APPROVALS.exists())

    def test_forget_global_removes_the_decision(self):
        it = local(1, "A", "a", 100)
        mg.record_global(it, {"uri": None}, "keep_local", "P")
        self.assertIn(mg.song_key(it), mg.load_global())
        mg.forget_global(it)
        self.assertNotIn(mg.song_key(it), mg.load_global())


class TrackId(unittest.TestCase):
    def test_accepts_uri_link_and_bare_id(self):
        tid = "5PX4uS1LqlWEPL69phPVQQ"
        self.assertEqual(mg.track_id(f"spotify:track:{tid}"), tid)
        self.assertEqual(mg.track_id(f"https://open.spotify.com/track/{tid}?si=abc"), tid)
        self.assertEqual(mg.track_id(f"https://open.spotify.com/intl-de/track/{tid}"), tid)
        self.assertEqual(mg.track_id(tid), tid)
        self.assertIsNone(mg.track_id("spotify:album:" + tid))
        self.assertIsNone(mg.track_id("not an id"))


class ApproveCommand(unittest.TestCase):
    """cmd_approve against a temporary work dir (no network)."""

    def setUp(self):
        import contextlib
        import io
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE)
        mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE = root, root / "approvals.json", "T"
        wd = root / "t"
        wd.mkdir()
        self.src = {"name": "T", "id": "p", "items": [
            local(0, "A", "a", 100), local(1, "B", "b", 100), local(2, "C", "c", 100)]}
        (wd / "source.json").write_text(json.dumps(self.src))
        (wd / "matches.json").write_text(json.dumps({
            "0": {"verdict": "title_variant", "uri": "spotify:track:A", "keep_local": True, "approved": False, "key": mg.song_key(self.src["items"][0])},
            "1": {"verdict": "carried", "uri": None, "keep_local": True, "approved": False, "key": mg.song_key(self.src["items"][1]), "carried_from": "P"},
            "2": {"verdict": "duration_mismatch", "uri": "spotify:track:C", "title": "C", "artist": "c", "key": mg.song_key(self.src["items"][2])},
        }))
        mg.GLOBAL_APPROVALS.write_text(json.dumps({
            mg.song_key(self.src["items"][0]): {"decision": "keep_local", "uri": None, "from_playlist": "T"},
            mg.song_key(self.src["items"][1]): {"decision": "keep_local", "uri": None, "from_playlist": "P"},
        }))
        self.wd = wd
        self.quiet = contextlib.redirect_stdout(io.StringIO())

    def tearDown(self):
        mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE = self.saved
        self.tmp.cleanup()

    def _args(self, **kw):
        import argparse
        base = {"reasons": None, "pos": None, "keep_local": None, "recheck": None, "unkeep": None, "batch": None}
        base.update(kw)
        return argparse.Namespace(**base)

    def _cache(self):
        return json.loads((self.wd / "matches.json").read_text())

    def test_unkeep_reopens_and_forgets_standing_rule(self):
        with self.quiet:
            mg.cmd_approve(self._args(unkeep="0"))
        c = self._cache()
        self.assertNotIn("keep_local", c["0"])
        self.assertNotIn(mg.song_key(self.src["items"][0]), mg.load_global())

    def test_unkeep_on_carried_entry_drops_it_for_resarch(self):
        with self.quiet:
            mg.cmd_approve(self._args(unkeep="1"))
        self.assertNotIn("1", self._cache())

    def test_unkeep_overlapping_another_option_is_refused(self):
        with self.assertRaises(SystemExit), self.quiet:
            mg.cmd_approve(self._args(unkeep="0", pos="0"))
        self.assertTrue(self._cache()["0"]["keep_local"])

    def test_approve_by_reason_skips_keep_local_and_approves_the_rest(self):
        with self.quiet:
            mg.cmd_approve(self._args(reasons="duration_mismatch,title_variant"))
        c = self._cache()
        self.assertTrue(c["0"]["keep_local"])
        self.assertTrue(c["2"]["approved"])

    def test_recheck_reopens_an_approved_song_and_forgets_the_rule(self):
        with self.quiet:
            mg.cmd_approve(self._args(pos="2"))
        self.assertIn(mg.song_key(self.src["items"][2]), mg.load_global())
        with self.quiet:
            mg.cmd_approve(self._args(recheck="2"))
        c = self._cache()
        self.assertTrue(c["2"]["recheck"])
        self.assertNotIn("approved", c["2"])
        self.assertFalse(mg.wanted(c["2"]))
        self.assertNotIn(mg.song_key(self.src["items"][2]), mg.load_global())
        rows = list(csv.DictReader((self.wd / "flagged.csv").open()))
        self.assertEqual([(r["pos"], r["reason"]) for r in rows], [("2", "duration_mismatch (recheck)")])
        summary = json.loads((self.wd / "summary.json").read_text())
        self.assertEqual(summary["counts"].get("recheck"), 1)
        self.assertEqual(summary["in_playlist_after_build"], 0)

    def test_recheck_warns_about_a_twin_still_wanted_and_blocks_global_reapply(self):
        twin = local(3, "C", "c", 100)
        self.src["items"].append(twin)
        (self.wd / "source.json").write_text(json.dumps(self.src))
        c = self._cache()
        c["3"] = {"verdict": "duration_mismatch", "uri": "spotify:track:C", "title": "C", "artist": "c", "key": mg.song_key(twin)}
        (self.wd / "matches.json").write_text(json.dumps(c))
        with self.quiet:
            mg.cmd_approve(self._args(reasons="duration_mismatch"))  # approves #2 and #3, records the standing rule
        err = io.StringIO()
        with self.quiet, contextlib.redirect_stderr(err):
            mg.cmd_approve(self._args(recheck="2"))
        self.assertIn("also at #3", err.getvalue())
        c = self._cache()
        self.assertTrue(c["2"]["recheck"])
        self.assertNotIn("approved", c["2"])
        self.assertTrue(c["3"]["approved"])  # the twin is untouched; the note tells the owner
        # a standing rule recorded later for the same song must not quietly re-approve the open entry
        mg.record_global(twin, c["3"], "approve", "T")
        self.assertEqual(mg.apply_global(self.src, c), 0)
        self.assertNotIn("approved", c["2"])
        # naming both twins in one --recheck gives no note about the other
        with self.quiet:
            mg.cmd_approve(self._args(pos="2"))
        err = io.StringIO()
        with self.quiet, contextlib.redirect_stderr(err):
            mg.cmd_approve(self._args(recheck="2,3"))
        self.assertNotIn("also at", err.getvalue())
        c = self._cache()
        self.assertTrue(c["2"]["recheck"] and c["3"]["recheck"])

    def test_recheck_on_keep_local_is_refused(self):
        err = io.StringIO()
        with self.quiet, contextlib.redirect_stderr(err):
            mg.cmd_approve(self._args(recheck="0"))
        self.assertNotIn("recheck", self._cache()["0"])
        self.assertIn("ignored #0: keep-local", err.getvalue())

    def test_bulk_approve_leaves_rechecks_alone_but_explicit_pos_resolves(self):
        with self.quiet:
            mg.cmd_approve(self._args(recheck="2"))
        err = io.StringIO()
        with self.quiet, contextlib.redirect_stderr(err):
            mg.cmd_approve(self._args(reasons="duration_mismatch"))
        self.assertTrue(self._cache()["2"]["recheck"])
        self.assertIn("skipped #2: marked recheck", err.getvalue())
        with self.quiet:
            mg.cmd_approve(self._args(pos="2"))
        c = self._cache()
        self.assertTrue(c["2"]["approved"])
        self.assertNotIn("recheck", c["2"])
        self.assertTrue(mg.wanted(c["2"]))


class RetryAfter(unittest.TestCase):
    def test_parses_int_float_and_garbage(self):
        self.assertEqual(mg._retry_after({"Retry-After": "5"}), 6)
        self.assertEqual(mg._retry_after({"Retry-After": "77000.5"}), 77001)
        self.assertEqual(mg._retry_after({"Retry-After": "Wed, 21 Oct"}), 3)
        self.assertEqual(mg._retry_after({}), 3)


if __name__ == "__main__":
    unittest.main()


class CarryKnownMatches(unittest.TestCase):
    """Exact matches found in one playlist are reused in others without a search."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE)
        mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE = root, root / "approvals.json", "T"
        self.root = root

    def tearDown(self):
        mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE = self.saved
        self.tmp.cleanup()

    def _playlist(self, slug, name, items, cache):
        wd = self.root / slug
        wd.mkdir()
        (wd / "source.json").write_text(json.dumps({"name": name, "id": slug, "items": items}))
        (wd / "matches.json").write_text(json.dumps(cache))
        return wd

    @staticmethod
    def _match(it, uri, **extra):
        m = {"verdict": "match", "score": 10, "uri": uri, "title": it["title"], "artist": it["artist"],
             "album": "A", "duration_ms": it["duration_ms"], "dur_diff_ms": 0, "batch": "d", "key": mg.song_key(it)}
        m.update(extra)
        return m

    def test_known_matches_collects_only_clean_exact_matches(self):
        items = [local(0, "Good", "g", 200), local(1, "Kept", "k", 200), local(2, "Recheck", "r", 200),
                 local(3, "Flagged", "f", 200), local(4, "NoUri", "n", 200), local(5, "Moved", "m", 200),
                 local(6, "", "", 200), local(7, "Copy", "c", 200), local(8, "Legacy", "l", 200)]
        self._playlist("p", "P", items, {
            "0": self._match(items[0], "spotify:track:good"),
            "1": self._match(items[1], "spotify:track:kept", keep_local=True),
            "2": self._match(items[2], "spotify:track:re", recheck=True),
            "3": {**self._match(items[3], "spotify:track:fl"), "verdict": "title_variant"},
            "4": self._match(items[4], None),
            "5": {**self._match(items[5], "spotify:track:mv"), "key": "other|song|1"},
            "6": self._match(items[6], "spotify:track:anon"),
            "7": self._match(items[7], "spotify:track:copy", carried_from="Elsewhere"),  # a copy, not a search result
            "8": {k: v for k, v in self._match(items[8], "spotify:track:old").items() if k != "key"},  # pre-key entry
        })
        known = mg.known_matches()
        self.assertEqual(set(known), {mg.song_key(items[0])})
        self.assertEqual(known[mg.song_key(items[0])]["uri"], "spotify:track:good")
        self.assertEqual(known[mg.song_key(items[0])]["from_playlist"], "P")

    def test_known_matches_tolerates_missing_or_broken_files(self):
        (self.root / "empty").mkdir()
        (self.root / "broken").mkdir()
        (self.root / "broken" / "source.json").write_text("{not json")
        (self.root / "broken" / "matches.json").write_text("{}")
        (self.root / "current").write_text("x")  # the marker file, not a work dir
        self.assertEqual(mg.known_matches(), {})

    def test_carry_unsearched_reuses_known_match_but_decision_wins(self):
        a, b, c, d = local(0, "A", "a", 100), local(1, "B", "b", 100), local(2, "C", "c", 100), local(3, "D", "d", 100)
        self._playlist("p", "P", [a, b, c], {
            "0": self._match(a, "spotify:track:A"),
            "1": self._match(b, "spotify:track:B"),
            "2": self._match(c, "spotify:track:C"),
        })
        mg.GLOBAL_APPROVALS.write_text(json.dumps({
            mg.song_key(b): {"decision": "keep_local", "uri": None, "from_playlist": "P"},
        }))
        cache = {}
        rest, decided, matched = mg.carry_unsearched({"name": "T", "items": [a, b, d]}, cache, [a, b, d], "today")
        self.assertEqual([it["pos"] for it in rest], [3])
        self.assertEqual((decided, matched), (1, 1))
        self.assertEqual(cache["0"]["verdict"], "match")
        self.assertEqual(cache["0"]["uri"], "spotify:track:A")
        self.assertEqual(cache["0"]["carried_from"], "P")
        self.assertEqual(cache["0"]["key"], mg.song_key(a))
        self.assertEqual(cache["0"]["batch"], "today")
        self.assertTrue(mg.wanted(cache["0"]))
        self.assertTrue(cache["1"]["keep_local"])  # the standing decision, not the exact match
        self.assertEqual(cache["1"]["verdict"], "carried")
        self.assertNotIn("3", cache)

    def test_carry_unsearched_ignores_files_without_identity(self):
        anon = local(0, "", "", 100)
        self._playlist("p", "P", [anon], {"0": self._match(anon, "spotify:track:anon")})
        cache = {}
        rest, decided, matched = mg.carry_unsearched({"name": "T", "items": [anon]}, cache, [anon], "today")
        self.assertEqual(len(rest), 1)
        self.assertEqual((decided, matched), (0, 0))

    def test_cmd_match_searches_each_distinct_song_once(self):
        import argparse
        import contextlib
        import io
        hit = local(0, "Known", "k", 100)
        self._playlist("p", "P", [hit], {"0": self._match(hit, "spotify:track:known")})
        dup1, dup2 = local(1, "Twice", "t", 150), local(3, "Twice", "t", 150)
        other = local(2, "Other", "o", 120)
        self._playlist("t", "T", [local(0, "Known", "k", 100), dup1, other, dup2], {})
        calls = []

        def fake_search(it):
            calls.append(it["pos"])
            return [cand(it["title"], [it["artist"]], it["duration_ms"], uri=f"spotify:track:{it['pos']}")]

        orig = mg.search_candidates
        mg.search_candidates = fake_search
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                mg.cmd_match(argparse.Namespace(limit=None))
        finally:
            mg.search_candidates = orig
        self.assertEqual(calls, [1, 2])
        cache = json.loads((self.root / "t" / "matches.json").read_text())
        self.assertEqual(len(cache), 4)  # the trailing duplicate was saved too
        self.assertEqual(cache["0"]["uri"], "spotify:track:known")
        self.assertEqual(cache["0"]["carried_from"], "P")
        self.assertEqual(cache["3"]["uri"], "spotify:track:1")
        self.assertEqual(cache["3"]["carried_from"], "T")
        self.assertEqual({k: v["verdict"] for k, v in cache.items()}, {k: "match" for k in "0123"})
        summary = json.loads((self.root / "t" / "summary.json").read_text())
        self.assertEqual(summary["searched"], 4)
        self.assertEqual(summary["in_playlist_after_build"], 4)


class MatchAfterReexport(unittest.TestCase):
    """`match` must align the cache first: a re-export that inserts a song shifts every position."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE)
        mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE = root, root / "approvals.json", "T"
        self.wd = root / "t"
        self.wd.mkdir()
        self.a, self.b = local(0, "A", "a", 100), local(1, "B", "b", 100)
        self.new = local(0, "New", "n", 100)
        old = {str(it["pos"]): CarryKnownMatches._match(it, f"spotify:track:{it['title']}") for it in (self.a, self.b)}
        (self.wd / "matches.json").write_text(json.dumps(old))
        # re-exported: New inserted at the top, A and B shift down by one
        items = [self.new, dict(self.a, pos=1), dict(self.b, pos=2)]
        (self.wd / "source.json").write_text(json.dumps({"name": "T", "id": "t", "items": items}))
        self.calls = []
        self.orig = mg.search_candidates
        mg.search_candidates = self._search

    def tearDown(self):
        mg.search_candidates = self.orig
        mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE = self.saved
        self.tmp.cleanup()

    def _search(self, it):
        self.calls.append(it["pos"])
        return [cand(it["title"], [it["artist"]], it["duration_ms"], uri=f"spotify:track:{it['title']}")]

    def _run(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            mg.cmd_match(argparse.Namespace(limit=None))
        return json.loads((self.wd / "matches.json").read_text())

    def test_only_the_new_song_is_searched_and_old_entries_follow_their_songs(self):
        cache = self._run()
        self.assertEqual(self.calls, [0])
        self.assertEqual({k: v["uri"] for k, v in cache.items()},
                         {"0": "spotify:track:New", "1": "spotify:track:A", "2": "spotify:track:B"})
        self.assertEqual(cache["1"]["key"], mg.song_key(self.a))
        self.assertNotIn("carried_from", cache["1"])

    def test_standing_decision_for_the_new_song_does_not_land_on_the_shifted_entry(self):
        mg.GLOBAL_APPROVALS.write_text(json.dumps({
            mg.song_key(self.new): {"decision": "approve", "uri": "spotify:track:Elsewhere", "from_playlist": "P"},
        }))
        cache = self._run()
        self.assertEqual(self.calls, [])
        self.assertEqual(cache["0"]["uri"], "spotify:track:Elsewhere")
        self.assertTrue(cache["0"]["approved"])
        self.assertEqual(cache["1"]["uri"], "spotify:track:A")  # untouched: still A's own exact match
        self.assertNotIn("approved", cache["1"])
        self.assertNotIn("carried_from", cache["1"])


class SearchCandidates(unittest.TestCase):
    def test_null_items_in_search_results_are_skipped(self):
        it = local(0, "Song", "Artist", 100)
        good = cand("Song", ["Artist"], 100000, uri="spotify:track:good")
        good["id"] = "good"
        orig = mg.api_get
        mg.api_get = lambda path, **params: {"tracks": {"items": [None, good]}}
        try:
            cands = mg.search_candidates(it)
        finally:
            mg.api_get = orig
        self.assertEqual([c["uri"] for c in cands], ["spotify:track:good"])


class BuildCommand(unittest.TestCase):
    """cmd_build end to end against a fake API: create, rewrite and merge modes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE, mg._call, mg.resolve_playlist, mg.live_uris)
        mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE = root, root / "approvals.json", "T"
        self.wd = root / "t"
        self.wd.mkdir()
        self.calls = []
        self.fail_on = None  # (method, path-substring) that raises RuntimeError
        self.live = []
        mg._call = self._call
        mg.resolve_playlist = lambda ref: {"id": "PID", "name": "P", "external_urls": {"spotify": "https://open.spotify.com/playlist/PID"}}
        mg.live_uris = lambda pid: list(self.live)

    def tearDown(self):
        mg.WORK_ROOT, mg.GLOBAL_APPROVALS, mg.WORK_OVERRIDE, mg._call, mg.resolve_playlist, mg.live_uris = self.saved
        self.tmp.cleanup()

    def _call(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if self.fail_on and method == self.fail_on[0] and self.fail_on[1] in path:
            raise RuntimeError("boom")
        if method == "POST" and path == "/me/playlists":
            return {"id": "NEW", "external_urls": {"spotify": "https://open.spotify.com/playlist/NEW"}}
        if method == "GET":
            return {"items": {"total": 7}, "snapshot_id": "s0"}
        return {"snapshot_id": f"s{len(self.calls)}"}

    def _source(self, n_local, n_remote=0):
        items = [remote(i, f"spotify:track:R{i}") for i in range(n_remote)]
        items += [local(n_remote + i, f"L{i}", "x", 100) for i in range(n_local)]
        cache = {str(it["pos"]): {"verdict": "match", "uri": f"spotify:track:{it['title']}", "key": mg.song_key(it)}
                 for it in items if it["is_local"]}
        (self.wd / "source.json").write_text(json.dumps({"name": "T", "id": "t", "items": items}))
        (self.wd / "matches.json").write_text(json.dumps(cache))
        return items

    def _args(self, **kw):
        base = {"name": None, "update": None, "new": False, "keep_duplicates": False, "dry_run": False}
        base.update(kw)
        return argparse.Namespace(**base)

    def _build(self, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            mg.cmd_build(self._args(**kw))
        return out.getvalue(), err.getvalue()

    def test_create_remembers_the_playlist_before_adding_tracks(self):
        self._source(3)
        self.fail_on = ("POST", "/playlists/NEW/items")
        with self.assertRaises(RuntimeError), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            mg.cmd_build(self._args())
        res = json.loads((self.wd / "result.json").read_text())
        self.assertEqual((res["playlist_id"], res["count"]), ("NEW", 0))
        self.assertEqual(self.calls[0][0:2], ("POST", "/me/playlists"))
        self.assertEqual(self.calls[0][2]["name"], "T (Spotify version)")
        self.assertEqual(self.calls[1], ("PUT", "/playlists/NEW", {"public": False}))

    def test_create_adds_in_batches_of_100_and_records_the_result(self):
        self._source(150)
        self._build()
        adds = [c for c in self.calls if c[0] == "POST" and c[1] == "/playlists/NEW/items"]
        self.assertEqual([len(c[2]["uris"]) for c in adds], [100, 50])
        self.assertNotIn("position", adds[0][2])
        res = json.loads((self.wd / "result.json").read_text())
        self.assertEqual((res["playlist_id"], res["count"], res["on_spotify"], res["duplicates_collapsed"]), ("NEW", 150, 7, 0))

    def test_existing_playlist_without_local_files_is_rewritten(self):
        self._source(120)
        (self.wd / "result.json").write_text(json.dumps({"playlist_id": "PID", "url": "u", "count": 0}))
        self.live = ["spotify:track:OLD"]
        self._build()
        writes = [c for c in self.calls if c[1] == "/playlists/PID/items"]
        self.assertEqual([(c[0], len(c[2]["uris"])) for c in writes], [("PUT", 100), ("POST", 20)])
        self.assertNotIn(("POST", "/me/playlists"), [c[0:2] for c in self.calls])

    def test_existing_playlist_with_local_files_is_merged_then_reordered(self):
        items = self._source(3, n_remote=1)
        cache = json.loads((self.wd / "matches.json").read_text())
        cache["3"] = {"verdict": "no_match", "uri": None, "keep_local": True, "approved": False, "key": mg.song_key(items[3])}
        (self.wd / "matches.json").write_text(json.dumps(cache))
        (self.wd / "result.json").write_text(json.dumps({"playlist_id": "PID", "url": "u", "count": 0}))
        # source order: R0, L0, L1, L2(local). Live: L1 missing, and L0 sits after the pasted local file.
        self.live = ["spotify:track:R0", items[3]["uri"], "spotify:track:L0"]
        out, _ = self._build()
        self.assertIn("Merging into", out)
        posts = [c for c in self.calls if c[0] == "POST"]
        self.assertEqual(posts, [("POST", "/playlists/PID/items", {"uris": ["spotify:track:L1"], "position": 1})])
        moves = [c[2] for c in self.calls if c[0] == "PUT" and c[1] == "/playlists/PID/items"]
        # live_uris is static in this test, so the reorder plan is against the pre-merge list: L0 moves up
        self.assertEqual([(m["range_start"], m["insert_before"]) for m in moves], [(2, 1)])
        self.assertTrue(all("snapshot_id" in m for m in moves))

    def test_dry_run_makes_no_write_calls(self):
        items = self._source(2)
        (self.wd / "result.json").write_text(json.dumps({"playlist_id": "PID", "url": "u", "count": 0}))
        self.live = ["spotify:track:L1", items[0]["uri"]]
        out, _ = self._build(dry_run=True)
        self.assertIn("would insert 1 track(s) in 1 call(s)", out)
        self.assertEqual([c for c in self.calls if c[0] != "GET"], [])
        self.live = ["spotify:track:OLD"]
        out, _ = self._build(dry_run=True)
        self.assertIn("would replace the 1 item(s)", out)
        self.assertEqual([c for c in self.calls if c[0] != "GET"], [])

    def test_keep_duplicates_is_flagged_as_ignored_when_merging(self):
        items = self._source(1)
        (self.wd / "result.json").write_text(json.dumps({"playlist_id": "PID", "url": "u", "count": 0}))
        self.live = ["spotify:track:L0", items[0]["uri"]]
        _, err = self._build(keep_duplicates=True, dry_run=True)
        self.assertIn("--keep-duplicates is ignored", err)
