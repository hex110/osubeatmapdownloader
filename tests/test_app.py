"""The app's own bookkeeping: the queue, its guards, and what survives a restart."""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def fresh_app(app_dir=None):
    """Import app.py against a throwaway data folder."""
    os.environ["OBD_APP_DIR"] = app_dir or tempfile.mkdtemp()
    for name in ("app",):
        sys.modules.pop(name, None)
    return importlib.import_module("app")


class QueueBookkeeping(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = fresh_app(self.dir)
        self.S = self.app.S
        self.S.history = set()
        # a real (tiny) lazer library, since set_queue re-scans it rather than trusting us
        self.lazer = Path(tempfile.mkdtemp())
        (self.lazer / "files" / "a").mkdir(parents=True)
        self.S.lazer_dir = str(self.lazer)

    def own(self, *texts):
        """Put beatmaps into the fake lazer library."""
        for i, text in enumerate(texts):
            (self.lazer / "files" / "a" / f"h{i}").write_text(text, encoding="utf-8")
        self.S._lazer_at = 0

    def items(self, *ids):
        return [{"id": i, "title": f"T{i}", "artist": "A", "creator": "C", "cover": ""} for i in ids]

    def test_replacing_the_queue(self):
        self.S.set_queue(self.items("1", "2"), source="two maps")
        self.S.set_queue(self.items("3"), source="one map")
        self.assertEqual([i["id"] for i in self.S.queue], ["3"])
        self.assertEqual(self.S.queue_source(), "one map")

    def test_appending_keeps_what_was_there(self):
        self.S.set_queue(self.items("1", "2"), source="two maps")
        self.S.set_queue(self.items("3"), append=True, source="one more")
        self.assertEqual([i["id"] for i in self.S.queue], ["1", "2", "3"])
        self.assertIn("1 more source", self.S.queue_source())

    def test_appending_does_not_duplicate(self):
        self.S.set_queue(self.items("1", "2"))
        self.S.set_queue(self.items("2", "3"), append=True)
        self.assertEqual([i["id"] for i in self.S.queue], ["1", "2", "3"])

    def test_clearing_forgets_where_it_came_from(self):
        self.S.set_queue(self.items("1"), source="somewhere")
        self.app.act_clear_queue({})
        self.assertEqual(self.S.queue, [])
        self.assertEqual(self.S.queue_source(), "")

    def test_maps_already_owned_are_marked(self):
        self.own("osu file format v14\n\n[Metadata]\nBeatmapSetID:2\nTitle:X\n")
        self.S.history = {"3"}
        self.S.set_queue(self.items("1", "2", "3"))
        self.assertEqual([i["status"] for i in self.S.queue], ["queued", "have", "have"])
        self.assertIn("lazer", self.S.queue[1]["note"])

    def test_an_old_map_is_matched_by_name_when_it_has_no_id(self):
        # pre-v10 maps carry no BeatmapSetID, so they match on artist/title/creator
        self.own("osu file format v9\n\n[Metadata]\nTitle:T1\nArtist:A\nCreator:C\n")
        self.S.set_queue(self.items("1"))
        self.assertEqual(self.S.queue[0]["status"], "have")
        self.assertIn("matched by name", self.S.queue[0]["note"])

    def test_a_pasted_list_has_no_names_so_cannot_match_by_name(self):
        self.own("osu file format v9\n\n[Metadata]\nTitle:T1\nArtist:A\nCreator:C\n")
        self.app.act_paste({"text": "1"})
        self.assertEqual(self.S.queue[0]["status"], "queued")


class Persistence(unittest.TestCase):
    def test_the_queue_and_its_source_come_back(self):
        d = tempfile.mkdtemp()
        app = fresh_app(d)
        app.S.set_queue([{"id": "1", "title": "T", "artist": "A", "creator": "C", "cover": ""}],
                        source="5 maps from somewhere")
        app.S.save_queue(force=True)
        again = fresh_app(d)
        self.assertEqual(len(again.S.queue), 1)
        self.assertEqual(again.S.queue_source(), "5 maps from somewhere")

    def test_a_map_caught_mid_download_is_queued_again(self):
        d = tempfile.mkdtemp()
        Path(d, "data").mkdir(parents=True, exist_ok=True)
        Path(d, "data", "queue.json").write_text(json.dumps(
            {"queue": [{"id": "1", "status": "downloading"}, {"id": "2", "status": "done"}],
             "sources": ["x"]}))
        app = fresh_app(d)
        self.assertEqual([i["status"] for i in app.S.queue], ["queued", "done"])

    def test_a_queue_file_from_the_previous_version_still_loads(self):
        d = tempfile.mkdtemp()
        Path(d, "data").mkdir(parents=True, exist_ok=True)
        Path(d, "data", "queue.json").write_text(json.dumps([{"id": "1", "status": "done"}]))
        app = fresh_app(d)
        self.assertEqual(len(app.S.queue), 1)
        self.assertEqual(app.S.queue_source(), "")

    def test_an_unreadable_queue_file_does_not_stop_the_app(self):
        d = tempfile.mkdtemp()
        Path(d, "data").mkdir(parents=True, exist_ok=True)
        Path(d, "data", "queue.json").write_text("{{{ not json")
        app = fresh_app(d)
        self.assertEqual(app.S.queue, [])

    def test_matches_survive_a_restart(self):
        d = tempfile.mkdtemp()
        app = fresh_app(d)
        app.S.matches = [{"artist": "A", "title": "B", "pick": 2, "include": True,
                          "candidates": [{"id": str(i), "text": 0.9} for i in range(3)]}]
        app.S.save_matches()
        again = fresh_app(d)
        self.assertEqual(len(again.S.matches), 1)
        self.assertEqual(again.S.matches[0]["pick"], 2)


class GuardsWhileDownloading(unittest.TestCase):
    """Nothing may swap the list out from under a running downloader."""

    def setUp(self):
        self.app = fresh_app()
        self.S = self.app.S
        self.S.owned = self.S.owned_lazer = set()
        self.S.owned_lazer_keys = set()
        self.S.history = set()

        class FakeJob:
            class thread:
                @staticmethod
                def is_alive():
                    return True
            signed_out = False
        self.S.job = FakeJob()

    def tearDown(self):
        self.S.job = None

    def refuses(self, fn, *a):
        with self.assertRaises(ValueError) as caught:
            fn(*a)
        return str(caught.exception)

    def test_the_queue_cannot_be_cleared(self):
        self.refuses(self.app.act_clear_queue, {})

    def test_a_new_list_cannot_be_pasted(self):
        self.refuses(self.app.act_paste, {"text": "1"})

    def test_playlist_matches_cannot_be_added(self):
        self.S.matches = [{"artist": "A", "title": "B", "pick": 0, "include": True,
                           "candidates": [{"id": "9", "artist": "A", "title": "B", "creator": "C"}]}]
        self.assertIn("Stop the download", self.refuses(self.app.act_match_queue, {}))

    def test_items_cannot_be_toggled(self):
        self.refuses(self.app.act_toggle, {"id": "1"})

    def test_the_download_folder_cannot_move(self):
        self.assertIn("Stop the download", self.refuses(self.app.act_settings, {"folder": "/tmp/elsewhere"}))

    def test_a_second_download_cannot_start(self):
        self.refuses(self.app.act_start, {})


if __name__ == "__main__":
    unittest.main()
