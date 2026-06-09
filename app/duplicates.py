"""Detección y borrado seguro de duplicados pendientes."""
import hashlib
import os

from . import db, filemeta, organizer


def analyze(items):
    """Marca duplicados posibles y exactos entre los items visibles.

    Posible duplicado: terminarían en el mismo leaf_path.
    Candidato exacto: mismo destino sugerido y mismo tamaño. El SHA-256 completo
    se calcula solo al borrar, para no leer GB cada vez que se abre la pantalla.
    """
    items = list(items)
    result = {it["id"]: _empty() for it in items}

    by_leaf = {}
    for item in items:
        if item["status"] != "pending":
            continue
        key = _leaf_key(item)
        if key:
            by_leaf.setdefault(key, []).append(item)

    for key, group in by_leaf.items():
        if len(group) < 2:
            continue
        for item in group:
            result[item["id"]]["similar_count"] = len(group)

        by_size = {}
        for item in group:
            size = _current_size(item)
            if size > 0:
                by_size.setdefault(size, []).append(item)

        for size_group in by_size.values():
            if len(size_group) < 2:
                continue
            for item in size_group:
                result[item["id"]]["same_size_count"] = len(size_group)

            by_hash = {}
            for item in size_group:
                digest = _stored_valid_hash(item)
                if digest:
                    by_hash.setdefault(digest, []).append(item)
            for digest, exact_group in by_hash.items():
                if len(exact_group) < 2:
                    continue
                for item in exact_group:
                    result[item["id"]].update({
                        "exact_count": len(exact_group),
                        "hash_short": digest[:12],
                    })

    return result


def comparison_groups(items):
    """Agrupa duplicados para mostrarlos juntos en la UI."""
    groups = []
    by_leaf = {}
    for item in items:
        if item["status"] != "pending":
            continue
        key = _leaf_key(item)
        if key:
            by_leaf.setdefault(key, []).append(item)

    for _key, group in by_leaf.items():
        if len(group) < 2:
            continue
        sizes = {}
        entries = []
        for item in group:
            size = _current_size(item)
            sizes[size] = sizes.get(size, 0) + 1
            entries.append({
                "id": item["id"],
                "filename": item["filename"],
                "original_path": item["original_path"],
                "status": item["status"],
                "size": size,
                "error": item["error"],
            })
        try:
            target = organizer.leaf_path(group[0])
        except Exception:
            target = group[0]["filename"]
        groups.append({
            "target": target.replace("\\", "/"),
            "target_name": os.path.splitext(os.path.basename(target))[0],
            "count": len(group),
            "same_size": any(count > 1 and size > 0 for size, count in sizes.items()),
            "entries": entries,
        })

    groups.sort(key=lambda g: g["target"].lower())
    return groups


def ensure_hash(item, force=False):
    """Calcula SHA-256 solo si hace falta y lo persiste junto al tamaño usado."""
    path = item["original_path"]
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, 0

    stored_hash = _get(item, "file_hash")
    stored_size = _get(item, "file_hash_size")
    if stored_hash and stored_size == size and not force:
        return stored_hash, size

    digest = _sha256(path)
    db.update_item(item["id"], file_hash=digest, file_hash_size=size, size_bytes=size)
    return digest, size


def delete_exact_duplicate(item_id):
    """Borra un item solo si queda otro archivo idéntico verificado."""
    item = db.get_item(item_id)
    if not item:
        return False, "El archivo ya no está en la lista."
    if item["status"] != "pending":
        return False, "No se puede borrar mientras se está moviendo."
    if not os.path.exists(item["original_path"]):
        db.delete_item(item_id)
        return True, "El registro se eliminó porque el archivo ya no existía."

    target_hash, target_size = ensure_hash(item, force=True)
    if not target_hash:
        return False, "No pude calcular el hash del archivo."

    key = _leaf_key(item)
    survivors = []
    for other in db.list_items(status="pending", media_type=item["media_type"]):
        if other["id"] == item_id or _leaf_key(other) != key:
            continue
        if not os.path.exists(other["original_path"]):
            continue
        other_hash, other_size = ensure_hash(other, force=True)
        if other_hash == target_hash and other_size == target_size:
            survivors.append(other)

    if not survivors:
        return False, "No queda otro archivo idéntico verificado; no se borró nada."

    readable = filemeta.media_is_readable(survivors[0]["original_path"])
    if readable is False:
        return False, "El duplicado que quedaría no pasó la validación básica de lectura."

    try:
        os.remove(item["original_path"])
    except OSError as exc:
        return False, f"No pude borrar el archivo: {exc}"

    db.delete_item(item_id)
    return True, "Duplicado exacto borrado."


def delete_all_exact_duplicates(item_ids):
    """Borra EN LOTE los duplicados exactos entre los items dados.

    Para cada grupo que terminaría en el mismo destino, calcula el SHA-256 (una
    vez, cacheado) y, dentro de cada conjunto idéntico (mismo tamaño y hash),
    CONSERVA una copia legible y borra las demás. Nunca borra si no queda una
    copia idéntica y legible. Devuelve (borrados, mensaje)."""
    candidates = []
    for item_id in item_ids:
        item = db.get_item(item_id)
        if item and item["status"] == "pending" and os.path.exists(item["original_path"]):
            candidates.append(item)

    by_leaf = {}
    for item in candidates:
        by_leaf.setdefault(_leaf_key(item), []).append(item)

    deleted = 0
    for _key, group in by_leaf.items():
        if len(group) < 2:
            continue
        by_hash = {}
        for item in group:
            digest, size = ensure_hash(item)
            if digest:
                by_hash.setdefault((digest, size), []).append(item)

        for _hs, identical in by_hash.items():
            if len(identical) < 2:
                continue
            # Conservamos la primera copia que pase la validación de lectura.
            keeper = None
            for item in identical:
                if filemeta.media_is_readable(item["original_path"]) is not False:
                    keeper = item
                    break
            if keeper is None:
                continue  # ninguna legible: demasiado arriesgado, no tocamos nada
            for victim in identical:
                if victim["id"] == keeper["id"]:
                    continue
                if not os.path.exists(keeper["original_path"]):
                    break  # si el que conservamos desapareció, paramos por seguridad
                try:
                    os.remove(victim["original_path"])
                    db.delete_item(victim["id"])
                    deleted += 1
                except OSError:
                    pass

    if deleted:
        return deleted, f"Se borraron {deleted} duplicado(s) idéntico(s)."
    return 0, "No se encontraron duplicados idénticos verificados para borrar."


def _empty():
    return {
        "similar_count": 0,
        "same_size_count": 0,
        "exact_count": 0,
        "hash_short": "",
        "can_delete_exact": False,
    }


def _leaf_key(item):
    try:
        leaf = organizer.leaf_path(item)
    except Exception:
        leaf = item["filename"]
    return (leaf or "").replace("\\", "/").strip().lower()


def _current_size(item):
    try:
        return os.path.getsize(item["original_path"])
    except OSError:
        return _get(item, "size_bytes") or 0


def _stored_valid_hash(item):
    digest = _get(item, "file_hash")
    size = _get(item, "file_hash_size")
    if not digest or not size:
        return None
    return digest if size == _current_size(item) else None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024 * 4), b""):
            h.update(chunk)
    return h.hexdigest()


def _get(item, key):
    try:
        return item[key]
    except (KeyError, IndexError, TypeError):
        return None
