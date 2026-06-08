"""Construye las rutas destino con la convención de Jellyfin y mueve los archivos."""
import os
import re
import shutil

from . import config

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


def move_item(item):
    """Mueve el archivo (y subtítulos) a su destino. Devuelve (ok, dest_path, mensaje)."""
    src = item["original_path"]
    if not os.path.exists(src):
        return False, None, f"El archivo de origen ya no existe: {src}"

    dest = build_dest(item)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # Protección anti-sobrescritura: si ya hay uno con ese nombre, no lo pisamos.
    dest = unique_path(dest)

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

    return True, dest, "Movido correctamente."
