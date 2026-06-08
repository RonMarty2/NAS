"""Listado y creación de carpetas destino dentro de las raíces de biblioteca.

Sirve para que el usuario elija a mano dónde mover cada descarga (desplegable),
manteniéndose siempre dentro de las carpetas permitidas por seguridad.
"""
import os

from . import config


def roots():
    return [r.strip() for r in config.get("library_roots").split(",") if r.strip()]


def within_roots(path):
    """True si `path` está dentro de alguna raíz de biblioteca (evita escapes)."""
    rp = os.path.realpath(path)
    for root in roots():
        rr = os.path.realpath(root)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def _walk(current, depth, max_depth, out):
    if depth > max_depth:
        return
    try:
        entries = sorted(os.listdir(current), key=str.lower)
    except OSError:
        return
    for e in entries:
        p = os.path.join(current, e)
        if os.path.isdir(p) and not e.startswith("."):
            out.append(p)
            _walk(p, depth + 1, max_depth, out)


def list_candidates(max_depth=2):
    """Carpetas candidatas (categorías) bajo las raíces, hasta `max_depth` niveles.

    Devuelve lista de dicts {path, label, depth} para pintar el desplegable
    con sangría según la profundidad.
    """
    out_paths = []
    for root in roots():
        if not os.path.isdir(root):
            continue
        out_paths.append(root)
        _walk(root, 1, max_depth, out_paths)

    items = []
    for p in out_paths:
        # Profundidad relativa a su raíz, para sangrar visualmente.
        depth = 0
        for root in roots():
            rr = os.path.realpath(root)
            rp = os.path.realpath(p)
            if rp == rr:
                depth = 0
                break
            if rp.startswith(rr + os.sep):
                depth = rp[len(rr) + 1:].count(os.sep) + 1
                break
        items.append({"path": p, "label": os.path.basename(p) or p, "depth": depth})
    return items


def ensure_folder(base, new_sub=""):
    """Crea (si hace falta) base[/new_sub] dentro de las raíces. Devuelve la ruta o None
    si quedaría fuera de las raíces permitidas."""
    new_sub = (new_sub or "").strip().strip("/\\")
    target = os.path.join(base, new_sub) if new_sub else base
    if not within_roots(target):
        return None
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        return None
    return target


def browse(path=""):
    """Navegación por carpetas para el árbol del navegador web.

    Devuelve la carpeta actual, su carpeta padre (si sigue dentro de las raíces),
    las raíces disponibles y las subcarpetas inmediatas. Permite un navegador
    expandible/contraíble en lugar de un desplegable gigante.
    """
    rts = roots()
    roots_entries = [{"name": os.path.basename(r) or r, "path": r} for r in rts]

    # Si la ruta no es válida, arrancamos en la primera raíz.
    if not path or not within_roots(path) or not os.path.isdir(path):
        path = rts[0] if rts else ""

    parent = None
    if path:
        rp = os.path.realpath(path)
        is_root = any(os.path.realpath(r) == rp for r in rts)
        if not is_root:
            cand = os.path.dirname(path)
            if within_roots(cand):
                parent = cand

    items = []
    if path and os.path.isdir(path):
        try:
            for e in sorted(os.listdir(path), key=str.lower):
                p = os.path.join(path, e)
                if os.path.isdir(p) and not e.startswith("."):
                    has_children = False
                    try:
                        has_children = any(
                            os.path.isdir(os.path.join(p, x)) and not x.startswith(".")
                            for x in os.listdir(p)
                        )
                    except OSError:
                        pass
                    items.append({"name": e, "path": p, "has_children": has_children})
        except OSError:
            pass

    return {"path": path, "parent": parent, "roots": roots_entries, "items": items}
