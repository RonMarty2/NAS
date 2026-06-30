"""Comprobaciones ligeras del destino antes de mover."""
import os

from . import config, organizer


def build_dest(item, base):
    return os.path.join(base, organizer.leaf_path(item))


def inspect(item, base, _folder_cache=None):
    """Devuelve si el destino exacto o su carpeta ya existen.

    `_folder_cache` (opcional) memoiza el listado de medios por carpeta durante un
    mismo render: si varios items apuntan a la misma carpeta destino, se lista una
    sola vez en vez de una por item (importa en carpetas grandes como peliculas/).
    """
    dest = build_dest(item, base)
    folder = os.path.dirname(dest)
    exact_exists = os.path.exists(dest)
    folder_exists = os.path.isdir(folder)
    existing_files = []
    source_size = _item_source_size(item)
    dest_size = _path_size(dest) if exact_exists else 0
    size_known = source_size > 0 and dest_size >= 0
    exact_size_matches = bool(exact_exists and size_known and source_size == dest_size)
    exact_size_mismatch = bool(exact_exists and size_known and source_size != dest_size)
    safe_to_delete_pending = bool(exact_exists and exact_size_matches)

    if folder_exists:
        if _folder_cache is not None and folder in _folder_cache:
            existing_files = _folder_cache[folder]
        else:
            existing_files = _media_files_in(folder)
            if _folder_cache is not None:
                _folder_cache[folder] = existing_files

    return {
        "dest_path": dest,
        "folder": folder,
        "exact_exists": exact_exists,
        "source_size_bytes": source_size,
        "dest_size_bytes": dest_size if dest_size >= 0 else 0,
        "size_known": size_known,
        "exact_size_matches": exact_size_matches,
        "exact_size_mismatch": exact_size_mismatch,
        "exact_dest_smaller": bool(exact_size_mismatch and dest_size < source_size),
        "safe_to_delete_pending": safe_to_delete_pending,
        "folder_exists": folder_exists,
        "existing_files": existing_files,
        "has_media_in_folder": bool(existing_files),
    }


def _media_files_in(folder):
    """Hasta 5 nombres de archivos de medios en `folder` (orden alfabético)."""
    try:
        media_exts = config.ext_list("video_exts") | config.ext_list("music_exts")
        found = []
        for name in sorted(os.listdir(folder), key=str.lower):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            if os.path.splitext(name)[1].lower() in media_exts:
                found.append(name)
            if len(found) >= 5:
                break
        return found
    except OSError:
        return []


def inspect_many(items, base):
    cache = {}
    details = [inspect(item, base, _folder_cache=cache) for item in items]
    exact = [d for d in details if d["exact_exists"]]
    folders = [d for d in details if d["has_media_in_folder"] and not d["exact_exists"]]
    unsafe = [d for d in exact if not d["safe_to_delete_pending"]]
    return {
        "details": details,
        "exact_count": len(exact),
        "unsafe_exact_count": len(unsafe),
        "folder_count": len(folders),
        "examples": _examples(exact or folders),
    }


def _examples(details):
    out = []
    for detail in details[:4]:
        if detail["exact_exists"]:
            out.append(os.path.basename(detail["dest_path"]))
        elif detail["existing_files"]:
            out.append(detail["existing_files"][0])
    return out


def _item_source_size(item):
    path = _get(item, "original_path")
    size = _safe_int(_get(item, "size_bytes"))
    if path and os.path.exists(path):
        disk_size = _path_size(path)
        if disk_size > 0:
            return disk_size
    return size


def _path_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def _safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _get(item, key):
    try:
        return item[key]
    except (KeyError, IndexError, TypeError):
        return None
