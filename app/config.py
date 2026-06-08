"""Configuración: combina variables de entorno (valores iniciales) con los ajustes
guardados en la base de datos (editables desde la web). La BD siempre tiene prioridad."""
import os

from . import db

# Claves de configuración y su variable de entorno por defecto.
DEFAULTS = {
    "downloads_dir":    os.environ.get("NAS_DOWNLOADS_DIR", "/downloads"),
    "movies_dir":       os.environ.get("NAS_MOVIES_DIR", "/media/Películas"),
    "series_dir":       os.environ.get("NAS_SERIES_DIR", "/media/Series"),
    "music_dir":        os.environ.get("NAS_MUSIC_DIR", "/media/Música"),
    "tmdb_api_key":     os.environ.get("TMDB_API_KEY", ""),
    "jellyfin_url":     os.environ.get("JELLYFIN_URL", ""),
    "jellyfin_api_key": os.environ.get("JELLYFIN_API_KEY", ""),
    "metadata_language": os.environ.get("METADATA_LANGUAGE", "es-ES"),
    # Extensiones que consideramos vídeo / música / subtítulos.
    "video_exts":    ".mkv,.mp4,.avi,.mov,.m4v,.wmv,.mpg,.mpeg,.ts",
    "music_exts":    ".mp3,.flac,.m4a,.aac,.ogg,.opus,.wav,.wma",
    "subtitle_exts": ".srt,.sub,.ass,.ssa,.vtt,.idx",
    "min_size_mb":   "10",  # ignora archivos minúsculos (basura)
}


def get(key):
    """Devuelve el valor efectivo: primero la BD, luego el valor por defecto/env."""
    val = db.get_setting(key)
    if val is None or val == "":
        return DEFAULTS.get(key, "")
    return val


def set(key, value):
    db.set_setting(key, value)


def as_dict():
    return {k: get(k) for k in DEFAULTS}


def ext_list(key):
    """Devuelve un set de extensiones en minúsculas a partir de una clave de config."""
    return {e.strip().lower() for e in get(key).split(",") if e.strip()}
