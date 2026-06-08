"""Metadatos de música: primero las etiquetas del propio archivo (mutagen),
y como respaldo una búsqueda en MusicBrainz (API gratuita, sin key)."""
import os

import requests
from mutagen import File as MutagenFile

from .. import config

MB_BASE = "https://musicbrainz.org/ws/2"
UA = {"User-Agent": "NAS-Organizer/1.0 (https://github.com/RonMarty2/NAS)"}


def from_tags(path):
    """Lee artista / álbum / título / nº de pista desde las etiquetas del archivo."""
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

    # Respaldo: nombre de archivo si falta el título
    if not data["title"]:
        data["title"] = os.path.splitext(os.path.basename(path))[0]
    return data


def search_musicbrainz(artist, title):
    """Respaldo cuando no hay etiquetas: intenta deducir artista/álbum."""
    if not (artist or title):
        return None
    query_parts = []
    if title:
        query_parts.append(f'recording:"{title}"')
    if artist:
        query_parts.append(f'artist:"{artist}"')
    try:
        r = requests.get(
            f"{MB_BASE}/recording",
            params={"query": " AND ".join(query_parts), "fmt": "json", "limit": 1},
            headers=UA, timeout=15,
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
    if rec.get("releases"):
        album = rec["releases"][0].get("title")
    return {"artist": artist_name, "album": album, "title": rec.get("title")}


def search_candidates(query, limit=8):
    """Búsqueda libre en MusicBrainz para la edición manual desde la web.

    Devuelve una lista de dicts {artist, album, title, track}.
    """
    if not query:
        return []
    try:
        r = requests.get(
            f"{MB_BASE}/recording",
            params={"query": query, "fmt": "json", "limit": limit},
            headers=UA, timeout=15,
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
    """Devuelve metadatos de música combinando etiquetas + MusicBrainz."""
    tags = from_tags(path)
    if not tags["artist"] or not tags["album"]:
        mb = search_musicbrainz(tags["artist"], tags["title"])
        if mb:
            tags["artist"] = tags["artist"] or mb.get("artist")
            tags["album"] = tags["album"] or mb.get("album")
            tags["title"] = tags["title"] or mb.get("title")
    return tags
