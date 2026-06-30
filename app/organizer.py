"""Construye las rutas destino con la convención de Jellyfin y mueve los archivos."""
import os
import re
import shutil
import time
import uuid
from xml.sax.saxutils import escape

import requests

from . import config
from .metadata import music as music_meta
from .metadata import tmdb

# Caracteres no válidos en nombres de archivo/carpeta
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_COPYING_TOKEN = ".copying-"
_COPYING_SUFFIX = ".tmp"


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


def cleanup_temp_copies(roots=None, max_age_seconds=6 * 60 * 60, delete_all=False, recursive=True):
    """Borra temporales propios de copias interrumpidas.

    Solo toca archivos ocultos creados por esta app con patrón:
    `.Nombre.ext.copying-<uuid>.tmp`. En arranque se puede usar `delete_all`
    porque no hay una copia activa del proceso anterior.
    """
    roots = roots or _library_roots()
    now = time.time()
    removed = 0
    freed = 0
    errors = 0

    seen_roots = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        root = os.path.abspath(root)
        if root in seen_roots:
            continue
        seen_roots.add(root)
        for path in _iter_copy_temps(root, recursive=recursive):
            try:
                age = now - os.path.getmtime(path)
                if not delete_all and age < max_age_seconds:
                    continue
                size = os.path.getsize(path)
                os.remove(path)
                removed += 1
                freed += size
            except OSError:
                errors += 1

    return {"removed": removed, "freed_bytes": freed, "errors": errors}


def _iter_copy_temps(root, recursive=True):
    if recursive:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if _is_copy_temp_name(name):
                    yield os.path.join(dirpath, name)
        return
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        if _is_copy_temp_name(name) and os.path.isfile(path):
            yield path


def _library_roots():
    roots = {p.strip() for p in config.get("library_roots").split(",") if p.strip()}
    for media_type in ("movie", "series", "music"):
        default = default_base(media_type)
        if default:
            roots.add(default)
    return sorted(roots)


def _is_copy_temp_name(name):
    return name.startswith(".") and _COPYING_TOKEN in name and name.endswith(_COPYING_SUFFIX)


def _replace_or_move(src, dest):
    """Mueve `src` a `dest`; si `dest` ya existe, lo reemplaza.

    Si no se puede renombrar de forma atómica (por ejemplo, entre montajes),
    copia a un temporal oculto en la carpeta destino y solo publica el archivo
    final cuando la copia terminó y el tamaño coincide.
    """
    if os.path.isdir(dest):
        raise IsADirectoryError(f"El destino existe y es una carpeta: {dest}")

    replaced = os.path.exists(dest)
    try:
        os.replace(src, dest)
        return replaced
    except OSError:
        return _copy_to_temp_then_replace(src, dest, replaced)


def _reflink_clone(src, dst):
    """Intenta un clon copy-on-write de btrfs (instantáneo, sin copiar datos).

    En Synology (btrfs) esto permite "mover" entre carpetas compartidas distintas
    casi al instante —igual que File Station— en vez de copiar gigabytes byte a
    byte. Devuelve True si funcionó. Si el sistema de archivos no lo soporta
    (no es btrfs, o no es el mismo volumen), devuelve False y se usa la copia
    normal como respaldo, así que nunca empeora nada."""
    try:
        import fcntl
        FICLONE = 0x40049409  # ioctl de clonado CoW (mismo en arquitecturas comunes)
        with open(src, "rb") as s, open(dst, "wb") as d:
            fcntl.ioctl(d.fileno(), FICLONE, s.fileno())
        return True
    except Exception:
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except OSError:
            pass
        return False


def _copy_to_temp_then_replace(src, dest, replaced):
    tmp_dest = os.path.join(
        os.path.dirname(dest),
        f".{os.path.basename(dest)}{_COPYING_TOKEN}{uuid.uuid4().hex}{_COPYING_SUFFIX}",
    )
    expected_size = os.path.getsize(src)

    # Camino rápido: clon CoW de btrfs (instantáneo). Si no se puede, seguimos
    # con la copia byte a byte de toda la vida.
    if _reflink_clone(src, tmp_dest):
        try:
            if os.path.getsize(tmp_dest) == expected_size:
                shutil.copystat(src, tmp_dest, follow_symlinks=True)
                os.replace(tmp_dest, dest)
                _fsync_parent(dest)
                os.remove(src)
                return replaced
            os.remove(tmp_dest)  # tamaño raro: descarta y cae al respaldo
        except OSError:
            try:
                if os.path.exists(tmp_dest):
                    os.remove(tmp_dest)
            except OSError:
                pass

    _ensure_free_space_for_copy(os.path.dirname(dest), expected_size)
    try:
        with open(src, "rb") as in_f, open(tmp_dest, "wb") as out_f:
            shutil.copyfileobj(in_f, out_f, length=1024 * 1024 * 16)
            out_f.flush()
            os.fsync(out_f.fileno())
        actual_size = os.path.getsize(tmp_dest)
        if actual_size != expected_size:
            raise IOError(
                f"Copia incompleta: {actual_size} bytes copiados de {expected_size}."
            )
        shutil.copystat(src, tmp_dest, follow_symlinks=True)
        os.replace(tmp_dest, dest)
        _fsync_parent(dest)
        os.remove(src)
        return replaced
    except Exception:
        try:
            if os.path.exists(tmp_dest):
                os.remove(tmp_dest)
        except OSError:
            pass
        raise


def _ensure_free_space_for_copy(dest_folder, expected_size):
    """Fallback copy needs a full temporary file before publishing final dest."""
    try:
        free = shutil.disk_usage(dest_folder).free
    except OSError:
        return
    reserve = min(max(256 * 1024 * 1024, expected_size // 20), 2 * 1024 * 1024 * 1024)
    needed = expected_size + reserve
    if free < needed:
        raise IOError(
            "Espacio insuficiente para una copia segura: "
            f"libre {_human_size(free)}, necesario {_human_size(needed)}."
        )


def _human_size(value):
    n = float(value or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = units[0]
    for unit in units:
        if n < 1024 or unit == units[-1]:
            break
        n /= 1024
    if unit in ("B", "KB"):
        return f"{int(n)} {unit}"
    return f"{n:.1f} {unit}"


def _fsync_parent(path):
    try:
        fd = os.open(os.path.dirname(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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


def move_item(item, on_existing="error"):
    """Mueve el archivo (y subtítulos) a su destino. Devuelve (ok, dest_path, mensaje)."""
    src = item["original_path"]
    if not os.path.exists(src):
        return False, None, f"El archivo de origen ya no existe: {src}"

    dest = build_dest(item)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    except OSError as e:
        return False, None, f"No pude crear la carpeta destino: {e}"
    if os.path.exists(dest):
        if on_existing == "keep_both":
            dest = unique_path(dest)
        elif on_existing != "replace":
            return False, None, f"Ya existe en destino: {dest}. Compara versiones antes de decidir."

    try:
        replaced = _replace_or_move(src, dest)
    except Exception as e:
        return False, None, f"Error al mover: {e}"

    # Mover subtítulos asociados (solo vídeo), junto al vídeo.
    if item["media_type"] in ("movie", "series"):
        dest_stem = os.path.splitext(dest)[0]
        used_sub_targets = set()
        for sub in _find_subtitles(src):
            sub_ext = os.path.splitext(sub)[1]
            sub_dest = dest_stem + sub_ext
            if sub_dest in used_sub_targets:
                sub_dest = unique_path(sub_dest)
            try:
                if on_existing == "replace":
                    _replace_or_move(sub, sub_dest)
                    used_sub_targets.add(sub_dest)
                else:
                    final_sub_dest = unique_path(sub_dest)
                    _replace_or_move(sub, final_sub_dest)
                    used_sub_targets.add(final_sub_dest)
            except Exception:
                pass

    # Escribe .nfo + póster para que Jellyfin reconozca exacto (mejor esfuerzo).
    write_metadata(item, dest)

    if replaced:
        return True, dest, "Movido correctamente. Se reemplazo el archivo existente."
    if on_existing == "keep_both":
        return True, dest, "Movido correctamente. Se conservaron ambas versiones."
    return True, dest, "Movido correctamente."
