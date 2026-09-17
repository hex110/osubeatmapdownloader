"""Parsing and matching: the small pure functions everything else is built on."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import osu_core as core


class ParseIds(unittest.TestCase):
    def test_accepts_the_shapes_people_actually_paste(self):
        self.assertEqual(core.parse_ids("118\nhttps://osu.ppy.sh/beatmapsets/914756#osu/1911308\n"
                                        "https://osu.ppy.sh/s/80\n  3  "), ["118", "914756", "80", "3"])

    def test_skips_blanks_and_duplicates(self):
        self.assertEqual(core.parse_ids("118\n\n118\n \n80"), ["118", "80"])

    def test_no_ids_at_all(self):
        self.assertEqual(core.parse_ids("just some words\n"), [])


class ParseTracks(unittest.TestCase):
    def test_plain_lines(self):
        self.assertEqual(core.parse_tracks("Yorushika - Say It\nCamellia - GHOST"),
                         [("Yorushika", "Say It"), ("Camellia", "GHOST")])

    def test_numbering_and_dash_variants(self):
        self.assertEqual(core.parse_tracks("1. A - B\n2) C – D\nE — F"),
                         [("A", "B"), ("C", "D"), ("E", "F")])

    def test_line_without_a_separator_is_a_title(self):
        self.assertEqual(core.parse_tracks("Bad Apple!!"), [("", "Bad Apple!!")])

    def test_exported_csv(self):
        csv = ('"Track URI","Track Name","Artist Name(s)","Album Name"\n'
               '"spotify:track:x","Believer","Imagine Dragons","Evolve"\n'
               '"spotify:track:y","Say It","Yorushika, n-buna","Elma"')
        self.assertEqual(core.parse_tracks(csv),
                         [("Imagine Dragons", "Believer"), ("Yorushika", "Say It")])

    def test_empty(self):
        self.assertEqual(core.parse_tracks(""), [])
        self.assertEqual(core.parse_tracks("   \n\n"), [])


class ParseLinks(unittest.TestCase):
    def test_collection_links(self):
        for text in ("https://osucollector.com/collections/23333",
                     "osucollector.com/collections/23333?x=1", "23333"):
            self.assertEqual(core.parse_collection_id(text), "23333")

    def test_bad_collection_link(self):
        with self.assertRaises(ValueError):
            core.parse_collection_id("https://example.com/nope")

    def test_spotify_links(self):
        pid = "37i9dQZF1DXcBWIGoYBM5M"
        for text in (f"https://open.spotify.com/playlist/{pid}?si=abc",
                     f"spotify:playlist:{pid}", pid):
            self.assertEqual(core.parse_spotify_link(text), ("playlist", pid))
        self.assertEqual(core.parse_spotify_link("https://open.spotify.com/album/1ATL5GLyefJaxhQzSPVrLX"),
                         ("album", "1ATL5GLyefJaxhQzSPVrLX"))

    def test_bad_spotify_link(self):
        with self.assertRaises(ValueError):
            core.parse_spotify_link("not a link")


class OszNames(unittest.TestCase):
    def test_uses_the_mirror_s_filename(self):
        name = core._osz_name("797", 'attachment; filename="797 m-flo - Love Comes and Goes.osz"', {})
        self.assertEqual(name, "797 m-flo - Love Comes and Goes.osz")

    def test_a_hostile_filename_cannot_escape_the_folder(self):
        import tempfile
        folder = Path(tempfile.mkdtemp()).resolve()
        for evil in ('attachment; filename="../../etc/passwd.osz"',
                     'attachment; filename="/etc/passwd.osz"',
                     'attachment; filename="..\\..\\windows\\system32.osz"'):
            name = core._osz_name("797", evil, {})
            # the only thing that matters: it still lands inside the download folder
            self.assertEqual((folder / name).resolve().parent, folder, f"escaped via {evil!r}")

    def test_a_title_containing_a_slash_survives(self):
        name = core._osz_name("797", None, {"artist": "m-flo", "title": "Love: Comes/Goes"})
        self.assertIn("Comes_Goes", name)

    def test_falls_back_to_the_bare_id(self):
        self.assertEqual(core._osz_name("797", None, {}), "797.osz")

    def test_find_osz_recognises_whatever_we_named_it(self):
        import tempfile
        folder = tempfile.mkdtemp()
        for disposition in ('attachment; filename="797 m-flo - Love.osz"', None):
            name = core._osz_name("797", disposition, {})
            Path(folder, name).write_bytes(b"PK")
            self.assertTrue(core.find_osz(folder, "797"), f"find_osz missed {name!r}")
            Path(folder, name).unlink()


class NameKeys(unittest.TestCase):
    """Maps older than osu! file format v10 carry no ID and match on name instead."""

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(core.name_key("TRF", "Survival dAnce", "Echo49"),
                         core.name_key("trf ", " survival  dance", "echo49"))

    def test_the_mapper_keeps_different_sets_apart(self):
        # there are a dozen "The Quick Brown Fox - The Big Black" sets by different mappers
        self.assertNotEqual(core.name_key("The Quick Brown Fox", "The Big Black", "Blue Dragon"),
                            core.name_key("The Quick Brown Fox", "The Big Black", "Rugure"))


class Scoring(unittest.TestCase):
    def candidate(self, artist, title, plays=0, favourites=0, status="graveyard"):
        return {"id": "1", "artist": artist, "title": title, "creator": "x",
                "status": status, "favourites": favourites, "plays": plays, "diffs": 1}

    def test_the_name_matters_more_than_popularity(self):
        right = core.score_candidate("Camellia", "GHOST", self.candidate("Camellia", "GHOST"))
        wrong = core.score_candidate("Camellia", "GHOST",
                                     self.candidate("Camellia", "Songs Compilation 2", plays=999999))
        self.assertGreater(right[0], wrong[0])

    def test_popularity_breaks_a_tie_between_identical_names(self):
        busy = core.score_candidate("A", "B", self.candidate("A", "B", plays=280000))
        quiet = core.score_candidate("A", "B", self.candidate("A", "B", plays=50))
        self.assertGreater(busy[0], quiet[0])

    def test_bracketed_qualifiers_are_ignored_when_comparing(self):
        self.assertEqual(core._norm_song("Believer (TV Size)"), core._norm_song("Believer"))
        self.assertEqual(core._norm_song("Say it. feat. someone"), core._norm_song("Say it"))

    def test_a_nonsense_song_does_not_clear_the_floor(self):
        score, text = core.score_candidate("Nobody", "zzzz xyzzy nothing",
                                           self.candidate("Camellia", "GHOST"))
        self.assertLess(text, core.MATCH_FLOOR)


if __name__ == "__main__":
    unittest.main()
