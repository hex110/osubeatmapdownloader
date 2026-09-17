"""Mirror selection, stall detection and the lazer library scan, against a fake server."""
import http.server
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import osu_core as core


class FakeMirror(http.server.BaseHTTPRequestHandler):
    """Serves /ok (a zip), /silent, /stall, /trickle, /missing, /html."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        mode = self.path.strip("/").split("/")[0]
        if mode == "missing":
            self.send_error(404)
            return
        if mode == "html":
            body = b"<!DOCTYPE html><html>nope</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if mode == "silent":
            time.sleep(30)
            return
        body = b"PK" + b"x" * (600 * 1024)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="123 Fake - Map.osz"')
        self.send_header("Content-Length", str(len(body) if mode == "ok" else 50_000_000))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        if mode == "stall":
            time.sleep(30)
        elif mode == "trickle":
            for _ in range(30):
                try:
                    self.wfile.write(b"y" * 50)
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(1)


class MirrorDownloads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMirror)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self._real = core.MIRRORS
        core.MIRROR_SPEEDS.clear()
        core.MIRROR_PACE.clear()

    def tearDown(self):
        core.MIRRORS = self._real
        core.MIRROR_SPEEDS.clear()

    def only(self, *modes):
        core.MIRRORS = tuple((m, f"{self.base}/{m}/{{sid}}", f"{self.base}/{m}/{{sid}}")
                             for m in modes)

    def test_a_good_mirror_downloads(self):
        self.only("ok")
        path, who = core.download_from_mirror("123", self.folder, {})
        self.assertIsNotNone(path, who)
        self.assertEqual(who, "ok")
        self.assertTrue(Path(path).name.startswith("123"))

    def test_a_silent_mirror_gives_up_on_time(self):
        self.only("silent")
        started = time.time()
        path, why = core.download_from_mirror("123", self.folder, {}, stall=2)
        self.assertIsNone(path)
        self.assertLess(time.time() - started, 8, "should have given up after the stall timeout")

    def test_a_mirror_that_goes_quiet_mid_download_gives_up(self):
        self.only("stall")
        started = time.time()
        path, why = core.download_from_mirror("123", self.folder, {}, stall=2)
        self.assertIsNone(path)
        self.assertLess(time.time() - started, 8)

    def test_a_dribbling_mirror_is_caught_too(self):
        self.only("trickle")
        started = time.time()
        path, why = core.download_from_mirror("123", self.folder, {}, stall=2)
        self.assertIsNone(path)
        self.assertLess(time.time() - started, 15, "the trickle guard never fired")

    def test_a_stalled_mirror_fails_over_to_a_working_one(self):
        self.only("stall", "ok")
        path, who = core.download_from_mirror("123", self.folder, {}, stall=2)
        self.assertIsNotNone(path)
        self.assertEqual(who, "ok")

    def test_an_html_page_is_not_a_beatmap(self):
        self.only("html")
        path, why = core.download_from_mirror("123", self.folder, {})
        self.assertIsNone(path)

    def test_everything_missing_says_so_plainly(self):
        self.only("missing", "missing")
        path, why = core.download_from_mirror("123", self.folder, {})
        self.assertIsNone(path)
        self.assertIn("No mirror has", why)

    def test_nothing_is_left_behind_when_a_download_fails(self):
        self.only("stall")
        core.download_from_mirror("123", self.folder, {}, stall=2)
        self.assertEqual([f for f in os.listdir(self.folder) if f.endswith(".part")], [])

    def test_cancelling_a_stalled_download_returns_promptly(self):
        # a healthy download notices the flag between reads; a stalled one notices it when
        # the read times out, so cancelling can never take longer than the stall timeout
        self.only("stall")
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        started = time.time()
        path, why = core.download_from_mirror("123", self.folder, {}, stall=3, stop=stop)
        self.assertIsNone(path)
        self.assertEqual(why, "Cancelled.")
        self.assertLess(time.time() - started, 8)

    def test_cancelling_before_it_starts(self):
        self.only("ok")
        stop = threading.Event()
        stop.set()
        path, why = core.download_from_mirror("123", self.folder, {}, stop=stop)
        self.assertIsNone(path)
        self.assertEqual(why, "Cancelled.")


class MirrorRanking(unittest.TestCase):
    def setUp(self):
        core.MIRROR_SPEEDS.clear()
        core.MIRROR_PACE.clear()
        core._since_probe = 0

    def names(self):
        return [m[0] for m in core.ordered_mirrors()]

    def test_the_fastest_goes_first(self):
        core.record_mirror_speed("catboy.best", 5_000_000, 10)      # 0.5 MB/s
        core.record_mirror_speed("osu.direct", 5_000_000, 1)        # 5 MB/s
        core.record_mirror_speed("nerinyan.moe", 5_000_000, 5)      # 1 MB/s
        core.record_mirror_speed("beatconnect.io", 0, 0, ok=False)
        self.assertEqual(self.names()[0], "osu.direct")
        self.assertEqual(self.names()[-1], "beatconnect.io")

    def test_a_failure_sinks_a_mirror(self):
        core.record_mirror_speed("osu.direct", 5_000_000, 1)
        core.record_mirror_speed("catboy.best", 0, 0, ok=False)
        self.assertLess(self.names().index("osu.direct"), self.names().index("catboy.best"))

    def test_a_sample_too_small_to_time_is_ignored(self):
        core.record_mirror_speed("osu.direct", 1000, 5)
        self.assertNotIn("osu.direct", core.MIRROR_SPEEDS)

    def test_it_does_not_re_probe_on_every_single_map(self):
        for name in [m[0] for m in core.MIRRORS]:
            core.record_mirror_speed(name, 5_000_000, 1)
        core.record_mirror_speed("osu.direct", 50_000_000, 1)  # clearly the best
        firsts = [self.names()[0] for _ in range(core.PROBE_EVERY - 2)]
        self.assertEqual(set(firsts), {"osu.direct"}, "should stick with the winner")

    def test_a_published_budget_is_respected(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=100))
        core.note_rate_limit("catboy.best", {"X-RateLimit-Remaining": "10", "X-RateLimit-Reset": soon})
        self.assertAlmostEqual(core.mirror_pace("catboy.best"), 10, delta=2)
        core.note_rate_limit("catboy.best", {"X-RateLimit-Remaining": "900", "X-RateLimit-Reset": soon})
        self.assertEqual(core.mirror_pace("catboy.best"), 0)

    def test_speeds_survive_a_round_trip(self):
        core.record_mirror_speed("osu.direct", 5_000_000, 1)
        saved = core.speeds_snapshot()
        core.MIRROR_SPEEDS.clear()
        core.restore_speeds(saved)
        self.assertIn("osu.direct", core.MIRROR_SPEEDS)

    def test_rubbish_saved_speeds_are_ignored(self):
        core.restore_speeds({"osu.direct": ["nonsense"], "not-a-mirror": [1, 2]})
        self.assertEqual(core.MIRROR_SPEEDS, {})


class LazerLibrary(unittest.TestCase):
    """lazer names files by hash, so the scan reads the .osu files themselves."""

    def library(self, *maps):
        root = Path(tempfile.mkdtemp())
        files = root / "files"
        for i, text in enumerate(maps):
            sub = files / str(i)
            sub.mkdir(parents=True)
            (sub / f"hash{i}").write_text(text, encoding="utf-8")
        (files / "0" / "cover.png").write_bytes(b"\x89PNG not a beatmap")
        return root

    def test_modern_maps_are_found_by_id(self):
        root = self.library("osu file format v14\n\n[Metadata]\nTitle:X\nBeatmapSetID:12345\n")
        ids, keys, sums = core.scan_lazer_library(root)
        self.assertEqual(ids, {"12345"})
        self.assertEqual(keys, set())
        self.assertEqual(sums, set(), "a map with an ID needs no checksum")

    def test_maps_older_than_the_id_field_fall_back_to_names(self):
        # BeatmapSetID postdates file format v10: The Big Black and friends have none
        root = self.library("osu file format v9\n\n[Metadata]\nTitle:The Big Black\n"
                            "Artist:The Quick Brown Fox\nCreator:Blue Dragon\n")
        ids, keys, sums = core.scan_lazer_library(root)
        self.assertEqual(ids, set())
        self.assertIn(core.name_key("The Quick Brown Fox", "The Big Black", "Blue Dragon"), keys)
        self.assertEqual(len(sums), 1, "an ID-less map is also recorded by checksum")

    def test_unsubmitted_maps_are_not_counted_by_id(self):
        root = self.library("osu file format v14\n\n[Metadata]\nBeatmapSetID:-1\n"
                            "Title:Mine\nArtist:Me\nCreator:Me\n")
        ids, _, _ = core.scan_lazer_library(root)
        self.assertEqual(ids, set())

    def test_a_folder_that_is_not_a_lazer_library(self):
        with self.assertRaises(ValueError):
            core.scan_lazer_library(tempfile.mkdtemp())


class ChildEnvironment(unittest.TestCase):
    """osu!lazer is an AppImage and mounts itself under TMPDIR, so it must get the real one."""

    def test_the_temp_redirect_is_undone_for_children(self):
        os.environ.update({"TMPDIR": "/app/data/temp", "OBD_ORIGINAL_TMPDIR": "/tmp",
                           "TEMP": "/app/data/temp", "OBD_ORIGINAL_TEMP": "C:/Real"})
        env = core._child_env()
        self.assertEqual(env["TMPDIR"], "/tmp")
        self.assertEqual(env["TEMP"], "C:/Real")
        self.assertFalse([k for k in env if k.startswith("OBD_ORIGINAL")])

    def test_a_variable_that_was_unset_is_removed_not_blanked(self):
        os.environ.update({"TMPDIR": "/app/data/temp", "OBD_ORIGINAL_TMPDIR": ""})
        self.assertNotIn("TMPDIR", core._child_env())


if __name__ == "__main__":
    unittest.main()
