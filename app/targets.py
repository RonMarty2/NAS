"""Comprobaciones ligeras del destino antes de mover."""
import os

from . import config, organizer


def build_dest(item, base):
    return os.path.join(base, organizer.leaf_path(item))


def inspect(item, base):
    """Devuelve si el destino exacto o su carpeta ya existen."""
    dest = build_dest(item, base)
    folder = os.path.dirname(dest)
    exact_exists = os.path.exists(dest)
    folder_exists = os.path.isdir(folder)
    existing_files = []

    if folder_exists:
        try:
            video_exts = config.ext_list("video_exts")
            music_exts = config.ext_list("music_exts")
            media_exts = video_exts | music_exts
            for name in sorted(os.listdir(folder), key=str.lower):
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext in media_exts:
                    existing_files.append(name)
                if len(existing_files) >= 5:
                    break
        except OSError:
            existing_files = []

    return {
        "dest_path": dest,
        "folder": folder,
        "exact_exists": exact_exists,
        "folder_exists": folder_exists,
        "existing_files": existing_files,
        "has_media_in_folder": bool(existing_files),
    }


def inspect_many(items, base):
    details = [inspect(item, base) for item in items]
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
