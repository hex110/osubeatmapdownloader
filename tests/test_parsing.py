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
        score, text, title = core.score_candidate("Nobody", "zzzz xyzzy nothing",
                                                  self.candidate("Camellia", "GHOST"))
        self.assertLess(text, core.MATCH_FLOOR)
        self.assertLess(title, core.TITLE_FLOOR)


if __name__ == "__main__":
    unittest.main()


class FetchFilters(unittest.TestCase):
    """A set is kept when any of its difficulties fits, since downloading brings them all."""

    def entry(self, *diffs, status="ranked"):
        maps = [{"mode": m, "difficulty_rating": r} for m, r in diffs]
        return {"beatmaps": maps, "status": status}

    def played(self, mode, stars, status="ranked"):
        return {"beatmap": {"mode": mode, "difficulty_rating": stars, "status": status},
                "beatmapset": {"status": status}}

    def test_no_filters_keeps_everything(self):
        self.assertTrue(core.wanted(self.entry(("mania", 9.0)), "favourite"))

    def test_mode(self):
        entry = self.entry(("osu", 4.0), ("mania", 3.0))
        self.assertTrue(core.wanted(entry, "favourite", mode="mania"))
        self.assertFalse(core.wanted(entry, "favourite", mode="taiko"))

    def test_star_range(self):
        entry = self.entry(("osu", 2.0), ("osu", 5.5))
        self.assertTrue(core.wanted(entry, "favourite", stars=(5, 6)))
        self.assertFalse(core.wanted(entry, "favourite", stars=(6, 8)))
        self.assertTrue(core.wanted(entry, "favourite", stars=(0, 3)))

    def test_mode_and_stars_must_hold_on_the_same_difficulty(self):
        entry = self.entry(("osu", 2.0), ("mania", 7.0))
        self.assertFalse(core.wanted(entry, "favourite", mode="osu", stars=(6, 8)))
        self.assertTrue(core.wanted(entry, "favourite", mode="mania", stars=(6, 8)))

    def test_ranked_only(self):
        self.assertFalse(core.wanted(self.entry(("osu", 4.0), status="graveyard"),
                                     "favourite", ranked_only=True))
        self.assertTrue(core.wanted(self.entry(("osu", 4.0), status="loved"),
                                    "favourite", ranked_only=True))

    def test_most_played_is_judged_on_its_one_difficulty(self):
        self.assertTrue(core.wanted(self.played("osu", 4.5), "most_played", stars=(4, 5)))
        self.assertFalse(core.wanted(self.played("osu", 1.2), "most_played", stars=(4, 5)))

    def test_an_entry_with_no_difficulty_data_is_kept(self):
        self.assertTrue(core.wanted({"beatmaps": []}, "favourite", mode="osu", stars=(4, 5)))


class SpotifyEmbedShape(unittest.TestCase):
    """The embed page's JSON moves around, so the track list is found by shape."""

    def test_finds_the_track_list_however_deeply_it_is_buried(self):
        blob = {"props": {"pageProps": {"state": {"data": {"entity": {
            "name": "My Playlist",
            "trackList": [{"title": "Believer", "subtitle": "Imagine Dragons"}]}}}}}}
        entry = core._find_track_list(blob)
        self.assertEqual(entry["name"], "My Playlist")
        self.assertEqual(len(entry["trackList"]), 1)

    def test_returns_nothing_when_there_is_no_track_list(self):
        self.assertIsNone(core._find_track_list({"props": {"a": [1, 2, {"b": "c"}]}}))


class ExportifyCsv(unittest.TestCase):
    """The file Exportify actually produces, byte for byte."""

    HEADER = ("﻿Track URI,Track Name,Album Name,Artist Name(s),Release Date,Duration (ms),"
              "Popularity,Explicit,Added By,Added At,Genres,Record Label\n")

    def test_the_byte_order_mark_and_column_order(self):
        # Exportify writes a BOM and puts Album before Artist
        csv = self.HEADER + 'spotify:track:x,"Believer","Evolve","Imagine Dragons",2017,204000,80,false,u,t,"","Label"\n'
        self.assertEqual(core.parse_tracks(csv), [("Imagine Dragons", "Believer")])

    def test_several_artists_are_separated_by_a_semicolon(self):
        csv = self.HEADER + 'spotify:track:y,"Stargirl Interlude","Starboy","The Weeknd;Lana Del Rey",2016,111640,78,false,u,t,"","XO"\n'
        self.assertEqual(core.parse_tracks(csv), [("The Weeknd", "Stargirl Interlude")])

    def test_a_comma_separated_artist_list_still_works(self):
        csv = self.HEADER + 'spotify:track:z,"Say It","Elma","Yorushika, n-buna",2019,200000,60,false,u,t,"","Label"\n'
        self.assertEqual(core.parse_tracks(csv), [("Yorushika", "Say It")])


class ConfidentMatches(unittest.TestCase):
    """A right artist with a wrong title must not be picked automatically."""

    def candidate(self, text, title_score):
        return {"text": text, "title_score": title_score}

    def test_a_wrong_title_is_not_confident_even_with_the_right_artist(self):
        # "Matt Maltese - little person" scored 0.579 against "As The World Caves In"
        self.assertFalse(core.confident(self.candidate(0.579, 0.353)))

    def test_a_matching_name_is_confident(self):
        self.assertTrue(core.confident(self.candidate(0.95, 1.0)))

    def test_nothing_at_all_is_not_confident(self):
        self.assertFalse(core.confident(None))
        self.assertFalse(core.confident({}))
