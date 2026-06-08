"""Vigila la carpeta de descargas y procesa archivos nuevos.

Estrategia: sondeo periódico (robusto frente a descargas en curso) + función de
escaneo manual para el botón "Buscar ahora". Cuando aparece un archivo nuevo y
estable, lo identifica y busca sus metadatos, dejándolo listo para revisión.
"""
import os
import threading
import time

from . import config, db, identify
from .metadata import music as music_meta
from .metadata import tmdb

# Extensiones de archivos incompletos que NUNCA debemos tocar.
INCOMPLETE_EXTS = {".part", ".tmp", ".!ut", ".crdownload", ".download"}
# Antigüedad mínima (segundos) para considerar un archivo "estable" (terminado).
STABLE_AGE = 60
POLL_INTERVAL = 30

_stop = threading.Event()


def _is_stable(path):
    """True si el archivo no parece estar descargándose todavía."""
    try:
        if time.time() - os.path.getmtime(path) < STABLE_AGE:
            return False
    except OSError:
        return False
    return True


def _enrich(item_id, path, ident):
    """Rellena metadatos según el tipo y actualiza el item en la BD."""
    media_type = ident["media_type"]
    fields = {
        "media_type": media_type,
        "detected_title": ident.get("title"),
        "detected_year": ident.get("year"),
        "season": ident.get("season"),
        "episode": ident.get("episode"),
    }

    if media_type in ("movie", "series"):
        match = tmdb.best_match(ident.get("title"), media_type, ident.get("year"))
        if match:
            fields.update({
                "tmdb_id": match["tmdb_id"],
                "chosen_title": match["title"],
                "chosen_year": match["year"],
                "poster_url": match["poster_url"],
                "overview": match["overview"],
            })
        else:
            fields["chosen_title"] = ident.get("title")
            fields["chosen_year"] = ident.get("year")

    elif media_type == "music":
        m = music_meta.identify_music(path)
        fields.update({
            "artist": m.get("artist") or "Desconocido",
            "album": m.get("album") or "Desconocido",
            "track_no": m.get("track"),
            "detected_title": m.get("title"),   # título de la canción
            "detected_year": m.get("year"),
        })

    db.update_item(item_id, **fields)


def _process_file(path):
    """Procesa un archivo concreto: lo añade y lo enriquece si es nuevo."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()

    if ext in INCOMPLETE_EXTS:
        return
    kind = identify.classify_extension(path)
    if kind not in ("video", "music"):
        return  # ignoramos subtítulos sueltos, basura, etc.

    try:
        size = os.path.getsize(path)
    except OSError:
        return
    min_bytes = int(config.get("min_size_mb") or 0) * 1024 * 1024
    if kind == "video" and size < min_bytes:
        return
    if not _is_stable(path):
        return

    item_id = db.add_item(path, name, size)
    if item_id is None:
        return  # ya existía
    ident = identify.identify(path)
    try:
        _enrich(item_id, path, ident)
    except Exception as e:  # nunca dejar el item a medias por un fallo de red
        db.update_item(item_id, error=str(e))


def reenrich_pending():
    """Vuelve a buscar metadatos para los pendientes de película/serie que aún no
    tienen coincidencia en TMDB. Útil cuando se acaba de poner la API key: los que
    ya estaban en la lista consiguen su póster/descripción sin tener que volver a
    descargarlos. No toca los que ya tienen coincidencia ni los elegidos a mano."""
    if not tmdb.configured():
        return
    for it in db.list_items(status="pending"):
        if it["media_type"] not in ("movie", "series"):
            continue
        if it["tmdb_id"]:
            continue  # ya reconocido o elegido manualmente
        query = it["chosen_title"] or it["detected_title"]
        if not query:
            continue
        try:
            match = tmdb.best_match(query, it["media_type"], it["detected_year"])
        except Exception:
            continue
        if match:
            db.update_item(
                it["id"], tmdb_id=match["tmdb_id"], chosen_title=match["title"],
                chosen_year=match["year"], poster_url=match["poster_url"],
                overview=match["overview"],
            )


def scan_once():
    """Recorre la carpeta de descargas una vez. Devuelve nº de archivos vistos."""
    root = config.get("downloads_dir")
    if not root or not os.path.isdir(root):
        return 0
    seen = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            _process_file(os.path.join(dirpath, f))
            seen += 1
    # Reintenta metadatos de lo que quedó pendiente sin reconocer.
    reenrich_pending()
    return seen


def _loop():
    while not _stop.is_set():
        try:
            db.init_db()
            scan_once()
        except Exception:
            pass
        _stop.wait(POLL_INTERVAL)


def start_background():
    """Lanza el hilo de vigilancia en segundo plano."""
    t = threading.Thread(target=_loop, name="nas-watcher", daemon=True)
    t.start()
    return t


def stop():
    _stop.set()
