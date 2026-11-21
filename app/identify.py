"""Identificación inicial del archivo: tipo (película/serie/música) y datos básicos
deducidos del nombre con guessit."""
import concurrent.futures
import os
import re

from guessit import guessit

from . import config

# Guardián con límite de tiempo: guessit analiza el nombre con expresiones
# regulares y, aunque es rarísimo, un nombre de archivo patológico podría
# colgarlo. Sin esto, UN solo archivo raro podía dejar pegado para siempre
# tanto el escaneo de descargas (automático, cada 30s) como una importación
# de biblioteca, sin ningún error visible.
#
# Se crea un ejecutor nuevo (un hilo) por llamada, no uno compartido: si un
# archivo se cuelga de verdad, ese hilo queda abandonado (se libera solo)
# en vez de bloquear también los archivos siguientes.
_GUARD_TIMEOUT = 8.0


def _run_guarded(fn, *args, fallback=None):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(fn, *args)
        return future.result(timeout=_GUARD_TIMEOUT)
    except Exception:
        return fallback
    finally:
        executor.shutdown(wait=False)

# Etiquetas de idioma frecuentes en los nombres (escena en español).
_LANG_MAP = {
    "lat": "Latino", "latino": "Latino", "esplat": "Latino",
    "cast": "Castellano", "castellano": "Castellano", "esp": "Castellano",
    "español": "Castellano", "espanol": "Castellano", "spa": "Castellano",
    "ing": "Inglés", "eng": "Inglés", "ingles": "Inglés", "inglés": "Inglés",
    "english": "Inglés",
    "dual": "Dual",
    "sub": "Subtítulos", "subs": "Subtítulos", "subtitulado": "Subtítulos",
    "vose": "Subtítulos", "vos": "Subtítulos", "subbed": "Subtítulos",
}


def tech_info(filename):
    """Saca calidad (resolución · fuente · códec) e idiomas del nombre del archivo.

    Devuelve (quality, langs) como textos cortos para mostrar y ayudar a decidir."""
    info = guessit(filename)
    parts = []
    for field in ("screen_size", "source", "video_codec"):
        val = info.get(field)
        if val:
            parts.append(str(val))
    quality = " · ".join(parts)

    found = []
    for tok in re.split(r"[^0-9a-záéíóúñ]+", filename.lower()):
        label = _LANG_MAP.get(tok)
        if label and label not in found:
            found.append(label)
    langs = " · ".join(found)
    return quality, langs


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


_GENERIC_TITLES = {"movie", "video", "pelicula", "película", "film", "full", "hd", "1080p", "720p"}


def _parent_folder_hint(path):
    """Si el archivo viene dentro de una subcarpeta de paquete (muy común en
    JDownloader), devuelve el nombre de esa carpeta para rescatar título, año,
    calidad o idioma que no estén en el nombre del archivo."""
    parent_dir = os.path.dirname(os.path.abspath(path or ""))
    parent_name = os.path.basename(parent_dir)
    if not parent_name:
        return ""
    roots = {
        os.path.abspath(r.strip())
        for r in [config.get("downloads_dir"), *config.get("library_roots").split(",")]
        if r and r.strip()
    }
    if os.path.abspath(parent_dir) in roots:
        return ""
    if re.match(r"^(?:season|temporada|temp|disc|cd)\s*\d+$", parent_name, re.I):
        grand = os.path.dirname(parent_dir)
        if os.path.abspath(grand) not in roots:
            return os.path.basename(grand)
        return ""
    return parent_name


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
    fallback_episode = _spanish_episode_from_name(filename)
    parent_hint = _parent_folder_hint(path)
    parent_info = guessit(parent_hint) if parent_hint else {}
    gtype = info.get("type")  # 'movie' o 'episode'

    if fallback_episode:
        media_type = "series"
    elif gtype == "episode" or info.get("season") is not None or info.get("episode") is not None:
        media_type = "series"
    elif gtype == "movie":
        media_type = "movie"
    else:
        media_type = "unknown"

    season = fallback_episode["season"] if fallback_episode else info.get("season")
    episode = fallback_episode["episode"] if fallback_episode else info.get("episode")
    # guessit a veces devuelve listas para episodios dobles
    if isinstance(season, list):
        season = season[0] if season else None
    if isinstance(episode, list):
        episode = episode[0] if episode else None

    title = fallback_episode["title"] if fallback_episode else info.get("title")
    parent_title = parent_info.get("title") if parent_info else None
    if (not title or str(title).strip().lower() in _GENERIC_TITLES) and parent_title:
        title = parent_title
    elif media_type == "series" and parent_title:
        # En series descargadas por carpeta (/descargas/NombreSerie/07 titulo_cap.mkv),
        # el nombre del archivo a veces solo trae el título del episodio.
        t_low = str(title or "").strip().lower()
        p_low = str(parent_title).strip().lower()
        if p_low and t_low and p_low not in t_low and t_low not in p_low:
            title = parent_title
    year = info.get("year") or parent_info.get("year")

    quality, langs = tech_info(f"{parent_hint} {filename}" if parent_hint else filename)
    return {
        "media_type": media_type,
        "title": title,
        "year": year,
        "season": season,
        "episode": episode,
        "quality": quality,
        "langs": langs,
    }


def _spanish_episode_from_name(filename):
    """Detecta patrones comunes en español que guessit no entiende:
    `Aida T10xC35.ts`, `Aida T10C35.ts`, `Aida T10x35.ts`.
    """
    stem = os.path.splitext(filename)[0]
    match = re.search(
        r"^(?P<title>.+?)[ ._\-]*T(?P<season>\d{1,2})[ ._\-]*(?:xC?|C)(?P<episode>\d{1,3})(?:\b|[ ._\-])",
        stem,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    title = re.sub(r"[._]+", " ", match.group("title")).strip(" -_.")
    if not title:
        return None
    return {
        "title": title,
        "season": int(match.group("season")),
        "episode": int(match.group("episode")),
    }


_IDENTIFY_FALLBACK = {"media_type": "unknown", "title": None, "year": None, "season": None, "episode": None}


def identify_safe(path):
    """Igual que identify(), pero nunca se queda colgada para siempre: si
    tarda más de unos segundos, sigue adelante con un resultado vacío."""
    return _run_guarded(identify, path, fallback=dict(_IDENTIFY_FALLBACK))


def guessit_safe(filename):
    """guessit() con el mismo límite de tiempo de seguridad. Para cualquier
    módulo que necesite el dict crudo de guessit sin riesgo de colgarse."""
    return _run_guarded(guessit, filename, fallback={}) or {}


def tech_info_safe(filename):
    """Igual que tech_info(), con el mismo límite de tiempo de seguridad."""
    result = _run_guarded(tech_info, filename, fallback=("", ""))
    return result if result is not None else ("", "")
