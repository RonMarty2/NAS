"""Vigila la carpeta de descargas y procesa archivos nuevos.

Estrategia: sondeo periódico (robusto frente a descargas en curso) + función de
escaneo manual para el botón "Buscar ahora". Cuando aparece un archivo nuevo y
estable, lo identifica y busca sus metadatos, dejándolo listo para revisión.
"""
import os
import threading
import time
from datetime import datetime

from . import config, db, filemeta, identify, notify, organizer
from .metadata import music as music_meta
from .metadata import tmdb

# Extensiones de archivos incompletos que NUNCA debemos tocar.
INCOMPLETE_EXTS = {".part", ".tmp", ".!ut", ".crdownload", ".download"}
# Antigüedad mínima (segundos) para considerar un archivo "estable" (terminado).
STABLE_AGE = 60
POLL_INTERVAL = 30

_stop = threading.Event()


def _probe_enabled():
    return str(config.get("probe_media_info")).strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def _is_stable(path):
    """True si el archivo no parece estar descargándose todavía."""
    try:
        if time.time() - os.path.getmtime(path) < STABLE_AGE:
            return False
    except OSError:
        return False
    return True


def _is_junk(name):
    """True si el nombre indica basura (sample, activador, crack, trailer, etc.)."""
    low = name.lower()
    patterns = [p.strip().lower() for p in config.get("junk_patterns").split(",") if p.strip()]
    return any(pat in low for pat in patterns)


def _enrich(item_id, path, ident):
    """Rellena metadatos según el tipo y actualiza el item en la BD."""
    media_type = ident["media_type"]
    fields = {
        "media_type": media_type,
        "detected_title": ident.get("title"),
        "detected_year": ident.get("year"),
        "season": ident.get("season"),
        "episode": ident.get("episode"),
        "quality": ident.get("quality"),
        "langs": ident.get("langs"),
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
        if m.get("cover_url"):
            fields["poster_url"] = m["cover_url"]
            fields["cover_attempts"] = 0

    db.update_item(item_id, **fields)


def _process_file(path):
    """Procesa un archivo concreto: lo añade y lo enriquece si es nuevo.

    Devuelve el id si se añadió uno nuevo, o None."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()

    if ext in INCOMPLETE_EXTS:
        return None
    kind = identify.classify_extension(path)
    if kind not in ("video", "music"):
        return None  # ignoramos subtítulos sueltos, basura, etc.
    if _is_junk(name):
        return None  # sample, activador, crack, trailer, etc.

    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    min_bytes = int(config.get("min_size_mb") or 0) * 1024 * 1024
    if kind == "video" and size < min_bytes:
        return None
    if not _is_stable(path):
        return None

    item_id = db.add_item(path, name, size)
    if item_id is None:
        return None  # ya existía
    try:
        info = filemeta.inspect_file(path, name, size, allow_probe=_probe_enabled())
        db.update_item(item_id, media_info=filemeta.to_json(info))
    except Exception:
        pass
    ident = identify.identify(path)
    try:
        _enrich(item_id, path, ident)
    except Exception as e:  # nunca dejar el item a medias por un fallo de red
        db.update_item(item_id, error=str(e))
    return item_id


def backfill_tech():
    """Rellena calidad/idioma de los pendientes que aún no los tengan (p.ej. items
    añadidos antes de esta función). Se deduce del nombre, no descarga nada."""
    for it in db.list_items(status="pending"):
        if it["media_type"] not in ("movie", "series"):
            continue
        if it["quality"] is not None:
            continue  # ya calculado
        quality, langs = identify.tech_info(it["filename"])
        db.update_item(it["id"], quality=quality, langs=langs)


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
        # No reconsultar TMDB para siempre: tras 3 intentos fallidos lo dejamos.
        # (Al guardar ajustes/poner la API key se reinicia el contador.)
        if (it["match_attempts"] or 0) >= 3:
            continue
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
        else:
            db.update_item(it["id"], match_attempts=(it["match_attempts"] or 0) + 1)


def reidentify(item_id, forced_type):
    """Vuelve a deducir datos del nombre y a buscar en TMDB cuando el usuario
    cambia el tipo de un item (p.ej. de Película a Serie). Corre en segundo plano."""
    item = db.get_item(item_id)
    if not item:
        return
    ident = identify.identify(item["original_path"])
    ident["media_type"] = forced_type  # respetamos la elección del usuario
    # Limpiamos la coincidencia anterior para que se vuelva a buscar bien.
    db.update_item(item_id, tmdb_id=None, chosen_title=None, chosen_year=None,
                   poster_url=None, overview=None, match_attempts=0)
    try:
        _enrich(item_id, item["original_path"], ident)
    except Exception as e:
        db.update_item(item_id, error=str(e))


def refresh_pending_file_info():
    """Completa peso/calidad/idioma para pendientes creados antes de esta versión."""
    for it in db.list_items(status="pending"):
        if it["media_info"]:
            continue
        path = it["original_path"]
        if not os.path.exists(path):
            continue
        try:
            size = os.path.getsize(path)
            info = filemeta.inspect_file(path, it["filename"], size, allow_probe=_probe_enabled())
            db.update_item(it["id"], size_bytes=size, media_info=filemeta.to_json(info))
        except Exception:
            continue


def refresh_music_cover(item_id):
    """Try to fill the cover art for a pending music item."""
    item = db.get_item(item_id)
    if not item or item["media_type"] != "music":
        return False
    if item["poster_url"]:
        return True
    if (item["cover_attempts"] or 0) >= 3:
        return False
    artist = item["artist"] if _meaningful_text(item["artist"]) else None
    album = item["album"] if _meaningful_text(item["album"]) else None
    title = item["detected_title"] or item["filename"]
    try:
        cover_url = music_meta.cover_url_for_path(
            item["original_path"], artist=artist, album=album, title=title,
        )
    except Exception:
        cover_url = None
    if cover_url:
        db.update_item(item_id, poster_url=cover_url, cover_attempts=0)
        return True
    db.update_item(item_id, cover_attempts=(item["cover_attempts"] or 0) + 1)
    return False


def refresh_pending_music_covers():
    """Completa la portada de canciones/albumes pendientes cuando falta."""
    for it in db.list_items(status="pending", media_type="music"):
        if it["poster_url"]:
            continue
        if (it["cover_attempts"] or 0) >= 3:
            continue
        refresh_music_cover(it["id"])


def _meaningful_text(value):
    if value is None:
        return False
    text = str(value).strip().lower()
    return bool(text) and text not in {"desconocido", "unknown", "n/a", "none", "-"}


def cleanup_missing_pending():
    """Limpia pendientes cuyo archivo de origen ya no existe en disco.

    Si el destino ya existe, primero se marca como movido. Si tampoco hay
    destino, se borra el registro huérfano.
    """
    removed = 0
    reconciled, _removed = reconcile_pending_moves()
    removed += _removed
    for it in db.list_items(status="pending"):
        path = it["original_path"]
        if path and not os.path.exists(path):
            db.delete_item(it["id"])
            removed += 1
    return removed


def reconcile_pending_moves():
    """Repara movimientos que quedaron hechos en disco pero no en la BD.

    Si el NAS se apaga justo después de mover el archivo pero antes de guardar
    status='done', el origen desaparece y el destino ya existe. En ese caso no
    hay conflicto real ni copia extra: marcamos el item como movido.
    """
    reconciled = 0
    removed = 0
    items = db.list_items(status="processing") + db.list_items(status="pending")
    seen = set()
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        src = it["original_path"]
        if src and os.path.exists(src):
            continue
        dest = it["dest_path"] or _expected_dest(it)
        if dest and os.path.exists(dest):
            db.update_item(
                it["id"],
                status="done",
                dest_path=dest,
                processed_at=_now(),
                error=None,
            )
            reconciled += 1
        elif it["status"] == "pending":
            db.delete_item(it["id"])
            removed += 1
    return reconciled, removed


def _expected_dest(item):
    try:
        return organizer.build_dest(item)
    except Exception:
        return None


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def scan_once():
    """Recorre la carpeta de descargas una vez. Devuelve nº de archivos vistos."""
    root = config.get("downloads_dir")
    if not root or not os.path.isdir(root):
        return 0
    reconcile_pending_moves()
    cleanup_missing_pending()
    seen = 0
    nuevos = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if _process_file(os.path.join(dirpath, f)) is not None:
                nuevos += 1
            seen += 1
    # Reintenta metadatos de lo que quedó pendiente sin reconocer.
    reenrich_pending()
    refresh_pending_file_info()
    refresh_pending_music_covers()
    # Avisa si llegaron descargas nuevas para revisar.
    if nuevos:
        plural = "s" if nuevos != 1 else ""
        notify.notify(
            "NAS Organizer",
            f"Llegaron {nuevos} archivo{plural} nuevo{plural} para revisar.",
            config.get("app_url") or None,
        )
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
