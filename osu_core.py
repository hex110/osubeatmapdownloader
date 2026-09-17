"""osu! beatmap fetching + headless Chrome downloading.

Everything that talks to osu.ppy.sh lives here; app.py only wires it to the UI.
"""
import json
import os
import re
import socket
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


def fetch_user_maps(user, kind, limit, on_progress=None, min_plays=0, stop=None):
    """Return [{id, title, artist, cover, plays}] from a user's profile list, deduplicated.

    `min_plays` only applies to the most_played list, which is ordered by play count, so
    the first map below the threshold ends the search.
    """
    uid = resolve_user_id(user)
    min_plays = min_plays if kind == "most_played" else 0
    out, seen, offset = [], set(), 0
    while len(out) < limit:
        _check(stop)
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
                        "creator": s.get("creator", ""),
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


# ---------------------------------------------------------------- osu!collector
#
# Collections are lists other players share, so they're just another way of naming a pile of
# beatmap sets. The paginated endpoint carries artist/title/creator alongside the IDs, which
# the flat one doesn't, and that metadata is what lets an already-owned map be recognised.

OSU_COLLECTOR = "https://osucollector.com"
COLLECTOR_PAGE = 100  # the API caps a page at this many beatmaps however much we ask for


def parse_collection_id(text):
    """The collection ID out of an osu!collector link, or a bare number."""
    text = (text or "").strip()
    m = re.search(r"osucollector\.com/collections/(\d+)", text) or re.fullmatch(r"(\d+)", text)
    if not m:
        raise ValueError("Paste an osu!collector link, e.g. https://osucollector.com/collections/23333")
    return m.group(1)


def fetch_collection(text, limit=20000, on_progress=None, stop=None):
    """Return (collection info, [{id, title, artist, creator}]) for an osu!collector collection."""
    cid = parse_collection_id(text)
    try:
        info = _get_json(f"{OSU_COLLECTOR}/api/collections/{cid}?perPage=1")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"osu!collector has no collection {cid}.") from None
        raise
    out, seen, cursor = [], set(), None
    while len(out) < limit:
        _check(stop)
        url = f"{OSU_COLLECTOR}/api/collections/{cid}/beatmapsv2?perPage={COLLECTOR_PAGE}"
        page = _get_json(url + (f"&cursor={cursor}" if cursor else ""))
        rows = page.get("beatmaps") or []
        for row in rows:
            meta = row.get("beatmapset") or {}
            sid = str(row.get("beatmapset_id") or meta.get("id") or "")
            if not sid or sid in seen:
                continue  # a set appears once per difficulty
            seen.add(sid)
            out.append({"id": sid, "title": meta.get("title", ""), "artist": meta.get("artist", ""),
                        "creator": meta.get("creator", ""), "cover": "", "plays": 0})
            if len(out) >= limit:
                break
        if on_progress:
            on_progress(len(out))
        if not page.get("hasMore") or not rows:
            break
        cursor = page.get("nextPageCursor")
        if not cursor:
            break
    return {"id": cid, "name": info.get("name") or f"Collection {cid}",
            "uploader": (info.get("uploader") or {}).get("username", ""),
            "unsubmitted": info.get("unsubmittedBeatmapCount") or 0}, out


# ---------------------------------------------------------------- matching songs to beatmaps
#
# Turning "Artist - Title" into a beatmap is guesswork: the same song exists as a dozen
# different maps, plus remixes, TV-size cuts and covers that read almost identically. So this
# ranks candidates and hands them to the user to confirm rather than picking silently.

SEARCH_PROVIDERS = ("https://catboy.best/api/search", "https://osu.direct/api/search")
SEARCH_STATUS = {-2: "graveyard", -1: "wip", 0: "pending", 1: "ranked",
                 2: "approved", 3: "qualified", 4: "loved"}
GOOD_STATUS = (1, 2, 4)      # ranked, approved, loved
MATCH_FLOOR = 0.55           # below this the names simply don't agree


def _norm_song(text):
    """Normalise a title or artist so near-identical names compare equal."""
    text = (text or "").casefold()
    text = re.sub(r"[(\[][^)\]]*[)\]]", " ", text)          # (TV Size), [Remix], ...
    text = re.sub(r"\b(feat|ft|featuring|with|vs)\b.*", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)                    # punctuation varies wildly
    return re.sub(r"\s+", " ", text).strip()


def parse_tracks(text):
    """[(artist, title)] from a pasted track list or an exported playlist CSV.

    Handles the CSV that playlist exporters produce, and plain "Artist - Title" lines.
    """
    import csv
    import io
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return []
    # a CSV export: find the artist and title columns by name
    if lines[0].count(",") >= 2 and '"' in lines[0] or "track name" in lines[0].casefold():
        try:
            rows = list(csv.DictReader(io.StringIO("\n".join(lines))))
        except csv.Error:
            rows = []
        if rows:
            def column(row, *wanted):
                for key in row:
                    name = (key or "").casefold().strip()
                    if any(w in name for w in wanted):
                        return (row[key] or "").strip()
                return ""
            out = []
            for row in rows:
                title = column(row, "track name", "song", "title")
                artist = column(row, "artist name", "artist")
                if title:
                    out.append((artist.split(",")[0].strip(), title))
            if out:
                return out
    out = []
    for line in lines:
        line = re.sub(r"^\s*\d+[.)]\s*", "", line.strip())   # "1. Artist - Title"
        parts = re.split(r"\s+[-\u2013\u2014]\s+", line, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            out.append((parts[0].strip(), parts[1].strip()))
        elif line:
            out.append(("", line))  # no separator: treat the whole line as a title
    return out


SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"


def parse_spotify_link(text):
    """(kind, id) from a Spotify playlist or album link, URI, or bare ID."""
    text = (text or "").strip()
    m = re.search(r"(playlist|album)[/:]([A-Za-z0-9]{22})", text)
    if m:
        return m.group(1), m.group(2)
    if re.fullmatch(r"[A-Za-z0-9]{22}", text):
        return "playlist", text
    raise ValueError("Paste a Spotify playlist link, e.g. "
                     "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")


def spotify_token(client_id, client_secret):
    """An app access token. Spotify needs one even to read a public playlist."""
    import base64
    if not client_id or not client_secret:
        raise ValueError("Add your Spotify client ID and secret in Folders & options first.")
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        SPOTIFY_TOKEN_URL, data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth}", "User-Agent": UA,
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))["access_token"]
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):
            raise ValueError("Spotify rejected those credentials. Check the client ID and "
                             "secret from your app at developer.spotify.com/dashboard.") from None
        raise


def fetch_spotify_tracks(link, client_id, client_secret, limit=500, on_progress=None, stop=None):
    """[(artist, title)] from a public Spotify playlist or album."""
    kind, sid = parse_spotify_link(link)
    token = spotify_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}", "User-Agent": UA, "Accept": "application/json"}
    url = f"{SPOTIFY_API}/{kind}s/{sid}/tracks?limit=50"
    out = []
    while url and len(out) < limit:
        _check(stop)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                page = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ValueError("Spotify has no such playlist, or it isn't public.") from None
            if e.code == 403:
                raise ValueError("Spotify won't share that playlist with an app. Editorial "
                                 "playlists made by Spotify itself are blocked; a playlist you "
                                 "or another user made works.") from None
            raise
        for item in page.get("items") or []:
            track = item.get("track") if kind == "playlist" else item
            if not track or track.get("is_local"):
                continue
            artists = ", ".join(a.get("name", "") for a in (track.get("artists") or []))
            if track.get("name"):
                out.append((artists.split(",")[0].strip(), track["name"]))
            if len(out) >= limit:
                break
        if on_progress:
            on_progress(len(out))
        url = page.get("next")
    return out


def search_beatmaps(query, amount=8):
    """Candidate beatmap sets for a search string, from whichever mirror answers."""
    last = None
    for provider in SEARCH_PROVIDERS:
        try:
            rows = _get_json(f"{provider}?{urllib.parse.urlencode({'query': query, 'amount': amount})}")
        except Exception as e:
            last = e
            continue
        out = []
        for row in rows if isinstance(rows, list) else []:
            children = row.get("ChildrenBeatmaps") or []
            out.append({
                "id": str(row.get("SetID") or ""),
                "artist": row.get("Artist") or "",
                "title": row.get("Title") or "",
                "creator": row.get("Creator") or "",
                "status": SEARCH_STATUS.get(row.get("RankedStatus"), "unknown"),
                "favourites": row.get("Favourites") or 0,
                "plays": sum(b.get("Playcount") or 0 for b in children),
                "diffs": len(children),
            })
        if out:
            return [c for c in out if c["id"]]
    if last:
        raise last
    return []


def score_candidate(artist, title, candidate):
    """How well a beatmap set matches a song: 0 to 1, with the name agreeing most."""
    import math
    from difflib import SequenceMatcher
    ratio = lambda a, b: SequenceMatcher(None, a, b).ratio() if a and b else 0.0
    title_score = ratio(_norm_song(title), _norm_song(candidate["title"]))
    artist_score = ratio(_norm_song(artist), _norm_song(candidate["artist"])) if artist else title_score
    text = 0.65 * title_score + 0.35 * artist_score
    # among maps of the same song, the one people actually played is the one wanted
    weight = candidate["plays"] + 50 * candidate["favourites"]
    popularity = min(1.0, math.log10(1 + weight) / 6)
    bonus = 0.05 if candidate["status"] in ("ranked", "approved", "loved") else 0.0
    return round(min(1.0, 0.75 * text + 0.20 * popularity + bonus), 4), round(text, 4)


def match_track(artist, title, amount=8):
    """Search for one song and return its candidates, best first."""
    query = f"{artist} {title}".strip() or title
    candidates = search_beatmaps(query, amount=amount)
    if not candidates and artist:
        candidates = search_beatmaps(title, amount=amount)  # the artist name may differ on osu!
    scored = []
    for candidate in candidates:
        score, text = score_candidate(artist, title, candidate)
        scored.append({**candidate, "score": score, "text": text})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored


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

# Mirrors are not equally quick, and which one is quick changes through the day: one that
# served 3 MB/s an hour ago can drop to 0.4 MB/s while another serves the same file from a
# CDN cache ten times faster. Trying them in a fixed order only moves on when one *fails*,
# so a mirror that is merely crawling would be used forever. Remember what each one has
# actually delivered and put the quickest first instead.
MIRROR_SPEEDS = {}                 # name -> (bytes per second, when measured)
MIRROR_PACE = {}                   # name -> seconds a mirror's own rate limit asks us to wait
_SPEED_LOCK = threading.Lock()
SPEED_MIN_SAMPLE = 512 * 1024      # a download too small to time reliably
PROBE_EVERY = 40                   # re-check one other mirror this often, to notice changes
_since_probe = 0


def record_mirror_speed(name, size, seconds, ok=True):
    """Remember how a mirror performed. A failure scores zero, which sends it to the back."""
    if ok and (seconds <= 0 or size < SPEED_MIN_SAMPLE):
        return  # too small or too quick to be a fair measurement
    rate = (size / seconds) if ok else 0.0
    with _SPEED_LOCK:
        previous = MIRROR_SPEEDS.get(name)
        if previous and previous[0] and ok:
            rate = previous[0] * 0.7 + rate * 0.3  # smooth out one odd download
        MIRROR_SPEEDS[name] = (rate, time.time())


def ordered_mirrors():
    """MIRRORS, quickest known first, occasionally re-checking one of the others.

    Mirrors are re-checked one at a time rather than all at once: sending every map to a
    mirror that has gone slow is the whole problem, and a wholesale re-probe reintroduces it
    every few minutes. Probing the single least recently measured mirror once every
    PROBE_EVERY downloads notices a mirror getting better or worse at a tiny cost.
    """
    global _since_probe
    with _SPEED_LOCK:
        known = dict(MIRROR_SPEEDS)
        _since_probe += 1
        probing = _since_probe >= PROBE_EVERY and len(known) >= len(MIRRORS)
        if probing:
            _since_probe = 0

    ranked = sorted(MIRRORS, key=lambda m: known.get(m[0], (float("inf"), 0))[0], reverse=True)
    if probing:
        stalest = min(MIRRORS, key=lambda m: known.get(m[0], (0, 0))[1])
        ranked.remove(stalest)
        ranked.insert(0, stalest)
    return ranked


def note_rate_limit(name, headers):
    """Respect a mirror that publishes a request budget, so we never outrun what it allows."""
    try:
        remaining = int(headers.get("X-RateLimit-Remaining"))
        reset = headers.get("X-RateLimit-Reset")
    except (TypeError, ValueError):
        return
    if not reset:
        return
    try:
        from email.utils import parsedate_to_datetime
        seconds_left = parsedate_to_datetime(reset).timestamp() - time.time()
    except (TypeError, ValueError):
        return
    if seconds_left <= 0:
        MIRROR_PACE.pop(name, None)
        return
    # spread whatever is left over the time left, with a little headroom
    MIRROR_PACE[name] = 0.0 if remaining > 50 else seconds_left / max(1, remaining)


def mirror_pace(name):
    """The minimum gap this mirror's own rate limit asks for, in seconds."""
    return MIRROR_PACE.get(name, 0.0)


def speeds_snapshot():
    """Learned mirror speeds, for saving between runs."""
    with _SPEED_LOCK:
        return {name: [rate, when] for name, (rate, when) in MIRROR_SPEEDS.items()}


def restore_speeds(saved):
    """Reload speeds measured in an earlier run, so a restart doesn't start from nothing."""
    if not isinstance(saved, dict):
        return
    with _SPEED_LOCK:
        for name, value in saved.items():
            try:
                rate, when = float(value[0]), float(value[1])
            except (TypeError, ValueError, IndexError):
                continue
            if any(name == m[0] for m in MIRRORS):
                MIRROR_SPEEDS[name] = (rate, when)


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


# A stalled download is one that stops making progress, not just a slow one: beatmaps run
# well past 20 MB, so capping the total time would fail perfectly good downloads on a slow
# link. Everything below measures the gap between bytes instead.
DEFAULT_STALL = 10
TRICKLE_FLOOR = 1024  # bytes/sec below which a download is stuck rather than merely slow


class _Stalled(Exception):
    """A download that stopped making progress."""


class _TooSlow(Exception):
    """A download crawling far behind a mirror we know to be quicker."""


SLOW_GRACE = 8          # give a download this long before judging it slow
SLOW_FRACTION = 0.25    # ...then abandon it if it's under this share of the best mirror


def _best_known_rate(exclude):
    with _SPEED_LOCK:
        rates = [r for name, (r, _) in MIRROR_SPEEDS.items() if name != exclude and r]
    return max(rates, default=0.0)


def download_from_mirror(sid, folder, item=None, no_video=False, timeout=90, stop=None,
                         stall=DEFAULT_STALL):
    """Fetch one beatmapset from the first mirror that has it.

    Returns (path, mirror name) on success or (None, reason). Tries every mirror before
    giving up, so one being down, missing a map or hanging doesn't fail the download.
    """
    item = item or {}
    reason, absent = "No mirror had this beatmap.", 0
    stall = max(1, float(stall or DEFAULT_STALL))
    for name, full_url, novideo_url in ordered_mirrors():
        if stop is not None and stop.is_set():
            return None, "Cancelled."
        url = (novideo_url if no_video else full_url).format(sid=sid)
        best_elsewhere = _best_known_rate(exclude=name)
        tmp, began = None, time.time()  # from before the request: a mirror that takes four
        try:                            # seconds to answer is slow, however fast it then sends
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            # the socket timeout covers connecting and each individual read, so a mirror
            # that accepts the connection and then goes quiet trips it
            with urllib.request.urlopen(req, timeout=stall) as r:
                note_rate_limit(name, r.headers)
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "html" in ctype or "json" in ctype:
                    reason, absent = f"{name}: doesn't have this beatmap.", absent + 1
                    record_mirror_speed(name, 0, 0, ok=False)
                    continue
                # read1() hands back whatever has arrived instead of blocking until the
                # buffer is full, which is what lets the stall checks below run at all
                first = r.read1(65536)
                if len(first) == 1:
                    first += r.read1(65536)  # a tiny opening segment; get one more
                if not first.startswith(b"PK"):  # every .osz is a zip
                    reason = f"{name}: didn't send a beatmap file."
                    continue
                target = Path(folder) / _osz_name(sid, r.headers.get("Content-Disposition"), item)
                tmp = target.with_name(target.name + ".part")
                size = 0
                # a mirror can also dribble bytes out slowly enough that the socket timeout
                # never fires, which hangs the queue just as badly. Measure that over a
                # sliding window: an average taken from the start would let a fast opening
                # burst hide a stall that begins a minute later.
                window_start, window_bytes = time.time(), 0
                with open(tmp, "wb") as f:
                    chunk = first
                    while chunk:
                        if stop is not None and stop.is_set():
                            raise _Cancelled
                        f.write(chunk)
                        size += len(chunk)
                        window_bytes += len(chunk)
                        if time.time() - window_start > stall:
                            if window_bytes < TRICKLE_FLOOR * stall:
                                raise _Stalled
                            window_start, window_bytes = time.time(), 0
                        # a mirror can be perfectly alive and still be the wrong choice; if
                        # one we know is much quicker exists, cut the loss and use that
                        spent = time.time() - began
                        if spent > SLOW_GRACE and best_elsewhere:
                            if size / spent < best_elsewhere * SLOW_FRACTION:
                                raise _TooSlow
                        chunk = r.read1(262144)
            if size < 1024:
                reason = f"{name}: sent an empty file."
                tmp.unlink(missing_ok=True)
                continue
            tmp.replace(target)
            record_mirror_speed(name, size, time.time() - began)
            return str(target), name
        except _Cancelled:
            return None, "Cancelled."
        except _TooSlow:
            # not a failure, just a poor choice: keep the measurement honest and move on
            record_mirror_speed(name, size, time.time() - began)
            reason = f"{name}: much slower than another mirror."
        except _Stalled:
            reason = f"{name}: stalled (barely any data for {stall:g}s)."
            record_mirror_speed(name, 0, 0, ok=False)
        except (TimeoutError, socket.timeout):
            reason = f"{name}: no response for {stall:g}s."
            record_mirror_speed(name, 0, 0, ok=False)
        except urllib.error.HTTPError as e:
            absent += e.code == 404
            reason = f"{name}: " + ("doesn't have this beatmap." if e.code == 404 else f"HTTP {e.code}.")
            if e.code != 404:
                record_mirror_speed(name, 0, 0, ok=False)
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


def _meta(blob, field):
    m = re.search(rb"^" + field + rb"\s*:(.*)$", blob, re.M)
    return m.group(1).decode("utf-8", "replace").strip() if m else ""


def name_key(artist, title, creator):
    """The fallback identity of a beatmap set: who made it, of what, by whom.

    Used only for maps too old to carry an ID. Creator matters: there are a dozen different
    "The Quick Brown Fox - The Big Black" sets by different mappers, and matching on artist
    and title alone would treat them all as the same map.
    """
    norm = lambda v: re.sub(r"\s+", " ", (v or "")).strip().casefold()
    return (norm(artist), norm(title), norm(creator))


def scan_lazer_library(data_dir):
    """What's already in osu!lazer, as (beatmapset IDs, name keys for maps that have none).

    lazer keeps every file under 'files/' named by its SHA-256, with the metadata in a Realm
    database that needs Realm itself to read. The .osu difficulty files are plain text
    though, so read those instead. Scanning ~24k files takes about a tenth of a second
    because all but the header of most is skipped.

    Beatmaps saved before osu! file format v10 predate the BeatmapSetID field, so classics
    like The Big Black and Can't Defeat Airman can't be matched by ID at all. They fall back
    to artist/title/creator, which is why this returns two sets rather than one.
    """
    root = Path(data_dir) / "files"
    if not root.is_dir():
        raise ValueError("That isn't an osu!lazer data folder (it has no 'files' inside).")
    ids, keys = set(), set()
    for dirpath, _, names in os.walk(root):
        for n in names:
            try:
                with open(os.path.join(dirpath, n), "rb") as f:
                    if not f.read(15).startswith(b"osu file format"):
                        continue
                    f.seek(0)
                    blob = f.read(16384)  # [Metadata] is always near the top
            except OSError:
                continue
            m = re.search(rb"^BeatmapSetID\s*:\s*(\d+)", blob, re.M)
            if m and m.group(1) != b"0":
                ids.add(m.group(1).decode())
                continue
            key = name_key(_meta(blob, b"Artist"), _meta(blob, b"Title"), _meta(blob, b"Creator"))
            if all(key):  # a blank field would match far too much
                keys.add(key)
    return ids, keys


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
    driver.set_page_load_timeout(60)  # never let one wedged page stop the whole queue
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


class Cancelled(Exception):
    """The user called off a background job."""


def _check(stop):
    if stop is not None and stop.is_set():
        raise Cancelled


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


# ---------------------------------------------------------------- sign-in via your own browser
#
# Nicer than the dedicated Chrome window: osu! opens as a tab in the browser you're already
# using, where you may well be signed in already. Afterwards the osu! session cookie is copied
# into this app's Chrome profile, which is what the downloader actually uses.
#
# Firefox-family browsers keep cookie values in plain text in cookies.sqlite, so no key-ring or
# decryption is involved. Chromium-family ones encrypt theirs, so those fall back to the
# dedicated sign-in window.

FIREFOX_FAMILY = ("~/.zen", "~/.mozilla/firefox", "~/.librewolf", "~/.floorp",
                  "~/.waterfox", "~/.var/app/org.mozilla.firefox/.mozilla/firefox")


def find_cookie_dbs():
    """cookies.sqlite of every Firefox-family profile, most recently used first."""
    found = []
    for base in FIREFOX_FAMILY:
        root = Path(base).expanduser()
        if not root.is_dir():
            continue
        for db in root.glob("*/cookies.sqlite"):
            if db.is_file():
                found.append(db)
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def read_osu_cookies(db):
    """osu! cookies from one Firefox-family cookie store, ready for Selenium.

    The database is copied first: the browser is probably running, and its journal must come
    along or we'd read a stale snapshot.
    """
    import shutil
    import sqlite3
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    copy = Path(tmp_dir) / "cookies.sqlite"
    try:
        for suffix in ("", "-wal", "-shm"):
            part = Path(str(db) + suffix)
            if part.exists():
                shutil.copy2(part, str(copy) + suffix)
        con = sqlite3.connect(copy)
        try:
            rows = con.execute(
                "SELECT name, value, host, path, expiry, isSecure, isHttpOnly FROM moz_cookies "
                "WHERE host LIKE '%ppy.sh'").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    cookies, month = [], int(time.time()) + 30 * 86400
    for name, value, host, path, expiry, secure, http_only in rows:
        if not value:
            continue
        cookie = {"name": name, "value": value, "domain": host, "path": path or "/",
                  "secure": bool(secure), "httpOnly": bool(http_only)}
        # osu! sessions last about a month; some browsers store absurd expiries that
        # Chrome refuses, so keep it within a sane range
        if expiry:
            cookie["expiry"] = min(int(expiry), month)
        cookies.append(cookie)
    return cookies


def browser_osu_session():
    """The osu! cookies from whichever of your browsers most recently has them."""
    for db in find_cookie_dbs():
        cookies = read_osu_cookies(db)
        if any(c["name"] == "osu_session" for c in cookies):
            return cookies, db
    return [], None


def import_browser_session(profile_dir, cookies):
    """Copy cookies from your browser into this app's Chrome profile, and say who that is."""
    if not cookies:
        raise SignInCancelled(
            "Couldn't find an osu! sign-in in your browser. Sign in to osu! in the tab that "
            "opened, then click “I've signed in”.")
    _hold_profile()
    try:
        driver = make_driver(profile_dir=profile_dir)
        try:
            driver.get(OSU)  # cookies can only be set while on their own domain
            for cookie in cookies:
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass  # one odd cookie shouldn't sink the whole sign-in
            return current_user(driver)
        finally:
            driver.quit()  # a clean quit is what writes the cookies into the profile
    finally:
        PROFILE_LOCK.release()


def open_url(url):
    """Open a page in the user's own default browser."""
    import subprocess
    if os.name == "nt":
        os.startfile(url)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", url],
                         env=_child_env(), start_new_session=True)


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
        self.pending_import = []   # finished maps waiting to go to osu! as one batch
        self.failed_import = []
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
            if self.opts.get("auto_open"):
                self._flush_imports()  # whatever didn't fill a batch
            if self.failed_import:
                self.log("warn", f"{len(self.failed_import)} map(s) downloaded but not imported. "
                                 f"They're still in the download folder: press “Import all into osu!”.")
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
            started = cycle_start = time.time()
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
                    self.pending_import.append(ok)
                    if len(self.pending_import) >= IMPORT_BATCH:
                        self._flush_imports()
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
            # `delay` is a minimum interval between requests, not an extra pause on the
            # end of each one: a download that itself took longer than the delay has
            # already been as gentle as the delay intends, so there's nothing left to wait.
            wait = delay - (time.time() - cycle_start)
            if mirror:
                wait = max(wait, mirror_pace(self.mirror_used) - (time.time() - cycle_start))
            if wait > 0 and not self._sleep(wait):
                return
            elif not self._sleep(0):  # still honour pause/stop when no wait is needed
                return

    def _flush_imports(self):
        """Send everything downloaded since the last batch to osu! in one go."""
        batch, self.pending_import = self.pending_import, []
        if not batch:
            return
        wanted = self.opts.get("import_client", "stable")
        self.log("info", f"Importing {len(batch)} map{'' if len(batch) == 1 else 's'} into osu!{wanted}…")
        used, failed = import_batch(batch, wanted, self.opts.get("songs_dir", ""),
                                    self.opts.get("osu_paths"), log=self.log)
        self.failed_import += failed
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
                                             timeout=timeout, stop=self.stop_flag,
                                             stall=self.opts.get("stall", DEFAULT_STALL))
            if path:
                if who != self.mirror_used:  # say which mirror only when it changes
                    self.mirror_used = who
                    rate = (MIRROR_SPEEDS.get(who) or (0, 0))[0]
                    speed = f" ({rate / 1e6:.1f} MB/s)" if rate else ""
                    self.log("info", f"Downloading from {who}{speed}.")
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

        try:
            driver.get(f"{OSU}/beatmapsets/{sid}")
        except Exception:
            return None, "The beatmap page didn't load in time."
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
        stall = max(1, float(self.opts.get("stall", DEFAULT_STALL)))
        start = time.time()
        moved_at, last_size = start, -1
        while time.time() - start < timeout:
            if self.stop_flag.is_set():
                return None, "Cancelled."
            done = find_osz(self.folder, sid)
            if done:
                return done, ""
            partials = list(self.folder.glob("*.crdownload"))
            if time.time() - start > 1 and not partials:
                # a refused download replaces the page with osu!'s "too many requests" page
                text = (driver.execute_script(
                    "return document.title + ' ' + (document.body ? document.body.innerText.slice(0, 500) : '')") or "").lower()
                if "quota" in text or "too many requests" in text:
                    driver.back()
                    return None, "osu! download quota reached."
            # Chrome writes into a .crdownload as it goes; if that stops growing the
            # download is stuck, and there's no point waiting out the whole timeout
            size = sum(f.stat().st_size for f in partials if f.exists()) if partials else -1
            if size != last_size:
                moved_at, last_size = time.time(), size
            elif time.time() - moved_at > stall:
                return None, f"Download stalled (no progress for {stall:g}s)."
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
    """Environment for programs we launch: undo app.py's temp redirect so osu! uses the real one.

    This matters more than it looks on Linux: osu!lazer ships as an AppImage, and an AppImage
    mounts its own squashfs under TMPDIR. Leave the redirect in place and lazer mounts itself
    inside the app's data/temp, where the next startup then can't clear it.
    """
    env = dict(os.environ)
    for key in ("TEMP", "TMP", "TMPDIR"):
        original = env.pop(f"OBD_ORIGINAL_{key}", None)
        if original is None:
            continue
        if original:
            env[key] = original
        else:
            env.pop(key, None)  # it simply wasn't set before we redirected it
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


IMPORT_BATCH = 15          # .osz files handed to one client invocation
IMPORT_ACK_TIMEOUT = 120   # a running client should answer an import far quicker than this


def _osu_pid():
    """PID of a running osu!lazer, or None. Linux only; elsewhere we can't tell."""
    if not Path("/proc").is_dir():
        return None
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            if Path(entry.path, "comm").read_text().strip() == "osu!":
                return int(entry.name)
        except OSError:
            continue
    return None


def _listen_port(pid):
    """The loopback port a process is listening on, by matching socket inodes to its fds."""
    try:
        fds = {os.readlink(f"/proc/{pid}/fd/{fd}") for fd in os.listdir(f"/proc/{pid}/fd")}
        lines = Path(f"/proc/{pid}/net/tcp").read_text().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        cols = line.split()
        if len(cols) > 9 and cols[3] == "0A" and f"socket:[{cols[9]}]" in fds:  # 0A = LISTEN
            return int(cols[1].split(":")[1], 16)
    return None


def wait_for_osu(timeout=120, stop=None):
    """Wait until a just-launched osu!lazer can accept imports over its IPC socket.

    It binds the socket early in startup, well before the menu appears, so this usually
    returns in well under a second.
    """
    import socket as socketlib
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop is not None and stop.is_set():
            return False
        pid = _osu_pid()
        port = _listen_port(pid) if pid else None
        if port:
            try:
                socketlib.create_connection(("127.0.0.1", port), 2).close()
                return True
            except OSError:
                pass
        time.sleep(0.5)
    return False


def import_batch(paths, client, songs_dir="", custom_paths=None, log=None):
    """Hand several .osz files to osu! at once. Returns (client used, files that failed).

    One osu!lazer invocation per beatmap is a bad deal on Linux: each one mounts the
    AppImage and starts a whole .NET runtime just to pass a filename over IPC, and a running
    lazer only has a few seconds to acknowledge before the sender aborts. Sending a batch
    through a single process is far cheaper and far less likely to time out.
    """
    paths = [str(p) for p in paths]
    if not paths:
        return client, []
    other = "lazer" if client == "stable" else "stable"
    for candidate in (client, other):
        exe = find_osu(candidate, songs_dir, (custom_paths or {}).get(candidate, ""))
        if not exe:
            continue
        native = os.name != "nt" and not str(exe).startswith(FLATPAK_PREFIX) \
            and Path(exe).suffix.lower() != ".exe"
        if not native or _osu_pid() is None:
            # nothing is running yet (or this isn't a shape we can talk to): the launch
            # becomes the game itself, so it must not be waited on
            for path in paths:
                _launch_osu(exe, path)
                time.sleep(0.4)
            if native:
                wait_for_osu()
            return candidate, []
        return candidate, _send_to_running_osu(exe, paths, log)
    open_file(paths[0])
    return "default", []


def _send_to_running_osu(exe, paths, log=None):
    """Pass files to an already-running osu! and report the ones it didn't take."""
    import subprocess
    failed = []
    for attempt in range(2):
        try:
            result = subprocess.run([str(exe), *paths], env=_child_env(),
                                    cwd=str(Path(exe).parent), capture_output=True,
                                    text=True, timeout=IMPORT_ACK_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0:
            return []
        # osu! aborts (and dumps core) when it can't hand a file over in time, which says
        # nothing about the files themselves, so it is worth one more try
        if attempt == 0:
            if log:
                log("info", f"osu! didn't accept {len(paths)} map(s) yet; trying once more.")
            time.sleep(5)
            continue
        failed = list(paths)
        if log:
            detail = (result.stderr or "").strip().splitlines()
            why = next((l for l in detail if "Exception" in l), detail[0] if detail else "no reason given")
            log("warn", f"osu! wouldn't import {len(paths)} map(s): {why[:120]} "
                        f"They're still in the download folder, so “Import all into osu!” can retry.")
    return failed


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
