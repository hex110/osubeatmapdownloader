"""osu! beatmap fetching + headless Chrome downloading.

Everything that talks to osu.ppy.sh lives here; app.py only wires it to the UI.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OSU = "https://osu.ppy.sh"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) osu-beatmap-downloader"


# ---------------------------------------------------------------- helpers

def parse_ids(text):
    """Pull beatmapset IDs out of pasted text: bare IDs, /beatmapsets/ links, /s/ links."""
    ids, seen = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = (re.search(r"beatmapsets/(\d+)", line)
             or re.search(r"/s/(\d+)", line)
             or re.match(r"(\d+)", line))
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            ids.append(m.group(1))
    return ids


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def resolve_user_id(user):
    """Turn an ID, profile URL, or username into a numeric user ID."""
    user = (user or "").strip()
    m = re.search(r"users/(\d+)", user) or re.fullmatch(r"(\d+)", user)
    if m:
        return m.group(1)
    m = re.search(r"users/([^/?#]+)", user)
    name = urllib.parse.unquote(m.group(1)) if m else user
    if not name:
        raise ValueError("Enter a username, profile link or user ID.")
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(f"{OSU}/users/{urllib.parse.quote(name)}", headers={"User-Agent": UA})
    try:
        opener.open(req, timeout=30)
    except urllib.error.HTTPError as e:
        m = re.search(r"/users/(\d+)", e.headers.get("Location", ""))
        if e.code in (301, 302) and m:
            return m.group(1)
    raise ValueError(f"Couldn't find an osu! user called “{name}”.")


def fetch_user_maps(user, kind, limit, on_progress=None, min_plays=0):
    """Return [{id, title, artist, cover, plays}] from a user's profile list, deduplicated.

    `min_plays` only applies to the most_played list, which is ordered by play count, so
    the first map below the threshold ends the search.
    """
    uid = resolve_user_id(user)
    min_plays = min_plays if kind == "most_played" else 0
    out, seen, offset = [], set(), 0
    while len(out) < limit:
        page = _get_json(f"{OSU}/users/{uid}/beatmapsets/{kind}?offset={offset}&limit=100")
        if not page:
            break
        for item in page:
            s = item["beatmapset"] if kind == "most_played" else item
            sid = str(s["id"])
            if min_plays and item.get("count", 0) < min_plays:
                return out  # sorted by plays, so everything after this is below the cut too
            if sid in seen:
                continue
            seen.add(sid)
            out.append({"id": sid, "title": s.get("title", ""), "artist": s.get("artist", ""),
                        "cover": (s.get("covers") or {}).get("list", ""),
                        # most_played is ordered by play count and lists one entry per difficulty,
                        # so this is the plays on the map's most played difficulty
                        "plays": item.get("count", 0) if kind == "most_played" else 0})
            if len(out) >= limit:
                break
        offset += len(page)
        if on_progress:
            on_progress(len(out))
        if len(page) < 100:
            break
    return out


def scan_songs_folder(path):
    """Beatmapset IDs present in an osu!stable Songs folder (folders are named '<id> Artist - Title')."""
    ids = set()
    p = Path(path)
    if not p.is_dir():
        raise ValueError("That Songs folder doesn't exist.")
    for entry in os.scandir(p):
        m = re.match(r"(\d+)\s", entry.name)
        if m:
            ids.add(m.group(1))
    return ids


def find_osz(folder, sid):
    """Finished .osz for this set in the folder, if any (osu names them '<id> Artist - Title.osz')."""
    try:
        for entry in os.scandir(folder):
            if entry.name.endswith(".osz") and re.match(rf"{sid}(\D|$)", entry.name):
                return entry.path
    except FileNotFoundError:
        pass
    return None


# ---------------------------------------------------------------- mirrors
#
# Public mirrors serve .osz files over plain HTTP: no sign-in, no browser and no hourly
# limit, which makes them roughly ten times faster than clicking Download on the website.
# Ordered best-first; each entry is (name, full URL, no-video URL).

MIRRORS = (
    ("catboy.best", "https://catboy.best/d/{sid}", "https://catboy.best/d/{sid}n"),
    ("osu.direct", "https://osu.direct/api/d/{sid}", "https://osu.direct/api/d/{sid}?noVideo=1"),
    ("beatconnect.io", "https://beatconnect.io/b/{sid}/", "https://beatconnect.io/b/{sid}/?novideo=1"),
    ("nerinyan.moe", "https://api.nerinyan.moe/d/{sid}", "https://api.nerinyan.moe/d/{sid}?noVideo=true"),
)

UNSAFE_IN_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _osz_name(sid, disposition, item):
    """A safe '<id> Artist - Title.osz' name for a mirror download.

    The name matters: find_osz() recognises an already-downloaded set by the leading ID, and
    osu!stable takes the folder name from it. A mirror's filename is untrusted, so strip any
    path separators before using it.
    """
    name = ""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition or "")
    if m:
        name = urllib.parse.unquote(m.group(1)).strip()
    if not name and (item.get("artist") or item.get("title")):
        name = f"{sid} {item.get('artist', '')} - {item.get('title', '')}.osz"
    # replace the unsafe characters first: doing it after Path().name would throw away
    # everything before a '/' in a title like "Love: Comes/Goes"
    name = Path(UNSAFE_IN_NAME.sub("_", name)).name.strip(". ")
    if not name.lower().endswith(".osz"):
        name = f"{sid}.osz"
    if not re.match(rf"{sid}(\D|$)", name):
        name = f"{sid} {name}"
    return name[:180]


def download_from_mirror(sid, folder, item=None, no_video=False, timeout=90, stop=None):
    """Fetch one beatmapset from the first mirror that has it.

    Returns (path, mirror name) on success or (None, reason). Tries every mirror before
    giving up, so one being down or missing a map doesn't fail the download.
    """
    item = item or {}
    reason, absent = "No mirror had this beatmap.", 0
    for name, full_url, novideo_url in MIRRORS:
        if stop is not None and stop.is_set():
            return None, "Cancelled."
        url = (novideo_url if no_video else full_url).format(sid=sid)
        tmp = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "html" in ctype or "json" in ctype:
                    reason, absent = f"{name}: doesn't have this beatmap.", absent + 1
                    continue
                first = r.read(65536)
                if not first.startswith(b"PK"):  # every .osz is a zip
                    reason = f"{name}: didn't send a beatmap file."
                    continue
                target = Path(folder) / _osz_name(sid, r.headers.get("Content-Disposition"), item)
                tmp = target.with_name(target.name + ".part")
                size = 0
                with open(tmp, "wb") as f:
                    chunk = first
                    while chunk:
                        if stop is not None and stop.is_set():
                            raise _Cancelled
                        f.write(chunk)
                        size += len(chunk)
                        chunk = r.read(262144)
            if size < 1024:
                reason = f"{name}: sent an empty file."
                tmp.unlink(missing_ok=True)
                continue
            tmp.replace(target)
            return str(target), name
        except _Cancelled:
            return None, "Cancelled."
        except urllib.error.HTTPError as e:
            absent += e.code == 404
            reason = f"{name}: " + ("doesn't have this beatmap." if e.code == 404 else f"HTTP {e.code}.")
        except Exception as e:  # timeout, DNS, dropped connection: just try the next mirror
            reason = f"{name}: {type(e).__name__}."
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)
    if absent == len(MIRRORS):
        return None, "No mirror has this beatmap (it may be unranked, deleted or very new)."
    return None, reason


# ---------------------------------------------------------------- osu!lazer's library

def find_lazer_data(custom=""):
    """osu!lazer's data folder (the one holding 'files' and 'client.realm'), or None."""
    bases = [Path(custom)] if custom else []
    if os.name == "nt":
        bases.append(Path(os.environ.get("APPDATA", Path.home())) / "osu")
    else:
        bases += [Path.home() / ".local/share/osu", Path.home() / ".var/app/sh.ppy.osu/data/osu"]
    for base in list(bases):
        try:  # lazer records a relocated library here
            m = re.search(r"FullPath\s*=\s*(.+)", (base / "storage.ini").read_text("utf-8"))
            if m:
                bases.append(Path(m.group(1).strip()))
        except OSError:
            pass
    for base in bases:
        if (base / "files").is_dir():
            return str(base)
    return None


def scan_lazer_library(data_dir):
    """Beatmapset IDs already imported into osu!lazer.

    lazer keeps every file under 'files/' named by its SHA-256, with the metadata in a Realm
    database that needs Realm itself to read. The .osu difficulty files are plain text
    though, and each one names the set it belongs to, so read those instead. Scanning ~24k
    files takes about a tenth of a second because all but the header of most is skipped.
    """
    root = Path(data_dir) / "files"
    if not root.is_dir():
        raise ValueError("That isn't an osu!lazer data folder (it has no 'files' inside).")
    ids = set()
    for dirpath, _, names in os.walk(root):
        for n in names:
            try:
                with open(os.path.join(dirpath, n), "rb") as f:
                    if not f.read(15).startswith(b"osu file format"):
                        continue
                    blob = f.read(16384)  # [Metadata] is always near the top
            except OSError:
                continue
            m = re.search(rb"^BeatmapSetID\s*:\s*(\d+)", blob, re.M)
            if m and m.group(1) != b"0":
                ids.add(m.group(1).decode())  # unsubmitted maps use -1 and don't match
    return ids


# ---------------------------------------------------------------- desktop notification

def notify(title, message):
    """A desktop notification when a long run finishes. Never fatal if it doesn't work."""
    import shutil
    import subprocess
    try:
        if os.name == "nt":
            return  # the web UI raises its own notification there
        if sys.platform == "darwin":
            body = message.replace('"', "'")
            subprocess.Popen(["osascript", "-e", f'display notification "{body}" with title "{title}"'],
                             start_new_session=True)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "osu! Beatmap Downloader", title, message],
                             start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------- browser

def make_driver(download_dir=None, headless=True, profile_dir=None):
    from selenium import webdriver  # imported lazily so the UI starts instantly

    opts = webdriver.ChromeOptions()
    # On Linux the browser is often Chromium, or Chrome under a name Selenium doesn't look for,
    # so point Selenium at the same binary the sign-in window uses.
    chrome = find_chrome()
    if chrome and os.name != "nt":
        opts.binary_location = chrome
    if profile_dir:
        opts.add_argument(f"--user-data-dir={profile_dir}")
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-first-run")
    opts.add_argument("--log-level=3")
    # stop Chrome's component updater from dropping files into the download folder
    opts.add_argument("--disable-component-update")
    opts.add_argument("--disable-background-networking")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    if download_dir:
        opts.add_experimental_option("prefs", {
            "download.default_directory": str(download_dir),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        })
    # Selenium Manager fetches a chromedriver that matches the installed Chrome. For a distro
    # Chromium it usually can't, but the distro ships a matching chromedriver alongside it.
    service = None
    local = _system_chromedriver(chrome)
    if local:
        from selenium.webdriver.chrome.service import Service
        service = Service(executable_path=local)
    driver = webdriver.Chrome(options=opts, service=service)
    if download_dir:
        driver.execute_cdp_cmd("Browser.setDownloadBehavior",
                               {"behavior": "allow", "downloadPath": str(download_dir)})
    return driver


def current_user(driver):
    """The osu! account this browser is signed in to, or None."""
    driver.get(f"{OSU}/home")
    return driver.execute_script(
        "const u = window.currentUser || {};"
        "return u.id ? {id: u.id, username: u.username, avatar: u.avatar_url} : null;")


# Chrome can only open a profile once at a time: the sign-in window, the sign-in check and
# the downloader all take this lock while they use it.
PROFILE_LOCK = threading.RLock()


def _hold_profile():
    if not PROFILE_LOCK.acquire(timeout=5):
        raise RuntimeError("The sign-in window is still open. Finish signing in (or cancel) first.")


def check_profile(profile_dir):
    """Who the saved Chrome profile is signed in as (starts a hidden Chrome briefly)."""
    if not Path(profile_dir).is_dir():
        return None
    _hold_profile()
    try:
        driver = make_driver(profile_dir=profile_dir)
        try:
            return current_user(driver)
        finally:
            driver.quit()
    finally:
        PROFILE_LOCK.release()


# ---------------------------------------------------------------- sign-in window
#
# osu!'s login form is protected by Cloudflare Turnstile, which fails in any browser that is
# automated or even just has a DevTools debugging port open. So the sign-in window is a plain
# Chrome window on the app's own profile, with nothing attached. The user tells the app when
# they're done (or closes the window); the app then closes Chrome normally, so the session is
# saved to disk, and checks the profile with a hidden Chrome.

LOGIN_URL = f"{OSU}/home/account/edit"  # logged-out visitors get the sign-in form here


class SignInCancelled(Exception):
    pass


class _Cancelled(Exception):
    """Raised inside a mirror download when the user stops the queue."""


# Chromium-based browsers Selenium can drive, best first. Chrome is preferred because
# Selenium Manager can always fetch a matching driver for it.
LINUX_BROWSERS = ("google-chrome", "google-chrome-stable", "google-chrome-beta",
                  "chromium", "chromium-browser", "brave-browser", "brave",
                  "vivaldi-stable", "vivaldi", "microsoft-edge", "microsoft-edge-stable")
MAC_BROWSERS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")


def find_chrome():
    """Path to Chrome (or another Chromium-based browser Selenium can drive), or None."""
    import shutil
    import sys
    if os.name == "nt":
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as k:
                    path = winreg.QueryValue(k, None)
                if path and Path(path).is_file():
                    return path
            except OSError:
                pass
        for base in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if os.environ.get(base):
                path = Path(os.environ[base]) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if path.is_file():
                    return str(path)
        return None
    if sys.platform == "darwin":
        for path in MAC_BROWSERS:
            if Path(path).exists():
                return path
        return None
    if os.environ.get("OBD_CHROME") and Path(os.environ["OBD_CHROME"]).is_file():
        return os.environ["OBD_CHROME"]  # escape hatch for an install we don't know about
    for name in LINUX_BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    return None


NO_CHROME_MESSAGE = (
    "No Chromium-based browser found. Install Google Chrome or Chromium "
    "(Arch: sudo pacman -S chromium · Debian/Ubuntu: sudo apt install chromium · Fedora: sudo dnf install chromium), "
    "or point the app at one with the OBD_CHROME environment variable. "
    "A Flatpak or Snap browser can't be used, because the app needs to run it directly."
) if os.name != "nt" else (
    "Google Chrome isn't installed. Install it from google.com/chrome and try again."
)


def _major_version(exe):
    """Major version of a browser or driver binary, e.g. 141, or None."""
    import subprocess
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.\d+\.\d+", out or "")
    return int(m.group(1)) if m else None


def _system_chromedriver(browser):
    """A chromedriver already on this system that matches `browser`, or None.

    Selenium Manager downloads drivers from Chrome for Testing, which has no builds for a
    distro's Chromium. Distros package the matching chromedriver instead, so prefer that.
    """
    import shutil
    if os.name == "nt" or not browser:
        return None
    candidates = [shutil.which("chromedriver"), "/usr/bin/chromedriver",
                  "/usr/lib/chromium/chromedriver", "/usr/lib64/chromium/chromedriver"]
    want = _major_version(browser)
    for driver in candidates:
        if driver and Path(driver).is_file() and want and _major_version(driver) == want:
            return str(driver)
    return None


def _close_gracefully(proc):
    """Close Chrome the way clicking X does, so it writes its cookies to disk."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def close_window(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == proc.pid and user32.IsWindowVisible(hwnd):
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return True
        user32.EnumWindows(close_window, 0)
    else:
        proc.terminate()  # Chrome treats SIGTERM as a normal quit
    try:
        proc.wait(20)
    except Exception:
        proc.kill()


def sign_in(profile_dir, cancel, done):
    """Open a Chrome window for the user to sign in to osu!, then return who signed in.

    `done` is set when the user says they've finished; closing the window counts too.
    Raises SignInCancelled if cancelled or if the profile still isn't signed in.
    """
    _hold_profile()
    try:
        return _sign_in(profile_dir, cancel, done)
    finally:
        PROFILE_LOCK.release()


def _sign_in(profile_dir, cancel, done):
    import subprocess
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError(NO_CHROME_MESSAGE)
    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([
        chrome, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-sync", "--window-size=620,860", "--new-window", LOGIN_URL,
    ], env=_child_env())

    while proc.poll() is None and not (cancel.is_set() or done.is_set()):
        time.sleep(0.5)
    if proc.poll() is None:
        _close_gracefully(proc)
    time.sleep(1)  # let Chrome's helper processes let go of the profile

    if cancel.is_set():
        raise SignInCancelled("Sign-in cancelled.")
    user = check_profile(profile)
    if not user:
        raise SignInCancelled("You're not signed in yet. Click “Sign in with osu!” to try again.")
    return user


CLICK_DOWNLOAD_JS = """
const [sid, noVideo] = arguments;
const links = [...document.querySelectorAll('a[href*="/beatmapsets/' + sid + '/download"]')];
if (!links.length) return null;
const pick = links.find(a => noVideo === a.href.includes('noVideo')) || links[0];
pick.click();
return pick.href;
"""


def _duration(seconds):
    return f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s"


# osu! allows a fixed number of downloads per rolling hour (200 for regular accounts,
# more for osu!supporters). When it refuses, retry on this cycle. It adds up to an hour,
# so the 4th retry lands after the window has rolled over. Then start again at 5 min.
QUOTA_RETRY_WAITS = (5 * 60, 10 * 60, 20 * 60, 25 * 60)
DEFAULT_HOURLY_LIMIT = 200


def _resume_time(refused_at, batch_start):
    """First retry (on the QUOTA_RETRY_WAITS cycle) after the batch's hour has rolled over."""
    free_at = batch_start + 3600
    t, i = refused_at, 0
    while True:
        t += QUOTA_RETRY_WAITS[i % len(QUOTA_RETRY_WAITS)]
        i += 1
        if t >= free_at:
            return t


class Downloader:
    """Runs a download queue on a background thread and reports through callbacks."""

    def __init__(self, items, profile_dir, folder, opts, emit, log, on_finish=None):
        self.items = items          # list of dicts; this class mutates item["status"]
        self.profile_dir = profile_dir
        self.folder = Path(folder)
        self.opts = opts
        self.emit = emit            # emit(item) after any status change
        self.log = log              # log(level, message)
        self.on_finish = on_finish  # called once the queue is done, however it ended
        self.stop_flag = threading.Event()
        self.pause_flag = threading.Event()
        self.warned_client = False
        self.signed_out = False
        self.driver = None
        self.locked = False
        # mirrors have no hourly quota and need no browser, so most of the pacing below
        # only applies to downloads that go through the osu! website
        self.mirror_first = opts.get("source", "mirror") == "mirror"
        self.allow_browser = not self.mirror_first or bool(opts.get("mirror_fallback", True))
        self.mirror_used = None
        # progress/ETA bookkeeping, read by the UI
        self.batch_done = 0             # downloads since the last time osu! let us resume
        self.batch_started_at = None    # time of the batch's first download
        self.hourly_limit = None if self.mirror_first else DEFAULT_HOURLY_LIMIT
        self.limit_learned = self.mirror_first  # nothing to learn: mirrors don't ration
        self.quota_hit_at = None        # when osu! started refusing (None = not limited)
        self.retry_at = None            # when the next retry happens while limited
        self.retry_attempt = 0
        self.per_map = None             # average seconds per successful map
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        self.pause_flag.clear()

    def status(self, remaining):
        """Progress info for the UI, including an ETA that accounts for osu!'s hourly limit."""
        limit = self.hourly_limit
        if not self.limit_learned and self.batch_done > limit:
            limit = None  # past the default without being refused: probably a supporter
        default_delay = float(self.opts.get("mirror_delay", 1) if self.mirror_first
                              else self.opts.get("delay", 5))
        per_map = self.per_map or default_delay + (2 if self.mirror_first else 3)
        now = time.time()
        t, left = now, remaining
        if self.quota_hit_at:
            batch_start = self.batch_started_at or self.quota_hit_at - 3600
            t = batch_start = max(now, _resume_time(self.quota_hit_at, batch_start))
            cap = limit or left
        else:
            batch_start = self.batch_started_at or now
            cap = left if limit is None else max(0, limit - self.batch_done)
        while True:
            n = min(left, cap)
            t += n * per_map
            left -= n
            if left <= 0 or not limit:
                break
            resumed = _resume_time(t, batch_start)  # osu! refuses at t; wait for the window
            t = batch_start = resumed
            cap = limit
        return {
            "eta": round(t - now),
            "hourly_limit": limit,
            "limited": bool(self.quota_hit_at),
            "retry_at": self.retry_at,
            "retry_attempt": self.retry_attempt,
            "retry_attempts": len(QUOTA_RETRY_WAITS),
        }

    def _sleep(self, seconds):
        """Interruptible sleep that also honours pause."""
        end = time.time() + seconds
        while time.time() < end or self.pause_flag.is_set():
            if self.stop_flag.is_set():
                return False
            time.sleep(0.25)
        return True

    def _set(self, item, status, **extra):
        item["status"] = status
        item.update(extra)
        self.emit(item)

    def _ensure_browser(self):
        """Start headless Chrome on demand: mirror downloads never need it."""
        if self.driver:
            return self.driver
        _hold_profile()
        self.locked = True
        self.log("info", "Starting Chrome in the background…")
        self.driver = make_driver(self.folder, headless=not self.opts.get("show_browser"),
                                  profile_dir=self.profile_dir)
        user = current_user(self.driver)
        if not user:
            self.signed_out = True
            raise PermissionError("You're signed out of osu! Click “Sign in with osu!” and try again.")
        self.log("ok", f"Signed in as {user['username']}.")
        return self.driver

    def _run(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        # a mirror download that was killed outright (rather than stopped) leaves its
        # part-file behind, so clear those before starting
        for leftover in self.folder.glob("*.osz.part"):
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            if self.mirror_first:
                self.log("info", "Downloading from public mirrors, so no sign-in or browser is needed.")
            else:
                self._ensure_browser()
            self._loop()
        except Exception as e:  # surface anything unexpected to the UI instead of dying silently
            self.log("error", friendly_error(e))
        finally:
            if self.driver:
                # give in-flight downloads a moment to land before closing Chrome
                deadline = time.time() + 30
                while time.time() < deadline and any(self.folder.glob("*.crdownload")):
                    time.sleep(0.5)
                self.driver.quit()
            if self.locked:
                PROFILE_LOCK.release()
            for item in self.items:
                if item["status"] in ("queued", "downloading"):
                    self._set(item, "cancelled" if self.stop_flag.is_set() else "failed")
            self.log("info", "Stopped." if self.stop_flag.is_set() else "All done.")
            if self.on_finish:
                # the last status changes may have been inside the save throttle, so make
                # sure what is on disk matches how the run actually ended
                try:
                    self.on_finish()
                except Exception:
                    pass
            self._notify_done()

    def _notify_done(self):
        """Desktop notification, since a big queue is something you walk away from."""
        if not self.opts.get("notify"):
            return
        done = sum(1 for i in self.items if i["status"] == "done")
        failed = sum(1 for i in self.items if i["status"] == "failed")
        if not done and not failed:
            return
        what = "Stopped" if self.stop_flag.is_set() else "Finished"
        notify("osu! Beatmap Downloader",
               f"{what}: {done} beatmap{'' if done == 1 else 's'} downloaded"
               + (f", {failed} failed." if failed else "."))

    def _loop(self):
        mirror = self.mirror_first
        delay = float(self.opts.get("mirror_delay", 1) if mirror else self.opts.get("delay", 5))
        batch, rest = int(self.opts.get("batch", 60)), float(self.opts.get("rest", 15))
        cooldown = float(self.opts.get("cooldown", 300))
        timeout = float(self.opts.get("timeout", 90))
        since_rest, fails_in_row = 0, 0

        for item in self.items:
            if self.stop_flag.is_set():
                return
            if item["status"] != "queued":
                continue
            if not mirror and since_rest >= batch:
                self.log("info", f"Taking a {rest:g}s breather to stay under osu!'s rate limit.")
                if not self._sleep(rest):
                    return
                since_rest = 0
            if not self._sleep(0):
                return

            self._set(item, "downloading")
            started = time.time()
            ok, reason = self._download_one(item, timeout)
            since_rest += 1
            attempt = 0
            while not ok and "quota" in reason.lower():
                # osu!'s hourly download limit: keep this map and retry it on a 5/10/20/25 min cycle
                if not self.quota_hit_at:
                    self.quota_hit_at = time.time()
                    # only learn *higher* limits (supporters): a run started partway through
                    # an hour sees fewer than the real allowance before being refused.
                    # hourly_limit is None when mirrors are doing the work and we only fell
                    # back to the website for this one map.
                    if self.hourly_limit is None:
                        self.hourly_limit = max(self.batch_done, DEFAULT_HOURLY_LIMIT)
                    elif self.batch_done > self.hourly_limit:
                        self.hourly_limit = self.batch_done
                    self.limit_learned = True
                wait = QUOTA_RETRY_WAITS[attempt % len(QUOTA_RETRY_WAITS)]
                attempt += 1
                self.retry_attempt = (attempt - 1) % len(QUOTA_RETRY_WAITS) + 1
                self.retry_at = time.time() + wait
                self._set(item, "queued", error="")
                self.log("warn", f"osu!'s hourly download limit was reached. Retrying in {_duration(wait)} "
                                 f"(at {time.strftime('%H:%M', time.localtime(self.retry_at))}, "
                                 f"attempt {self.retry_attempt} of {len(QUOTA_RETRY_WAITS)}).")
                if not self._sleep(wait):
                    return
                self.retry_at = None
                self._set(item, "downloading")
                started = time.time()
                ok, reason = self._download_one(item, timeout)
            if ok and self.quota_hit_at:
                self.log("ok", "osu! is accepting downloads again.")
                self.quota_hit_at, self.retry_attempt, self.batch_done = None, 0, 0
                self.batch_started_at = None
            if ok:
                fails_in_row = 0
                self.batch_done += 1
                self.batch_started_at = self.batch_started_at or started
                took = time.time() - started + delay
                self.per_map = took if self.per_map is None else self.per_map * 0.9 + took * 0.1
                self._set(item, "done", file=ok, error="")
                if self.opts.get("auto_open"):
                    self._import(ok)
            else:
                fails_in_row += 1
                self._set(item, "failed", error=reason)
                self.log("warn", f"{item['id']}: {reason}")
                if fails_in_row >= 3:
                    self.log("warn", f"Several failures in a row, so osu! may be rate limiting. "
                                     f"Cooling down for {_duration(cooldown)}.")
                    if not self._sleep(cooldown):
                        return
                    fails_in_row = 0
            if not self._sleep(delay):
                return

    def _import(self, path):
        wanted = self.opts.get("import_client", "stable")
        used = import_into_osu(path, wanted, self.opts.get("songs_dir", ""), self.opts.get("osu_paths"))
        if used != wanted and not self.warned_client:
            self.warned_client = True
            other = f"osu!{used}" if used != "default" else "the default app"
            self.log("warn", f"osu!{wanted} isn't installed, so importing with {other} instead.")

    def _download_one(self, item, timeout):
        """Download one set. Returns (path, '') or (None, reason)."""
        sid = item["id"]
        existing = find_osz(self.folder, sid)
        if existing:
            return existing, ""
        if self.mirror_first:
            path, who = download_from_mirror(sid, self.folder, item,
                                             no_video=bool(self.opts.get("no_video")),
                                             timeout=timeout, stop=self.stop_flag)
            if path:
                if who != self.mirror_used:  # say which mirror once, not on every map
                    self.mirror_used = who
                    self.log("info", f"Downloading from {who}.")
                return path, ""
            if who == "Cancelled." or self.stop_flag.is_set():
                return None, "Cancelled."
            if not self.allow_browser:
                return None, who
            self.log("warn", f"{sid}: {who} Trying osu.ppy.sh instead.")
        return self._download_via_browser(item, timeout)

    def _download_via_browser(self, item, timeout):
        sid = item["id"]
        try:
            driver = self._ensure_browser()
        except PermissionError:
            raise
        except Exception as e:
            return None, friendly_error(e)

        # close stray tabs a download might have opened
        while len(driver.window_handles) > 1:
            driver.switch_to.window(driver.window_handles[-1])
            driver.close()
        driver.switch_to.window(driver.window_handles[0])

        driver.get(f"{OSU}/beatmapsets/{sid}")
        href = None
        for _ in range(20):  # page is rendered client-side; wait for the button
            href = driver.execute_script(CLICK_DOWNLOAD_JS, sid, bool(self.opts.get("no_video")))
            if href or self.stop_flag.is_set():
                break
            time.sleep(0.25)
        if not item.get("title"):
            meta = driver.execute_script(
                "const t = document.title.replace(/[\\u200e\\u200f\\u202a-\\u202e]/g, '');"
                "return t.split(' · ')[0];")
            if meta and "not found" not in meta.lower():
                artist, _, title = meta.partition(" - ")
                item.update(artist=artist, title=title or artist)
                self.emit(item)
        if not href:
            text = driver.execute_script("return document.body.innerText.slice(0, 2000)") or ""
            if "not found" in text.lower() or "doesn't exist" in text.lower():
                return None, "Beatmap not found (deleted or restricted)."
            return None, "No download button (map may be unavailable, or explicit content is hidden in your osu! settings)."

        # wait for the .osz to appear and finish
        start = time.time()
        while time.time() - start < timeout:
            if self.stop_flag.is_set():
                return None, "Cancelled."
            done = find_osz(self.folder, sid)
            if done:
                return done, ""
            if time.time() - start > 1 and not any(self.folder.glob("*.crdownload")):
                # a refused download replaces the page with osu!'s "too many requests" page
                text = (driver.execute_script(
                    "return document.title + ' ' + (document.body ? document.body.innerText.slice(0, 500) : '')") or "").lower()
                if "quota" in text or "too many requests" in text:
                    driver.back()
                    return None, "osu! download quota reached."
            time.sleep(0.5)
        return None, "Timed out waiting for the download."


# ---------------------------------------------------------------- process housekeeping

CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def close_chrome_with_app(profile_dir=None):
    """Make every Chrome this app starts die with it.

    Without this, closing the terminal leaves headless Chrome running, and that Chrome keeps
    the profile locked so the next launch can't start a browser. On Windows that's a job
    object; on POSIX we sweep away the browsers started with our own Chrome profile.
    Programs that should outlive the app (osu!) are unaffected by either.
    """
    if os.name != "nt":
        return _close_chrome_with_app_posix(profile_dir)
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    job = k32.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    if not (job and k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())):
        return None
    return job  # the caller must keep this handle alive for the life of the app


BROWSER_EXE_NAMES = ("chrome", "chromium", "chromedriver", "brave", "msedge", "vivaldi")


def _is_browser_exe(pid, argv0):
    """Whether this process really is a browser binary, not something merely mentioning one."""
    name = Path(os.readlink(f"/proc/{pid}/exe")).name if os.path.islink(f"/proc/{pid}/exe") else Path(argv0).name
    return any(part in name.lower() for part in BROWSER_EXE_NAMES)


def _chrome_pids_using(profile_dir):
    """PIDs of Chrome/chromedriver processes running against our profile directory.

    Matching is deliberately strict: the profile has to appear as an actual --user-data-dir
    argument and the process has to be a browser binary. A loose substring match would also
    catch this very app, whose own command line mentions both.
    """
    if not Path("/proc").is_dir():
        return []  # Linux-only; macOS just leaves the browser alone, as it always has
    wanted = str(profile_dir).rstrip("/")
    # Chromium rewrites its argv into a single space-joined string, so /proc/<pid>/cmdline is
    # not reliably NUL-separated. Flatten it and match the flag as text.
    flag = re.compile(r"--user-data-dir=" + re.escape(wanted) + r"/?(?:\s|$)")
    me = os.getpid()
    pids = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            cmdline = Path(entry.path, "cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ")
        except OSError:
            continue  # the process exited while we were looking
        if not cmdline.strip() or not flag.search(cmdline):
            continue
        try:
            if _is_browser_exe(entry.name, cmdline.split(" ", 1)[0]):
                pids.append(int(entry.name))
        except OSError:
            continue
    return pids


def _kill_pids(pids, wait=10):
    """SIGTERM, then SIGKILL whatever is left. Returns how many were signalled."""
    import signal
    alive = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            alive.append(pid)
        except OSError:
            pass  # already gone, or not ours to signal
    signalled = len(alive)
    deadline = time.time() + wait
    while alive and time.time() < deadline:
        alive = [pid for pid in alive if Path(f"/proc/{pid}").exists()]
        if alive:
            time.sleep(0.25)
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return signalled


def _close_chrome_with_app_posix(profile_dir):
    """Sweep our Chrome processes away when the app exits.

    We can't use a process group here: leaving the terminal's foreground group would stop
    Ctrl+C reaching us. Instead, find the browsers by the profile they were started with,
    which also means we only ever touch Chrome windows that belong to this app.
    """
    import atexit
    import signal
    if not profile_dir:
        return None

    def sweep(signum=None, _frame=None):
        _kill_pids(_chrome_pids_using(profile_dir), wait=5)
        if signum is not None:
            os._exit(0)

    atexit.register(sweep)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, sweep)
        except (OSError, ValueError):
            pass  # not the main thread, or the signal doesn't exist here
    return sweep


def _close_leftover_chrome_posix(profile_dir):
    """Kill a Chrome still holding our profile, and clear the lock it left behind.

    Chrome guards a profile with a SingletonLock symlink; a stale one (the app was killed)
    makes the next Chrome refuse to start.
    """
    killed = _kill_pids(_chrome_pids_using(profile_dir))
    if killed:
        time.sleep(1)
    profile = Path(profile_dir)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        link = profile / name
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
        except OSError:
            pass
    return killed


def close_leftover_chrome(profile_dir):
    """Stop any Chrome still using our profile (e.g. from a copy of the app that crashed)."""
    if os.name != "nt":
        return _close_leftover_chrome_posix(profile_dir) if Path(profile_dir).is_dir() else 0
    lock = Path(profile_dir) / "lockfile"
    if not lock.exists():
        return 0
    try:
        lock.unlink()  # succeeds only if no Chrome has the profile open
        return 0
    except OSError:
        pass
    import subprocess
    needle = str(Path(profile_dir)).replace("'", "''")
    script = ("$n = 0; Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='chromedriver.exe'\" | "
              f"Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{needle}') }} | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }; $n")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                         timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    time.sleep(1)
    try:
        return int(out.stdout.strip() or 0)
    except ValueError:
        return 0


def friendly_error(e):
    """A readable one-line message for an exception (Selenium's include whole stack traces)."""
    text = str(e).strip()
    if "session not created" in text and ("crashed" in text or "DevToolsActivePort" in text):
        return ("Chrome couldn't start. Another copy of this app (or a leftover Chrome) may still be using "
                "its Chrome profile. Close other copies and try again.")
    if "session not created" in text and "version" in text.lower():
        return "Chrome couldn't start: ChromeDriver doesn't match your Chrome. Update Chrome and try again."
    first = text.removeprefix("Message: ").splitlines()[0] if text else type(e).__name__
    return first


# ---------------------------------------------------------------- osu! clients

def _child_env():
    """Environment for programs we launch: undo app.py's TEMP redirect so osu! uses the real one."""
    env = dict(os.environ)
    for key in ("TEMP", "TMP"):
        if env.get(f"OBD_ORIGINAL_{key}"):
            env[key] = env[f"OBD_ORIGINAL_{key}"]
    return env


def _association_exe(prog_id):
    """Executable registered for a file type, e.g. 'osustable.File.osz' → C:\\...\\osu!.exe."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as key:
            command = winreg.QueryValue(key, None)
    except (ImportError, OSError):
        return None
    m = re.match(r'\s*"([^"]+)"|\s*(\S+)', command or "")
    return (m.group(1) or m.group(2)) if m else None


FLATPAK_PREFIX = "flatpak:"  # a launcher we run with `flatpak run <id>` instead of directly


def _is_lazer(exe):
    """Whether this launcher is osu!lazer rather than osu!stable."""
    path = Path(str(exe).removeprefix(FLATPAK_PREFIX))
    if str(exe).startswith(FLATPAK_PREFIX):
        return True
    if path.suffix.lower() == ".exe":
        # both clients ship an 'osu!.exe'; only lazer has the .NET game assembly beside it
        return (path.parent / "osu.Game.dll").exists()
    return True  # a native Linux/macOS launcher is always lazer; stable only runs under Wine


def _exe_in(folder):
    """osu! launchers inside a folder the user picked (lazer's install root keeps it under current/)."""
    if not folder:
        return []
    p = Path(folder)
    if p.suffix.lower() == ".exe" or (os.name != "nt" and p.is_file()):
        return [p]
    out = [p / "osu!.exe", p / "current" / "osu!.exe"]
    if os.name != "nt":
        # a native lazer install: the AppImage, or the launcher script a distro package adds
        out += [p / "osu.AppImage", p / "osu-lazer", p / "osu!", *sorted(p.glob("osu*.AppImage"))]
    return out


def osu_in_folder(client, folder):
    """The osu!stable/lazer exe inside a user-picked folder, or None."""
    for exe in _exe_in(folder):
        if exe.is_file() and _is_lazer(exe) == (client == "lazer"):
            return str(exe)
    return None


def _flatpak_app(app_id):
    """'flatpak:<id>' if that Flatpak is installed, else None."""
    import shutil
    import subprocess
    if not shutil.which("flatpak"):
        return None
    try:
        out = subprocess.run(["flatpak", "info", app_id], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return FLATPAK_PREFIX + app_id if out.returncode == 0 else None


def _wine_prefix(exe):
    """The WINEPREFIX an osu!stable install lives in, or None."""
    for parent in Path(exe).parents:
        if parent.name == "drive_c":
            return parent.parent
    return None


def _find_osu_linux(client, songs_dir="", custom=""):
    """osu! on Linux: lazer runs natively, stable only under Wine."""
    import shutil
    home = Path.home()
    if client == "lazer":
        candidates = [*_exe_in(custom), custom,
                      shutil.which("osu-lazer"), shutil.which("osu!"), shutil.which("osu-lazer-bin"),
                      "/opt/osu-lazer/osu.AppImage",
                      *sorted(home.glob("Applications/osu*.AppImage")),
                      *sorted(home.glob(".local/bin/osu*.AppImage")),
                      *sorted(home.glob("Downloads/osu*.AppImage")),
                      _flatpak_app("sh.ppy.osu")]
    else:
        candidates = [*_exe_in(custom),
                      Path(songs_dir).parent / "osu!.exe" if songs_dir else None,
                      # osu-winello / osu-wine, then the plain default prefix
                      home / ".local/share/osu-wine/osu!/osu!.exe",
                      home / ".local/share/wineprefixes/osu-wine/drive_c/osu!/osu!.exe",
                      home / ".wine/drive_c/osu!/osu!.exe",
                      home / "Games/osu!/osu!.exe",
                      *sorted(home.glob(".wine*/drive_c/**/osu!.exe"))[:5]]
    for exe in candidates:
        if not exe:
            continue
        if str(exe).startswith(FLATPAK_PREFIX):
            return str(exe)
        path = Path(exe)
        if path.is_file() and os.access(path, os.X_OK if path.suffix.lower() != ".exe" else os.F_OK) \
                and _is_lazer(path) == (client == "lazer"):
            return str(path)
    return None


def find_osu(client, songs_dir="", custom=""):
    """Path to the osu!stable or osu!lazer executable, or None if it isn't installed.

    Installs can live anywhere, so check (in order) the folder the user picked, the folder
    above their Songs folder, the program Windows opens .osz files with, and the default paths.
    """
    import sys
    if os.name != "nt" and sys.platform != "darwin":
        return _find_osu_linux(client, songs_dir, custom)
    local = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    if client == "stable":
        candidates = [*_exe_in(custom),
                      Path(songs_dir).parent / "osu!.exe" if songs_dir else None,
                      _association_exe("osustable.File.osz"),
                      _association_exe("osu!"),
                      local / "osu!" / "osu!.exe"]
    else:
        candidates = [*_exe_in(custom),
                      _association_exe("osu.File.osz"),
                      local / "osulazer" / "current" / "osu!.exe",
                      local / "osulazer" / "osu!.exe"]
    for exe in candidates:
        if exe and Path(exe).is_file() and _is_lazer(exe) == (client == "lazer"):
            return str(exe)
    return None


def _wine_path(path, prefix):
    """A Windows-side path for an .osz, so osu!stable under Wine can open it."""
    import subprocess
    env = {**_child_env(), "WINEPREFIX": str(prefix), "WINEDEBUG": "-all"}
    try:
        out = subprocess.run(["winepath", "-w", str(path)], capture_output=True, text=True,
                             timeout=60, env=env).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    # Wine maps the whole filesystem to Z: by default, so this is a safe fallback
    return "Z:" + str(path).replace("/", "\\")


def _launch_osu(exe, path):
    """Hand an .osz to an osu! launcher, whatever shape it takes on this platform."""
    import shutil
    import subprocess
    env, cwd = _child_env(), None
    if str(exe).startswith(FLATPAK_PREFIX):
        cmd = ["flatpak", "run", str(exe).removeprefix(FLATPAK_PREFIX), str(path)]
    elif os.name != "nt" and Path(exe).suffix.lower() == ".exe":
        # osu!stable only runs under Wine, and it needs the prefix its install lives in
        wine = shutil.which("wine")
        if not wine:
            raise RuntimeError("osu!stable needs Wine to run. Install wine, or import into osu!lazer instead.")
        prefix = _wine_prefix(exe)
        if prefix:
            env = {**env, "WINEPREFIX": str(prefix)}
        cmd, cwd = [wine, str(exe), _wine_path(path, prefix or Path.home() / ".wine")], str(Path(exe).parent)
    else:
        cmd, cwd = [str(exe), str(path)], str(Path(exe).parent)
    extra = ({"creationflags": CREATE_BREAKAWAY_FROM_JOB} if os.name == "nt"
             else {"start_new_session": True})  # osu! keeps running after the app closes
    subprocess.Popen(cmd, env=env, cwd=cwd, **extra)


def import_into_osu(path, client, songs_dir="", custom_paths=None):
    """Open an .osz with the chosen client (the other one if it's missing). Returns what was used."""
    other = "lazer" if client == "stable" else "stable"
    for c in (client, other):
        exe = find_osu(c, songs_dir, (custom_paths or {}).get(c, ""))
        if exe:
            _launch_osu(exe, path)
            return c
    open_file(path)
    return "default"


def open_file(path):
    """Hand a file or folder to the OS (.osz files open in whichever osu! owns the file type)."""
    import subprocess, sys
    if os.name == "nt":
        # explorer hands the file to its default app from the desktop shell, so that app
        # doesn't inherit our redirected TEMP the way os.startfile's children would
        subprocess.Popen(["explorer", str(Path(path))], creationflags=CREATE_BREAKAWAY_FROM_JOB)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                         env=_child_env(), start_new_session=True)
