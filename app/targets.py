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
    return {
        "details": details,
        "exact_count": len(exact),
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
