"""osu! Beatmap Downloader: local web UI.

Run `python app.py` (or start.bat, or the built exe) and a browser tab opens. Everything stays on
this machine: the server only listens on 127.0.0.1.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import osu_core as core

if "--app-dir" in sys.argv:  # before the paths below are worked out
    os.environ["OBD_APP_DIR"] = sys.argv[sys.argv.index("--app-dir") + 1]

FROZEN = getattr(sys, "frozen", False)  # running as the PyInstaller build
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # bundled resources
# Portable: everything the app creates lives next to the exe (or the project folder from source).
# OBD_APP_DIR moves everything the app writes somewhere else: handy for a second copy, and
# it's what lets the tests run without touching the real data folder.
APP_DIR = Path(os.environ["OBD_APP_DIR"]).expanduser() if os.environ.get("OBD_APP_DIR") \
    else (Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent)


def _writable(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


DATA = APP_DIR / "data"
if not _writable(DATA):
    # e.g. unzipped into Program Files, so fall back to the user's AppData
    APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "osu! Beatmap Downloader"
    DATA = APP_DIR / "data"
    DATA.mkdir(parents=True, exist_ok=True)
DEFAULT_DOWNLOADS = APP_DIR / "downloads"
TEMP_DIR = DATA / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
# Selenium Manager caches ChromeDriver here, and Chrome/ChromeDriver put their scratch files
# in TEMP, so both stay inside the app folder instead of the user profile.
os.environ["SE_CACHE_PATH"] = str(DATA / "selenium")
for _key in ("TEMP", "TMP", "TMPDIR"):  # remembered so osu! can be launched with the real TEMP
    os.environ.setdefault(f"OBD_ORIGINAL_{_key}", os.environ.get(_key, ""))
    os.environ[_key] = str(TEMP_DIR)
PROFILE_DIR = DATA / "chrome-profile"  # the signed-in osu! session lives here
CONFIG_FILE = DATA / "config.json"
HISTORY_FILE = DATA / "history.json"
QUEUE_FILE = DATA / "queue.json"     # so a long run survives restarting the app
MATCHES_FILE = DATA / "matches.json"  # ...and so does a playlist you were half-way through
INDEX = ROOT / "web" / "index.html"
PREFERRED_PORT = 8765

DEFAULT_OPTS = {"no_video": False, "auto_open": False, "import_client": "stable", "show_browser": False,
                "delay": 5, "batch": 60, "rest": 15, "cooldown": 300, "timeout": 90,
                # mirrors are the default source: no sign-in, no browser, no hourly limit
                "source": "mirror", "mirror_fallback": True, "mirror_delay": 1, "notify": True,
                # give up on a download that stops making progress for this long
                "stall": core.DEFAULT_STALL}


def _load(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _save(path, value):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2), "utf-8")
    tmp.replace(path)


class State:
    def __init__(self):
        self.lock = threading.RLock()
        cfg = _load(CONFIG_FILE, {})
        self.folder = cfg.get("folder") or str(DEFAULT_DOWNLOADS)
        self.songs_dir = cfg.get("songs_dir", "")
        self.lazer_dir = cfg.get("lazer_dir", "")
        self.spotify = {"id": "", "secret": "", **cfg.get("spotify", {})}
        self.osu_paths = {"stable": "", "lazer": "", **cfg.get("osu_paths", {})}  # user-picked installs
        self.opts = {**DEFAULT_OPTS, **cfg.get("opts", {})}
        core.restore_speeds(cfg.get("mirror_speeds"))  # don't relearn the mirrors every launch
        self.last_user_query = cfg.get("last_user_query", "")
        self.user = cfg.get("user") if PROFILE_DIR.is_dir() else None  # confirmed on startup
        self.history = set(_load(HISTORY_FILE, []))
        self.owned = set()          # in an osu!stable Songs folder
        self.owned_lazer = set()      # already imported into osu!lazer
        self.owned_lazer_keys = set()  # ...and the pre-v10 maps that carry no ID
        self.owned_lazer_sums = set()  # ...identified by checksum, which survives a rename
        self.queue, self.queue_sources = self._restore_queue()
        self.matches = _load(MATCHES_FILE, [])  # song -> candidates, awaiting confirmation
        self.matches = self.matches if isinstance(self.matches, list) else []
        self.match_total = len(self.matches)
        self.queue_saved_at = 0
        self.logs = []
        self.busy = ""
        self.busy_token = None
        self.job = None
        self.busy_cancel = None  # set to stop the running background job
        self.signing_in = None  # (cancel, done) events while the sign-in window is open
        self.browser_login = False  # waiting for the user to sign in in their own browser

    # -- persistence
    def save_config(self):
        _save(CONFIG_FILE, {
            "user": self.user, "folder": self.folder, "songs_dir": self.songs_dir,
            "lazer_dir": self.lazer_dir, "osu_paths": self.osu_paths, "opts": self.opts,
            "spotify": self.spotify,
            "last_user_query": self.last_user_query, "mirror_speeds": core.speeds_snapshot(),
        })

    def save_history(self):
        _save(HISTORY_FILE, sorted(self.history, key=int))

    def _restore_queue(self):
        """Bring back the queue and where it came from, with any in-flight maps re-queued."""
        data = _load(QUEUE_FILE, {})
        if isinstance(data, list):
            items, sources = data, []      # the format before sources were recorded
        elif isinstance(data, dict):
            items, sources = data.get("queue") or [], data.get("sources") or []
        else:
            return [], []
        if not isinstance(items, list) or not isinstance(sources, list):
            return [], []
        for it in items:
            if not isinstance(it, dict) or "id" not in it:
                return [], []
            if it.get("status") in ("downloading", None):
                it["status"] = "queued"
        return items, [str(x) for x in sources]

    def queue_source(self):
        """A short line saying where the queue came from, however many sources filled it."""
        if not self.queue_sources:
            return ""
        first, extra = self.queue_sources[0], len(self.queue_sources) - 1
        if not extra:
            return first
        return f"{first} + {extra} more source{'' if extra == 1 else 's'}"

    def save_matches(self):
        try:
            _save(MATCHES_FILE, self.matches)
        except OSError:
            pass  # losing the resume file must never stop anything

    def save_queue(self, force=False):
        """Persist the queue, at most once every few seconds during a run."""
        now = time.time()
        if not force and now - self.queue_saved_at < 5:
            return
        self.queue_saved_at = now
        try:
            _save(QUEUE_FILE, {"queue": self.queue, "sources": self.queue_sources})
        except OSError:
            pass  # losing the resume file must never stop a download

    # -- logging / queue
    def log(self, level, msg):
        with self.lock:
            self.logs.append({"i": len(self.logs), "t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
            del self.logs[:-2000]  # keep memory bounded
        print(f"[{level}] {msg}", flush=True)

    def check_job(self):
        if self.job and self.job.signed_out and self.user:
            self.user = None
            self.save_config()

    def on_item(self, item):
        with self.lock:
            if item["status"] == "done":
                self.history.add(item["id"])
                self.save_history()
            self.save_queue()

    def clients(self):
        """Which osu! installs exist (cached briefly, since this runs on every UI poll)."""
        now = time.time()
        if now - getattr(self, "_clients_at", 0) > 10:
            self._clients = {c: core.find_osu(c, self.songs_dir, self.osu_paths[c])
                             for c in ("stable", "lazer")}
            self._clients_at = now
        return self._clients

    def running(self):
        return bool(self.job and self.job.thread.is_alive())

    def can_read_browser(self):
        """Whether the user's browser stores cookies somewhere we can read (cached)."""
        now = time.time()
        if now - getattr(self, "_cookies_at", 0) > 30:
            self._cookies_found = bool(core.find_cookie_dbs())
            self._cookies_at = now
        return self._cookies_found

    def lazer_data_dir(self):
        """Where osu!lazer keeps its library (cached: the UI asks on every poll)."""
        if self.lazer_dir:
            return self.lazer_dir
        now = time.time()
        if now - getattr(self, "_lazer_at", 0) > 30:
            self._lazer_found = core.find_lazer_data() or ""
            self._lazer_at = now
        return self._lazer_found

    def refresh_owned(self):
        self.owned, self.owned_lazer = set(), set()
        self.owned_lazer_keys, self.owned_lazer_sums = set(), set()
        if self.songs_dir:
            try:
                self.owned = core.scan_songs_folder(self.songs_dir)
            except (ValueError, OSError) as e:
                self.log("warn", f"Couldn't read Songs folder: {e}")
        data = self.lazer_data_dir()
        if data:
            try:
                (self.owned_lazer, self.owned_lazer_keys,
                 self.owned_lazer_sums) = core.scan_lazer_library(data)
            except (ValueError, OSError) as e:
                self.log("warn", f"Couldn't read your osu!lazer library: {e}")

    def classify(self, item):
        sid = item["id"]
        if sid in self.owned:
            return "have", "Already in your osu! Songs folder"
        if sid in self.owned_lazer:
            return "have", "Already in osu!lazer"
        if self.owned_lazer_sums and item.get("checksums"):
            if self.owned_lazer_sums.intersection(item["checksums"]):
                return "have", "Already in osu!lazer (an old map, matched by checksum)"
        if self.owned_lazer_keys:
            key = core.name_key(item.get("artist"), item.get("title"), item.get("creator"))
            if all(key) and key in self.owned_lazer_keys:
                return "have", "Already in osu!lazer (an old map, matched by name)"
        if core.find_osz(self.folder, sid):
            return "have", "Already in the download folder"
        if sid in self.history:
            return "have", "Downloaded in an earlier session"
        return "queued", ""

    def set_queue(self, items, append=False, source=""):
        """Replace the queue, or add to it without disturbing what's already there."""
        self.refresh_owned()
        for it in items:
            it["status"], it["note"] = self.classify(it)
            it.setdefault("error", "")
        with self.lock:
            if append:
                have = {i["id"] for i in self.queue}
                self.queue = self.queue + [i for i in items if i["id"] not in have]
            else:
                self.queue = items
            if not append:
                self.queue_sources = []
            if source:
                self.queue_sources.append(source)
        self.save_queue(force=True)

    def snapshot(self, log_since):
        self.check_job()
        with self.lock:
            counts = {}
            for it in self.queue:
                counts[it["status"]] = counts.get(it["status"], 0) + 1
            return {
                "user": self.user, "signing_in": bool(self.signing_in) or self.browser_login,
                "can_cancel": bool(self.busy_cancel),
                "login_mode": "browser" if self.browser_login else "chrome",
                "can_read_browser": self.can_read_browser(),
                "folder": self.folder, "songs_dir": self.songs_dir, "opts": self.opts,
                "match_done": len(self.matches), "match_total": self.match_total,
                "spotify_id": self.spotify["id"], "spotify_ready": bool(self.spotify["secret"]),
                "lazer_dir": self.lazer_dir, "lazer_found": self.lazer_data_dir(),
                "lazer_count": len(self.owned_lazer) + len(self.owned_lazer_keys),
                "lazer_by_name": len(self.owned_lazer_keys),
                "last_user_query": self.last_user_query, "history_count": len(self.history),
                "clients": self.clients(),
                "queue": self.queue, "counts": counts, "busy": self.busy,
                "queue_source": self.queue_source(),
                "running": self.running(),
                "job": self.job.status(counts.get("queued", 0) + counts.get("downloading", 0))
                       if self.running() else None,
                "paused": bool(self.job and self.job.pause_flag.is_set()),
                "logs": [l for l in self.logs if l["i"] >= log_since],
            }


S = State()


# ---------------------------------------------------------------- actions

def in_background(label, fn):
    """Run a slow job off the request thread, with a way to call it off.

    `fn` is handed a threading.Event that it should check wherever it loops: fetching a
    thousand maps or matching a playlist takes minutes, and there has to be a way out.
    """
    token, cancel = object(), threading.Event()
    # set before the thread starts so a second click can't slip in
    S.busy, S.busy_token, S.busy_cancel = label, token, cancel

    def run():   # the thread's own entry point takes nothing; `fn` is what gets `cancel`
        try:
            fn(cancel)
        except core.Cancelled:
            S.log("info", "Cancelled.")
        except Exception as e:
            S.log("error", core.friendly_error(e))
        finally:
            if S.busy_token is token:  # the task may have updated the label with its progress
                S.busy, S.busy_token, S.busy_cancel = "", None, None
    threading.Thread(target=run, daemon=True).start()


def act_cancel_task(_):
    """Stop whatever background job is running (fetching, matching, importing)."""
    if S.busy_cancel:
        S.busy_cancel.set()
        S.busy = "Cancelling…"


def act_login(body):
    """Sign in to osu!.

    By default this uses the browser you're already reading this page in: osu! opens as a
    normal tab, and afterwards the session is copied into the app's own Chrome profile. Pass
    mode='chrome' for the old dedicated-window flow, which is the fallback for browsers whose
    cookies we can't read (anything Chromium-based).
    """
    if S.running() or S.busy:
        raise ValueError("Wait for the current task to finish first.")
    if (body or {}).get("mode") != "chrome":
        return _login_via_browser()
    cancel, done = threading.Event(), threading.Event()
    S.signing_in = (cancel, done)

    def run(cancel):
        try:
            S.log("info", "Opened a Chrome window. Sign in to osu! there.")
            user = core.sign_in(PROFILE_DIR, cancel, done)
            S.user = user
            S.save_config()
            S.log("ok", f"Signed in as {user['username']}.")
        except core.SignInCancelled as e:
            S.log("info", str(e))
        finally:
            S.signing_in = None
    in_background("Waiting for you to sign in…", run)


def _adopt_browser_session():
    """Copy the osu! session out of the user's browser into our Chrome profile."""
    def run(cancel):
        try:
            cookies, db = core.browser_osu_session()
            user = core.import_browser_session(PROFILE_DIR, cookies)
            if not user:
                raise core.SignInCancelled(
                    "That sign-in didn't work. Make sure you're signed in to osu! in your "
                    "browser, then click “I've signed in” again.")
            S.user = user
            S.save_config()
            S.log("ok", f"Signed in as {user['username']}, using the session from your browser"
                        f" ({db.parent.name}).")
        except core.SignInCancelled as e:
            S.log("warn", str(e))
        finally:
            S.browser_login = False
    in_background("Reading the sign-in from your browser…", run)


def _login_via_browser():
    """Open osu! in the user's own browser, or adopt a sign-in that's already there."""
    cookies, _ = core.browser_osu_session()
    if cookies:
        S.log("info", "Found an osu! sign-in in your browser already.")
        return _adopt_browser_session()
    if not core.find_cookie_dbs():
        S.log("warn", "Your browser keeps its cookies encrypted, so the app can't read the "
                      "sign-in from it. Use “sign in with a separate window” instead.")
    S.browser_login = True
    core.open_url(core.LOGIN_URL)
    S.log("info", "Opened osu! in your browser. Sign in there, then click “I've signed in”.")


def act_cancel_login(_):
    if S.browser_login:
        S.browser_login = False
        S.log("info", "Sign-in cancelled.")
    elif S.signing_in:
        S.signing_in[0].set()


def act_finish_login(_):
    if S.browser_login:
        return _adopt_browser_session()
    if S.signing_in:
        S.signing_in[1].set()


def act_logout(_):
    if S.running() or S.busy:
        raise ValueError("Wait for the current task to finish first.")
    shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    S.user = None
    S.save_config()
    S.log("info", "Signed out. This app no longer has access to your osu! account.")


def verify_sign_in():
    """On startup, make sure the saved session still works (it lasts about a month)."""
    if not PROFILE_DIR.is_dir() or S.busy:
        return

    def run(cancel):
        user = core.check_profile(PROFILE_DIR)
        if S.user and not user:
            S.log("warn", "Your osu! sign-in has expired. Please sign in again.")
        S.user = user
        S.save_config()
    in_background("Checking your osu! sign-in…", run)


def act_fetch(body):
    if S.running() or S.busy:
        raise ValueError("Wait for the current download to finish first.")
    query = (body.get("user") or "").strip() or (str(S.user["id"]) if S.user else "")
    kind = body.get("kind", "most_played")
    if kind not in ("most_played", "favourite", "ranked", "loved", "graveyard", "guest", "nominated"):
        raise ValueError("Unknown list type.")
    limit = max(1, min(int(body.get("limit") or 100), 20000))
    min_plays = max(0, min(int(body.get("min_plays") or 0), 1000000))
    mode = body.get("mode") or ""
    if mode and mode not in core.GAME_MODES:
        raise ValueError("Unknown game mode.")
    stars = (max(0.0, float(body.get("min_stars") or 0)), max(0.0, float(body.get("max_stars") or 0)))
    if stars[1] and stars[0] > stars[1]:
        raise ValueError("The star range is the wrong way round.")
    ranked_only = bool(body.get("ranked_only"))
    S.last_user_query = body.get("user", "")
    S.save_config()

    def run(cancel):
        cut = f" played at least {min_plays} times" if min_plays else ""
        cut += f", {mode}" if mode else ""
        cut += f", {stars[0] or 0:g}-{stars[1]:g}★" if stars[1] else (f", {stars[0]:g}★+" if stars[0] else "")
        cut += ", ranked only" if ranked_only else ""
        S.log("info", f"Fetching up to {limit} maps{cut} from {query}'s {kind.replace('_', ' ')} list…")
        items = core.fetch_user_maps(query, kind, limit, min_plays=min_plays, stop=cancel,
                                     mode=mode, stars=stars, ranked_only=ranked_only,
                                     on_progress=lambda n: setattr(S, "busy", f"Fetching… {n} maps"))
        S.set_queue(items, source=f"{len(items)} maps from {query}'s {kind.replace('_', ' ')} list")
        have = sum(1 for i in items if i["status"] == "have")
        S.log("ok", f"Found {len(items)} beatmap sets" + (f", {have} of which you already have." if have else "."))
    in_background("Fetching…", run)


MAX_TRACKS = 500
SEARCH_GAP = 0.25  # searches are cheap, but there's no reason to hammer a mirror with them


def act_spotify(body):
    """Pull a Spotify playlist's songs in, then match them like any other list."""
    if S.running() or S.busy:
        raise ValueError("Wait for the current download to finish first.")
    link = (body.get("link") or "").strip()
    core.parse_spotify_link(link)  # fail fast on a bad link
    if not S.spotify["secret"]:
        raise ValueError("Add your Spotify client ID and secret in Folders & options first.")
    limit = max(1, min(int(body.get("limit") or MAX_TRACKS), MAX_TRACKS))

    def run(cancel):
        S.log("info", "Reading the playlist from Spotify…")
        tracks = core.fetch_spotify_tracks(link, S.spotify["id"], S.spotify["secret"],
                                           limit=limit, stop=cancel,
                                           on_progress=lambda n: setattr(S, "busy", f"Reading… {n} songs"))
        if not tracks:
            S.log("warn", "That playlist has no songs the app can read.")
            return
        S.log("ok", f"Got {len(tracks)} songs. Looking for beatmaps…")
        with S.lock:
            S.matches, S.match_total = [], len(tracks)
        try:
            _match_tracks(tracks, cancel)
        finally:
            _report_matches(len(tracks))
    in_background("Reading the playlist…", run)


def act_match(body):
    """Look up a beatmap for every song in a pasted playlist, for the user to confirm."""
    if S.running() or S.busy:
        raise ValueError("Wait for the current download to finish first.")
    tracks = core.parse_tracks(body.get("text", ""))
    if not tracks:
        raise ValueError("No songs found. Paste one 'Artist - Title' per line, or a playlist CSV.")
    if len(tracks) > MAX_TRACKS:
        tracks = tracks[:MAX_TRACKS]
    with S.lock:
        S.matches, S.match_total = [], len(tracks)

    def run(cancel):
        try:
            _match_tracks(tracks, cancel)
        finally:
            _report_matches(len(tracks))  # keep whatever was matched before stopping
    in_background("Matching…", run)


def _match_tracks(tracks, cancel):
    for i, (artist, title) in enumerate(tracks, 1):
        core._check(cancel)
        try:
            candidates = core.match_track(artist, title)[:6]
        except Exception as e:
            candidates = []
            if i == 1:  # a dead search endpoint would otherwise repeat this 500 times
                S.log("warn", f"Beatmap search failed: {core.friendly_error(e)}")
        good = candidates and candidates[0]["text"] >= core.MATCH_FLOOR
        with S.lock:
            S.matches.append({"artist": artist, "title": title, "candidates": candidates,
                              "pick": 0 if good else -1, "include": bool(good)})
        S.busy = f"Matching… {i}/{len(tracks)}"
        if i < len(tracks):
            time.sleep(SEARCH_GAP)


def _report_matches(total):
    S.save_matches()  # both matching paths end here, including a cancelled one
    found = sum(1 for m in S.matches if m["pick"] >= 0)
    S.log("ok", f"Matched {found} of {len(S.matches)} songs"
                + (f" (stopped early, {total} asked for)." if len(S.matches) < total else ".")
                + " Check the picks, then add them to the queue.")


def act_match_pick(body):
    """Choose a different beatmap for one song, or leave that song out."""
    i, pick = int(body.get("i", -1)), int(body.get("pick", -1))
    with S.lock:
        if not 0 <= i < len(S.matches):
            raise ValueError("No such song.")
        row = S.matches[i]
        row["pick"] = pick if 0 <= pick < len(row["candidates"]) else -1
        row["include"] = row["pick"] >= 0 and bool(body.get("include", True))
    S.save_matches()


def act_match_queue(_):
    """Add every confirmed match to the download queue."""
    if S.running():
        raise ValueError("Stop the download before changing the queue.")
    items, seen = [], set()
    with S.lock:
        for row in S.matches:
            if not row["include"] or row["pick"] < 0:
                continue
            chosen = row["candidates"][row["pick"]]
            if chosen["id"] in seen:
                continue  # two songs can land on the same beatmap set
            seen.add(chosen["id"])
            items.append({"id": chosen["id"], "title": chosen["title"], "artist": chosen["artist"],
                          "creator": chosen["creator"], "cover": "", "plays": 0})
    if not items:
        raise ValueError("Nothing confirmed yet: pick a beatmap for at least one song.")
    before = len(S.queue)
    S.set_queue(items, append=True, source=f"{len(items)} maps from a playlist")
    added = len(S.queue) - before
    S.log("ok", f"Added {added} beatmap set{'' if added == 1 else 's'} from your playlist"
                + (f" ({len(items) - added} already in the queue)." if added < len(items) else "."))


def act_match_bulk(body):
    """Tick or untick matches in one go, optionally only the confident ones."""
    what = body.get("what", "all")
    changed = 0
    with S.lock:
        for row in S.matches:
            if not row["candidates"]:
                continue
            best = row["candidates"][0]["text"]
            if what == "none":
                want = False
            elif what == "good":
                want = best >= 0.85
            else:
                want = True
            if want and row["pick"] < 0:
                row["pick"] = 0
            row["include"] = want and row["pick"] >= 0
            changed += 1
    S.save_matches()
    return {"changed": changed}


def act_clear_matches(_):
    with S.lock:
        S.matches, S.match_total = [], 0
    S.save_matches()


def act_collection(body):
    """Queue every beatmap set in a shared osu!collector collection."""
    if S.running() or S.busy:
        raise ValueError("Wait for the current download to finish first.")
    link = (body.get("link") or "").strip()
    if not link:
        raise ValueError("Paste an osu!collector link, e.g. https://osucollector.com/collections/23333")
    core.parse_collection_id(link)  # fail fast on a bad link, before going to the background
    limit = max(1, min(int(body.get("limit") or 20000), 20000))

    def run(cancel):
        S.log("info", "Reading the collection from osu!collector…")
        info, items = core.fetch_collection(link, limit=limit, stop=cancel,
                                            on_progress=lambda n: setattr(S, "busy", f"Reading… {n} sets"))
        S.set_queue(items, source=f"{len(items)} maps from the osu!collector collection “{info['name']}”")
        have = sum(1 for i in items if i["status"] == "have")
        by = f" by {info['uploader']}" if info["uploader"] else ""
        S.log("ok", f"“{info['name']}”{by}: {len(items)} beatmap sets"
                    + (f", {have} of which you already have." if have else "."))
        if info["unsubmitted"]:
            S.log("warn", f"{info['unsubmitted']} map(s) in this collection were never submitted to "
                          f"osu!, so they can't be downloaded from anywhere.")
    in_background("Reading the collection…", run)


def act_paste(body):
    if S.running() or S.busy:
        raise ValueError("Wait for the current download to finish first.")
    ids = core.parse_ids(body.get("text", ""))
    if not ids:
        raise ValueError("No beatmap IDs or links found in that text.")
    S.set_queue([{"id": i, "title": "", "artist": "", "cover": ""} for i in ids],
                source=f"{len(ids)} maps from a pasted list")
    S.log("ok", f"Loaded {len(ids)} beatmap sets from your list.")


def act_identify(_):
    """Look up names for queued sets we only know the ID of, then re-check what's owned."""
    if S.running() or S.busy:
        raise ValueError("Wait for the current task to finish first.")
    unknown = [i for i in S.queue if not i.get("title") and not i.get("artist")]
    if not unknown:
        raise ValueError("Every map in the queue already has its name.")

    def run(cancel):
        S.log("info", f"Looking up {len(unknown)} beatmap names…")
        filled = core.fill_metadata(
            unknown, stop=cancel,
            on_progress=lambda n, total: setattr(S, "busy", f"Looking up… {n}/{total}"))
        S.set_queue(S.queue)  # names and checksums may reveal maps already owned
        have = sum(1 for i in S.queue if i["status"] == "have")
        S.log("ok", f"Named {filled} of {len(unknown)} maps; {have} of the queue is already yours.")
    in_background("Looking up names…", run)


def act_estimate(_):
    """Roughly how much disk the queued maps will take, and whether there's room."""
    ids = [i["id"] for i in S.queue if i["status"] == "queued"]
    if not ids:
        return {"bytes": 0, "maps": 0}
    total, sampled = core.estimate_size(ids, no_video=bool(S.opts.get("no_video")))
    free = shutil.disk_usage(Path(S.folder).parent if not Path(S.folder).exists()
                             else S.folder).free
    return {"bytes": total, "maps": len(ids), "sampled": sampled, "free": free}


def act_settings(body):
    with S.lock:
        if "folder" in body and body["folder"].strip():
            if S.running() and body["folder"].strip() != S.folder:
                # the running job captured the old path, so a change now would only
                # scatter the remaining maps across two folders
                raise ValueError("Stop the download before changing where maps are saved.")
            S.folder = body["folder"].strip()
        if "songs_dir" in body:
            S.songs_dir = body["songs_dir"].strip()
        if "spotify" in body:
            for key in ("id", "secret"):
                if key in body["spotify"]:
                    S.spotify[key] = (body["spotify"][key] or "").strip()
        if "lazer_dir" in body:
            folder = body["lazer_dir"].strip()
            if folder and not (Path(folder) / "files").is_dir():
                raise ValueError("That isn't an osu!lazer data folder. Pick the one containing "
                                 "'files' and 'client.realm' (usually ~/.local/share/osu).")
            S.lazer_dir = folder
            S._lazer_at = 0
        for client, folder in (body.get("osu_paths") or {}).items():
            if client not in S.osu_paths:
                continue
            if folder and not core.osu_in_folder(client, folder):
                raise ValueError(f"Couldn't find osu!{client} in that folder. Pick the folder that contains osu!.exe.")
            S.osu_paths[client] = folder
            S._clients_at = 0  # re-detect
        if "opts" in body:
            for k, v in body["opts"].items():
                if k == "import_client" and v not in ("stable", "lazer"):
                    raise ValueError("Pick osu!stable or osu!lazer.")
                if k == "source" and v not in ("mirror", "official"):
                    raise ValueError("Pick a mirror or the official osu! website.")
                if k in DEFAULT_OPTS:
                    S.opts[k] = type(DEFAULT_OPTS[k])(v)
        S.save_config()
        if "songs_dir" in body:
            S._clients_at = 0
        if ("folder" in body or "songs_dir" in body or "lazer_dir" in body) and not S.running():
            S.set_queue(S.queue)  # re-evaluate what's already owned


def act_start(_):
    if S.running():
        raise ValueError("Already downloading.")
    if S.busy:
        raise ValueError("Wait for the current task to finish first.")
    mirror = S.opts.get("source", "mirror") == "mirror"
    if not S.user and not mirror:
        raise ValueError("Sign in first, or switch the download source to the mirrors.")
    if not any(i["status"] == "queued" for i in S.queue):
        raise ValueError("Nothing to download: the queue is empty or you already have everything.")
    opts = {**S.opts, "songs_dir": S.songs_dir, "osu_paths": dict(S.osu_paths),
            # falling back to the website needs a signed-in browser
            "mirror_fallback": bool(S.opts.get("mirror_fallback")) and bool(S.user)}
    S.job = core.Downloader(S.queue, PROFILE_DIR, S.folder, opts, S.on_item, S.log,
                            on_finish=lambda: (S.save_queue(force=True), S.save_config()))
    S.job.verify_import = verify_imports
    S.job.start()


def act_pause(_):
    if S.running():
        if S.job.pause_flag.is_set():
            S.job.pause_flag.clear()
            S.log("info", "Resumed.")
        else:
            S.job.pause_flag.set()
            S.log("info", "Paused. The current map will finish first.")


def act_stop(_):
    if S.running():
        S.job.stop()
        S.log("info", "Stopping…")


def act_retry(_):
    if S.running():
        raise ValueError("Wait for the current run to finish.")
    n = 0
    for it in S.queue:
        if it["status"] in ("failed", "cancelled"):
            it["status"], it["error"] = "queued", ""
            n += 1
    S.save_queue(force=True)
    S.log("info", f"Re-queued {n} maps.")


def act_toggle(body):
    """Flip a single item between skipped and queued."""
    if S.running():
        raise ValueError("Can't change the queue while downloading.")
    for it in S.queue:
        if it["id"] == str(body.get("id")):
            if it["status"] in ("have", "skipped"):
                it["status"] = "queued"
            elif it["status"] == "queued":
                it["status"] = "skipped"
    S.save_queue(force=True)


def act_clear_queue(_):
    if S.running():
        raise ValueError("Stop the download first.")
    S.queue, S.queue_sources = [], []
    S.save_queue(force=True)


def act_clear_history(_):
    S.history = set()
    S.save_history()
    if not S.running():
        S.set_queue(S.queue)
    S.log("info", "Forgot download history.")


def verify_imports(sent):
    """Say so when maps handed to osu!lazer don't actually turn up in it.

    Worth checking: lazer can accept a file over IPC, acknowledge it, and still not import it,
    which is how a run can report success and leave nothing behind. Anything in a batch was
    missing from the library beforehand, so one look afterwards settles it.
    """
    data = S.lazer_data_dir()
    if not sent or not data or S.opts.get("import_client") != "lazer":
        return []
    try:
        ids, keys, sums = core.scan_lazer_library(data)
    except (ValueError, OSError):
        return []
    missing = []
    for item in sent:
        key = core.name_key(item.get("artist"), item.get("title"), item.get("creator"))
        if item["id"] in ids or (all(key) and key in keys):
            continue
        if sums.intersection(item.get("checksums") or ()):
            continue
        if not all(key):
            continue  # too old to carry an ID and we have no names for it: can't tell
        missing.append(item)
    if missing:
        example = missing[0].get("title") or missing[0]["id"]
        S.log("warn", f"{len(missing)} of {len(sent)} maps didn't appear in osu!lazer "
                      f"(e.g. {example}). They downloaded fine, so their .osz files are still "
                      f"in the download folder for “Import all into osu!”.")
    # the download itself succeeded, so the maps stay "done"; they're reported as not
    # imported instead, which is what the end-of-run summary already covers
    return [i.get("file") for i in missing if i.get("file")]


def act_open_all(_):
    files = [f for it in S.queue if it["status"] == "done"
             for f in [it.get("file") or core.find_osz(S.folder, it["id"])] if f and os.path.exists(f)]
    if not files:
        files = sorted(str(p) for p in Path(S.folder).glob("*.osz"))
    if not files:
        raise ValueError("No .osz files waiting in the download folder (osu! may have imported them already).")

    client = S.opts["import_client"]

    def run(cancel):
        S.log("info", f"Sending {len(files)} maps to osu!{client}…")
        used, failed, sent = client, [], 0
        for start in range(0, len(files), core.IMPORT_BATCH):
            core._check(cancel)
            batch = files[start:start + core.IMPORT_BATCH]
            used, bad = core.import_batch(batch, client, S.songs_dir, S.osu_paths, log=S.log)
            failed += bad
            sent += len(batch)
            S.busy = f"Importing… {sent}/{len(files)}"
        if used != client:
            S.log("warn", f"osu!{client} isn't installed, so the maps went to "
                          f"{'osu!' + used if used != 'default' else 'the default app'} instead.")
        if failed:
            S.log("warn", f"{len(failed)} map(s) weren't accepted. They're still in the download "
                          f"folder, so you can press Import again.")
        S.log("ok", f"Handed {len(files) - len(failed)} maps to osu!, "
                    f"which will finish importing on its own.")
    in_background("Importing…", run)


def act_open_folder(body):
    path = Path(S.songs_dir if body.get("which") == "songs" else S.folder)
    path.mkdir(parents=True, exist_ok=True)
    core.open_file(str(path))


def _desktop_picker(initial, title):
    """A folder picker from the desktop, since Linux python often ships without tkinter."""
    for cmd in (["zenity", "--file-selection", "--directory", f"--title={title}",
                 f"--filename={initial.rstrip('/')}/"],
                ["kdialog", "--getexistingdirectory", initial, "--title", title],
                ["qarma", "--file-selection", "--directory", f"--title={title}"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.SubprocessError):
            continue
        return out.stdout.strip()  # empty when cancelled
    return None


def act_browse(body):
    """Native folder picker, run as a child process so tkinter can't upset the server threads."""
    initial = body.get("initial") or str(Path.home())
    title = body.get("title", "Choose a folder")
    if os.name != "nt":
        path = _desktop_picker(initial, title)
        if path is not None:
            return {"path": path}
    cmd = [sys.executable] + ([] if FROZEN else [str(Path(__file__).resolve())])
    out = subprocess.run(cmd + ["--pick-folder", initial, title],
                         capture_output=True, text=True, timeout=600,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    path = out.stdout.strip()
    if not path and out.returncode != 0:
        raise ValueError("No folder picker available. Install zenity or kdialog, "
                         "or type the path into the box instead.")
    return {"path": os.path.normpath(path) if path else ""}


def pick_folder(initial, title):
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    print(filedialog.askdirectory(initialdir=initial, title=title) or "", flush=True)


def act_scan(body):
    """Count what's already in an osu!stable Songs folder or an osu!lazer library."""
    if body.get("which") == "lazer":
        data = S.lazer_data_dir()
        if not data:
            raise ValueError("Couldn't find your osu!lazer data folder. Set it in Folders & options.")
        ids, keys, _ = core.scan_lazer_library(data)
        older = f" ({len(keys)} of them too old to carry an ID)" if keys else ""
        S.log("ok", f"Your osu!lazer library has {len(ids) + len(keys)} beatmap sets{older}.")
        # only the ID-bearing ones can go into an exportable list
    else:
        if not S.songs_dir:
            raise ValueError("Pick your osu! Songs folder first.")
        ids = core.scan_songs_folder(S.songs_dir)
        S.log("ok", f"Your Songs folder has {len(ids)} beatmap sets.")
    if not S.running():
        S.refresh_owned()
        S.set_queue(S.queue)
    return {"ids": sorted(ids, key=int)}


ACTIONS = {
    "login": act_login, "cancel-login": act_cancel_login, "finish-login": act_finish_login, "logout": act_logout, "fetch": act_fetch, "paste": act_paste,
    "cancel-task": act_cancel_task, "identify": act_identify, "estimate": act_estimate, "collection": act_collection, "match": act_match,
    "spotify": act_spotify, "match-pick": act_match_pick,
    "match-queue": act_match_queue, "match-bulk": act_match_bulk, "clear-matches": act_clear_matches, "settings": act_settings, "start": act_start, "pause": act_pause, "stop": act_stop,
    "retry": act_retry, "toggle": act_toggle, "clear-queue": act_clear_queue,
    "clear-history": act_clear_history, "open-all": act_open_all,
    "open-folder": act_open_folder, "browse": act_browse, "scan": act_scan,
}


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _allowed(self):
        # Block DNS-rebinding and cross-site requests: only our own page may talk to us.
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/state":
            return self._send(200, S.snapshot(int(q.get("since", ["0"])[0])))
        if url.path == "/api/matches":
            with S.lock:
                return self._send(200, {"matches": S.matches, "total": S.match_total})
        if url.path == "/api/export":
            which = q.get("which", ["all"])[0]
            if which == "library":
                ids = sorted(core.scan_songs_folder(S.songs_dir), key=int) if S.songs_dir else []
            elif which == "lazer":
                data = S.lazer_data_dir()
                ids = sorted(core.scan_lazer_library(data)[0], key=int) if data else []
            elif which == "history":
                ids = sorted(S.history, key=int)
            else:
                ids = [i["id"] for i in S.queue if which == "all" or i["status"] == which]
            return self._send(200, ("\n".join(ids) + "\n").encode(), "text/plain; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="beatmaps_{which}.txt"'})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        # The custom header forces a CORS preflight, which we never approve, so
        # other websites can't drive this server from the user's browser.
        if not self._allowed() or self.headers.get("X-Osu-Dl") != "1":
            return self._send(403, {"error": "forbidden"})
        name = urlparse(self.path).path.removeprefix("/api/")
        fn = ACTIONS.get(name)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            result = fn(body) or {}
            self._send(200, {"ok": True, **result})
        except Exception as e:
            self._send(400, {"ok": False, "error": str(e) or type(e).__name__})


def already_running(port):
    """True if another copy of this app is already serving on the port."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state?since=999999")
        with urllib.request.urlopen(req, timeout=1) as r:
            return "history_count" in json.loads(r.read())
    except (OSError, ValueError):
        return False


def free_port(preferred):
    for port in range(preferred, preferred + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No free port found.")


PORT = PREFERRED_PORT
JOB = None  # Windows job handle that closes our Chrome processes when the app exits


USAGE = """osu! Beatmap Downloader

  python app.py [options]

Interface:
  --port N              serve on another port (default 8765)
  --app-dir DIR         keep settings and downloads somewhere other than the app folder
  --no-browser          don't open a browser tab

Fill the queue without touching the interface:
  --profile NAME        a player's list (empty NAME means you)
  --kind KIND           most_played (default), favourite, ranked, loved, graveyard, guest
  --limit N             how many maps (default 100)
  --min-plays N         for most_played, stop below this play count
  --mode MODE           osu, taiko, fruits or mania
  --stars LOW-HIGH      difficulty range, e.g. 4-6 or 5- for 5 and up
  --ranked-only         skip unranked and graveyard maps
  --collection URL      an osu!collector collection
  --spotify URL         a Spotify playlist (needs credentials saved in the app first)
  --list FILE           a text file of beatmap IDs or links
  --songs FILE          a text file of "Artist - Title" lines, or a playlist CSV
  --confident-only      with --spotify/--songs, queue only the confident matches
  --start               begin downloading once the queue is filled
  --exit-when-done      quit after the download finishes (implies --start, --no-browser)

  python app.py --collection https://osucollector.com/collections/23333 --start
  python app.py --profile Hex110 --limit 500 --min-plays 5 --exit-when-done
  python app.py --spotify https://open.spotify.com/playlist/… --confident-only --start
"""


def _arg(name, default=None):
    """The value after --name on the command line, or default."""
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def queue_from_args(cancel):
    """Fill the queue from a --profile / --collection / --spotify / --list / --songs flag."""
    if "--collection" in sys.argv:
        act_collection({"link": _arg("--collection"), "limit": _arg("--limit", 20000)})
    elif "--list" in sys.argv:
        act_paste({"text": Path(_arg("--list")).read_text("utf-8")})
    elif "--spotify" in sys.argv or "--songs" in sys.argv:
        return _queue_songs_from_args()
    elif "--profile" in sys.argv:
        low, _, high = _arg("--stars", "").partition("-")
        act_fetch({"user": _arg("--profile", ""), "kind": _arg("--kind", "most_played"),
                   "limit": _arg("--limit", 100), "min_plays": _arg("--min-plays", 0),
                   "mode": _arg("--mode", ""), "min_stars": low or 0, "max_stars": high or 0,
                   "ranked_only": "--ranked-only" in sys.argv})
    else:
        return False
    return True


def _queue_songs_from_args():
    """Match a playlist and queue the picks, since nobody is there to confirm them."""
    if "--spotify" in sys.argv:
        act_spotify({"link": _arg("--spotify"), "limit": _arg("--limit", MAX_TRACKS)})
    else:
        act_match({"text": Path(_arg("--songs")).read_text("utf-8")})
    while S.busy:
        time.sleep(0.5)
    if "--confident-only" in sys.argv:
        act_match_bulk({"what": "good"})
    unsure = sum(1 for m in S.matches if m["include"]
                 and m["candidates"][m["pick"]]["text"] < 0.85)
    if unsure:
        S.log("warn", f"{unsure} song(s) matched a beatmap only loosely. Nobody is here to "
                      f"check them; use --confident-only to queue just the clear ones.")
    if any(m["include"] for m in S.matches):
        act_match_queue({})
    return True


def run_from_args():
    """Carry out a scripted run once the server is up: fill the queue, then download."""
    def run():
        try:
            if not queue_from_args(None):
                return
            while S.busy:  # the fetch itself runs in the background
                time.sleep(0.5)
            if "--start" in sys.argv or "--exit-when-done" in sys.argv:
                queued = sum(1 for i in S.queue if i["status"] == "queued")
                if not queued:
                    S.log("info", "Nothing to download.")
                else:
                    act_start({})
            if "--exit-when-done" in sys.argv:
                while S.running() or S.busy:
                    time.sleep(1)
                S.log("info", "Finished; exiting.")
                os._exit(0)
        except Exception as e:
            S.log("error", core.friendly_error(e))
            if "--exit-when-done" in sys.argv:
                os._exit(1)
    threading.Thread(target=run, daemon=True).start()


def main():
    global PORT
    for stream in (sys.stdout, sys.stderr):
        if stream:  # never let an odd character in a map title crash the log
            stream.reconfigure(errors="replace")
    if "--pick-folder" in sys.argv:
        i = sys.argv.index("--pick-folder")
        return pick_folder(*sys.argv[i + 1:i + 3])
    if "--help" in sys.argv or "-h" in sys.argv:
        return print(USAGE)

    open_browser = "--no-browser" not in sys.argv and "--exit-when-done" not in sys.argv
    preferred = PREFERRED_PORT
    if "--port" in sys.argv:
        preferred = int(sys.argv[sys.argv.index("--port") + 1])
    if already_running(preferred):
        # double-clicking the exe again just brings the existing app back up
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{preferred}/")
        print("Already running, so it was opened in your browser.")
        return

    global JOB
    JOB = core.close_chrome_with_app(PROFILE_DIR)
    # we're the only copy running, so anything using our profile or temp is from an earlier session
    stopped = core.close_leftover_chrome(PROFILE_DIR)
    if stopped:
        S.log("info", f"Closed {stopped} leftover Chrome process(es) from an earlier session.")
    for leftover in TEMP_DIR.iterdir():
        # Never clear something another program is still living in. An osu!lazer started by
        # an earlier run of this app used to mount its AppImage in here, and wiping that out
        # from under it left a running lazer that silently refused every import.
        if os.path.ismount(leftover) or leftover.name.startswith(".mount_"):
            continue
        try:
            shutil.rmtree(leftover) if leftover.is_dir() else leftover.unlink()
        except OSError:
            pass  # still locked by something; try again next launch

    # osu!stable is the better default, but on Linux it only exists under Wine, so don't
    # point a fresh install at a client that isn't there.
    if "import_client" not in _load(CONFIG_FILE, {}).get("opts", {}) and os.name != "nt":
        installed = S.clients()
        if installed.get("lazer") and not installed.get("stable"):
            S.opts["import_client"] = "lazer"
            S.save_config()

    if S.queue:
        S.refresh_owned()
        left = sum(1 for i in S.queue if i["status"] == "queued")
        S.log("info", f"Restored your queue from last time: {len(S.queue)} maps"
                      + (f", {left} still to download." if left else ", all finished."))

    verify_sign_in()
    PORT = free_port(preferred)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    if os.name == "nt":
        os.system("title osu! Beatmap Downloader")
    print("osu! Beatmap Downloader\n"
          f"  Running at {url}\n"
          "  Your browser should open automatically. Keep this window open while downloading;\n"
          "  close it (or press Ctrl+C) to quit.\n", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    run_from_args()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if S.job:
            S.job.stop()
        print("Bye!")


if __name__ == "__main__":
    main()
