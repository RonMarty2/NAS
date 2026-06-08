"""Identificación inicial del archivo: tipo (película/serie/música) y datos básicos
deducidos del nombre con guessit."""
import os

from guessit import guessit

from . import config


def classify_extension(path):
    """Clasifica por extensión: 'video', 'music', 'subtitle' o None."""
    ext = os.path.splitext(path)[1].lower()
    if ext in config.ext_list("video_exts"):
        return "video"
    if ext in config.ext_list("music_exts"):
        return "music"
    if ext in config.ext_list("subtitle_exts"):
        return "subtitle"
    return None


def identify(path):
    """Devuelve un dict con: media_type, title, year, season, episode.

    media_type ∈ {movie, series, music, unknown}.
    """
    kind = classify_extension(path)
    filename = os.path.basename(path)

    if kind == "music":
        return {"media_type": "music", "title": None, "year": None,
                "season": None, "episode": None}

    info = guessit(filename)
    gtype = info.get("type")  # 'movie' o 'episode'

    if gtype == "episode" or info.get("season") is not None or info.get("episode") is not None:
        media_type = "series"
    elif gtype == "movie":
        media_type = "movie"
    else:
        media_type = "unknown"

    season = info.get("season")
    episode = info.get("episode")
    # guessit a veces devuelve listas para episodios dobles
    if isinstance(season, list):
        season = season[0] if season else None
    if isinstance(episode, list):
        episode = episode[0] if episode else None

    return {
        "media_type": media_type,
        "title": info.get("title"),
        "year": info.get("year"),
        "season": season,
        "episode": episode,
    }
