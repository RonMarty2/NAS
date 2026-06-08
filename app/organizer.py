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


def build_dest(item):
    """Calcula la ruta destino completa según el tipo de medio y los datos elegidos.

    `item` es una fila de la BD (sqlite3.Row) o un dict.
    Devuelve la ruta absoluta destino (incluido el nombre de archivo).
    """
    g = item.__getitem__ if hasattr(item, "__getitem__") else item.get
    media_type = g("media_type")
    ext = os.path.splitext(g("filename"))[1].lower()

    if media_type == "movie":
        title = safe_name(g("chosen_title") or g("detected_title"))
        year = g("chosen_year") or g("detected_year")
        folder_name = f"{title} ({year})" if year else title
        base = os.path.join(config.get("movies_dir"), folder_name)
        fname = folder_name + ext
        return os.path.join(base, fname)

    if media_type == "series":
        title = safe_name(g("chosen_title") or g("detected_title"))
        season = _two(g("season"))
        episode = _two(g("episode"))
        base = os.path.join(config.get("series_dir"), title, f"Season {season}")
        fname = f"{title} S{season}E{episode}{ext}"
        return os.path.join(base, fname)

    if media_type == "music":
        artist = safe_name(g("detected_title") and None)  # se rellena abajo
        # Para música usamos campos guardados en chosen_title="Artista - Álbum"
        # pero preferimos columnas dedicadas si existen.
        artist = safe_name(g("chosen_title") or "Desconocido")
        album = safe_name(g("overview") or "Desconocido")  # reutilizamos overview para álbum
        track = _two(g("episode"))  # reutilizamos episode para nº de pista
        title = safe_name(g("detected_title") or os.path.splitext(g("filename"))[0])
        base = os.path.join(config.get("music_dir"), artist, album)
        fname = f"{track} - {title}{ext}"
        return os.path.join(base, fname)

    # Desconocido: lo dejamos en una subcarpeta "Sin clasificar" dentro de películas
    base = os.path.join(config.get("movies_dir"), "Sin clasificar")
    return os.path.join(base, safe_name(g("filename")))


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


def move_item(item):
    """Mueve el archivo (y subtítulos) a su destino. Devuelve (ok, dest_path, mensaje)."""
    src = item["original_path"]
    if not os.path.exists(src):
        return False, None, f"El archivo de origen ya no existe: {src}"

    dest = build_dest(item)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    try:
        shutil.move(src, dest)
    except Exception as e:
        return False, None, f"Error al mover: {e}"

    # Mover subtítulos asociados (solo vídeo)
    if item["media_type"] in ("movie", "series"):
        dest_stem = os.path.splitext(dest)[0]
        for sub in _find_subtitles(src):
            sub_ext = os.path.splitext(sub)[1]
            try:
                shutil.move(sub, dest_stem + sub_ext)
            except Exception:
                pass

    return True, dest, "Movido correctamente."
