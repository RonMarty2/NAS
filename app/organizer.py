"""Construye las rutas destino con la convención de Jellyfin y mueve los archivos."""
import os
import re
import shutil
from xml.sax.saxutils import escape

import requests

from . import config
from .metadata import music as music_meta
from .metadata import tmdb

# Caracteres no válidos en nombres de archivo/carpeta
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name):
    """Limpia un nombre para que sea válido como carpeta/archivo."""
    name = _INVALID.sub("", str(name)).strip().rstrip(".")
    return name or "Desconocido"


def _two(n):
    try:
        return f"{int(n):02d}"
    except (TypeError, ValueError):
        return "01"


def default_base(media_type):
    """Carpeta base sugerida por defecto para un tipo de medio."""
    return config.default_dir_for(media_type)


def leaf_path(item):
    """Estructura interna (subcarpetas + nombre de archivo) que se crea DENTRO de la
    carpeta base elegida. No incluye la base. Sirve también para la vista previa.
    """
    g = item.__getitem__ if hasattr(item, "__getitem__") else item.get
    media_type = g("media_type")
    ext = os.path.splitext(g("filename"))[1].lower()

    if media_type == "movie":
        title = safe_name(g("chosen_title") or g("detected_title"))
        year = g("chosen_year") or g("detected_year")
        folder_name = f"{title} ({year})" if year else title
        return os.path.join(folder_name, folder_name + ext)

    if media_type == "series":
        title = safe_name(g("chosen_title") or g("detected_title"))
        season = _two(g("season"))
        episode = _two(g("episode"))
        return os.path.join(title, f"Season {season}", f"{title} S{season}E{episode}{ext}")

    if media_type == "music":
        artist = safe_name(g("artist") or "Desconocido")
        album = safe_name(g("album") or "Desconocido")
        title = safe_name(g("detected_title") or os.path.splitext(g("filename"))[0])
        track_raw = g("track_no")
        if track_raw not in (None, ""):
            fname = f"{_two(track_raw)} - {title}{ext}"
        else:
            fname = f"{title}{ext}"
        return os.path.join(artist, album, fname)

    # Desconocido: se deja con su nombre original
    return safe_name(g("filename"))


def build_dest(item):
    """Ruta destino completa = carpeta base elegida (o la por defecto) + leaf_path.

    `item` es una fila de la BD (sqlite3.Row) o un dict.
    """
    g = item.__getitem__ if hasattr(item, "__getitem__") else item.get
    base = g("dest_folder") or default_base(g("media_type"))
    return os.path.join(base, leaf_path(item))


def _find_subtitles(original_path):
    """Busca subtítulos junto al vídeo (mismo nombre base)."""
    folder = os.path.dirname(original_path)
    stem = os.path.splitext(os.path.basename(original_path))[0]
    sub_exts = config.ext_list("subtitle_exts")
    found = []
    if not os.path.isdir(folder):
        return found
    for f in os.listdir(folder):
        p = os.path.join(folder, f)
        if not os.path.isfile(p):
            continue
        name, ext = os.path.splitext(f)
        if ext.lower() in sub_exts and name.startswith(stem):
            found.append(p)
    return found


def unique_path(path):
    """Si `path` ya existe, devuelve una variante ' (2)', ' (3)'… que no exista,
    para no sobrescribir nunca un archivo que ya estaba."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def _g(item, key):
    try:
        return item[key]
    except (KeyError, IndexError, TypeError):
        return None


def _safe_int(value, default=1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _save_image(url, dest_path):
    """Descarga una imagen remota si existe y todavía no hay archivo local."""
    if not url or os.path.exists(dest_path):
        return
    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(r.content)
    except Exception:
        pass


def _season_poster_name(season):
    season = _safe_int(season, 1)
    if season == 0:
        return "season-specials-poster.jpg"
    return f"season{_two(season)}-poster.jpg"


def _write_text_if_missing(path, text):
    if os.path.exists(path):
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _episode_nfo(item, dest, episode_meta, include_thumb):
    series_title = escape(str(_g(item, "chosen_title") or _g(item, "detected_title") or ""))
    season = _safe_int(_g(item, "season"), 1)
    episode = _safe_int(_g(item, "episode"), 1)
    fallback_title = f"{series_title} S{_two(season)}E{_two(episode)}"
    title = escape(str(episode_meta.get("title") or fallback_title))
    overview = escape(str(episode_meta.get("overview") or ""))
    aired = escape(str(episode_meta.get("aired") or ""))
    episode_id = episode_meta.get("tmdb_id") or ""
    thumb_name = os.path.basename(os.path.splitext(dest)[0] + "-thumb.jpg")
    thumb_line = f"  <thumb>{thumb_name}</thumb>\n" if include_thumb else ""
    runtime = episode_meta.get("runtime")
    runtime_line = f"  <runtime>{runtime}</runtime>\n" if runtime else ""
    uniqueid_line = (
        f'  <uniqueid type="tmdb" default="true">{episode_id}</uniqueid>\n'
        if episode_id else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<episodedetails>\n'
        f"  <title>{title}</title>\n"
        f"  <showtitle>{series_title}</showtitle>\n"
        f"  <season>{season}</season>\n"
        f"  <episode>{episode}</episode>\n"
        f"  <plot>{overview}</plot>\n"
        f"  <aired>{aired}</aired>\n"
        f"{thumb_line}"
        f"{runtime_line}"
        f"{uniqueid_line}"
        "</episodedetails>\n"
    )


def _write_episode_metadata(item, dest):
    season = _safe_int(_g(item, "season"), 1)
    episode = _safe_int(_g(item, "episode"), 1)
    episode_meta = tmdb.episode_metadata(_g(item, "tmdb_id"), season, episode)
    nfo_path = os.path.splitext(dest)[0] + ".nfo"
    thumb_path = os.path.splitext(dest)[0] + "-thumb.jpg"
    _save_image(episode_meta.get("still_url"), thumb_path)
    _write_text_if_missing(nfo_path, _episode_nfo(item, dest, episode_meta, os.path.exists(thumb_path)))


def write_metadata(item, dest):
    """Escribe .nfo (con el id de TMDB) y guarda el póster local, para que Jellyfin
    reconozca EXACTO y respete el título elegido. Mejor esfuerzo: nunca rompe el movido."""
    tmdb_id = _g(item, "tmdb_id")
    if not tmdb_id:
        return
    media_type = _g(item, "media_type")
    title = escape(str(_g(item, "chosen_title") or _g(item, "detected_title") or ""))
    year = _g(item, "chosen_year") or _g(item, "detected_year") or ""
    poster = _g(item, "poster_url")
    overview = escape(str(_g(item, "overview") or ""))

    try:
        if media_type == "movie":
            folder = os.path.dirname(dest)
            nfo = os.path.splitext(dest)[0] + ".nfo"
            with open(nfo, "w", encoding="utf-8") as f:
                f.write(
                    f'<?xml version="1.0" encoding="UTF-8"?>\n<movie>\n'
                    f'  <title>{title}</title>\n  <year>{year}</year>\n'
                    f'  <plot>{overview}</plot>\n'
                    f'  <uniqueid type="tmdb" default="true">{tmdb_id}</uniqueid>\n</movie>\n'
                )
            poster_path = os.path.join(folder, "poster.jpg")
            fanart_path = os.path.join(folder, "fanart.jpg")
            clearlogo_path = os.path.join(folder, "clearlogo.png")
            assets = {}
            if not all(os.path.exists(p) for p in (poster_path, fanart_path, clearlogo_path)):
                assets = tmdb.image_assets(media_type, tmdb_id)
            _save_image(assets.get("poster") or poster, poster_path)
            _save_image(assets.get("fanart"), fanart_path)
            _save_image(assets.get("clearlogo"), clearlogo_path)

        elif media_type == "series":
            series_root = os.path.dirname(os.path.dirname(dest))  # …/Título
            nfo = os.path.join(series_root, "tvshow.nfo")
            if not os.path.exists(nfo):
                with open(nfo, "w", encoding="utf-8") as f:
                    f.write(
                        f'<?xml version="1.0" encoding="UTF-8"?>\n<tvshow>\n'
                        f'  <title>{title}</title>\n'
                        f'  <plot>{overview}</plot>\n'
                        f'  <uniqueid type="tmdb" default="true">{tmdb_id}</uniqueid>\n</tvshow>\n'
                    )
            poster_path = os.path.join(series_root, "poster.jpg")
            fanart_path = os.path.join(series_root, "fanart.jpg")
            clearlogo_path = os.path.join(series_root, "clearlogo.png")
            assets = {}
            if not all(os.path.exists(p) for p in (poster_path, fanart_path, clearlogo_path)):
                assets = tmdb.image_assets(media_type, tmdb_id)
            _save_image(assets.get("poster") or poster, poster_path)
            _save_image(assets.get("fanart"), fanart_path)
            _save_image(assets.get("clearlogo"), clearlogo_path)

            season = _safe_int(_g(item, "season"), 1)
            season_poster_path = os.path.join(series_root, _season_poster_name(season))
            if not os.path.exists(season_poster_path):
                season_assets = tmdb.season_assets(tmdb_id, season)
                _save_image(season_assets.get("poster"), season_poster_path)
            _write_episode_metadata(item, dest)

        elif media_type == "music":
            poster = _g(item, "poster_url") or ""
            if poster.startswith("/music-covers/"):
                src = music_meta.cached_cover_path(os.path.basename(poster))
                if src:
                    album_folder = os.path.dirname(dest)
                    ext = os.path.splitext(src)[1].lower() or ".jpg"
                    for name in (f"folder{ext}", f"cover{ext}"):
                        target = os.path.join(album_folder, name)
                        if not os.path.exists(target):
                            try:
                                shutil.copyfile(src, target)
                            except Exception:
                                pass
                            else:
                                break
    except Exception:
        pass


def move_item(item):
    """Mueve el archivo (y subtítulos) a su destino. Devuelve (ok, dest_path, mensaje)."""
    src = item["original_path"]
    if not os.path.exists(src):
        return False, None, f"El archivo de origen ya no existe: {src}"

    dest = build_dest(item)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        return False, None, (
            f"Ya existe en destino: {dest}. No se movio para evitar crear una copia (2)."
        )

    try:
        shutil.move(src, dest)
    except Exception as e:
        return False, None, f"Error al mover: {e}"

    # Mover subtítulos asociados (solo vídeo), junto al vídeo y sin sobrescribir
    if item["media_type"] in ("movie", "series"):
        dest_stem = os.path.splitext(dest)[0]
        for sub in _find_subtitles(src):
            sub_ext = os.path.splitext(sub)[1]
            try:
                shutil.move(sub, unique_path(dest_stem + sub_ext))
            except Exception:
                pass

    # Escribe .nfo + póster para que Jellyfin reconozca exacto (mejor esfuerzo).
    write_metadata(item, dest)

    return True, dest, "Movido correctamente."
