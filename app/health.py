"""Salud de la biblioteca: detecta archivos rotos/corruptos y huérfanos.

Corre bajo demanda (botón), en segundo plano, por tandas para no saturar el
NAS. No borra nada automáticamente: solo reporta para que el usuario decida.
"""
import json
import os
import time

from . import config, db, filemeta

STATUS_KEY = "health_status"
RESULT_KEY = "health_result"
SKIP_DIR_NAMES = {"@eadir", "#recycle", "@tmp", ".trash", "$recycle.bin"}


def status():
    raw = db.get_setting(STATUS_KEY, "")
    if not raw:
        return {"visible": False, "running": False, "message": ""}
    try:
        data = json.loads(raw)
    except Exception:
        return {"visible": False, "running": False, "message": ""}
    data["visible"] = bool(data.get("running") or data.get("message"))
    return data


def set_status(data):
    db.set_setting(STATUS_KEY, json.dumps(data or {}, ensure_ascii=False))


def last_result():
    raw = db.get_setting(RESULT_KEY, "")
    if not raw:
        return {"checked_at": None, "broken": [], "orphans": []}
    try:
        return json.loads(raw)
    except Exception:
        return {"checked_at": None, "broken": [], "orphans": []}


def _set_result(data):
    db.set_setting(RESULT_KEY, json.dumps(data or {}, ensure_ascii=False))


def _library_roots():
    roots = {p.strip() for p in config.get("library_roots").split(",") if p.strip()}
    for media_type in ("movie", "series", "music"):
        default = config.default_dir_for(media_type)
        if default:
            roots.add(default)
    return sorted(r for r in roots if r and os.path.isdir(r))


def _skip_dir(name):
    low = (name or "").lower()
    return low in SKIP_DIR_NAMES or low.startswith("@eadir")


def _iter_all_files(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not _skip_dir(d)]
        for name in files:
            yield os.path.join(dirpath, name)


def suggested_roots():
    """Carpetas sugeridas para el selector (bibliotecas configuradas)."""
    return _library_roots()


def _within_roots(path):
    try:
        rp = os.path.realpath(path)
    except (OSError, TypeError):
        return False
    for root in _library_roots():
        try:
            rr = os.path.realpath(root)
        except (OSError, TypeError):
            continue
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def run_scan(progress=None, folder=None):
    """Recorre la(s) biblioteca(s) buscando: videos corruptos/ilegibles y
    archivos .nfo/poster/fanart huérfanos (sin video asociado en la carpeta).

    Si `folder` se indica, analiza SOLO esa carpeta (debe estar dentro de las
    bibliotecas configuradas); si no, analiza todas."""
    video_exts = config.ext_list("video_exts")
    folder = (folder or "").strip()
    if folder:
        if not _within_roots(folder) or not os.path.isdir(folder):
            return {
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "broken": [], "orphans": [], "videos_checked": 0,
                "error": "Esa carpeta no existe o está fuera de tus bibliotecas configuradas.",
            }
        roots = [folder]
    else:
        roots = _library_roots()
    all_paths = []
    for root in roots:
        all_paths.extend(_iter_all_files(root))

    broken = []
    checked = 0
    total_videos = sum(1 for p in all_paths if os.path.splitext(p)[1].lower() in video_exts)
    for path in all_paths:
        ext = os.path.splitext(path)[1].lower()
        if ext not in video_exts:
            continue
        checked += 1
        if progress and checked % 5 == 0:
            progress({"done": checked, "total": total_videos, "current": os.path.basename(path)})
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size <= 0:
            broken.append({"path": path, "reason": "El archivo pesa 0 bytes."})
            continue
        readable = filemeta.media_is_readable(path)
        if readable is False:
            broken.append({"path": path, "reason": "ffprobe no pudo leer video/audio del archivo."})

    # Huérfanos: .nfo/poster/fanart/clearlogo sin ningún video en su misma carpeta.
    orphans = []
    by_dir = {}
    for path in all_paths:
        by_dir.setdefault(os.path.dirname(path), []).append(path)
    aux_names = {"poster.jpg", "fanart.jpg", "clearlogo.png", "tvshow.nfo"}
    for folder, paths in by_dir.items():
        has_video = any(os.path.splitext(p)[1].lower() in video_exts for p in paths)
        if has_video:
            continue
        for p in paths:
            name = os.path.basename(p).lower()
            if name in aux_names or name.endswith(".nfo"):
                orphans.append({"path": p, "reason": f"Sin ningún video en la carpeta: {folder}"})

    result = {
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "broken": broken,
        "orphans": orphans,
        "videos_checked": checked,
    }
    _set_result(result)
    return result


def delete_broken(path):
    """Borra un archivo reportado como roto/huérfano. Valida que esté dentro de
    las bibliotecas configuradas antes de tocar el disco."""
    if not path or not _within_roots(path):
        return False
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        return False
    result = last_result()
    result["broken"] = [b for b in result.get("broken", []) if b["path"] != path]
    result["orphans"] = [o for o in result.get("orphans", []) if o["path"] != path]
    _set_result(result)
    return True
