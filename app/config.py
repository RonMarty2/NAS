"""Configuración: combina variables de entorno (valores iniciales) con los ajustes
guardados en la base de datos (editables desde la web). La BD siempre tiene prioridad."""
import os

from . import db

# Claves de configuración y su variable de entorno por defecto.
DEFAULTS = {
    # Carpeta donde JDownloader deja las descargas (ruta DENTRO del contenedor).
    "downloads_dir": os.environ.get("NAS_DOWNLOADS_DIR", "/downloads"),
    # Raíces de biblioteca que el programa puede mostrar y donde puede mover.
    # Separadas por comas. Son las carpetas "base" que verás en el desplegable.
    "library_roots": os.environ.get("NAS_LIBRARY_ROOTS", "/video,/music"),
    # Carpetas sugeridas por defecto (se preseleccionan en el desplegable).
    "default_movie_dir":  os.environ.get("NAS_DEFAULT_MOVIE_DIR", "/video/peliculas"),
    "default_series_dir": os.environ.get("NAS_DEFAULT_SERIES_DIR", "/video/series"),
    "default_music_dir":  os.environ.get("NAS_DEFAULT_MUSIC_DIR", "/music"),
    # Claves y Jellyfin
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


def default_dir_for(media_type):
    """Carpeta sugerida por defecto según el tipo de medio."""
    return {
        "movie":  get("default_movie_dir"),
        "series": get("default_series_dir"),
        "music":  get("default_music_dir"),
    }.get(media_type, get("default_movie_dir"))
