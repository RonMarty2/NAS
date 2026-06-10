"""Music metadata: first the file tags, then MusicBrainz/Cover Art Archive."""
import base64
import hashlib
import os
from io import BytesIO

import requests
from mutagen import File as MutagenFile
from mutagen.flac import Picture
from mutagen.mp4 import MP4Cover

from .. import db

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
UA = {"User-Agent": "NAS-Organizer/1.0 (https://github.com/RonMarty2/NAS)"}
COVER_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "music_covers")

_COVER_NAMES = ("cover", "folder", "front", "album", "artwork", "art")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def from_tags(path):
    """Read artist / album / title / track / year from the file tags."""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        audio = None

    data = {"artist": None, "album": None, "title": None, "track": None, "year": None}
    if audio and audio.tags:

        def first(key):
            v = audio.tags.get(key)
            return v[0] if v else None

        data["artist"] = first("artist") or first("albumartist")
        data["album"] = first("album")
        data["title"] = first("title")
        track = first("tracknumber")
        if track:
            data["track"] = track.split("/")[0].strip()
        date = first("date") or first("year")
        if date and str(date)[:4].isdigit():
            data["year"] = int(str(date)[:4])

    if not data["title"]:
        data["title"] = os.path.splitext(os.path.basename(path))[0]
    return data


def search_musicbrainz(artist, title):
    """Fallback when tags are missing: try to deduce artist / album."""
    if not (artist or title):
        return None
    query_parts = []
    if title:
        query_parts.append(f'recording:"{_mb_escape(title)}"')
    if artist:
        query_parts.append(f'artist:"{_mb_escape(artist)}"')
    try:
        r = requests.get(
            f"{MB_BASE}/recording",
            params={"query": " AND ".join(query_parts), "fmt": "json", "limit": 1},
            headers=UA, timeout=8,
        )
        r.raise_for_status()
        recs = r.json().get("recordings", [])
    except requests.RequestException:
        return None
    if not recs:
        return None
    rec = recs[0]
    artist_name = None
    if rec.get("artist-credit"):
        artist_name = rec["artist-credit"][0].get("name")
    album = None
    release_id = None
    if rec.get("releases"):
        rel = rec["releases"][0]
        album = rel.get("title")
        release_id = rel.get("id")
    return {
        "artist": artist_name,
        "album": album,
        "title": rec.get("title"),
        "release_id": release_id,
    }


def search_candidates(query, limit=8):
    """Free search in MusicBrainz for the manual editor from the web.

    Returns a list of dicts {artist, album, title, track}.
    """
    if not query:
        return []
    try:
        r = requests.get(
            f"{MB_BASE}/recording",
            params={"query": query, "fmt": "json", "limit": limit},
            headers=UA, timeout=8,
        )
        r.raise_for_status()
        recs = r.json().get("recordings", [])
    except requests.RequestException:
        return []

    out = []
    for rec in recs:
        artist = None
        if rec.get("artist-credit"):
            artist = rec["artist-credit"][0].get("name")
        album = track = None
        if rec.get("releases"):
            rel = rec["releases"][0]
            album = rel.get("title")
            media = rel.get("media") or []
            if media and media[0].get("track"):
                track = media[0]["track"][0].get("number")
        out.append({
            "artist": artist, "album": album,
            "title": rec.get("title"), "track": track,
        })
    return out


def identify_music(path):
    """Return music metadata from tags plus optional cover art."""
    tags = from_tags(path)
    mb = None
    if not tags["artist"] or not tags["album"]:
        mb = search_musicbrainz(tags["artist"], tags["title"])
        if mb:
            tags["artist"] = tags["artist"] or mb.get("artist")
            tags["album"] = tags["album"] or mb.get("album")
            tags["title"] = tags["title"] or mb.get("title")

    cover_url = cover_url_for_path(
        path,
        artist=tags.get("artist"),
        album=tags.get("album"),
        title=tags.get("title"),
        release_id=(mb or {}).get("release_id"),
    )
    if cover_url:
        tags["cover_url"] = cover_url
    return tags


def cover_url_for_path(path, artist=None, album=None, title=None, release_id=None):
    """Best-effort cover art lookup for a music file."""
    local = _local_cover_url(path)
    if local:
        return local

    if release_id:
        cover = _cover_from_release(release_id)
        if cover:
            return cover

    release_id = _search_release_id(artist, album)
    if release_id:
        cover = _cover_from_release(release_id)
        if cover:
            return cover

    if not release_id and _meaningful(artist) and _meaningful(title):
        mb = search_musicbrainz(artist, title)
        release_id = (mb or {}).get("release_id")
        if release_id:
            cover = _cover_from_release(release_id)
            if cover:
                return cover
    return None


def cached_cover_path(name):
    """Return the local cached file path for a cover name if it exists."""
    if not name:
        return None
    safe = os.path.basename(name)
    path = os.path.join(COVER_CACHE_DIR, safe)
    return path if os.path.isfile(path) else None


def _meaningful(value):
    if value is None:
        return False
    text = str(value).strip().lower()
    return bool(text) and text not in {"desconocido", "unknown", "n/a", "none", "-"}


def _cache_dir():
    os.makedirs(COVER_CACHE_DIR, exist_ok=True)
    return COVER_CACHE_DIR


def _mime_ext(mime):
    mime = (mime or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(mime, ".jpg")


def _cache_bytes(data, mime, prefix):
    if not data:
        return None
    _cache_dir()
    digest = hashlib.sha256(data).hexdigest()
    ext = _mime_ext(mime)
    filename = f"{prefix}-{digest}{ext}"
    path = os.path.join(COVER_CACHE_DIR, filename)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return f"/music-covers/{filename}"


def _local_cover_url(path):
    audio = None
    try:
        audio = MutagenFile(path)
    except Exception:
        audio = None

    if audio:
        cover = _embedded_cover(audio)
        if cover:
            return _cache_bytes(cover["data"], cover["mime"], "embedded")

    sidecar = _sidecar_cover(path)
    if sidecar:
        try:
            with open(sidecar, "rb") as f:
                data = f.read()
        except OSError:
            return None
        mime = _mime_from_path(sidecar)
        return _cache_bytes(data, mime, "sidecar")

    return None


def _embedded_cover(audio):
    tags = getattr(audio, "tags", None)
    if not tags:
        return None

    try:
        frames = tags.getall("APIC")
    except Exception:
        frames = []
    for frame in frames:
        data = getattr(frame, "data", None)
        if data:
            return {"data": data, "mime": getattr(frame, "mime", "image/jpeg")}

    pictures = getattr(audio, "pictures", None) or []
    for pic in pictures:
        data = getattr(pic, "data", None)
        if data:
            return {"data": data, "mime": getattr(pic, "mime", "image/jpeg")}

    covr = None
    try:
        covr = tags.get("covr")
    except Exception:
        covr = None
    if covr:
        cover = covr[0]
        mime = "image/png" if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
        return {"data": bytes(cover), "mime": mime}

    for key in ("metadata_block_picture", "coverart"):
        raw = tags.get(key)
        if not raw:
            continue
        raw = raw[0] if isinstance(raw, (list, tuple)) else raw
        try:
            decoded = base64.b64decode(raw)
            pic = Picture()
            pic.load(BytesIO(decoded))
            if pic.data:
                return {"data": pic.data, "mime": pic.mime or "image/jpeg"}
        except Exception:
            continue

    return None


def _sidecar_cover(path):
    folder = os.path.dirname(path)
    parents = [folder]
    parent = os.path.dirname(folder)
    if parent and parent != folder:
        parents.append(parent)

    for current in parents:
        if not current or not os.path.isdir(current):
            continue
        try:
            entries = os.listdir(current)
        except OSError:
            continue

        exact = []
        loose = []
        for entry in entries:
            ext = os.path.splitext(entry)[1].lower()
            stem = os.path.splitext(entry)[0].strip().lower()
            if ext not in _IMAGE_EXTS:
                continue
            if stem in _COVER_NAMES:
                exact.append(os.path.join(current, entry))
            elif any(stem.startswith(prefix) for prefix in _COVER_NAMES):
                loose.append(os.path.join(current, entry))
        if exact:
            return exact[0]
        if loose:
            return loose[0]
    return None


def _mime_from_path(path):
    ext = os.path.splitext(path)[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext, "image/jpeg")


def _search_release_id(artist, album):
    if not _meaningful(artist) or not _meaningful(album):
        return None
    query = f'artist:"{_mb_escape(artist)}" AND release:"{_mb_escape(album)}"'
    try:
        r = requests.get(
            f"{MB_BASE}/release",
            params={"query": query, "fmt": "json", "limit": 1},
            headers=UA, timeout=8,
        )
        r.raise_for_status()
        releases = r.json().get("releases", [])
    except requests.RequestException:
        return None
    if not releases:
        return None
    return releases[0].get("id")


def _mb_escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _cover_from_release(release_id):
    if not _meaningful(release_id):
        return None

    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        cached = os.path.join(COVER_CACHE_DIR, f"release-{release_id}{ext}")
        if os.path.isfile(cached):
            return f"/music-covers/{os.path.basename(cached)}"

    try:
        r = requests.get(
            f"{CAA_BASE}/release/{release_id}/front-250",
            headers=UA, timeout=8,
        )
        r.raise_for_status()
    except requests.RequestException:
        return None

    mime = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        return None

    ext = _mime_ext(mime)
    _cache_dir()
    filename = f"release-{release_id}{ext}"
    path = os.path.join(COVER_CACHE_DIR, filename)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(r.content)
    return f"/music-covers/{filename}"
