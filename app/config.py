"""Configuración: combina variables de entorno (valores iniciales) con los ajustes
guardados en la base de datos (editables desde la web). La BD siempre tiene prioridad."""
import os
import threading
import time

from . import db

# Caché de los ajustes para no abrir una conexión SQLite en cada config.get().
# Antes, pintar una pestaña o escanear la carpeta abría decenas/cientos de
# conexiones (una por cada lectura de ajuste). Con una caché de pocos segundos
# se hace una sola lectura por ventana y se nota mucho menos en hardware modesto.
# Cualquier cambio desde la web invalida la caché al instante (ver set()).
_CACHE_TTL = 3.0
_cache = None
_cache_at = 0.0
_cache_lock = threading.Lock()


def _settings():
    global _cache, _cache_at
    snapshot = _cache
    if snapshot is not None and (time.monotonic() - _cache_at) < _CACHE_TTL:
        return snapshot
    with _cache_lock:
        if _cache is not None and (time.monotonic() - _cache_at) < _CACHE_TTL:
            return _cache
        _cache = db.all_settings()
        _cache_at = time.monotonic()
        return _cache


def invalidate():
    """Olvida la caché para que la próxima lectura traiga valores frescos."""
    global _cache
    _cache = None

# Claves de configuración y su variable de entorno por defecto.
DEFAULTS = {
    # Carpeta donde JDownloader deja las descargas.
    "downloads_dir": os.environ.get("NAS_DOWNLOADS_DIR", "/volume1/homes/rnd261190/jdownloader"),
    # Raíces de biblioteca que el programa puede mostrar y donde puede mover.
    # Separadas por comas. Son las carpetas "base" del navegador de carpetas.
    "library_roots": os.environ.get("NAS_LIBRARY_ROOTS", "/volume1/video,/volume1/music"),
    # Carpetas sugeridas por defecto (se preseleccionan al elegir destino).
    "default_movie_dir":  os.environ.get("NAS_DEFAULT_MOVIE_DIR", "/volume1/video/peliculas"),
    "default_series_dir": os.environ.get("NAS_DEFAULT_SERIES_DIR", "/volume1/video/series"),
    "default_music_dir":  os.environ.get("NAS_DEFAULT_MUSIC_DIR", "/volume1/music"),
    # Claves y Jellyfin
    "tmdb_api_key":     os.environ.get("TMDB_API_KEY", ""),
    "jellyfin_url":     os.environ.get("JELLYFIN_URL", ""),
    "jellyfin_api_key": os.environ.get("JELLYFIN_API_KEY", ""),
    "metadata_language": os.environ.get("METADATA_LANGUAGE", "es-MX"),
    # Extensiones que consideramos vídeo / música / subtítulos.
    "video_exts":    ".mkv,.mp4,.avi,.mov,.m4v,.wmv,.mpg,.mpeg,.ts",
    "music_exts":    ".mp3,.flac,.m4a,.aac,.ogg,.opus,.wav,.wma",
    "subtitle_exts": ".srt,.sub,.ass,.ssa,.vtt,.idx",
    "min_size_mb":   "10",  # ignora archivos minúsculos (basura)
    # En NAS modestos conviene dejarlo apagado: sin ffprobe solo usa nombre + peso.
    "probe_media_info": os.environ.get("NAS_PROBE_MEDIA_INFO", "false"),
    # Palabras que marcan basura: si el nombre las contiene, se ignora el archivo.
    "junk_patterns": "sample,muestra,activador,activator,crack,keygen,rarbg,proof,trailer",
    # Notificaciones (todas opcionales; se configuran en Ajustes).
    "app_url":          "",  # ej. http://192.168.100.178:8678 (enlace del aviso)
    "ntfy_server":      "https://ntfy.sh",
    "ntfy_topic":       "",
    "discord_webhook":  "",
    "telegram_token":   "",
    "telegram_chat_id": "",
}


def get(key):
    """Devuelve el valor efectivo: primero la BD, luego el valor por defecto/env."""
    val = _settings().get(key)
    if val is None or val == "":
        return DEFAULTS.get(key, "")
    return val


def set(key, value):
    db.set_setting(key, value)
    invalidate()


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
