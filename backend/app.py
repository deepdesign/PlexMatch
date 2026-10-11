"""
PlexMatch - Plex Media Renamer - Flask Backend
----------------------------------------
Scans a folder, matches files against TMDB (movies + TV),
proposes Plex-compliant renames, and executes approved ones.
"""

import os
import re
import sys
import json
import shutil
import requests
import subprocess
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# How many files to match against TMDB at once during a scan. TMDB lookups are
# network-bound, so concurrency dramatically reduces total scan time.
SCAN_CONCURRENCY = 8

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")
RENAME_LOG_FILE = os.path.join(os.path.dirname(__file__), "rename_log.json")

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".flv", ".webm"
}

TMDB_BASE = "https://api.themoviedb.org/3"

# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    return {
        "tmdb_api_key": "",
        "root_folder": "",
        "auto_pick_top": False,
        "restructure_folders": False,
        "clean_empty_folders": False,
        "clean_unmatched": False,
        "web_lookup": True,
        "split_libraries": False,
        "skip_output_folders": False,
        "embed_ids": False,
        "omdb_api_key": "",
        "ai_enabled": False,
        "ai_provider": "",
        "openai_api_key": "",
        "anthropic_api_key": "",
        "ai_model": "",
        "openai_base_url": "",
    }

def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def post_settings():
    data = request.json
    save_settings(data)
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

# Common noise tokens to strip when building a clean search title
NOISE_PATTERN = re.compile(
    r"""
    \b(
        4k|2160p|1080p|1080i|720p|480p|576p|       # resolution
        bluray|blu-ray|bdrip|bdrip|bray|            # source
        webrip|web-rip|web-dl|webdl|amzn|nf|hulu|  # web sources
        hbo|dsnp|atvp|pcok|                         # streaming
        hdtv|pdtv|dvdrip|dvdscr|dvd|               # other sources
        x264|x265|h264|h\.264|h265|h\.265|          # codecs
        xvid|divx|hevc|avc|                         # codecs
        dd5\.1|dd2\.0|aac|ac3|dts|truehd|atmos|    # audio
        dts-hd|dts\.hd|                             # audio
        proper|repack|extended|theatrical|           # release flags
        directors\.cut|unrated|remastered|           # release flags
        bbc|itv|pbs|nhk|natgeo|mvgroup|             # doc networks / release sites
        yify|yts|rarbg|ettv|eztv|               # groups (common)
        [a-z0-9]+-[a-z0-9]+$                        # release group at end
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

YEAR_PATTERN = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")
# A span such as 1985-1999 is part of a title ("Russia 1985-1999: TraumaZone"),
# not the release year. Dots may sit on either side in a scene filename.
YEAR_RANGE_PATTERN = re.compile(
    r"\b(19[5-9]\d|20[0-3]\d)\s*[-–—]\s*(19[5-9]\d|20[0-3]\d)\b"
)

# Episode markers, in priority order. SxxExx is most explicit; "N of M",
# "Part N" / "Episode N", and a bare "E02" are common in documentary rips.
SXXEXX_PATTERN = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,2})")
NOFM_PATTERN = re.compile(r"\b(\d{1,2})\s*of\s*(\d{1,2})\b", re.IGNORECASE)
PART_PATTERN = re.compile(r"\b(?:part|pt|episode|ep)\s*\.?\s*(\d{1,2})\b", re.IGNORECASE)
# "Show-E02-Title" has no season. The E must be its own token so "S01E02" is
# left to the pattern above and words like "Episode" are left to PART_PATTERN.
EXX_PATTERN = re.compile(r"(?<![A-Za-z0-9])[Ee](\d{1,2})(?![A-Za-z0-9])")

# Folder names that carry no useful title info, so they must never be used as a
# search hint (e.g. a single episode inside a "Season 01" folder).
SEASON_FOLDER_PATTERN = re.compile(
    r"^(?:(?:season|series)[\s._-]*\d+|specials|s\d{1,2})$",
    re.IGNORECASE,
)
# Disk, extra, and sorting folders inside a show. They are not a separate program.
DISK_FOLDER_PATTERN = re.compile(
    r"^(?:(?:cd|disc|disk|dvd|part|pt)[\s._-]*\d+|extras?|bonus|featurettes?|"
    r"samples?|subs?|subtitles|unsorted|misc|other|new|tmp|temp|rips?)$",
    re.IGNORECASE,
)

# Library-level folder names that never describe a single title, so they must
# not be used as a search hint for a lone video file inside them.
GENERIC_LIBRARY_FOLDERS = {
    "movies", "films", "tv", "tv shows", "tv series", "series", "shows",
    "documentaries", "docs", "kids", "anime", "downloads", "media", "videos",
}

# "sample" as a whole token in a filename stem -> a throwaway preview clip
SAMPLE_PATTERN = re.compile(r"(?:^|[\s._-])sample(?:[\s._-]|$)", re.IGNORECASE)

# An embedded IMDb rating tag, e.g. "IMDB 8.2" / "imdb8.1"
IMDB_RATING_PATTERN = re.compile(r"\bimdb\b\s*\d+(?:\.\d+)?", re.IGNORECASE)

# Bracketed scene tags, e.g. "[rartv]", "[RARBG]", "(1080p)". A bare year in
# parentheses - "The Matrix (1999)" - is kept so it can be parsed as the year.
BRACKET_TAG_PATTERN = re.compile(r"\[[^\]]*\]|\((?!\s*(?:19|20)\d{2}\s*\))[^)]*\)")
# A trailing scene release group after the final hyphen, e.g. "x264-SHORTBREHD".
# Only stripped from dot-separated scene names so real titles (e.g. "Spider-Man")
# survive once their year/extension have been removed.
SCENE_GROUP_PATTERN = re.compile(r"-[A-Za-z0-9]{2,}$")
# A trailing country/region tag scene releases add to disambiguate a remake,
# e.g. "The.Office.UK" -> "The Office". Only applied to multi-word titles; the
# country is kept as a hint to pick the right show from TMDB's results.
COUNTRY_SUFFIX_PATTERN = re.compile(r"\s+(UK|US|USA|AU|CA|NZ)$", re.IGNORECASE)
# Scene tag -> ISO 3166 code used in TMDB's origin_country
COUNTRY_CODES = {"UK": "GB", "US": "US", "USA": "US", "AU": "AU", "CA": "CA", "NZ": "NZ"}


def strip_scene_group(text: str) -> str:
    """Drop a trailing dot-separated scene release group (e.g. '-SHORTBREHD')."""
    if "." in text:
        return SCENE_GROUP_PATTERN.sub("", text.strip())
    return text


def find_episode_marker(name: str):
    """Return (season, episode, start, end) for the first episode marker, else None."""
    m = SXXEXX_PATTERN.search(name)
    if m:
        return int(m.group(1)), int(m.group(2)), m.start(), m.end()
    m = NOFM_PATTERN.search(name)
    if m:
        return 1, int(m.group(1)), m.start(), m.end()
    m = PART_PATTERN.search(name)
    if m:
        return 1, int(m.group(1)), m.start(), m.end()
    m = EXX_PATTERN.search(name)
    if m:
        return 1, int(m.group(1)), m.start(), m.end()
    return None


def clean_episode_title(text: str) -> str:
    """Tidy the text that follows an episode marker into a usable episode name."""
    text = BRACKET_TAG_PATTERN.sub(" ", text)
    text = strip_scene_group(text)  # drop "-GROUP" before NOISE orphans it
    text = IMDB_RATING_PATTERN.sub("", text)
    text = re.sub(r"\(?\b(19[5-9]\d|20[0-3]\d)\b\)?", "", text)  # stray year
    text = NOISE_PATTERN.sub("", text)
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[\s\-\[\]\(\)]+|[\s\-\[\]\(\);,]+$", "", text).strip()
    return text


def release_year_match(name: str):
    """The first standalone year, skipping years that belong to a title range."""
    ranges = list(YEAR_RANGE_PATTERN.finditer(name))

    def in_range(match) -> bool:
        return any(match.start() >= span.start() and match.end() <= span.end() for span in ranges)

    for match in YEAR_PATTERN.finditer(name):
        if not in_range(match):
            return match
    return None


def parse_filename(raw: str) -> dict:
    """
    Extract title, year, season, episode from a raw filename/folder name.
    Returns a dict with keys: title, year, season, episode, is_tv
    """
    name = raw
    # Strip file extension if present
    name = re.sub(r"\.[a-z0-9]{2,4}$", "", name, flags=re.IGNORECASE)
    # Drop bracketed scene tags (e.g. "[rartv]") so they can't leak into titles
    name = BRACKET_TAG_PATTERN.sub(" ", name)

    # Detect episode info, trying the most explicit form first.
    season = episode = None
    is_tv = False
    marker_start = None
    marker = find_episode_marker(name)
    if marker:
        season, episode, marker_start, _marker_end = marker
        is_tv = True

    # The show title is the text before the episode marker. Anything after it
    # is the episode title already on the file, kept when a lookup has no name.
    episode_title = None
    if marker:
        episode_title = clean_episode_title(name[marker[3]:]) or None
        name = name[:marker_start]

    # Extract a release year. A span such as 1985-1999 stays in the title.
    year_match = release_year_match(name)
    year = int(year_match.group(0)) if year_match else None

    # Remove everything from the year (or resolution/source markers) onward
    if year_match:
        name = name[: year_match.start()]

    # Strip an embedded IMDb rating tag, a trailing scene group, then noise
    name = IMDB_RATING_PATTERN.sub("", name)
    name = strip_scene_group(name)
    name = NOISE_PATTERN.sub("", name)

    # Clean separators: dots, underscores, multiple spaces → single space
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Remove trailing/leading hyphens and brackets
    name = re.sub(r"^[\s\-\[\]\(\)]+|[\s\-\[\]\(\)]+$", "", name).strip()
    # Drop a trailing country tag scene releases add (e.g. "The Office UK")
    country = None
    if len(name.split()) >= 2:
        cm = COUNTRY_SUFFIX_PATTERN.search(name)
        if cm:
            country = COUNTRY_CODES[cm.group(1).upper()]
            name = name[: cm.start()].strip()

    return {
        "title": name,
        "year": year,
        "season": season,
        "episode": episode,
        "is_tv": is_tv,
        "country": country,
        "episode_title": episode_title,
    }


_PLACEHOLDER_EPISODE = re.compile(r"^(?:episode|ep)\s*\.?\s*#?\d{1,3}$", re.IGNORECASE)


def useful_episode_title(name: str | None) -> str | None:
    """A real episode title. Placeholders such as 'Episode 1' do not count."""
    text = (name or "").strip()
    if not text or text.upper() in {"N/A", "TBA", "TBD"}:
        return None
    if _PLACEHOLDER_EPISODE.match(text):
        return None
    return text


def choose_episode_name(remote: str | None, local: str | None) -> str | None:
    """Prefer the database title, and keep the filename title when that is missing."""
    return useful_episode_title(remote) or useful_episode_title(local)


# ---------------------------------------------------------------------------
# TMDB lookups
# ---------------------------------------------------------------------------

# Cached so repeated titles (e.g. many episodes of one series) aren't looked up
# twice. lru_cache is thread-safe, which matters for the concurrent scan.
@lru_cache(maxsize=2048)
def tmdb_search_movie(title: str, year: int | None, api_key: str) -> tuple:
    params = {"api_key": api_key, "query": title, "include_adult": False}
    if year:
        params["year"] = year
    r = requests.get(f"{TMDB_BASE}/search/movie", params=params, timeout=10)
    if r.status_code != 200:
        return ()
    return tuple(r.json().get("results", []))


@lru_cache(maxsize=2048)
def tmdb_search_tv(title: str, year: int | None, api_key: str) -> tuple:
    params = {"api_key": api_key, "query": title, "include_adult": False}
    if year:
        params["first_air_date_year"] = year
    r = requests.get(f"{TMDB_BASE}/search/tv", params=params, timeout=10)
    if r.status_code != 200:
        return ()
    return tuple(r.json().get("results", []))


def tmdb_episode_record(tv_id: int, season: int, episode: int, api_key: str):
    """Episode payload, None when that show has no such episode, False on any other failure."""
    r = requests.get(
        f"{TMDB_BASE}/tv/{tv_id}/season/{season}/episode/{episode}",
        params={"api_key": api_key},
        timeout=10,
    )
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        return False
    return r.json() or {}


def tmdb_episode_name(tv_id: int, season: int, episode: int, api_key: str) -> str | None:
    record = tmdb_episode_record(tv_id, season, episode, api_key)
    if not record:
        return None
    return record.get("name")


def related_show_title(left: str, right: str) -> bool:
    """True when one title is the other plus extra words, as with QI and QI XL."""
    a = re.sub(r"\s+", " ", (left or "").lower()).strip()
    b = re.sub(r"\s+", " ", (right or "").lower()).strip()
    if not a or not b or a == b:
        return False
    return a.startswith(b + " ") or b.startswith(a + " ")


@lru_cache(maxsize=512)
def tmdb_named_seasons(tv_id: int, api_key: str) -> tuple:
    """(season number, season name) pairs, such as (7, 'Series L')."""
    details = tmdb_get_details("tv", tv_id, api_key) or {}
    rows = []
    for season in details.get("seasons") or []:
        number = season.get("season_number")
        name = (season.get("name") or "").strip()
        if number is None or not name:
            continue
        rows.append((int(number), name))
    return tuple(rows)


def _generic_season_name(name: str) -> bool:
    """'Season 7' carries no series identity. 'Series L' does."""
    return bool(re.match(r"^(season|series)\s*\d+$", name.strip(), re.IGNORECASE))


def borrow_episode_title(show_id: int, show_title: str, season: int, episode: int, api_key: str) -> str | None:
    """Episode title from a related show whose season has the same name.

    Used for any series, not one title. QI XL season 7 is named Series L and
    only lists two episodes; QI's Series L has the rest. The file stays on the
    show it matched. Only the missing title is taken.
    """
    season_name = ""
    for number, name in tmdb_named_seasons(show_id, api_key):
        if number == season:
            season_name = name
            break
    if not season_name or _generic_season_name(season_name):
        return None
    for item in tmdb_search_tv(show_title, None, api_key):
        other_id = item.get("id")
        other_name = item.get("name") or ""
        if not other_id or other_id == show_id or not related_show_title(show_title, other_name):
            continue
        for number, name in tmdb_named_seasons(other_id, api_key):
            if name.lower() != season_name.lower():
                continue
            record = tmdb_episode_record(other_id, number, episode, api_key)
            if isinstance(record, dict):
                title = useful_episode_title(record.get("name"))
                if title:
                    return title
    return None


@lru_cache(maxsize=2048)
def tmdb_imdb_id(tv_id: int, api_key: str) -> str | None:
    r = requests.get(
        f"{TMDB_BASE}/tv/{tv_id}/external_ids",
        params={"api_key": api_key},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    imdb_id = (r.json().get("imdb_id") or "").strip()
    return imdb_id or None


def resolve_episode_title(show_id: int, show_title: str, season: int, episode: int,
                          api_key: str, omdb_key: str = "") -> str | None:
    """Best episode title for any series. Stops at the first real name.

    Order: this show on TMDB, a related show's season of the same name, then
    OMDb for this show's IMDb id. A placeholder such as "Episode 2" is skipped.
    The name already on the file is applied later, if all of these are empty.
    """
    record = tmdb_episode_record(show_id, season, episode, api_key)
    if record is False:
        return None
    if isinstance(record, dict):
        title = useful_episode_title(record.get("name"))
        if title:
            return title
    borrowed = borrow_episode_title(show_id, show_title, season, episode, api_key)
    if borrowed:
        return borrowed
    if omdb_key:
        imdb_id = tmdb_imdb_id(show_id, api_key)
        if imdb_id:
            return useful_episode_title(omdb_episode_title(imdb_id, season, episode, omdb_key))
    return None


def tmdb_get_details(media_type: str, tmdb_id: int, api_key: str) -> dict | None:
    """Fetch a single movie/TV record by id (used for manual re-selection)."""
    path = "movie" if media_type == "movie" else "tv"
    r = requests.get(
        f"{TMDB_BASE}/{path}/{tmdb_id}",
        params={"api_key": api_key},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    return r.json()


def build_match_from_details(details: dict, media_type: str, parsed: dict, api_key: str,
                             confidence: float = 1.0, omdb_key: str = "") -> dict:
    """Build a match dict from a specific TMDB record (a user-chosen title)."""
    matched_title = details.get("title") or details.get("name") or ""
    raw_date = details.get("release_date") or details.get("first_air_date") or ""
    matched_year = raw_date[:4] if raw_date else str(parsed.get("year") or "")
    tmdb_id = details.get("id")

    episode_name = None
    if media_type == "tv" and parsed.get("season") and parsed.get("episode") and tmdb_id:
        episode_name = resolve_episode_title(
            tmdb_id, matched_title, parsed["season"], parsed["episode"], api_key, omdb_key,
        )

    return {
        "matched": True,
        "confidence": confidence,
        "type": media_type,
        "tmdb_id": tmdb_id,
        "matched_title": matched_title,
        "matched_year": matched_year,
        "episode_name": episode_name,
        "season": parsed.get("season"),
        "episode": parsed.get("episode"),
        "tmdb_url": f"https://www.themoviedb.org/{'tv' if media_type == 'tv' else 'movie'}/{tmdb_id}",
    }


def score_result(result: dict, parsed_title: str, parsed_year: int | None, is_tv: bool,
                 country: str | None = None) -> float:
    """
    Simple confidence score 0-1 based on title similarity and year match,
    nudged by country of origin when the filename carried a tag like "UK".
    """
    from difflib import SequenceMatcher

    candidate_title = result.get("title") or result.get("name") or ""
    candidate_year_str = (
        result.get("release_date") or result.get("first_air_date") or ""
    )[:4]
    candidate_year = int(candidate_year_str) if candidate_year_str.isdigit() else None

    title_score = SequenceMatcher(
        None, parsed_title.lower(), candidate_title.lower()
    ).ratio()

    year_score = 1.0
    if parsed_year and candidate_year:
        diff = abs(parsed_year - candidate_year)
        year_score = 1.0 if diff == 0 else (0.7 if diff == 1 else 0.3)
    elif parsed_year and not candidate_year:
        year_score = 0.8

    score = title_score * 0.7 + year_score * 0.3

    origins = result.get("origin_country") or []
    if country and origins:
        score += 0.1 if country in origins else -0.15

    return round(max(0.0, min(1.0, score)), 3)


def find_best_match(parsed: dict, api_key: str, omdb_key: str = "") -> dict:
    """
    Search TMDB (movies + TV). Return best match with confidence score.
    """
    title = parsed["title"]
    year = parsed["year"]
    is_tv = parsed["is_tv"]

    candidates = []

    # Episode-numbered files are a series. Do not let a movie with a similar
    # year (Russian Roulette, 1985) outrank the show.
    if not is_tv:
        for r in tmdb_search_movie(title, year, api_key):
            candidates.append({"result": r, "type": "movie"})

    for r in tmdb_search_tv(title, year, api_key):
        candidates.append({"result": r, "type": "tv"})

    if is_tv:
        candidates = [c for c in candidates if c["type"] != "movie"]

    if not candidates:
        return {"matched": False, "confidence": 0}

    scored = []
    for c in candidates:
        score = score_result(c["result"], title, year, is_tv, parsed.get("country"))
        scored.append({**c, "confidence": score})

    scored.sort(key=lambda x: x["confidence"], reverse=True)
    best = scored[0]

    r = best["result"]
    media_type = best["type"]
    confidence = best["confidence"]

    matched_title = r.get("title") or r.get("name") or ""
    raw_date = r.get("release_date") or r.get("first_air_date") or ""
    matched_year = raw_date[:4] if raw_date else str(year or "")
    tmdb_id = r.get("id")

    # Every series: this show, then a related show with the same season name, then OMDb.
    episode_name = None
    needs_review = False
    if media_type == "tv" and parsed["season"] and parsed["episode"] and tmdb_id:
        episode_name = resolve_episode_title(
            tmdb_id, matched_title or title, parsed["season"], parsed["episode"], api_key, omdb_key,
        )

    return {
        "matched": True,
        "confidence": confidence,
        "type": media_type,
        "tmdb_id": tmdb_id,
        "matched_title": matched_title,
        "matched_year": matched_year,
        "episode_name": episode_name,
        "needs_review": needs_review,
        "season": parsed.get("season"),
        "episode": parsed.get("episode"),
        "tmdb_url": f"https://www.themoviedb.org/{'tv' if media_type == 'tv' else 'movie'}/{tmdb_id}",
        "alternatives": [
            {
                "title": s["result"].get("title") or s["result"].get("name"),
                "year": (s["result"].get("release_date") or s["result"].get("first_air_date") or "")[:4],
                "type": s["type"],
                "tmdb_id": s["result"].get("id"),
                "confidence": s["confidence"],
            }
            for s in scored[1:4]  # up to 3 alternatives
        ],
    }


def needs_manual_pick(match: dict, auto_pick: bool) -> bool:
    """Auto-pick checks a match only when its confidence is over 90%.

    With auto-pick off, a weak or close call still waits for review.
    """
    if not match.get("matched"):
        return False
    confidence = match.get("confidence") or 0
    if auto_pick:
        # Same rounding the review screen uses, so "91%" is checked and "90%" is not.
        shown = int(confidence * 100 + 0.5)
        return shown <= 90
    if confidence < 0.8:
        return True
    for alt in match.get("alternatives") or []:
        alt_confidence = alt.get("confidence") or 0
        if alt_confidence >= 0.5 and alt_confidence >= confidence - 0.1:
            return True
    return False


# ---------------------------------------------------------------------------
# Wikipedia fallback (free, no API key) - used when TMDB has no confident match
# ---------------------------------------------------------------------------

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_HEADERS = {"User-Agent": "PlexMatch/1.1 (local Plex media renamer)"}


@lru_cache(maxsize=512)
def wiki_search(query: str) -> tuple:
    """Return ((page_title, snippet), ...) candidates for a query, best first."""
    try:
        r = requests.get(WIKI_API, params={
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": 5, "srnamespace": 0, "format": "json",
        }, headers=WIKI_HEADERS, timeout=10)
        if r.status_code != 200:
            return ()
        hits = r.json().get("query", {}).get("search", [])
        return tuple((h["title"], h.get("snippet", "")) for h in hits)
    except requests.RequestException:
        return ()


@lru_cache(maxsize=512)
def wiki_wikitext(page_title: str) -> str:
    """Fetch the raw wikitext of a page (following redirects)."""
    try:
        r = requests.get(WIKI_API, params={
            "action": "parse", "page": page_title, "prop": "wikitext",
            "redirects": 1, "format": "json",
        }, headers=WIKI_HEADERS, timeout=10)
        if r.status_code != 200:
            return ""
        return r.json().get("parse", {}).get("wikitext", {}).get("*", "")
    except requests.RequestException:
        return ""


def wiki_canonical_title(page_title: str) -> str:
    """Drop a trailing disambiguation qualifier, e.g. 'Cosmos (1980 TV series)'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", page_title).strip()


def _clean_wiki_markup(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", s)  # [[A|B]]->B, [[A]]->A
    s = re.sub(r"\{\{.*?\}\}", "", s)                          # drop templates
    s = s.replace("''", "").replace('"', "").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def wiki_year(wikitext: str) -> int | None:
    m = re.search(r"\{\{\s*[Ss]tart date\s*\|\s*(\d{4})", wikitext)
    if m:
        return int(m.group(1))
    m = re.search(
        r"\|\s*(?:first_aired|released|air_date|first_run|date)\s*=\s*[^\n]*?(19[5-9]\d|20[0-3]\d)",
        wikitext,
    )
    if m:
        return int(m.group(1))
    return None


def wiki_episode_title(wikitext: str, episode: int) -> str | None:
    """Best-effort: pull a title from {{Episode list}} entries by episode number."""
    for chunk in re.split(r"\{\{\s*[Ee]pisode list", wikitext)[1:]:
        chunk = chunk[:1500]  # an entry is short; avoid bleeding into the next
        num = re.search(r"EpisodeNumber\s*=\s*([0-9]+)", chunk)
        title = re.search(r"\bTitle\s*=\s*([^\n|]+)", chunk)
        if num and title and int(num.group(1)) == episode:
            cleaned = _clean_wiki_markup(title.group(1))
            if cleaned:
                return cleaned
    return None


def wiki_lookup(title: str, year: int | None, season, episode) -> dict | None:
    """Find a confident Wikipedia page for `title`; return canonical title/year
    (and episode name when possible). None if nothing matches well enough."""
    from difflib import SequenceMatcher

    results = wiki_search(title)
    if not results:
        return None

    best_page, best_score = None, 0.0
    for page_title, _snippet in results:
        cand = wiki_canonical_title(page_title)
        score = SequenceMatcher(None, title.lower(), cand.lower()).ratio()
        if score > best_score:
            best_score, best_page = score, page_title

    if not best_page or best_score < 0.6:
        return None

    wikitext = wiki_wikitext(best_page)
    ep_title = wiki_episode_title(wikitext, episode) if (season and episode) else None

    return {
        "title": wiki_canonical_title(best_page),
        "year": wiki_year(wikitext) or year,
        "episode_name": ep_title,
        "wiki_url": "https://en.wikipedia.org/wiki/" + best_page.replace(" ", "_"),
        "confidence": round(best_score, 3),
    }


# ---------------------------------------------------------------------------
# OMDb / IMDb fallback - free key (1000/day). Runs after TMDB; its main wins are
# an IMDb ID (for the {imdb-...} folder tag) and coverage of obscure titles.
# ---------------------------------------------------------------------------

OMDB_BASE = "https://www.omdbapi.com/"


@lru_cache(maxsize=2048)
def omdb_by_title(title: str, year, media_type, api_key: str) -> dict | None:
    """Look up a single best record by title (and optional year/type)."""
    params = {"apikey": api_key, "t": title}
    if year:
        params["y"] = year
    if media_type:
        params["type"] = media_type
    try:
        r = requests.get(OMDB_BASE, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        return data if data.get("Response") == "True" else None
    except requests.RequestException:
        return None


@lru_cache(maxsize=2048)
def omdb_episode_title(imdb_id: str, season, episode, api_key: str) -> str | None:
    """Episode name for a series IMDb id + season/episode."""
    try:
        r = requests.get(OMDB_BASE, params={
            "apikey": api_key, "i": imdb_id, "Season": season, "Episode": episode,
        }, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("Response") == "True":
            name = data.get("Title")
            return name if name and name != "N/A" else None
    except requests.RequestException:
        return None
    return None


def omdb_lookup(parsed: dict, api_key: str) -> dict | None:
    """
    Search OMDb for `parsed`. Returns a match dict in the same shape as
    find_best_match (with imdb_id/imdb_url, source='omdb') or None.
    """
    from difflib import SequenceMatcher

    if not api_key:
        return None
    title = parsed["title"]
    year = parsed["year"]
    is_tv = parsed["is_tv"]

    # A file with an episode number is a series. Do not fall back to a movie.
    type_order = (["series"] if is_tv else ["movie", "series", None])
    data = None
    for mt in type_order:
        data = omdb_by_title(title, year, mt, api_key)
        if data:
            break
    if not data:
        return None
    if is_tv and (data.get("Type") or "").lower() != "series":
        return None

    cand_title = data.get("Title") or ""
    cand_year_str = (data.get("Year") or "")[:4]
    cand_year = int(cand_year_str) if cand_year_str.isdigit() else None

    title_score = SequenceMatcher(None, title.lower(), cand_title.lower()).ratio()
    year_score = 1.0
    if year and cand_year:
        year_score = 1.0 if year == cand_year else (0.7 if abs(year - cand_year) == 1 else 0.3)
    elif year and not cand_year:
        year_score = 0.8
    confidence = round(title_score * 0.7 + year_score * 0.3, 3)
    if confidence < 0.5:
        return None

    media_type = "tv" if data.get("Type") == "series" else "movie"
    imdb_id = data.get("imdbID")

    episode_name = None
    if media_type == "tv" and parsed.get("season") and parsed.get("episode") and imdb_id:
        episode_name = omdb_episode_title(imdb_id, parsed["season"], parsed["episode"], api_key)

    return {
        "matched": True,
        "confidence": confidence,
        "type": media_type,
        "tmdb_id": None,
        "imdb_id": imdb_id,
        "matched_title": cand_title,
        "matched_year": cand_year_str,
        "episode_name": episode_name,
        "season": parsed.get("season"),
        "episode": parsed.get("episode"),
        "source": "omdb",
        "imdb_url": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None,
    }


_IMDB_ID = re.compile(r"\b(tt\d{5,})\b", re.IGNORECASE)
_TMDB_URL = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)", re.IGNORECASE)
_TMDB_SEASON = re.compile(r"/season/(\d+)", re.IGNORECASE)
_TMDB_EPISODE = re.compile(r"/episode/(\d+)", re.IGNORECASE)


def parse_media_link(text: str) -> dict | None:
    """A TMDB movie/tv URL, or any text that contains an IMDb title id."""
    text = (text or "").strip()
    if not text:
        return None
    tmdb = _TMDB_URL.search(text)
    if tmdb:
        ref = {
            "source": "tmdb",
            "media_type": "tv" if tmdb.group(1).lower() == "tv" else "movie",
            "tmdb_id": int(tmdb.group(2)),
        }
        season = _TMDB_SEASON.search(text)
        episode = _TMDB_EPISODE.search(text)
        if season:
            ref["season"] = int(season.group(1))
        if episode:
            ref["episode"] = int(episode.group(1))
        return ref
    imdb = _IMDB_ID.search(text)
    if imdb:
        return {"source": "imdb", "imdb_id": imdb.group(1).lower()}
    return None


def tmdb_find_imdb(imdb_id: str, api_key: str) -> dict | None:
    """TMDB's cross-reference for an IMDb id. IMDb itself has no public API."""
    try:
        r = requests.get(
            f"{TMDB_BASE}/find/{imdb_id}",
            params={"api_key": api_key, "external_source": "imdb_id"},
            timeout=10,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return r.json()


def omdb_by_imdb_id(imdb_id: str, api_key: str, parsed: dict) -> dict | None:
    """A title the user picked by IMDb id. Confidence is 1 because they chose it."""
    try:
        r = requests.get(OMDB_BASE, params={"apikey": api_key, "i": imdb_id}, timeout=10)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    if data.get("Response") != "True":
        return None
    if (data.get("Type") or "").lower() == "episode":
        return None
    media_type = "tv" if (data.get("Type") or "").lower() == "series" else "movie"
    year = (data.get("Year") or "")[:4]
    episode_name = None
    if media_type == "tv" and parsed.get("season") and parsed.get("episode"):
        episode_name = omdb_episode_title(imdb_id, parsed["season"], parsed["episode"], api_key)
    return {
        "matched": True,
        "confidence": 1,
        "type": media_type,
        "tmdb_id": None,
        "imdb_id": data.get("imdbID") or imdb_id,
        "matched_title": data.get("Title") or "",
        "matched_year": year if year.isdigit() else "",
        "episode_name": episode_name,
        "season": parsed.get("season"),
        "episode": parsed.get("episode"),
        "source": "omdb",
        "imdb_url": f"https://www.imdb.com/title/{imdb_id}/",
    }


def match_from_link(text: str, parsed: dict, api_key: str, omdb_key: str = "",
                    keep_numbers: bool = False) -> tuple[dict | None, dict, str | None]:
    """Turn a pasted TMDB or IMDb link into a match.

    A link to one episode can set that file's season and episode. When
    keep_numbers is set, the link only chooses the show; each file keeps the
    season and episode already on it.
    """
    ref = parse_media_link(text)
    if not ref:
        return None, parsed, "Paste a TMDB or IMDb title link."
    parsed = dict(parsed)
    if keep_numbers:
        parsed["is_tv"] = True
    else:
        if ref.get("season"):
            parsed["season"] = ref["season"]
        if ref.get("episode"):
            parsed["episode"] = ref["episode"]
            parsed["is_tv"] = True

    if ref["source"] == "tmdb":
        if keep_numbers and ref["media_type"] != "tv":
            return None, parsed, "Paste a TV series link."
        if ref["media_type"] == "tv":
            parsed["is_tv"] = True
        details = tmdb_get_details(ref["media_type"], ref["tmdb_id"], api_key)
        if not details:
            return None, parsed, "Could not load that TMDB title."
        return build_match_from_details(
            details, ref["media_type"], parsed, api_key, 1.0, omdb_key,
        ), parsed, None

    found = tmdb_find_imdb(ref["imdb_id"], api_key) or {}
    movies = found.get("movie_results") or []
    shows = found.get("tv_results") or []
    episodes = found.get("tv_episode_results") or []
    prefer_tv = bool(keep_numbers or parsed.get("is_tv") or parsed.get("episode"))
    imdb_url = f"https://www.imdb.com/title/{ref['imdb_id']}/"

    if episodes and (prefer_tv or not movies):
        ep = episodes[0]
        show_id = ep.get("show_id")
        if not keep_numbers:
            if ep.get("season_number"):
                parsed["season"] = ep["season_number"]
            if ep.get("episode_number"):
                parsed["episode"] = ep["episode_number"]
        parsed["is_tv"] = True
        if show_id:
            details = tmdb_get_details("tv", show_id, api_key)
            if details:
                match = build_match_from_details(details, "tv", parsed, api_key, 1.0, omdb_key)
                match["imdb_id"] = ref["imdb_id"]
                match["imdb_url"] = imdb_url
                if not keep_numbers and ep.get("name"):
                    match["episode_name"] = ep["name"]
                return match, parsed, None

    hit = kind = None
    if prefer_tv and shows:
        hit, kind = shows[0], "tv"
    elif movies and not keep_numbers:
        hit, kind = movies[0], "movie"
    elif shows:
        hit, kind = shows[0], "tv"
    if keep_numbers and kind != "tv":
        return None, parsed, "Paste a TV series link."
    if hit:
        if kind == "tv":
            parsed["is_tv"] = True
        details = tmdb_get_details(kind, hit["id"], api_key)
        if not details:
            return None, parsed, "Could not load that title from TMDB."
        match = build_match_from_details(details, kind, parsed, api_key, 1.0, omdb_key)
        match["imdb_id"] = ref["imdb_id"]
        match["imdb_url"] = imdb_url
        return match, parsed, None

    if omdb_key:
        omdb = omdb_by_imdb_id(ref["imdb_id"], omdb_key, parsed)
        if omdb:
            if keep_numbers and omdb.get("type") != "tv":
                return None, parsed, "Paste a TV series link."
            return omdb, parsed, None
    return None, parsed, "No TMDB or OMDb title for that IMDb id."


# ---------------------------------------------------------------------------
# AI fallback (OpenAI or Anthropic) - user-supplied API keys, local only
# ---------------------------------------------------------------------------

AI_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-20241022",
}

AI_CONSOLE_URLS = {
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
}

AI_SYSTEM_PROMPT = """You help organize video files (movies, TV series, documentaries) for Plex media servers.
Given folder context, filename, and parsed hints, respond with ONLY a JSON object (no markdown).
Required keys:
  canonical_title (string),
  year (integer or null),
  media_type ("movie" or "tv"),
  season (integer or null),
  episode (integer or null),
  episode_title (string or null),
  confidence (number 0-1),
  reason (one short sentence explaining your choice).
Rules:
- Episodic content (SxxExx, NofM, Part N, Episode N) is usually media_type "tv".
- Standalone feature films, including feature documentaries, are usually "movie".
- Strip release groups, codecs, resolution, [bracket tags], and scene country tags (UK/US) unless part of the official title.
- Use the official English title as listed on TMDB or IMDb."""


_ai_result_cache: dict[str, dict | None] = {}
_AI_CACHE_MAX = 512


def _ai_cache_key(provider: str, model: str, rel_path: str, filename: str, parsed: dict) -> str:
    payload = {
        "provider": provider,
        "model": model,
        "rel_path": rel_path,
        "filename": filename,
        "parsed": {k: parsed.get(k) for k in ("title", "year", "season", "episode", "is_tv")},
    }
    return json.dumps(payload, sort_keys=True)


def parse_ai_settings(data: dict) -> dict:
    """Extract AI options from a scan/rematch request body."""
    provider = (data.get("ai_provider") or "").strip().lower()
    if provider not in ("openai", "anthropic"):
        provider = ""
    return {
        "enabled": bool(data.get("ai_enabled")) and bool(provider),
        "provider": provider,
        "openai_api_key": (data.get("openai_api_key") or "").strip(),
        "anthropic_api_key": (data.get("anthropic_api_key") or "").strip(),
        "ai_model": (data.get("ai_model") or "").strip(),
        "openai_base_url": (data.get("openai_base_url") or "").strip().rstrip("/"),
    }


def _ai_api_key(ai_cfg: dict) -> str | None:
    if ai_cfg["provider"] == "openai":
        return ai_cfg["openai_api_key"] or None
    if ai_cfg["provider"] == "anthropic":
        return ai_cfg["anthropic_api_key"] or None
    return None


def _extract_json_object(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _call_openai_chat(api_key: str, model: str, user_prompt: str, base_url: str) -> str:
    root = base_url or "https://api.openai.com/v1"
    url = f"{root}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI API error ({r.status_code}): {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"]


def _call_anthropic_chat(api_key: str, model: str, user_prompt: str) -> str:
    url = "https://api.anthropic.com/v1/messages"
    body = {
        "model": model,
        "max_tokens": 1024,
        "system": AI_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    r = requests.post(
        url,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Anthropic API error ({r.status_code}): {r.text[:200]}")
    blocks = r.json().get("content") or []
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "".join(parts)


def ai_infer_media(f: dict, parsed: dict, prior_match: dict | None, ai_cfg: dict,
                   harder: bool = False, siblings: list | None = None) -> dict | None:
    """
    Ask the configured AI provider to infer canonical Plex metadata.
    Results are cached per file + parsed hints (no API key in cache key).
    A harder retry skips that cache and is told the current match was rejected.
    """
    if not ai_cfg.get("enabled"):
        return None
    api_key = _ai_api_key(ai_cfg)
    if not api_key:
        return None

    provider = ai_cfg["provider"]
    model = ai_cfg["ai_model"] or AI_DEFAULT_MODELS[provider]
    cache_key = _ai_cache_key(provider, model, f.get("rel_path", ""), f.get("filename", ""), parsed)
    if not harder and cache_key in _ai_result_cache:
        return _ai_result_cache[cache_key]

    alts = (prior_match or {}).get("alternatives") or []
    alt_lines = [
        f"- {a.get('title')} ({a.get('year')}) [{a.get('type')}] score={a.get('confidence')}"
        for a in alts[:3]
    ]
    prior_title = (prior_match or {}).get("matched_title")
    prior_conf = (prior_match or {}).get("confidence")
    prior_id = (prior_match or {}).get("tmdb_id")
    sibling_lines = [f"- {name}" for name in (siblings or []) if name]

    reject = ""
    if harder:
        reject = f"""
This match was rejected. Do not return it:
  rejected_title: {prior_title}
  rejected_tmdb_id: {prior_id}
Other files in the same folder:
{chr(10).join(sibling_lines) if sibling_lines else '  (none)'}
If any filename contains an episode number, media_type must be "tv"."""

    user_prompt = f"""File context:
  relative_path: {f.get('rel_path', '')}
  folder: {f.get('folder', '')}
  filename: {f.get('filename', '')}
Parsed hints:
  title: {parsed.get('title')}
  year: {parsed.get('year')}
  season: {parsed.get('season')}
  episode: {parsed.get('episode')}
  is_tv: {parsed.get('is_tv')}
Prior TMDB attempt:
  matched_title: {prior_title}
  confidence: {prior_conf}
TMDB alternatives:
{chr(10).join(alt_lines) if alt_lines else '  (none)'}{reject}"""

    try:
        if provider == "openai":
            raw = _call_openai_chat(api_key, model, user_prompt, ai_cfg["openai_base_url"])
        else:
            raw = _call_anthropic_chat(api_key, model, user_prompt)
        data = _extract_json_object(raw)
    except (json.JSONDecodeError, KeyError, RuntimeError, requests.RequestException):
        if not harder:
            _ai_result_cache[cache_key] = None
            if len(_ai_result_cache) > _AI_CACHE_MAX:
                _ai_result_cache.clear()
        return None

    media_type = (data.get("media_type") or "movie").strip().lower()
    if parsed.get("is_tv"):
        media_type = "tv"
    elif media_type not in ("movie", "tv"):
        media_type = "movie"

    year = data.get("year")
    if year is not None and not isinstance(year, int):
        try:
            year = int(str(year)[:4])
        except (TypeError, ValueError):
            year = parsed.get("year")

    confidence = data.get("confidence", 0.5)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    result = {
        "canonical_title": (data.get("canonical_title") or parsed.get("title") or "").strip(),
        "year": year,
        "media_type": media_type,
        "season": data.get("season") if data.get("season") is not None else parsed.get("season"),
        "episode": data.get("episode") if data.get("episode") is not None else parsed.get("episode"),
        "episode_title": (data.get("episode_title") or "").strip() or None,
        "confidence": round(confidence, 3),
        "reason": (data.get("reason") or "").strip(),
    }
    if not result["canonical_title"]:
        result = None

    if not harder:
        _ai_result_cache[cache_key] = result
        if len(_ai_result_cache) > _AI_CACHE_MAX:
            _ai_result_cache.clear()
    return result


def _same_rejected(match: dict | None, prior: dict | None) -> bool:
    """True when a new match is the title or TMDB id the user already rejected."""
    if not match or not match.get("matched") or not prior:
        return False
    rejected_id = prior.get("tmdb_id")
    if rejected_id and str(match.get("tmdb_id") or "") == str(rejected_id):
        return True
    rejected = (prior.get("matched_title") or "").strip().casefold()
    title = (match.get("matched_title") or "").strip().casefold()
    return bool(rejected) and title == rejected


def apply_ai_match(f: dict, parsed: dict, prior_match: dict, tmdb_api_key: str, ai_cfg: dict,
                   force: bool = False, harder: bool = False, siblings: list | None = None) -> dict | None:
    """
    Use AI to recover weak matches. Optionally re-query TMDB with the AI title.
    Returns a match dict or None. A harder retry will not repeat the rejected match.
    """
    weak = force or not prior_match.get("matched") or prior_match.get("confidence", 0) < 0.5
    if not weak or not ai_cfg.get("enabled"):
        return None

    inference = ai_infer_media(f, parsed, prior_match, ai_cfg, harder=harder, siblings=siblings)
    if not inference or inference["confidence"] < 0.55:
        return None

    is_tv = True if parsed.get("is_tv") else inference["media_type"] == "tv"
    search_parsed = {
        "title": inference["canonical_title"],
        "year": inference.get("year") or parsed.get("year"),
        "season": inference.get("season") or parsed.get("season"),
        "episode": inference.get("episode") or parsed.get("episode"),
        "is_tv": is_tv,
    }

    try:
        tmdb_match = find_best_match(search_parsed, tmdb_api_key)
        if tmdb_match.get("matched") and tmdb_match.get("confidence", 0) >= 0.4:
            if harder and _same_rejected(tmdb_match, prior_match):
                return None
            tmdb_match["source"] = "ai+tmdb"
            tmdb_match["ai_reason"] = inference.get("reason")
            if inference.get("episode_title") and not tmdb_match.get("episode_name"):
                tmdb_match["episode_name"] = inference["episode_title"]
            return tmdb_match
    except Exception:
        pass

    fallback = {
        "matched": True,
        "confidence": inference["confidence"],
        "type": "tv" if is_tv else inference["media_type"],
        "tmdb_id": None,
        "imdb_id": None,
        "matched_title": inference["canonical_title"],
        "matched_year": str(inference["year"]) if inference.get("year") else "",
        "episode_name": inference.get("episode_title"),
        "season": search_parsed.get("season"),
        "episode": search_parsed.get("episode"),
        "source": "ai",
        "ai_reason": inference.get("reason"),
    }
    if harder and _same_rejected(fallback, prior_match):
        return None
    return fallback


# ---------------------------------------------------------------------------
# Plex naming
# ---------------------------------------------------------------------------

def sanitise_filename(name: str) -> str:
    """Remove characters not safe for Windows/Mac/Linux filenames."""
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


# Subfolder names used when splitting films/series into separate Plex libraries
MOVIES_DIR = "Movies"
TV_DIR = "TV"


def build_plex_names(match: dict, original_ext: str,
                     split: bool = False, embed_ids: bool = False) -> dict:
    """
    Returns proposed folder path (relative to root) and filename.
    When `split`, films go under "Movies/" and series under "TV/" so each can
    be served by a separate Plex library. When
    `embed_ids`, a {tmdb-...} tag is appended to the folder for exact matching.
    """
    title = sanitise_filename(match["matched_title"])
    year = match["matched_year"]
    media_type = match["type"]

    # Plex prefers "Title (Year)" but a year isn't always known (local cleanups)
    title_year = f"{title} ({year})" if year else title

    # Optional {tmdb-id}/{imdb-id} tag on the show/movie folder for exact matching
    id_tag = ""
    if embed_ids:
        if match.get("tmdb_id"):
            id_tag = f" {{tmdb-{match['tmdb_id']}}}"
        elif match.get("imdb_id"):
            id_tag = f" {{imdb-{match['imdb_id']}}}"
    show_folder = title_year + id_tag

    if media_type == "movie":
        filename = f"{title_year}{original_ext}"
        folder = os.path.join(MOVIES_DIR, show_folder) if split else show_folder
    else:
        season = match["season"] or 1
        episode = match["episode"] or 1
        ep_title = sanitise_filename(match["episode_name"]) if match["episode_name"] else ""
        season_folder = f"Season {season:02d}"
        if match.get("folder_locked") and not match.get("season") and not match.get("episode"):
            loose = sanitise_filename(match.get("loose_name") or "")
            if loose and loose.lower() != title.lower():
                filename = f"{title_year} - {loose}{original_ext}"
            else:
                filename = f"{title_year}{original_ext}"
            folder = os.path.join(TV_DIR, show_folder) if split else show_folder
            return {"folder": folder, "filename": filename}
        if ep_title:
            filename = f"{title_year} - S{season:02d}E{episode:02d} - {ep_title}{original_ext}"
        else:
            filename = f"{title_year} - S{season:02d}E{episode:02d}{original_ext}"
        base = os.path.join(show_folder, season_folder)
        folder = os.path.join(TV_DIR, base) if split else base

    return {"folder": folder, "filename": filename}


def apply_layout(plex_names: dict, current_folder: str | None, restructure: bool) -> dict:
    """When restructure is off, rename the file where it already sits."""
    if restructure:
        return plex_names
    folder = current_folder if current_folder and current_folder != "." else ""
    return {**plex_names, "folder": folder}


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------

# Folders the app creates at the library root when films and series are split.
OUTPUT_DIR_NAMES = {MOVIES_DIR.lower(), TV_DIR.lower()}
# Plex episode name with no title after the number: "QI XL (2009) - S18E01".
_PLEX_EPISODE_NO_TITLE = re.compile(r"^.+ \((?:19|20)\d{2}\) - S\d{2}E\d{2}$")
SKIP_DIR_NAMES = {
    "subs", "subtitles", "extras", "featurettes", "behind the scenes", "sample", "samples",
}


def present_output_dirs(root: str) -> list:
    """Movies and TV folders that already exist directly under the library."""
    try:
        names = os.listdir(root)
    except OSError:
        return []
    found = []
    for canonical in (MOVIES_DIR, TV_DIR):
        if any(name.lower() == canonical.lower() and os.path.isdir(os.path.join(root, name)) for name in names):
            found.append(canonical)
    return found


def scan_directory(root: str, skip_output: bool = False) -> list:
    """
    Walk root recursively. For each video file found, return its path and
    the best display name to parse (prefers parent folder name for single-file
    folders, falls back to filename).
    When skip_output is set, Movies and TV directly under the library are left
    out, except episode files that have a season and episode but no title.
    """
    results = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Filter to video files only
        video_files = [
            f for f in filenames
            if Path(f).suffix.lower() in VIDEO_EXTENSIONS
        ]

        blocked = set(SKIP_DIR_NAMES)
        rel = os.path.relpath(dirpath, root)
        first = "" if rel in (".", "") else Path(rel).parts[0].lower()
        in_output = skip_output and first in OUTPUT_DIR_NAMES
        # Skip subtitle/extras/sample folders. Movies and TV stay walkable so an
        # episode with no title can be filled in; finished files there are ignored.
        dirnames[:] = [d for d in dirnames if d.lower() not in blocked]

        # Drop obvious sample clips (e.g. "...-sample.avi", "movie.sample.mkv")
        video_files = [f for f in video_files if not SAMPLE_PATTERN.search(Path(f).stem)]
        if in_output:
            video_files = [f for f in video_files if _PLEX_EPISODE_NO_TITLE.match(Path(f).stem)]

        for vf in video_files:
            full_path = os.path.join(dirpath, vf)
            rel_dir = os.path.relpath(dirpath, root)

            # If this is the only video file in its folder, the folder name is
            # usually a cleaner hint than a cryptic release filename - EXCEPT for
            # "Season NN"/"Specials" folders, whose names carry no title.
            folder_name = Path(dirpath).name
            use_folder_as_hint = (
                len(video_files) == 1
                and not is_structural_folder(folder_name, os.path.basename(root))
            )

            parse_hint = folder_name if use_folder_as_hint else Path(vf).stem

            results.append({
                "full_path": full_path,
                "rel_path": os.path.relpath(full_path, root),
                "filename": vf,
                "folder": rel_dir,
                "parse_hint": parse_hint,
                "ext": Path(vf).suffix.lower(),
            })

    by_folder = defaultdict(list)
    for item in results:
        by_folder[item["folder"]].append(item)
    for items in by_folder.values():
        episodic = any(
            find_episode_marker(item["filename"]) or find_episode_marker(item["parse_hint"])
            for item in items
        )
        if episodic:
            for item in items:
                item["group_tv"] = True

    assign_show_folders(results, root)
    return results


# ---------------------------------------------------------------------------
# Proposal building
# ---------------------------------------------------------------------------

def proposal_status(proposed_full_path: str, current_full_path: str, default: str = "pending") -> str:
    """organised if already in place, conflict if target taken, else `default`."""
    def _norm(p):
        return os.path.normcase(os.path.normpath(p))
    if _norm(proposed_full_path) == _norm(current_full_path):
        return "organised"
    if os.path.exists(proposed_full_path):
        return "conflict"
    return default


def _norm_title(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _titles_agree(folder_title: str | None, file_title: str | None) -> bool:
    """True when a filename is already naming the same program as its folder."""
    folder = _norm_title(folder_title)
    file_title = _norm_title(file_title)
    if not folder or not file_title:
        return False
    if len(folder) < 3 or len(file_title) < 3:
        return folder == file_title
    return folder == file_title or file_title.startswith(folder + " ") or folder.startswith(file_title + " ")


def is_structural_folder(name: str, root_name: str = "") -> bool:
    """Season, disk, and library folders. They do not name a program."""
    stripped = (name or "").strip()
    low = stripped.lower()
    if not low or low in GENERIC_LIBRARY_FOLDERS or (root_name and low == root_name.lower()):
        return True
    if SEASON_FOLDER_PATTERN.match(stripped) or DISK_FOLDER_PATTERN.match(stripped):
        return True
    return False


def _folder_parts(folder: str | None) -> list[str]:
    if not folder or folder in (".", ""):
        return []
    return [part for part in str(folder).replace("\\", "/").split("/") if part and part != "."]


def assign_show_folders(items: list, root: str) -> None:
    """Mark every video under a program folder with that program.

    A folder like "The Simpsons" owns the files inside it and inside its
    season, disk, and other subfolders. A file with a messy name is matched
    as that show instead of being split into its own folder.
    """
    root_name = os.path.basename(os.path.abspath(root))
    records = []
    for item in items:
        parts = _folder_parts(item.get("folder"))
        file_parsed = parse_filename(Path(item.get("filename") or "").stem)
        records.append((item, parts, file_parsed))

    files_under = defaultdict(list)
    children = defaultdict(set)
    for _item, parts, file_parsed in records:
        for depth in range(len(parts) + 1):
            files_under[tuple(parts[:depth])].append(file_parsed)
        for depth, name in enumerate(parts):
            children[tuple(parts[:depth])].add(name)

    def titled(path: tuple) -> dict | None:
        if not path:
            return None
        name = path[-1]
        if is_structural_folder(name, root_name):
            return None
        parsed = parse_filename(name)
        if not (parsed.get("title") or "").strip():
            return None
        return parsed

    def has_season_child(path: tuple) -> bool:
        return any(SEASON_FOLDER_PATTERN.match(child.strip()) for child in children[path])

    def has_episode(path: tuple) -> bool:
        return any(parsed.get("is_tv") or parsed.get("episode") for parsed in files_under[path])

    info = {}
    for path in files_under:
        parsed = titled(path)
        if not parsed:
            info[path] = None
            continue
        agreed = any(_titles_agree(parsed["title"], file_parsed.get("title")) for file_parsed in files_under[path])
        seasons = has_season_child(path)
        info[path] = {
            "title": parsed["title"],
            "year": parsed.get("year"),
            "strong": seasons or agreed,
            "weak": has_episode(path),
            "is_tv": seasons or has_episode(path),
        }

    for item, parts, file_parsed in records:
        strong_hit = None
        weak_hit = None
        for depth in range(len(parts), 0, -1):
            meta = info.get(tuple(parts[:depth]))
            if not meta:
                continue
            if meta["strong"]:
                strong_hit = meta
                break
            if meta["weak"]:
                weak_hit = meta
        chosen = strong_hit or weak_hit
        if not chosen:
            continue
        item["show_folder"] = {
            "title": chosen["title"],
            "year": chosen["year"],
            "is_tv": chosen["is_tv"],
        }
        if chosen["is_tv"]:
            item["group_tv"] = True


def series_folder_name(full_path: str, root: str) -> str | None:
    """The nearest folder that names a program, skipping season and disk folders."""
    root_name = os.path.basename(os.path.abspath(root))
    directory = os.path.dirname(full_path)
    while os.path.abspath(directory) != os.path.abspath(root):
        name = os.path.basename(directory)
        if not is_structural_folder(name, root_name):
            return name
        directory = os.path.dirname(directory)
    return None


def build_local_cleanup(f: dict, root: str, web_lookup: bool = False,
                        split: bool = False, embed_ids: bool = False,
                        restructure: bool = True,
                        title_override: str | None = None,
                        year_override=None) -> dict | None:
    """
    Best-effort Plex naming WITHOUT TMDB: derive the title/year from the series
    folder and the season/episode/episode-title from the filename. When
    web_lookup is set, refine the title/year/episode-name via Wikipedia.
    title_override/year_override let the user fix the result by hand.
    Returns a proposal dict (status cleanup/organised/conflict) or None.
    """
    filename_stem = Path(f["filename"]).stem

    folder_name = series_folder_name(f["full_path"], root)
    folder_parsed = parse_filename(folder_name) if folder_name else {"title": "", "year": None}
    file_parsed = parse_filename(filename_stem)

    # Season/episode + episode title come from the filename's marker
    marker = find_episode_marker(filename_stem)
    season = episode = None
    episode_title = ""
    if marker:
        season, episode, _start, end = marker
        episode_title = clean_episode_title(filename_stem[end:])
    else:
        season, episode = file_parsed["season"], file_parsed["episode"]

    # Title: a folder named "Title (Year)" is the most reliable source; without a
    # year the folder is often just a release name, so prefer the filename's title.
    folder_title, file_title = folder_parsed["title"], file_parsed["title"]
    if folder_parsed["year"] or not file_title:
        title = folder_title or file_title
    else:
        title = file_title or folder_title
    if not title:
        return None
    year = folder_parsed["year"] or file_parsed["year"]
    is_tv = bool(season and episode)
    show = f.get("show_folder") or {}
    if show.get("title"):
        title = show["title"]
        if show.get("year"):
            year = show["year"]
        if show.get("is_tv"):
            is_tv = True

    # Drop a redundant series-name prefix from the episode title
    if episode_title and episode_title.lower().startswith(title.lower()):
        episode_title = re.sub(r"^[\s\-:;,]+", "", episode_title[len(title):]).strip()

    # Optionally refine via Wikipedia: canonical title, year, episode name
    source = "local"
    info_url = None
    confidence = 0
    if web_lookup:
        hit = wiki_lookup(title, year, season, episode)
        if hit:
            source = "wikipedia"
            title = hit["title"]
            year = hit["year"]
            if hit["episode_name"]:
                episode_title = hit["episode_name"]
            info_url = hit["wiki_url"]
            confidence = hit["confidence"]

    # User-supplied corrections win over anything parsed/looked-up
    if title_override:
        title = title_override.strip()
    if year_override not in (None, ""):
        year = year_override

    media_type = "tv" if is_tv else "movie"
    match_like = {
        "matched_title": title,
        "matched_year": str(year) if year else "",
        "type": media_type,
        "season": season,
        "episode": episode,
        "episode_name": episode_title or None,
    }
    plex_names = apply_layout(
        build_plex_names(match_like, f["ext"], split, embed_ids),
        f.get("folder"), restructure,
    )
    proposed_full_path = os.path.join(root, plex_names["folder"], plex_names["filename"])
    status = proposal_status(proposed_full_path, f["full_path"], default="cleanup")

    return {
        **f,
        "parsed": file_parsed,
        "matched": False,
        "confidence": confidence,
        "type": media_type,
        "matched_title": title,
        "matched_year": match_like["matched_year"],
        "episode_name": episode_title or None,
        "season": season,
        "episode": episode,
        "local_cleanup": True,
        "source": source,
        "wiki_url": info_url,
        "missing_year": not year,
        "proposed_folder": plex_names["folder"],
        "proposed_filename": plex_names["filename"],
        "proposed_full_path": proposed_full_path,
        "status": status,
    }


def _id_fields(tag: str | None) -> dict:
    """Turn a folder tag like 'tmdb-123' or 'imdb-tt123' into match fields."""
    if not tag:
        return {}
    kind, _, value = tag.partition("-")
    if kind.lower() == "tmdb" and value.isdigit():
        return {"tmdb_id": int(value)}
    if kind.lower() == "imdb" and value.lower().startswith("tt"):
        return {"imdb_id": value}
    return {}


def _strip_folder_id(name: str) -> tuple[str, str | None]:
    match = re.search(r"\s\{(tmdb-\d+|imdb-tt\d+)\}$", name, re.IGNORECASE)
    if not match:
        return name, None
    return name[: match.start()], match.group(1)


_MOVIE_STEM = re.compile(r"^(?P<title>.+?) \((?P<year>(?:19|20)\d{2})\)$")
_TV_STEM = re.compile(
    r"^(?P<title>.+?) \((?P<year>(?:19|20)\d{2})\) - "
    r"S(?P<season>\d{2})E(?P<episode>\d{2})(?: - (?P<episode_name>.+))?$"
)


def already_organised_proposal(f: dict, root: str, split: bool, embed_ids: bool,
                               restructure: bool) -> dict | None:
    """A file whose path is already the name this scan would write.

    Returning a proposal skips TMDB, OMDb and AI. Anything that doesn't already
    match falls through to a normal lookup.
    """
    rel = f.get("rel_path") or ""
    parts = Path(rel).parts
    if not parts:
        return None
    stem = Path(parts[-1]).stem
    ext = Path(parts[-1]).suffix
    movie = _MOVIE_STEM.match(stem)
    tv = _TV_STEM.match(stem)
    if not movie and not tv:
        return None

    title = (movie or tv).group("title")
    year = (movie or tv).group("year")
    season = int(tv.group("season")) if tv else None
    episode = int(tv.group("episode")) if tv else None
    episode_name = tv.group("episode_name") if tv else None
    ids: dict = {}

    if restructure:
        if movie:
            if len(parts) < 2:
                return None
            show_base, tag = _strip_folder_id(parts[-2])
            prefix = parts[:-2]
            if show_base != f"{title} ({year})":
                return None
            if split and prefix != (MOVIES_DIR,):
                return None
            if not split and prefix:
                return None
        else:
            if len(parts) < 3:
                return None
            if parts[-2] != f"Season {season:02d}":
                return None
            show_base, tag = _strip_folder_id(parts[-3])
            prefix = parts[:-3]
            if show_base != f"{title} ({year})":
                return None
            if split and prefix != (TV_DIR,):
                return None
            if not split and prefix:
                return None
        if embed_ids and not tag:
            return None
        if not embed_ids and tag:
            return None
        ids = _id_fields(tag)
    elif embed_ids:
        # Ids live on the folder. Without restructuring we can't confirm the tag
        # belongs where this scan would put it, so look the file up.
        return None

    match = {
        "matched_title": title,
        "matched_year": year,
        "type": "tv" if tv else "movie",
        "season": season,
        "episode": episode,
        "episode_name": episode_name,
        **ids,
    }
    plex = build_plex_names(match, ext, split and restructure, embed_ids and restructure)
    if not restructure:
        current = f.get("folder") if f.get("folder") not in (None, ".") else ""
        plex = {**plex, "folder": current}
    proposed = os.path.join(root, plex["folder"], plex["filename"])
    if os.path.normcase(os.path.normpath(proposed)) != os.path.normcase(os.path.normpath(f["full_path"])):
        return None

    return {
        **f,
        "parsed": {
            "title": title,
            "year": int(year),
            "season": season,
            "episode": episode,
            "is_tv": bool(tv),
            "country": None,
        },
        "matched": True,
        "confidence": 1,
        "type": match["type"],
        "matched_title": title,
        "matched_year": year,
        "tmdb_id": ids.get("tmdb_id"),
        "imdb_id": ids.get("imdb_id"),
        "episode_name": episode_name,
        "season": season,
        "episode": episode,
        "proposed_folder": plex["folder"],
        "proposed_filename": plex["filename"],
        "proposed_full_path": proposed,
        "missing_year": False,
        "status": "organised",
        "skipped_lookup": True,
    }


def build_proposal(f: dict, root: str, api_key: str,
                   clean_unmatched: bool = False, web_lookup: bool = False,
                   split: bool = False, embed_ids: bool = False,
                   omdb_key: str = "", ai_cfg: dict | None = None,
                   restructure: bool = True, auto_pick: bool = True) -> dict:
    """Match a single scanned file against TMDB, OMDb, AI, then local cleanup."""
    organised = already_organised_proposal(f, root, split, embed_ids, restructure)
    if organised:
        return organised
    ai_cfg = ai_cfg or {"enabled": False}
    do_cleanup = clean_unmatched or web_lookup
    parsed = parse_filename(f["parse_hint"])
    # A single file in a titled folder is parsed from the folder, which drops
    # "E02" and the episode title from the filename. Keep both.
    file_parsed = parse_filename(Path(f.get("filename") or "").stem)
    if file_parsed.get("episode") is not None and parsed.get("episode") is None:
        parsed["season"], parsed["episode"] = file_parsed["season"], file_parsed["episode"]
        parsed["is_tv"] = True
    if not parsed.get("episode_title") and file_parsed.get("episode_title"):
        parsed["episode_title"] = file_parsed["episode_title"]
    show = f.get("show_folder") or {}
    if show.get("title"):
        agreed = _titles_agree(show["title"], parsed.get("title"))
        parsed["title"] = show["title"]
        if show.get("year"):
            parsed["year"] = show["year"]
        elif not agreed:
            parsed["year"] = None
        if show.get("is_tv") or f.get("group_tv"):
            parsed["is_tv"] = True
    elif f.get("group_tv"):
        parsed["is_tv"] = True
    if not parsed["title"]:
        if do_cleanup:
            cleanup = build_local_cleanup(f, root, web_lookup, split, embed_ids, restructure)
            if cleanup:
                return cleanup
        return {
            **f,
            "parsed": parsed,
            "matched": False,
            "confidence": 0,
            "status": "unmatched",
            "error": "Could not parse a title from filename",
        }

    try:
        match = find_best_match(parsed, api_key, omdb_key)
    except Exception as e:
        return {
            **f,
            "parsed": parsed,
            "matched": False,
            "confidence": 0,
            "status": "error",
            "error": str(e),
        }

    # TMDB missed - try OMDb/IMDb before any local/Wikipedia cleanup
    if (not match["matched"] or match["confidence"] < 0.3) and omdb_key:
        try:
            omdb = omdb_lookup(parsed, omdb_key)
        except Exception:
            omdb = None
        if omdb and omdb["confidence"] >= 0.5:
            match = omdb

    if (not match["matched"] or match["confidence"] < 0.5) and ai_cfg.get("enabled"):
        try:
            ai_match = apply_ai_match(f, parsed, match, api_key, ai_cfg)
        except Exception:
            ai_match = None
        if ai_match:
            match = ai_match

    if not match["matched"] or match["confidence"] < 0.3:
        if do_cleanup:
            cleanup = build_local_cleanup(f, root, web_lookup, split, embed_ids, restructure)
            if cleanup:
                return cleanup
        return {**f, "parsed": parsed, **match, "status": "unmatched"}

    if match.get("type") == "tv" or parsed.get("is_tv"):
        match["episode_name"] = choose_episode_name(match.get("episode_name"), parsed.get("episode_title"))
    if show.get("is_tv") and not parsed.get("season") and not parsed.get("episode"):
        match["folder_locked"] = True
        match["loose_name"] = file_parsed.get("title") or Path(f.get("filename") or "").stem

    plex_names = apply_layout(
        build_plex_names(match, f["ext"], split, embed_ids),
        f.get("folder"), restructure,
    )
    proposed_full_path = os.path.join(root, plex_names["folder"], plex_names["filename"])
    status = proposal_status(proposed_full_path, f["full_path"], default="pending")

    return {
        **f,
        "parsed": parsed,
        **match,
        "proposed_folder": plex_names["folder"],
        "proposed_filename": plex_names["filename"],
        "proposed_full_path": proposed_full_path,
        "missing_year": not match.get("matched_year"),
        "needs_choice": needs_manual_pick(match, auto_pick) or bool(match.get("needs_review")),
        "status": status,
    }


# ---------------------------------------------------------------------------
# Rename log + folder cleanup helpers
# ---------------------------------------------------------------------------

def load_rename_log() -> list:
    if os.path.exists(RENAME_LOG_FILE):
        try:
            with open(RENAME_LOG_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_rename_log(log: list) -> None:
    with open(RENAME_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


def log_rename_batch(moves: list, root: str) -> str:
    """Append a batch of successful moves and return its batch id."""
    log = load_rename_log()
    now = datetime.now()
    batch_id = now.strftime("%Y%m%d%H%M%S%f")
    log.append({
        "batch_id": batch_id,
        "timestamp": now.isoformat(timespec="seconds"),
        "root": root,
        "moves": moves,  # [{ "src": original, "dst": new }]
        "undone": False,
    })
    save_rename_log(log)
    return batch_id


def remove_empty_dirs(root: str) -> list:
    """Remove empty directories under root (never root itself). Bottom-up."""
    removed = []
    root_abs = os.path.abspath(root)
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if os.path.abspath(dirpath) == root_abs:
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed.append(dirpath)
        except OSError:
            pass
    return removed


def _subtree_has_video(dirpath: str) -> bool:
    """True if dirpath or any descendant contains a video file."""
    for _dp, _dn, filenames in os.walk(dirpath):
        if any(Path(f).suffix.lower() in VIDEO_EXTENSIONS for f in filenames):
            return True
    return False


def find_orphan_folders(root: str) -> list:
    """
    Find the top-most folders under root whose entire subtree contains no video
    files (e.g. leftover release folders holding only .txt/.nfo/.jpg). A folder
    is reported only if its parent DOES still contain video (or is root), so a
    junk folder rolls up to a single deletable entry instead of every subfolder.
    """
    root_abs = os.path.abspath(root)

    # Bottom-up pass: does each directory's subtree contain any video?
    has_video = {}
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        dp_abs = os.path.abspath(dirpath)
        vid_here = any(Path(f).suffix.lower() in VIDEO_EXTENSIONS for f in filenames)
        vid_child = any(has_video.get(os.path.abspath(os.path.join(dirpath, d)), False) for d in dirnames)
        has_video[dp_abs] = vid_here or vid_child

    orphans = []
    for dirpath in has_video:
        if dirpath == root_abs or has_video[dirpath]:
            continue
        parent = os.path.dirname(dirpath)
        # Report only the top-most empty folder (parent still has video, or is root)
        if parent != root_abs and not has_video.get(parent, True):
            continue

        # Summarise what's inside so the user knows what they're deleting
        file_count = 0
        exts = set()
        for _dp, _dn, filenames in os.walk(dirpath):
            file_count += len(filenames)
            for f in filenames:
                suffix = Path(f).suffix.lower()
                if suffix:
                    exts.add(suffix)

        orphans.append({
            "path": dirpath,
            "rel_path": os.path.relpath(dirpath, root),
            "name": os.path.basename(dirpath),
            "file_count": file_count,
            "extensions": sorted(exts),
        })

    orphans.sort(key=lambda o: o["rel_path"].lower())
    return orphans


# Files a release folder often ships that Plex never uses.
# Text subtitles and posters are never offered. .nfo and VobSub (.idx + .sub)
# are listed so the user can choose, but they start unchecked.
JUNK_EXTENSIONS = {
    ".txt", ".text", ".md", ".markdown", ".rtf", ".html", ".htm",
    ".url", ".lnk", ".webloc", ".website",
    ".diz", ".sfv", ".md5", ".sha", ".sha1", ".sha256", ".par2",
    ".torrent", ".nzb", ".srr",
    ".log", ".exe", ".bat", ".cmd", ".com", ".scr",
    ".pdf", ".doc", ".docx",
    ".m3u", ".m3u8", ".pls", ".cue",
    ".ini", ".db",
}
JUNK_NAMES = {
    "thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store",
    "readme", "read.me",
}
# Never deletable from this tool: text subs and artwork stay with the video.
KEEP_EXTENSIONS = {
    ".srt", ".ass", ".ssa", ".sup", ".vtt",
    ".jpg", ".jpeg", ".png", ".webp", ".tbn",
}
_README_EXT = {".txt", ".text", ".md", ".markdown", ".rtf", ".diz", ".html", ".htm", ".pdf", ".doc", ".docx"}
_LINK_EXT = {".url", ".lnk", ".webloc", ".website"}
_RELEASE_EXT = {".sfv", ".md5", ".sha", ".sha1", ".sha256", ".par2", ".torrent", ".nzb", ".srr", ".cue"}

JUNK_CATALOG = [
    {
        "id": "readme",
        "label": "Readme",
        "detail": "Notes or instructions that came with the download. Plex does not read them, so they are safe to delete.",
        "default_selected": True,
    },
    {
        "id": "shortcut",
        "label": "Shortcut",
        "detail": "A link to a website or another file, not the video. Safe to delete.",
        "default_selected": True,
    },
    {
        "id": "release",
        "label": "Release file",
        "detail": "Checksums, torrent files and repair data from the download. Plex does not use them, so they are safe to delete.",
        "default_selected": True,
    },
    {
        "id": "other",
        "label": "Other leftover",
        "detail": "Logs, playlists, Windows clutter and programs shipped with the download. They are not used for playback. A program is safer deleted than opened.",
        "default_selected": True,
    },
    {
        "id": "nfo",
        "label": "NFO",
        "detail": "Usually a scene release note. A file named after the video can hold Kodi metadata. Plex ignores it. Leave it if another app reads it.",
        "default_selected": False,
    },
    {
        "id": "vobsub",
        "label": "Picture subtitle",
        "detail": ".idx is the timing and .sub is the pictures. Plex can show that pair. Leave both unless you already have a text subtitle (.srt). Checking one also checks its pair.",
        "default_selected": False,
    },
]
JUNK_BY_ID = {item["id"]: item for item in JUNK_CATALOG}


def junk_kind(filename: str) -> str | None:
    """Category id for a file this tool may delete, or None if it must be kept."""
    lower = filename.lower()
    suffix = Path(filename).suffix.lower()
    if suffix in VIDEO_EXTENSIONS or suffix in KEEP_EXTENSIONS:
        return None
    if suffix in {".idx", ".sub"}:
        return "vobsub"
    if suffix == ".nfo":
        return "nfo"
    if lower not in JUNK_NAMES and suffix not in JUNK_EXTENSIONS:
        return None
    if suffix in _LINK_EXT:
        return "shortcut"
    if suffix in _README_EXT or lower.startswith("readme") or lower == "read.me":
        return "readme"
    if suffix in _RELEASE_EXT:
        return "release"
    return "other"


def find_junk_files(root: str) -> list:
    """Non-media files under root, including optional .nfo and VobSub sidecars."""
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            kind_id = junk_kind(name)
            if not kind_id:
                continue
            info = JUNK_BY_ID[kind_id]
            path = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            found.append({
                "path": path,
                "rel_path": os.path.relpath(path, root),
                "name": name,
                "kind": info["label"],
                "kind_id": kind_id,
                "detail": info["detail"],
                "default_selected": info["default_selected"],
                "size": size,
            })
    found.sort(key=lambda item: (item["kind_id"], item["rel_path"].lower()))
    return found


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

_SKIP_MODEL = re.compile(
    r"realtime|audio|transcribe|tts|image|embed|whisper|moderation|dall-e|sora",
    re.IGNORECASE,
)


def _chat_model_ids(provider: str, response) -> list[str]:
    """Chat model ids from a provider's /models response. Presets stay first in the UI."""
    try:
        rows = response.json().get("data") or []
    except ValueError:
        return []
    ids = []
    for item in rows:
        model_id = (item.get("id") or "").strip()
        if not model_id:
            continue
        low = model_id.lower()
        if provider == "openai":
            if not (low.startswith("gpt-") or low.startswith(("o1", "o3", "o4", "chatgpt-"))):
                continue
            if _SKIP_MODEL.search(low):
                continue
        elif not low.startswith("claude-"):
            continue
        ids.append(model_id)
    return sorted(set(ids), key=str.lower)


@app.route("/api/ai/console-urls", methods=["GET"])
def ai_console_urls():
    """Browser URLs where users sign in and create API keys (no OAuth in-app)."""
    return jsonify(AI_CONSOLE_URLS)


@app.route("/api/ai/verify-key", methods=["POST"])
def ai_verify_key():
    """
    Check an AI API key by listing models - a free call on both providers, so
    verifying never spends credit. Body: { provider, api_key, base_url? }
    """
    data = request.json or {}
    provider = (data.get("provider") or "").strip().lower()
    api_key = (data.get("api_key") or "").strip()
    if provider not in ("openai", "anthropic"):
        return jsonify({"valid": False, "error": "Unknown provider"}), 400
    if not api_key:
        return jsonify({"valid": False, "error": "No key entered"}), 400

    try:
        if provider == "openai":
            root = (data.get("base_url") or "").strip().rstrip("/") or "https://api.openai.com/v1"
            r = requests.get(f"{root}/models",
                             headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
        else:
            r = requests.get("https://api.anthropic.com/v1/models",
                             headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                             timeout=10)
    except requests.RequestException:
        return jsonify({"valid": False, "error": "Couldn't reach the provider. Check your connection."})

    if r.status_code == 200:
        return jsonify({"valid": True, "models": _chat_model_ids(provider, r)})
    if r.status_code in (401, 403):
        return jsonify({"valid": False, "error": "Key not recognised. Check it was copied in full."})

    try:
        message = (r.json().get("error") or {}).get("message") or ""
    except ValueError:
        message = ""
    if "workspace" in message.lower():
        return jsonify({"valid": False, "error": (
            "This Anthropic key isn't attached to a workspace. In the Anthropic Console, "
            "create a new key inside a workspace (e.g. Default) and paste that instead."
        )})
    if message:
        return jsonify({"valid": False, "error": f"{message} ({r.status_code})"})
    return jsonify({"valid": False, "error": f"Provider returned an error ({r.status_code}). Try again shortly."})


@app.route("/api/scan", methods=["POST"])
def scan():
    """Blocking scan: returns all proposals at once (legacy/fallback)."""
    data = request.json
    root = data.get("root_folder", "").strip()
    api_key = data.get("tmdb_api_key", "").strip()

    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400
    if not api_key:
        return jsonify({"error": "TMDB API key is required"}), 400

    clean_unmatched = bool(data.get("clean_unmatched", False))
    web_lookup = bool(data.get("web_lookup", True))
    split = bool(data.get("split_libraries", False))
    embed_ids = bool(data.get("embed_ids", False))
    restructure = bool(data.get("restructure_folders", False))
    auto_pick = bool(data.get("auto_pick_top", False))
    omdb_key = data.get("omdb_api_key", "").strip()
    ai_cfg = parse_ai_settings(data)
    skip_output = bool(data.get("skip_output_folders", False))
    files = scan_directory(root, skip_output=skip_output)
    with ThreadPoolExecutor(max_workers=SCAN_CONCURRENCY) as executor:
        # executor.map preserves input order
        proposals = list(executor.map(
            lambda f: build_proposal(
                f, root, api_key, clean_unmatched, web_lookup, split, embed_ids, omdb_key, ai_cfg, restructure, auto_pick
            ),
            files,
        ))
    return jsonify({"proposals": proposals, "root": root})


@app.route("/api/scan/stream", methods=["POST"])
def scan_stream():
    """
    Streaming scan via Server-Sent Events. Emits one proposal at a time so the
    UI can show live progress on large libraries.
    Events (JSON in each `data:` frame):
      { "type": "start", "total": N, "root": ... }
      { "type": "proposal", "index": i, "proposal": {...} }
      { "type": "done" }
      { "type": "error", "error": ... }
    """
    data = request.json
    root = data.get("root_folder", "").strip()
    api_key = data.get("tmdb_api_key", "").strip()

    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400
    if not api_key:
        return jsonify({"error": "TMDB API key is required"}), 400

    clean_unmatched = bool(data.get("clean_unmatched", False))
    web_lookup = bool(data.get("web_lookup", True))
    split = bool(data.get("split_libraries", False))
    embed_ids = bool(data.get("embed_ids", False))
    restructure = bool(data.get("restructure_folders", False))
    auto_pick = bool(data.get("auto_pick_top", False))
    omdb_key = data.get("omdb_api_key", "").strip()
    ai_cfg = parse_ai_settings(data)
    skip_output = bool(data.get("skip_output_folders", False))
    skipped = present_output_dirs(root) if skip_output else []
    files = scan_directory(root, skip_output=skip_output)

    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    def generate():
        yield sse({"type": "start", "total": len(files), "root": root, "skipped": skipped})
        # Match files concurrently. Cancel anything not yet started if the
        # client stops the scan; a file already in flight finishes.
        executor = ThreadPoolExecutor(max_workers=SCAN_CONCURRENCY)
        futures = {
            executor.submit(
                build_proposal, f, root, api_key, clean_unmatched, web_lookup,
                split, embed_ids, omdb_key, ai_cfg, restructure, auto_pick,
            ): i
            for i, f in enumerate(files)
        }
        try:
            for future in as_completed(futures):
                i = futures[future]
                if future.cancelled():
                    continue
                try:
                    proposal = future.result()
                except Exception as e:  # defensive: never break the stream
                    proposal = {
                        **files[i], "matched": False, "confidence": 0,
                        "status": "error", "error": str(e),
                    }
                yield sse({"type": "proposal", "index": i, "proposal": proposal})
            yield sse({"type": "done"})
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/rematch", methods=["POST"])
def rematch():
    """
    Re-match a single file, either by:
      - a specific TMDB id + media_type (chosen from alternatives), or
      - a pasted TMDB or IMDb link, or
      - a manual_title (+ optional manual_year) free-text re-search.
    Returns refreshed proposal fields including a correctly-rooted
    proposed_full_path.
    """
    data = request.json or {}
    root = data.get("root_folder", "")
    api_key = data.get("tmdb_api_key", "").strip()
    ext = data.get("ext", "")
    parsed = data.get("parsed") or {}
    split = bool(data.get("split_libraries", False))
    embed_ids = bool(data.get("embed_ids", False))
    restructure = bool(data.get("restructure_folders", False))

    tmdb_id = data.get("tmdb_id")
    media_type = data.get("media_type")
    manual_title = (data.get("manual_title") or "").strip()
    manual_year = data.get("manual_year")

    # "Use as-is" - rebuild a local name (no TMDB) with optional title/year fixes
    if data.get("local"):
        f = {
            "full_path": data.get("full_path", ""),
            "rel_path": data.get("rel_path", ""),
            "filename": data.get("filename", ""),
            "folder": data.get("folder", ""),
            "parse_hint": Path(data.get("filename", "")).stem,
            "ext": ext,
        }
        year_override = None
        if manual_year is not None and str(manual_year).strip().isdigit():
            year_override = int(str(manual_year).strip())
        cleanup = build_local_cleanup(
            f, root, web_lookup=False, split=split, embed_ids=embed_ids,
            restructure=restructure,
            title_override=manual_title or None, year_override=year_override,
        )
        if not cleanup:
            return jsonify({"error": "Could not build a name from this file"}), 400
        return jsonify(cleanup)

    ai_cfg = parse_ai_settings(data)
    match = None

    if data.get("ai"):
        if not api_key:
            return jsonify({"error": "TMDB API key is required"}), 400
        if not _ai_api_key(ai_cfg):
            return jsonify({"error": "Choose OpenAI or Anthropic and paste an API key in Settings"}), 400
        ai_cfg["enabled"] = True
        filename = data.get("filename", "")
        siblings = [name for name in (data.get("siblings") or []) if isinstance(name, str) and name]
        f = {
            "full_path": data.get("full_path", ""),
            "rel_path": data.get("rel_path", ""),
            "filename": filename,
            "folder": data.get("folder", ""),
            "parse_hint": Path(filename).stem,
            "ext": ext,
        }
        file_parsed = parse_filename(f["parse_hint"])
        group_tv = file_parsed["is_tv"] or any(find_episode_marker(name) for name in siblings)
        if group_tv:
            file_parsed["is_tv"] = True
        rejected_title = (data.get("rejected_title") or data.get("matched_title") or "").strip()
        rejected_id = data.get("rejected_tmdb_id")
        prior_match = {
            "matched": bool(rejected_title),
            "matched_title": rejected_title,
            "tmdb_id": rejected_id,
            "confidence": data.get("confidence", 0),
            "alternatives": data.get("alternatives") or [],
        }

        def usable(candidate):
            return (
                candidate
                and candidate.get("matched")
                and candidate.get("confidence", 0) >= 0.5
                and not _same_rejected(candidate, prior_match)
            )

        try:
            fresh = find_best_match(file_parsed, api_key, (data.get("omdb_api_key") or "").strip())
        except Exception as e:
            return jsonify({"error": str(e)}), 502
        omdb_key = (data.get("omdb_api_key") or "").strip()
        if not usable(fresh) and omdb_key:
            try:
                omdb = omdb_lookup(file_parsed, omdb_key)
            except Exception:
                omdb = None
            if usable(omdb):
                fresh = omdb
        if usable(fresh):
            match = fresh
        else:
            try:
                match = apply_ai_match(
                    f, file_parsed, prior_match, api_key, ai_cfg,
                    force=True, harder=True, siblings=siblings,
                )
            except RuntimeError as e:
                return jsonify({"error": str(e)}), 502
            if not match or _same_rejected(match, prior_match):
                return jsonify({
                    "matched": False,
                    "message": "AI could not find a different match.",
                })
        parsed = file_parsed
    elif (data.get("link") or "").strip():
        if not api_key:
            return jsonify({"error": "TMDB API key is required"}), 400
        match, parsed, link_error = match_from_link(
            data.get("link"), parsed, api_key, (data.get("omdb_api_key") or "").strip(),
            keep_numbers=bool(data.get("series")),
        )
        if link_error:
            return jsonify({"error": link_error}), 400
    elif not api_key:
        return jsonify({"error": "TMDB API key is required"}), 400
    elif tmdb_id and media_type:
        details = tmdb_get_details(media_type, tmdb_id, api_key)
        if not details:
            return jsonify({"error": "Could not load that TMDB title"}), 502
        confidence = data.get("confidence", 1.0)
        match = build_match_from_details(details, media_type, parsed, api_key, confidence)
    elif manual_title:
        year = None
        if manual_year is not None and str(manual_year).strip().isdigit():
            year = int(str(manual_year).strip())
        else:
            year = parsed.get("year")
        search_parsed = {
            "title": manual_title,
            "year": year,
            "season": parsed.get("season"),
            "episode": parsed.get("episode"),
            "is_tv": parsed.get("is_tv", False),
            "episode_title": parsed.get("episode_title"),
        }
        try:
            match = find_best_match(search_parsed, api_key, (data.get("omdb_api_key") or "").strip())
        except Exception as e:
            return jsonify({"error": str(e)}), 502
        # Fall back to OMDb/IMDb if TMDB has no confident manual match
        omdb_key = data.get("omdb_api_key", "").strip()
        if (not match.get("matched") or match.get("confidence", 0) < 0.3) and omdb_key:
            try:
                omdb = omdb_lookup(search_parsed, omdb_key)
            except Exception:
                omdb = None
            if omdb and omdb["confidence"] >= 0.5:
                match = omdb
        parsed = search_parsed
    else:
        return jsonify({"error": "Provide a TMDB or IMDb link, a TMDB id, or a title"}), 400

    if not match or not match.get("matched"):
        return jsonify({"matched": False, "confidence": 0, "status": "unmatched"})

    if match.get("type") == "tv" or (parsed or {}).get("is_tv"):
        match["episode_name"] = choose_episode_name(
            match.get("episode_name"), (parsed or {}).get("episode_title"),
        )

    plex_names = apply_layout(
        build_plex_names(match, ext, split, embed_ids),
        data.get("folder"), restructure,
    )
    proposed_full_path = os.path.join(root, plex_names["folder"], plex_names["filename"])

    def _norm(p):
        return os.path.normcase(os.path.normpath(p))
    full_path = data.get("full_path", "")
    if full_path and _norm(proposed_full_path) == _norm(full_path):
        status = "organised"
    elif os.path.exists(proposed_full_path):
        status = "conflict"
    else:
        status = "pending"

    return jsonify({
        **match,
        "parsed": parsed,
        "proposed_folder": plex_names["folder"],
        "proposed_filename": plex_names["filename"],
        "proposed_full_path": proposed_full_path,
        "missing_year": not match.get("matched_year"),
        "status": status,
    })


# Same-folder companions that belong with a video (subs, artwork, nfo).
SIDECAR_EXTENSIONS = {
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt",
    ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".tbn",
}


def sidecar_paths(video_path: str) -> list:
    """Companion files beside a video, as (path, suffix after the video stem).

    `Show.mkv` picks up `Show.en.srt` and `Show.jpg`, not `Other.srt` or another video.
    """
    folder = os.path.dirname(video_path)
    stem = Path(video_path).stem
    if not stem or not os.path.isdir(folder):
        return []
    stem_cf = stem.casefold()
    found = []
    try:
        entries = os.listdir(folder)
    except OSError:
        return []
    for name in entries:
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        if not name.casefold().startswith(stem_cf) or len(name) <= len(stem):
            continue
        rest = name[len(stem):]
        if not rest.startswith("."):
            continue
        suffix = Path(name).suffix.lower()
        if suffix in VIDEO_EXTENSIONS or suffix not in SIDECAR_EXTENSIONS:
            continue
        found.append((path, rest))
    return found


@app.route("/api/rename", methods=["POST"])
def rename():
    """
    Execute approved renames. Moves files into new folder structure, logs the
    batch for undo, and optionally cleans up empty source folders.
    """
    data = request.json
    root = data.get("root_folder", "")
    approved = data.get("approved", [])  # list of proposal objects
    clean_empty = bool(data.get("clean_empty_folders", False))

    results = []
    moves = []

    for item in approved:
        src = item["full_path"]
        dst = item["proposed_full_path"]

        try:
            if os.path.abspath(src) == os.path.abspath(dst):
                results.append({"src": src, "dst": dst, "ok": True})
                continue
            if os.path.exists(dst):
                raise FileExistsError(f"Target already exists: {dst}")
            companions = sidecar_paths(src)
            dst_dir = os.path.dirname(dst)
            os.makedirs(dst_dir, exist_ok=True)
            shutil.move(src, dst)
            results.append({"src": src, "dst": dst, "ok": True})
            moves.append({"src": src, "dst": dst})
            new_stem = Path(dst).stem
            for side_src, rest in companions:
                side_dst = os.path.join(dst_dir, new_stem + rest)
                try:
                    if os.path.exists(side_dst):
                        raise FileExistsError(f"Target already exists: {side_dst}")
                    shutil.move(side_src, side_dst)
                    results.append({"src": side_src, "dst": side_dst, "ok": True, "sidecar": True})
                    moves.append({"src": side_src, "dst": side_dst})
                except Exception as e:
                    results.append({"src": side_src, "dst": side_dst, "ok": False, "error": str(e), "sidecar": True})
        except Exception as e:
            results.append({"src": src, "dst": dst, "ok": False, "error": str(e)})

    removed_folders = []
    if clean_empty and root and os.path.isdir(root):
        removed_folders = remove_empty_dirs(root)

    batch_id = log_rename_batch(moves, root) if moves else None

    return jsonify({
        "results": results,
        "batch_id": batch_id,
        "removed_folders": removed_folders,
    })


@app.route("/api/rename-log", methods=["GET"])
def rename_log():
    """Return logged rename batches, most recent first."""
    log = load_rename_log()
    return jsonify({"batches": list(reversed(log))})


@app.route("/api/undo", methods=["POST"])
def undo():
    """
    Reverse a rename batch (moves files back to their original paths).
    Body: { "batch_id": ... }  -- if omitted, the most recent batch is undone.
    """
    data = request.json or {}
    batch_id = data.get("batch_id")
    log = load_rename_log()
    active = [b for b in log if not b.get("undone")]

    if not active:
        return jsonify({"error": "Nothing to undo"}), 400

    if batch_id:
        batch = next((b for b in log if b["batch_id"] == batch_id and not b.get("undone")), None)
    else:
        batch = active[-1]

    if not batch:
        return jsonify({"error": "Batch not found or already undone"}), 404

    results = []
    for mv in reversed(batch["moves"]):
        original, current = mv["src"], mv["dst"]
        try:
            if not os.path.exists(current):
                raise FileNotFoundError(f"File no longer at {current}")
            if os.path.exists(original):
                raise FileExistsError(f"Original path is occupied: {original}")
            os.makedirs(os.path.dirname(original), exist_ok=True)
            shutil.move(current, original)
            results.append({"src": current, "dst": original, "ok": True})
        except Exception as e:
            results.append({"src": current, "dst": original, "ok": False, "error": str(e)})

    removed_folders = []
    if batch.get("root") and os.path.isdir(batch["root"]):
        removed_folders = remove_empty_dirs(batch["root"])

    batch["undone"] = True
    save_rename_log(log)

    return jsonify({
        "results": results,
        "removed_folders": removed_folders,
        "batch_id": batch["batch_id"],
    })


# Runs in a separate process so the GUI event loop never touches the Flask
# server thread (tkinter is not thread-safe). Prints the chosen path (or an
# empty line if cancelled) to stdout.
_PICKER_CODE = r"""
import sys
import tkinter as tk
from tkinter import filedialog

initial = sys.argv[1] if len(sys.argv) > 1 else ""
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
kwargs = {"title": "Select media library folder"}
import os
if initial and os.path.isdir(initial):
    kwargs["initialdir"] = initial
path = filedialog.askdirectory(**kwargs)
root.destroy()
sys.stdout.write(path or "")
"""


@app.route("/api/pick-folder", methods=["POST"])
def pick_folder():
    """Open the native OS folder-picker dialog and return the chosen path."""
    data = request.json or {}
    initial = (data.get("initial") or "").strip()

    run_kwargs = {"capture_output": True, "text": True, "timeout": 600}
    if os.name == "nt":
        # Avoid a console window flashing when spawning python.exe
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(
            [sys.executable, "-c", _PICKER_CODE, initial],
            **run_kwargs,
        )
    except Exception as e:
        return jsonify({"error": f"Could not open folder dialog: {e}"}), 500

    path = (result.stdout or "").strip()

    if result.returncode != 0 and not path:
        return jsonify({
            "error": (result.stderr or "").strip() or "Folder dialog failed to open",
        }), 500

    if path:
        path = os.path.normpath(path)  # askdirectory returns forward slashes on Windows

    return jsonify({"path": path, "cancelled": path == ""})


@app.route("/api/validate-folder", methods=["POST"])
def validate_folder():
    path = request.json.get("path", "")
    exists = os.path.isdir(path)
    return jsonify({"exists": exists, "path": path})


@app.route("/api/orphan-folders", methods=["POST"])
def orphan_folders():
    """Preview folders under root that contain no video files (safe to delete)."""
    data = request.json or {}
    root = (data.get("root_folder") or "").strip()
    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400
    return jsonify({"folders": find_orphan_folders(root)})


@app.route("/api/junk-files", methods=["POST"])
def junk_files():
    """Preview leftover files grouped by type. Nothing is deleted."""
    data = request.json or {}
    root = (data.get("root_folder") or "").strip()
    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400
    return jsonify({"files": find_junk_files(root), "kinds": JUNK_CATALOG})


@app.route("/api/delete-junk", methods=["POST"])
def delete_junk():
    """
    Delete the given files, but only if each one still classifies as removable
    and lives inside root. A video, text subtitle or poster is never removed.
    .nfo and VobSub files are removed only when the user included them.
    Body: { root_folder, files: [<abs path>, ...] }
    """
    data = request.json or {}
    root = (data.get("root_folder") or "").strip()
    files = data.get("files") or []

    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400

    root_abs = os.path.abspath(root)
    results = []
    for fpath in files:
        target = os.path.abspath(fpath)
        try:
            if target == root_abs or os.path.commonpath([root_abs, target]) != root_abs:
                raise ValueError("File is outside the library root")
            if not os.path.isfile(target):
                raise FileNotFoundError("File no longer exists")
            if not junk_kind(os.path.basename(target)):
                raise ValueError("File is not junk - skipped")
            os.remove(target)
            results.append({"path": fpath, "ok": True})
        except Exception as e:  # noqa: BLE001 - report any failure per-file
            results.append({"path": fpath, "ok": False, "error": str(e)})

    return jsonify({"results": results})


@app.route("/api/delete-folders", methods=["POST"])
def delete_folders():
    """
    Delete the given folders. Each is re-verified to be inside root and to
    contain no video files before removal, so an out-of-date preview can never
    delete a folder that has since gained a video.
    Body: { root_folder, folders: [<abs path>, ...] }
    """
    data = request.json or {}
    root = (data.get("root_folder") or "").strip()
    folders = data.get("folders") or []

    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400

    root_abs = os.path.abspath(root)
    results = []
    for folder in folders:
        target = os.path.abspath(folder)
        try:
            # Safety: must live strictly inside root and never be root itself
            if target == root_abs or os.path.commonpath([root_abs, target]) != root_abs:
                raise ValueError("Folder is outside the library root")
            if not os.path.isdir(target):
                raise FileNotFoundError("Folder no longer exists")
            if _subtree_has_video(target):
                raise ValueError("Folder now contains a video file - skipped")

            shutil.rmtree(target)
            results.append({"path": folder, "ok": True})
        except Exception as e:  # noqa: BLE001 - report any failure per-folder
            results.append({"path": folder, "ok": False, "error": str(e)})

    return jsonify({"results": results})


@app.route("/api/delete-files", methods=["POST"])
def delete_files():
    """
    Delete individual files (used to remove duplicate source files whose target
    is already organised). Each file is re-verified to live inside root.
    Body: { root_folder, files: [<abs path>, ...] }
    """
    data = request.json or {}
    root = (data.get("root_folder") or "").strip()
    files = data.get("files") or []

    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400

    root_abs = os.path.abspath(root)
    results = []
    for fpath in files:
        target = os.path.abspath(fpath)
        try:
            if target == root_abs or os.path.commonpath([root_abs, target]) != root_abs:
                raise ValueError("File is outside the library root")
            if not os.path.isfile(target):
                raise FileNotFoundError("File no longer exists")
            os.remove(target)
            results.append({"path": fpath, "ok": True})
        except Exception as e:  # noqa: BLE001 - report any failure per-file
            results.append({"path": fpath, "ok": False, "error": str(e)})

    return jsonify({"results": results})


if __name__ == "__main__":
    # threaded=True so the streaming scan endpoint doesn't block other requests
    app.run(port=5174, debug=True, threaded=True)
