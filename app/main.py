"""Aplicación web (FastAPI) — bandeja de revisión de descargas para Jellyfin."""
import json
import html
import os
import queue
import threading
import time
from typing import List
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import catalog, config, db, duplicates, filemeta, folders, jellyfin, organizer, targets, watcher
from .metadata import music as music_meta
from .metadata import tmdb

BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

app = FastAPI(title="NAS Organizer")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

TABS = [
    ("movie", "Películas"),
    ("series", "Series"),
    ("music", "Música"),
]

DEDUP_STATUS_KEY = "dedup_status"
DEDUP_LOCK = threading.Lock()
DELETE_DUP_STATUS_KEY = "delete_dup_status"
DELETE_DUP_LOCK = threading.Lock()
SCAN_STATUS_KEY = "scan_status"
SCAN_LOCK = threading.Lock()
LOCAL_METADATA_STATUS_KEY = "local_metadata_status"
LOCAL_METADATA_LOCK = threading.Lock()
CATALOG_LOCK = threading.Lock()
IO_LOCK = threading.Lock()
MOVE_QUEUE = queue.Queue()
MOVE_LOCK = threading.Lock()
MOVE_QUEUED = set()
MOVE_WORKER_STARTED = False
JELLYFIN_REFRESH_DELAY_SECONDS = int(os.environ.get("NAS_JELLYFIN_REFRESH_DELAY_SECONDS", "45"))
JELLYFIN_REFRESH_LOCK = threading.Lock()
JELLYFIN_REFRESH_TIMER = None
RECONCILE_LOCK = threading.Lock()
RECONCILE_LAST = 0.0
RECONCILE_INTERVAL_SECONDS = int(os.environ.get("NAS_RECONCILE_INTERVAL_SECONDS", "20"))


@app.on_event("startup")
def _startup():
    db.init_db()
    db.reset_processing()  # recupera movimientos que quedaron a medias
    try:
        watcher.reconcile_pending_moves()
    except Exception:
        pass
    try:
        organizer.cleanup_temp_copies(
            roots=_temp_cleanup_dirs_from_db(),
            delete_all=True,
            recursive=False,
        )
    except Exception:
        pass
    _set_dedup_state({})
    _set_delete_dup_state({})
    _recover_scan_state()
    _recover_local_metadata_state()
    _start_move_worker()
    watcher.start_background()


def _temp_cleanup_dirs_from_db():
    """Carpetas concretas donde pudo quedar un temporal de copia.

    Evita caminar toda la biblioteca en cada arranque del NAS.
    """
    dirs = set()
    for status in ("pending", "processing", "error", "done"):
        for item in db.list_items(status=status):
            for path in (item["dest_path"], _expected_dest_for_item(item)):
                if path:
                    dirs.add(os.path.dirname(path))
    return sorted(dirs)


def _expected_dest_for_item(item):
    try:
        return organizer.build_dest(item)
    except Exception:
        return None


def _maybe_reconcile_pending_moves():
    global RECONCILE_LAST
    now = time.time()
    if now - RECONCILE_LAST < RECONCILE_INTERVAL_SECONDS:
        return
    if not RECONCILE_LOCK.acquire(blocking=False):
        return
    try:
        now = time.time()
        if now - RECONCILE_LAST < RECONCILE_INTERVAL_SECONDS:
            return
        watcher.reconcile_pending_moves()
        RECONCILE_LAST = now
    except Exception:
        RECONCILE_LAST = now
    finally:
        RECONCILE_LOCK.release()


# ---------------- Vistas principales ----------------

@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse("/tab/movie")


# Service worker en la raíz (necesario para que la PWA tenga ámbito "/").
_SW_JS = """
self.addEventListener('install', function (e) { self.skipWaiting(); });
self.addEventListener('activate', function (e) { self.clients.claim(); });
self.addEventListener('fetch', function (e) { /* passthrough: red primero */ });
"""


@app.get("/sw.js")
def service_worker():
    return Response(content=_SW_JS, media_type="application/javascript")


@app.get("/music-covers/{cover_name}")
def music_cover(cover_name: str):
    path = music_meta.cached_cover_path(cover_name)
    if not path:
        return Response(status_code=404)
    media_type = "image/jpeg"
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        media_type = "image/png"
    elif ext == ".webp":
        media_type = "image/webp"
    elif ext == ".bmp":
        media_type = "image/bmp"
    return FileResponse(path, media_type=media_type)


def _group_series(items):
    """Agrupa los episodios pendientes por serie (una tarjeta por serie)."""
    groups = {}
    for it in items:
        key = it["tmdb_id"] or (it["chosen_title"] or it["detected_title"] or "¿?").lower()
        g = groups.get(key)
        if not g:
            g = {
                "gid": "g%d" % len(groups),
                "title": it["chosen_title"] or it["detected_title"] or "¿?",
                "year": it["chosen_year"],
                "poster_url": it["poster_url"],
                "overview": it["overview"],
                "default_base": it["dest_folder"] or organizer.default_base("series"),
                "episodes": [],
            }
            groups[key] = g
        # Conservamos el primer póster/sinopsis que aparezca
        if not g["poster_url"] and it["poster_url"]:
            g["poster_url"] = it["poster_url"]
        if not g["overview"] and it["overview"]:
            g["overview"] = it["overview"]
        g["episodes"].append(it)
    result = list(groups.values())
    for g in result:
        g["episodes"].sort(key=lambda x: ((x["season"] or 0), (x["episode"] or 0)))
        g["count"] = len(g["episodes"])
        g["processing"] = any(e["status"] == "processing" for e in g["episodes"])
    result.sort(key=lambda g: g["title"].lower())
    return result


def _file_meta_map(items):
    return {it["id"]: filemeta.display_info(it) for it in items}


def _target_map(items, defaults):
    cache = {}  # memoiza listados de carpeta para no re-listar la misma N veces
    return {it["id"]: targets.inspect(it, defaults[it["id"]], _folder_cache=cache)
            for it in items}


def _dedup_state():
    raw = db.get_setting(DEDUP_STATUS_KEY, "")
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _dedup_notice():
    state = _dedup_state()
    if not state:
        return {
            "visible": False,
            "running": False,
            "deleted": 0,
            "errors": 0,
            "done": 0,
            "total": 0,
            "groups_total": 0,
            "groups_done": 0,
            "skipped_groups": 0,
            "current": "",
            "last_error": "",
        }

    running = bool(state.get("running"))
    visible = running or bool(state.get("message"))

    state["running"] = running
    state["visible"] = visible
    state.setdefault("deleted", 0)
    state.setdefault("errors", 0)
    state.setdefault("done", 0)
    state.setdefault("total", 0)
    state.setdefault("groups_total", 0)
    state.setdefault("groups_done", 0)
    state.setdefault("skipped_groups", 0)
    state.setdefault("current", "")
    state.setdefault("last_error", "")
    return state


def _set_dedup_state(state):
    db.set_setting(DEDUP_STATUS_KEY, json.dumps(state or {}, ensure_ascii=False))


def _delete_dup_state():
    raw = db.get_setting(DELETE_DUP_STATUS_KEY, "")
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _delete_dup_notice():
    state = _delete_dup_state()
    if not state:
        return {"visible": False, "running": False}

    running = bool(state.get("running"))
    state["running"] = running
    state["visible"] = running or bool(state.get("message"))
    return state


def _set_delete_dup_state(state):
    db.set_setting(DELETE_DUP_STATUS_KEY, json.dumps(state or {}, ensure_ascii=False))


def _scan_state():
    raw = db.get_setting(SCAN_STATUS_KEY, "")
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _scan_notice():
    state = _scan_state()
    if not state:
        return {"visible": False, "running": False}
    running = bool(state.get("running"))
    state["running"] = running
    state["visible"] = running or bool(state.get("message"))
    state.setdefault("message", "")
    state.setdefault("seen", 0)
    state.setdefault("new_pending", 0)
    return state


def _set_scan_state(state):
    db.set_setting(SCAN_STATUS_KEY, json.dumps(state or {}, ensure_ascii=False))


def _recover_scan_state():
    state = _scan_state()
    if state.get("running"):
        state.update({
            "running": False,
            "finished_at": _now(),
            "message": "El escaneo anterior se interrumpio al reiniciar la app. Pulsa Buscar ahora para intentarlo de nuevo.",
        })
        _set_scan_state(state)


def _base_context():
    scan_notice = _scan_notice()
    local_metadata_notice = _local_metadata_notice()
    catalog_notice = catalog.status()
    return {
        "scan_notice": scan_notice,
        "scan_running": bool(scan_notice.get("running")),
        "local_metadata_notice": local_metadata_notice,
        "local_metadata_running": bool(local_metadata_notice.get("running")),
        "catalog_notice": catalog_notice,
        "catalog_running": bool(catalog_notice.get("running")),
        "activity": _activity(scan_notice, local_metadata_notice, catalog_notice),
    }


def _activity(scan_notice=None, local_metadata_notice=None, catalog_notice=None):
    """Resumen barato del estado para que la página sondee en vez de recargarse
    entera cada pocos segundos. `active` indica si hay algo en curso (y por tanto
    conviene seguir mirando); `sig` cambia solo cuando algo relevante cambió, de
    modo que el navegador recarga una vez (al terminar) y no en cada tick."""
    scan_notice = scan_notice if scan_notice is not None else _scan_notice()
    local_notice = local_metadata_notice if local_metadata_notice is not None else _local_metadata_notice()
    catalog_notice = catalog_notice if catalog_notice is not None else catalog.status()
    dedup = _dedup_notice()
    delete_dup = _delete_dup_notice()
    processing = db.count_processing()
    counts = db.pending_counts()

    active = bool(
        processing
        or dedup.get("running") or delete_dup.get("running")
        or scan_notice.get("running") or local_notice.get("running")
        or catalog_notice.get("running")
    )
    sig = "|".join(str(x) for x in [
        processing, sum(counts.values()),
        dedup.get("running"), dedup.get("done"), dedup.get("deleted"), dedup.get("errors"),
        delete_dup.get("running"), delete_dup.get("message"),
        scan_notice.get("running"), scan_notice.get("message"),
        local_notice.get("running"), local_notice.get("done"), local_notice.get("errors"),
        catalog_notice.get("running"), catalog_notice.get("done"), catalog_notice.get("message"),
    ])
    return {"active": active, "sig": sig}


@app.get("/api/status")
def api_status():
    """Estado ligero para el sondeo del front (no renderiza HTML pesado)."""
    return _activity()


def _start_scan_job():
    started_at = _now()
    with SCAN_LOCK:
        current = _scan_state()
        if current.get("running"):
            return False
        before_counts = db.pending_counts()
        before_total = sum(before_counts.values())
        state = {
            "running": True,
            "started_at": started_at,
            "message": "Buscando archivos nuevos en la carpeta de descargas...",
            "seen": 0,
            "new_pending": 0,
        }
        _set_scan_state(state)

    def worker():
        try:
            seen = watcher.scan_once()
            after_counts = db.pending_counts()
            after_total = sum(after_counts.values())
            new_pending = max(0, after_total - before_total)
            if new_pending:
                message = f"Escaneo terminado: {new_pending} pendiente(s) nuevo(s). Revise {seen} archivo(s)."
            else:
                message = f"Escaneo terminado: revise {seen} archivo(s). No encontre pendientes nuevos."
            _set_scan_state({
                "running": False,
                "started_at": started_at,
                "finished_at": _now(),
                "message": message,
                "seen": seen,
                "new_pending": new_pending,
            })
        except Exception as exc:
            _set_scan_state({
                "running": False,
                "started_at": started_at,
                "finished_at": _now(),
                "message": f"No se pudo completar el escaneo: {exc}",
                "error": str(exc),
            })

    threading.Thread(target=worker, daemon=True).start()
    return True


def _local_metadata_state():
    raw = db.get_setting(LOCAL_METADATA_STATUS_KEY, "")
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _local_metadata_notice():
    state = _local_metadata_state()
    if not state:
        return {"visible": False, "running": False}
    running = bool(state.get("running"))
    state["running"] = running
    state["visible"] = running or bool(state.get("message"))
    state.setdefault("message", "")
    state.setdefault("done", 0)
    state.setdefault("total", 0)
    state.setdefault("errors", 0)
    state.setdefault("current", "")
    return state


def _set_local_metadata_state(state):
    db.set_setting(LOCAL_METADATA_STATUS_KEY, json.dumps(state or {}, ensure_ascii=False))


def _recover_local_metadata_state():
    state = _local_metadata_state()
    if state.get("running"):
        state.update({
            "running": False,
            "finished_at": _now(),
            "message": "La generacion anterior de metadata local se interrumpio al reiniciar la app.",
        })
        _set_local_metadata_state(state)


def _start_local_metadata_job():
    started_at = _now()
    with LOCAL_METADATA_LOCK:
        current = _local_metadata_state()
        if current.get("running"):
            return False
        items = [
            it for it in db.list_items(status="done")
            if it["media_type"] in ("movie", "series") and it["dest_path"]
        ]
        state = {
            "running": True,
            "started_at": started_at,
            "message": "Generando .nfo, posters y miniaturas locales para lo ya movido...",
            "done": 0,
            "total": len(items),
            "errors": 0,
            "current": "",
        }
        _set_local_metadata_state(state)

    def worker():
        done = 0
        errors = 0
        _set_local_metadata_state({
            "running": True,
            "started_at": started_at,
            "message": "Esperando turno: el NAS solo hara una tarea pesada a la vez.",
            "done": done,
            "total": len(items),
            "errors": errors,
            "current": "",
        })
        _wait_for_move_queue_to_clear()
        with IO_LOCK:
            for item in items:
                current = item["dest_path"] or item["filename"]
                try:
                    if item["dest_path"] and os.path.exists(item["dest_path"]):
                        organizer.write_metadata(item, item["dest_path"])
                    else:
                        errors += 1
                except Exception:
                    errors += 1
                done += 1
                _set_local_metadata_state({
                    "running": True,
                    "started_at": started_at,
                    "message": "Generando metadata local...",
                    "done": done,
                    "total": len(items),
                    "errors": errors,
                    "current": current,
                })

        _set_local_metadata_state({
            "running": False,
            "started_at": started_at,
            "finished_at": _now(),
            "message": (
                f"Metadata local terminada: {done - errors} archivo(s) procesado(s), "
                f"{errors} con problema."
            ),
            "done": done,
            "total": len(items),
            "errors": errors,
            "current": "",
        })

    threading.Thread(target=worker, daemon=True).start()
    return True


def _start_dedup_job(item_ids, scope):
    started_at = _now()
    scope_label = "de esta serie" if scope == "series" else "de toda la biblioteca pendiente"
    with DEDUP_LOCK:
        current = _dedup_state()
        if current.get("running"):
            return False
        state = {
            "running": True,
            "scope": scope,
            "started_at": started_at,
            "item_count": len(item_ids),
            "message": (
                f"Preparando el borrado de duplicados idénticos {scope_label}. "
                "Se irá mostrando el avance mientras calcula SHA-256."
            ),
            "deleted": 0,
            "errors": 0,
            "done": 0,
            "total": 0,
            "groups_total": 0,
            "groups_done": 0,
            "skipped_groups": 0,
            "current": "",
            "last_error": "",
        }
        _set_dedup_state(state)

    def worker():
        def push(update):
            state.update(update or {})
            _set_dedup_state(dict(state))

        try:
            state.update({
                "message": "Esperando turno: el NAS solo hara una tarea pesada a la vez.",
                "phase": "waiting",
            })
            _set_dedup_state(dict(state))
            _wait_for_move_queue_to_clear()
            with IO_LOCK:
                result = duplicates.delete_all_exact_duplicates(item_ids, progress=push)
            state.update({
                "running": False,
                "scope": scope,
                "started_at": started_at,
                "finished_at": _now(),
                "item_count": len(item_ids),
                "deleted": result.get("deleted", 0),
                "errors": result.get("errors", 0),
                "done": result.get("done", 0),
                "total": result.get("total", 0),
                "groups_total": result.get("groups_total", 0),
                "groups_done": result.get("groups_done", result.get("groups_total", 0)),
                "skipped_groups": result.get("skipped_groups", 0),
                "last_error": result.get("last_error", ""),
                "message": result.get("message", "Limpieza terminada."),
                "phase": result.get("phase", "done"),
            })
            _set_dedup_state(dict(state))
        except Exception as exc:
            state.update({
                "running": False,
                "scope": scope,
                "started_at": started_at,
                "finished_at": _now(),
                "item_count": len(item_ids),
                "deleted": state.get("deleted", 0),
                "message": f"No se pudo limpiar duplicados: {exc}",
                "last_error": str(exc),
                "phase": "error",
            })
            _set_dedup_state(dict(state))

    threading.Thread(target=worker, daemon=True).start()
    return True


def _start_delete_dup_job(item_id):
    started_at = _now()
    with DELETE_DUP_LOCK:
        current = _delete_dup_state()
        if current.get("running"):
            return False
        item = db.get_item(item_id)
        if item:
            db.update_item(item_id, error=None)
        _set_delete_dup_state({
            "running": True,
            "item_id": item_id,
            "started_at": started_at,
            "message": "Verificando SHA-256 y borrando solo si es un duplicado idéntico.",
        })

    def worker():
        try:
            _set_delete_dup_state({
                "running": True,
                "item_id": item_id,
                "started_at": started_at,
                "message": "Esperando turno: el NAS solo hara una tarea pesada a la vez.",
            })
            _wait_for_move_queue_to_clear()
            with IO_LOCK:
                ok, message = duplicates.delete_exact_duplicate(item_id)
            item = db.get_item(item_id)
            if not ok and item:
                db.update_item(item_id, error=message)
            _set_delete_dup_state({
                "running": False,
                "item_id": item_id,
                "started_at": started_at,
                "finished_at": _now(),
                "ok": ok,
                "message": message,
            })
        except Exception as exc:
            item = db.get_item(item_id)
            if item:
                db.update_item(item_id, error=f"No se pudo borrar el duplicado: {exc}")
            _set_delete_dup_state({
                "running": False,
                "item_id": item_id,
                "started_at": started_at,
                "finished_at": _now(),
                "ok": False,
                "message": f"No se pudo borrar el duplicado: {exc}",
            })

    threading.Thread(target=worker, daemon=True).start()
    return True


@app.get("/tab/{media_type}", response_class=HTMLResponse)
def tab(request: Request, media_type: str, dedup: int = 0):
    _maybe_reconcile_pending_moves()
    # Mostramos lo pendiente y lo que se está moviendo (para ver el progreso).
    processing = db.list_items(status="processing", media_type=media_type)
    pending = db.list_items(status="pending", media_type=media_type)
    items = processing + pending
    dedup_notice = _dedup_notice()
    delete_dup_notice = _delete_dup_notice()

    if media_type == "series":
        # Series: una tarjeta por serie, expandible con sus episodios.
        groups = _group_series(items)
        target_map = {}
        for g in groups:
            g["target"] = targets.inspect_many(g["episodes"], g["default_base"])
            g["duplicate_groups"] = duplicates.comparison_groups(g["episodes"])
            g["target_exact_pending_ids"] = []
            g["target_unsafe_exact_count"] = 0
            _folder_cache = {}
            for ep in g["episodes"]:
                target = targets.inspect(ep, g["default_base"], _folder_cache=_folder_cache)
                target_map[ep["id"]] = target
                if ep["status"] == "pending" and target["exact_exists"]:
                    if target["safe_to_delete_pending"]:
                        g["target_exact_pending_ids"].append(ep["id"])
                    else:
                        g["target_unsafe_exact_count"] += 1
        return templates.TemplateResponse("series.html", {
            "request": request, "tabs": TABS, "active": media_type, "page": "tabs",
            "groups": groups, "has_processing": bool(processing),
            **_base_context(),
            "dedup_notice": dedup_notice,
            "delete_dup_notice": delete_dup_notice,
            "dedup_running": bool(dedup_notice.get("running")),
            "delete_dup_running": bool(delete_dup_notice.get("running")),
            "deduping": bool(dedup), "tab_counts": db.pending_counts(),
            "file_meta": _file_meta_map(items), "duplicate_map": duplicates.analyze(items),
            "target_map": target_map,
        })

    leaves = {it["id"]: organizer.leaf_path(it) for it in items}
    defaults = {it["id"]: (it["dest_folder"] or organizer.default_base(it["media_type"]))
                for it in items}
    return templates.TemplateResponse("index.html", {
        "request": request, "tabs": TABS, "active": media_type,
        "items": items, "page": "tabs", "tab_counts": db.pending_counts(),
        **_base_context(),
        "leaves": leaves, "defaults": defaults,
        "file_meta": _file_meta_map(items), "duplicate_map": duplicates.analyze(items),
        "target_map": _target_map(items, defaults),
        "has_processing": bool(processing),
        "dedup_running": bool(dedup_notice.get("running")),
        "delete_dup_notice": delete_dup_notice,
        "delete_dup_running": bool(delete_dup_notice.get("running")),
    })


@app.get("/api/folders")
def api_folders(path: str = ""):
    """Devuelve las subcarpetas de `path` para el navegador de carpetas (árbol)."""
    return folders.browse(path)


@app.get("/api/target-check")
def api_target_check(ids: str = "", dest_folder: str = ""):
    """Comprueba si el destino elegido ya tiene archivos sin mover nada."""
    base = dest_folder.strip()
    item_ids = []
    for raw in ids.split(","):
        raw = raw.strip()
        if raw.isdigit():
            item_ids.append(int(raw))
    items = [db.get_item(item_id) for item_id in item_ids]
    items = [it for it in items if it]
    if not base or not items:
        return {"ok": False, "html": ""}
    if not folders.within_roots(base):
        return {"ok": False, "html": "<div class=\"target-warning target-warning-strong\"><strong>Destino no permitido</strong><span>La carpeta está fuera de las raíces configuradas.</span></div>"}
    summary = targets.inspect_many(items, base)
    return {"ok": True, "html": _target_check_html(summary)}


def _target_check_html(summary):
    examples = html.escape(", ".join(summary["examples"]))
    if summary["exact_count"]:
        plural = "s" if summary["exact_count"] != 1 else ""
        if summary.get("unsafe_exact_count"):
            unsafe_plural = "s" if summary["unsafe_exact_count"] != 1 else ""
            return (
                '<div class="target-warning target-warning-strong">'
                '<strong>Destino sospechoso</strong>'
                f'<span>{summary["unsafe_exact_count"]} archivo{unsafe_plural} final{unsafe_plural} existe{"" if summary["unsafe_exact_count"] == 1 else "n"}, '
                'pero no se pudo confirmar que sea igual al pendiente. No borres el pendiente hasta comparar.'
                + (f' Ejemplo: {examples}.' if examples else '')
                + '</span></div>'
            )
        return (
            '<div class="target-info">'
            '<strong>Ya hay copia igual en biblioteca</strong>'
            f'<span>{summary["exact_count"]} archivo{plural} final{plural} ya existe{"" if summary["exact_count"] == 1 else "n"}. '
            'Puedes comparar o borrar solo la copia pendiente de descargas.'
            + (f' Ejemplo: {examples}.' if examples else '')
            + '</span></div>'
        )
    if summary["folder_count"]:
        plural = "s" if summary["folder_count"] != 1 else ""
        return (
            '<div class="target-warning">'
            '<strong>Ya hay contenido en esa carpeta</strong>'
            f'<span>{summary["folder_count"]} destino{plural} tiene{"" if summary["folder_count"] == 1 else "n"} archivos de medios. '
            + (f'Ejemplo: {examples}.' if examples else 'Revisa antes de mover.')
            + '</span></div>'
        )
    return ""


@app.get("/catalog", response_class=HTMLResponse)
def catalog_page(request: Request):
    dedup_notice = _dedup_notice()
    delete_dup_notice = _delete_dup_notice()
    return templates.TemplateResponse(request, "catalog.html", {
        "request": request,
        "tabs": TABS,
        "active": "catalog",
        "page": "catalog",
        "tab_counts": db.pending_counts(),
        **_base_context(),
        "catalog": catalog.build_catalog(),
        "discover": catalog.build_discover(),
        "library_dups": catalog.library_duplicates(),
        "suggested_roots": catalog.suggested_roots(),
        "dedup_running": bool(dedup_notice.get("running")),
        "delete_dup_running": bool(delete_dup_notice.get("running")),
    })


@app.post("/catalog/import")
def catalog_import(folder: str = Form(""), limit: int = Form(80)):
    started = _start_catalog_import(folder, limit)
    if not started:
        catalog.set_status({
            "running": False,
            "message": "Ya hay una actualizacion de catalogo en curso.",
        })
    return RedirectResponse("/catalog", status_code=303)


@app.post("/catalog/update")
def catalog_update(limit: int = Form(80)):
    started = _start_catalog_update(limit)
    if not started:
        catalog.set_status({
            "running": False,
            "message": "Ya hay una actualizacion de catalogo en curso.",
        })
    return RedirectResponse("/catalog", status_code=303)


@app.post("/catalog/delete-file")
def catalog_delete_file(path: str = Form("")):
    """Borra un archivo concreto de la biblioteca (una copia duplicada).

    Solo permite borrar dentro de las bibliotecas configuradas, por seguridad."""
    path = (path or "").strip()
    if path and catalog._within_catalog_roots(path) and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
        db.delete_catalog_file(path)
        catalog.invalidate_build()
    return RedirectResponse("/catalog", status_code=303)


def _start_catalog_import(folder, limit):
    if not CATALOG_LOCK.acquire(blocking=False):
        return False
    folder = (folder or "").strip()
    limit = max(0, min(int(limit or 80), 300))
    catalog.set_status({
        "running": True,
        "message": "Importando biblioteca actual...",
        "done": 0,
        "total": 0,
        "current": folder,
    })

    def worker():
        try:
            def push(update):
                catalog.set_status({
                    "running": True,
                    "message": update.get("message") or "Importando biblioteca actual...",
                    "done": update.get("done", 0),
                    "total": update.get("total", 0),
                    "current": update.get("current", ""),
                })

            result = catalog.import_folder(folder, enrich_limit=limit, progress=push)
            catalog.invalidate_build()
            catalog.set_status({
                "running": False,
                "message": result.get("message", "Importacion terminada."),
                "done": result.get("scanned", 0),
                "total": result.get("scanned", 0),
                "current": "",
            })
        except Exception as exc:
            catalog.set_status({
                "running": False,
                "message": f"No se pudo importar la biblioteca: {exc}",
            })
        finally:
            CATALOG_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()
    return True


def _start_catalog_update(limit):
    if not CATALOG_LOCK.acquire(blocking=False):
        return False
    limit = max(1, min(int(limit or 80), 300))
    catalog.set_status({
        "running": True,
        "message": "Actualizando sagas y faltantes...",
        "done": 0,
        "total": 0,
        "current": "",
    })

    def worker():
        try:
            def push(update):
                catalog.set_status({
                    "running": True,
                    "message": "Actualizando sagas y faltantes...",
                    "done": update.get("done", 0),
                    "total": update.get("total", 0),
                    "current": update.get("current", ""),
                })

            enriched = catalog.enrich_unmatched(limit=None, progress=push)  # completa todo
            result = catalog.update_catalog(limit=limit, progress=push)
            catalog.update_discover(limit=len(catalog.DISCOVER_SECTIONS), progress=push)
            catalog.invalidate_build()
            if enriched:
                result["message"] = result.get("message", "") + f" Pósters completados: {enriched}."
            catalog.set_status({
                "running": False,
                "message": result.get("message", "Catalogo actualizado."),
                "done": result.get("done", 0),
                "total": result.get("total", 0),
                "current": "",
            })
        except Exception as exc:
            catalog.set_status({
                "running": False,
                "message": f"No se pudo actualizar el catalogo: {exc}",
            })
        finally:
            CATALOG_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()
    return True


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    dedup_notice = _dedup_notice()
    delete_dup_notice = _delete_dup_notice()
    done = db.list_items(status="done")
    skipped = db.list_items(status="skipped")
    errored = db.list_items(status="error")
    return templates.TemplateResponse("history.html", {
        "request": request, "tabs": TABS, "active": "history",
        "done": done, "skipped": skipped, "errored": errored, "page": "history",
        "tab_counts": db.pending_counts(),
        **_base_context(),
        "dedup_running": bool(dedup_notice.get("running")),
        "delete_dup_running": bool(delete_dup_notice.get("running")),
    })


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: bool = False, msg: str = ""):
    dedup_notice = _dedup_notice()
    delete_dup_notice = _delete_dup_notice()
    return templates.TemplateResponse("settings.html", {
        "request": request, "tabs": TABS, "active": "settings",
        "cfg": config.as_dict(), "saved": saved, "msg": msg, "page": "settings",
        "tab_counts": db.pending_counts(),
        **_base_context(),
        "dedup_notice": dedup_notice,
        "dedup_running": bool(dedup_notice.get("running")),
        "delete_dup_running": bool(delete_dup_notice.get("running")),
    })


@app.post("/settings")
def settings_save(
    downloads_dir: str = Form(""), library_roots: str = Form(""),
    default_movie_dir: str = Form(""), default_series_dir: str = Form(""),
    default_music_dir: str = Form(""),
    tmdb_api_key: str = Form(""), jellyfin_url: str = Form(""),
    jellyfin_api_key: str = Form(""), metadata_language: str = Form("es-MX"),
    min_size_mb: str = Form("10"), junk_patterns: str = Form(""),
    probe_media_info: str = Form("false"),
    app_url: str = Form(""), ntfy_server: str = Form(""), ntfy_topic: str = Form(""),
    discord_webhook: str = Form(""), telegram_token: str = Form(""),
    telegram_chat_id: str = Form(""),
):
    for key, val in {
        "downloads_dir": downloads_dir, "library_roots": library_roots,
        "default_movie_dir": default_movie_dir, "default_series_dir": default_series_dir,
        "default_music_dir": default_music_dir,
        "tmdb_api_key": tmdb_api_key, "jellyfin_url": jellyfin_url,
        "jellyfin_api_key": jellyfin_api_key, "metadata_language": metadata_language,
        "min_size_mb": min_size_mb, "junk_patterns": junk_patterns,
        "probe_media_info": probe_media_info,
        "app_url": app_url, "ntfy_server": ntfy_server, "ntfy_topic": ntfy_topic,
        "discord_webhook": discord_webhook, "telegram_token": telegram_token,
        "telegram_chat_id": telegram_chat_id,
    }.items():
        config.set(key, val.strip())
    # Al guardar (p.ej. tras poner la API key) reintentamos identificar lo pendiente.
    db.reset_match_attempts()
    return RedirectResponse("/settings?saved=true", status_code=303)


@app.post("/notify/test")
def notify_test():
    """Envía una notificación de prueba a los canales configurados."""
    from . import notify as _notify
    _notify.notify("NAS Organizer", "🔔 Notificación de prueba: ¡funciona!",
                   config.get("app_url") or None)
    return RedirectResponse("/settings?saved=false&msg=Notificación de prueba enviada.",
                            status_code=303)


# ---------------- Acciones sobre items ----------------

def _redirect_to_type(media_type):
    if media_type not in ("movie", "series", "music"):
        media_type = "movie"
    return RedirectResponse(f"/tab/{media_type}", status_code=303)


CONFLICT_NOTICE = "Ya existe en destino. Compara versiones y elige cual conservar."


def _destination_exists(item, dest_folder):
    return targets.inspect(item, dest_folder)["exact_exists"]


def _target_detail(item):
    base = item["dest_folder"] or organizer.default_base(item["media_type"])
    return targets.inspect(item, base)


def _target_delete_safety(item, target=None):
    """Solo permite borrar el pendiente si el destino existe y pesa igual."""
    if not item:
        return False, "No encontre el item pendiente."
    target = target or _target_detail(item)
    if not target["exact_exists"]:
        return True, ""
    if target["safe_to_delete_pending"]:
        return True, ""
    if target["size_known"]:
        return False, (
            "No borre el pendiente: el archivo del destino no pesa igual. "
            f"Pendiente: {_format_bytes(target['source_size_bytes'])}. "
            f"Destino: {_format_bytes(target['dest_size_bytes'])}. "
            "Compara versiones y reemplaza o conserva ambos."
        )
    return False, (
        "No borre el pendiente: no pude comprobar que el archivo del destino "
        "tenga el mismo peso. Compara versiones antes de borrar."
    )


def _format_bytes(value):
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = units[0]
    for unit in units:
        if n < 1024 or unit == units[-1]:
            break
        n /= 1024
    if unit in ("B", "KB"):
        return f"{int(n)} {unit}"
    return f"{n:.1f} {unit}"


def _allow_probe():
    return str(config.get("probe_media_info")).strip().lower() in ("1", "true", "yes", "on")


def _file_summary(path, filename=None, size_bytes=0, media_info=""):
    filename = filename or os.path.basename(path or "")
    info = filemeta.from_json(media_info)
    if not info:
        info = filemeta.inspect_file(path or "", filename=filename, size_bytes=size_bytes, allow_probe=_allow_probe())
    elif not info.get("size_bytes"):
        info["size_bytes"] = size_bytes or 0

    display_item = {
        "media_info": filemeta.to_json(info),
        "original_path": path or "",
        "filename": filename,
        "size_bytes": info.get("size_bytes") or size_bytes or 0,
    }
    modified = ""
    if path and os.path.exists(path):
        try:
            from datetime import datetime
            modified = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            modified = ""

    return {
        "path": path or "",
        "filename": filename,
        "exists": bool(path and os.path.exists(path)),
        "facts": filemeta.display_info(display_item)["facts"],
        "modified": modified,
    }


def _start_move_worker():
    global MOVE_WORKER_STARTED
    with MOVE_LOCK:
        if MOVE_WORKER_STARTED:
            return
        t = threading.Thread(target=_move_worker_loop, name="nas-move-worker", daemon=True)
        t.start()
        MOVE_WORKER_STARTED = True


def _queue_move(item_id, on_existing="error"):
    _start_move_worker()
    with MOVE_LOCK:
        if item_id in MOVE_QUEUED:
            return False
        MOVE_QUEUED.add(item_id)
    MOVE_QUEUE.put((item_id, on_existing))
    return True


def _move_worker_loop():
    while True:
        item_id, on_existing = MOVE_QUEUE.get()
        try:
            with MOVE_LOCK:
                MOVE_QUEUED.discard(item_id)
            _do_move(item_id, on_existing)
        except Exception as exc:
            # Nunca dejar morir al worker: si un movimiento falla de forma
            # inesperada, marcamos el item como error y seguimos con la cola.
            # (Antes, una excepción aquí mataba el hilo y TODOS los movimientos
            # siguientes se quedaban en cola para siempre, atascados en
            # "procesando".)
            try:
                db.update_item(item_id, status="error",
                               error=f"Error inesperado al mover: {exc}")
            except Exception:
                pass
        finally:
            MOVE_QUEUE.task_done()


def _wait_for_move_queue_to_clear():
    while True:
        with MOVE_LOCK:
            has_queued_moves = bool(MOVE_QUEUED) or not MOVE_QUEUE.empty()
        if not has_queued_moves:
            return
        time.sleep(2)


def _schedule_jellyfin_refresh():
    global JELLYFIN_REFRESH_TIMER
    if not jellyfin.configured():
        return
    with JELLYFIN_REFRESH_LOCK:
        if JELLYFIN_REFRESH_TIMER:
            JELLYFIN_REFRESH_TIMER.cancel()
        JELLYFIN_REFRESH_TIMER = threading.Timer(
            JELLYFIN_REFRESH_DELAY_SECONDS,
            _run_jellyfin_refresh,
        )
        JELLYFIN_REFRESH_TIMER.daemon = True
        JELLYFIN_REFRESH_TIMER.start()


def _run_jellyfin_refresh():
    global JELLYFIN_REFRESH_TIMER
    try:
        jellyfin.refresh_incremental()
    finally:
        with JELLYFIN_REFRESH_LOCK:
            JELLYFIN_REFRESH_TIMER = None


def _start_move(item_id, on_existing="error"):
    item = db.get_item(item_id)
    if not item:
        return RedirectResponse("/", status_code=303)
    if item["status"] == "processing":
        return _redirect_to_type(item["media_type"])
    db.update_item(item_id, status="processing", error=None)
    _queue_move(item_id, on_existing)
    return _redirect_to_type(item["media_type"])


def _delete_item_file_and_record(item):
    if item and os.path.exists(item["original_path"]):
        try:
            os.remove(item["original_path"])
        except OSError as exc:
            return False, f"No pude borrar el pendiente: {exc}"
    if item:
        db.delete_item(item["id"])
    return True, ""


def _do_move(item_id, on_existing="error"):
    """Mueve el archivo en segundo plano (puede tardar si hay que copiar GB) y
    actualiza el estado al terminar. Así la web no se queda congelada."""
    item = db.get_item(item_id)
    if not item:
        return
    if item["status"] != "processing":
        return
    with IO_LOCK:
        ok, dest, message = organizer.move_item(item, on_existing=on_existing)
    if ok:
        db.update_item(item_id, status="done", dest_path=dest,
                       processed_at=_now(), error=None)
        catalog.invalidate_build()  # una peli nueva entra al catálogo
        _schedule_jellyfin_refresh()
    elif message and message.startswith("Ya existe en destino"):
        db.update_item(item_id, status="pending", error=message)
    else:
        db.update_item(item_id, status="error", error=message)


@app.post("/item/{item_id}/confirm")
def confirm(item_id: int, dest_folder: str = Form(""), new_subfolder: str = Form("")):
    item = db.get_item(item_id)
    if not item:
        return RedirectResponse("/", status_code=303)

    # Carpeta base elegida por el usuario (o la sugerida por defecto).
    base = dest_folder.strip() or organizer.default_base(item["media_type"])
    target = folders.ensure_folder(base, new_subfolder)
    if target is None:
        db.update_item(item_id, status="error",
                       error="La carpeta destino está fuera de las rutas permitidas.")
        return _redirect_to_type(item["media_type"])
    if _destination_exists(item, target):
        db.update_item(item_id, dest_folder=target, status="pending", error=None)
        return RedirectResponse(f"/item/{item_id}/conflict", status_code=303)

    # Marcamos como "procesando" y movemos en segundo plano (no bloquea la página).
    db.update_item(item_id, dest_folder=target)
    return _start_move(item_id)


@app.post("/series/confirm-all")
def confirm_series(ids: List[int] = Form(...), dest_folder: str = Form(""),
                   new_subfolder: str = Form("")):
    """Confirma y mueve TODOS los episodios de una serie a la vez."""
    base = dest_folder.strip() or organizer.default_base("series")
    target = folders.ensure_folder(base, new_subfolder)
    if target is None:
        return _redirect_to_type("series")
    for item_id in ids:
        item = db.get_item(item_id)
        if not item or item["status"] != "pending":
            continue
        if _destination_exists(item, target):
            db.update_item(item_id, dest_folder=target, status="pending", error=CONFLICT_NOTICE)
            continue
        db.update_item(item_id, dest_folder=target, status="processing", error=None)
        _queue_move(item_id)
    return _redirect_to_type("series")


@app.get("/item/{item_id}/conflict", response_class=HTMLResponse)
def conflict_form(request: Request, item_id: int):
    item = db.get_item(item_id)
    if not item:
        return RedirectResponse("/", status_code=303)
    target = _target_detail(item)
    if not target["exact_exists"]:
        db.update_item(item_id, error=None)
        return _redirect_to_type(item["media_type"])

    pending = _file_summary(
        item["original_path"],
        filename=item["filename"],
        size_bytes=item["size_bytes"],
        media_info=item["media_info"],
    )
    existing = _file_summary(target["dest_path"])
    return templates.TemplateResponse("conflict.html", {
        "request": request,
        "tabs": TABS,
        "active": item["media_type"],
        "tab_counts": db.pending_counts(),
        **_base_context(),
        "dedup_running": bool(_dedup_state().get("running")),
        "delete_dup_running": bool(_delete_dup_state().get("running")),
        "item": item,
        "target": target,
        "pending": pending,
        "existing": existing,
    })


@app.post("/item/{item_id}/conflict/keep-existing")
def conflict_keep_existing(item_id: int):
    item = db.get_item(item_id)
    mt = item["media_type"] if item else "movie"
    if item:
        target = _target_detail(item)
        if target["exact_exists"]:
            ok, message = _target_delete_safety(item, target)
            if ok:
                ok, message = _delete_item_file_and_record(item)
            if not ok:
                db.update_item(item_id, error=message)
        else:
            db.update_item(item_id, error="Ya no existe un archivo en destino para comparar.")
    return _redirect_to_type(mt)


@app.post("/item/{item_id}/conflict/replace")
def conflict_replace(item_id: int):
    return _start_move(item_id, on_existing="replace")


@app.post("/item/{item_id}/conflict/keep-both")
def conflict_keep_both(item_id: int):
    return _start_move(item_id, on_existing="keep_both")


@app.post("/series/delete-existing")
def delete_existing_series(ids: List[int] = Form(...), dest_folder: str = Form("")):
    """Borra pendientes de descargas solo cuando el archivo final ya existe."""
    base = dest_folder.strip() or organizer.default_base("series")
    for item_id in ids:
        item = db.get_item(item_id)
        if not item or item["status"] != "pending":
            continue
        target = targets.inspect(item, base)
        if target["exact_exists"]:
            ok, message = _target_delete_safety(item, target)
            if ok:
                ok, message = _delete_item_file_and_record(item)
            if not ok:
                db.update_item(item_id, error=message)
    return _redirect_to_type("series")


@app.post("/series/dedup")
def dedup_series(ids: List[int] = Form(...)):
    """Borra en lote los duplicados idénticos (SHA-256), conservando uno de cada.

    Se hace en segundo plano porque puede leer varios GB para verificar."""
    _start_dedup_job(list(ids), "series")
    return RedirectResponse("/tab/series", status_code=303)


@app.post("/dedup-all")
def dedup_all():
    """Borra en lote los duplicados idénticos de TODO lo pendiente (en segundo plano)."""
    ids = [it["id"] for it in db.list_items(status="pending")]
    _start_dedup_job(ids, "all")
    return RedirectResponse(
        "/settings?msg=" + quote("🧹 Limpiando duplicados idénticos en segundo plano. "
                                 "Revisa las pestañas en un momento."),
        status_code=303)


@app.post("/metadata/regenerate-local")
def regenerate_local_metadata():
    """Genera .nfo e imagenes locales para archivos ya movidos."""
    _start_local_metadata_job()
    return RedirectResponse(
        "/settings?msg=" + quote("Generando metadata local en segundo plano."),
        status_code=303,
    )


@app.post("/item/{item_id}/skip")
def skip(item_id: int):
    item = db.get_item(item_id)
    db.update_item(item_id, status="skipped", processed_at=_now())
    return _redirect_to_type(item["media_type"] if item else "movie")


@app.post("/item/{item_id}/delete")
def delete(item_id: int):
    """Borra el archivo del disco y el registro."""
    item = db.get_item(item_id)
    mt = item["media_type"] if item else "movie"
    if item:
        target = _target_detail(item)
        if target["exact_exists"]:
            ok, message = _target_delete_safety(item, target)
            if not ok:
                db.update_item(item_id, error=message)
                return _redirect_to_type(mt)
        ok, message = _delete_item_file_and_record(item)
        if not ok:
            db.update_item(item_id, error=message)
    return _redirect_to_type(mt)


@app.post("/item/{item_id}/reset-processing")
def reset_processing_item(item_id: int):
    """Devuelve un item atascado en 'processing' a pendiente para poder revisarlo."""
    item = db.get_item(item_id)
    if not item:
        return RedirectResponse("/", status_code=303)
    if item["status"] == "processing":
        db.update_item(
            item_id,
            status="pending",
            error="Se devolvio a pendiente. Revisa el destino antes de mover o borrar.",
        )
    return _redirect_to_type(item["media_type"])


@app.post("/item/{item_id}/delete-duplicate")
def delete_duplicate(item_id: int):
    """Borra solo si hay otro pendiente con mismo destino, tamaño y SHA-256."""
    item = db.get_item(item_id)
    mt = item["media_type"] if item else "movie"
    if not item:
        return _redirect_to_type(mt)
    if item["status"] != "pending":
        db.update_item(item_id, error="No se puede borrar mientras el archivo está en proceso.")
        return _redirect_to_type(mt)
    started = _start_delete_dup_job(item_id)
    if not started:
        db.update_item(item_id, error="Ya hay otro borrado de duplicado en curso.")
    return _redirect_to_type(mt)


@app.post("/item/{item_id}/type")
def change_type(item_id: int, media_type: str = Form(...)):
    db.update_item(item_id, media_type=media_type)
    # Re-identifica en segundo plano (temporada/episodio + búsqueda TMDB del nuevo tipo).
    threading.Thread(target=watcher.reidentify, args=(item_id, media_type),
                     daemon=True).start()
    return _redirect_to_type(media_type)


@app.get("/item/{item_id}/search", response_class=HTMLResponse)
def search_form(request: Request, item_id: int, q: str = ""):
    item = db.get_item(item_id)
    results = []
    if q and item and item["media_type"] in ("movie", "series"):
        results = tmdb.search(q, item["media_type"])
    return templates.TemplateResponse("search_results.html", {
        "request": request, "item": item, "results": results, "q": q,
        **_base_context(),
        "dedup_running": bool(_dedup_state().get("running")),
        "delete_dup_running": bool(_delete_dup_state().get("running")),
    })


@app.post("/item/{item_id}/choose")
def choose(item_id: int, tmdb_id: int = Form(...), title: str = Form(...),
           year: str = Form(""), poster_url: str = Form(""), overview: str = Form("")):
    db.update_item(
        item_id, tmdb_id=tmdb_id, chosen_title=title,
        chosen_year=int(year) if year.isdigit() else None,
        poster_url=poster_url or None, overview=overview,
    )
    item = db.get_item(item_id)
    return _redirect_to_type(item["media_type"] if item else "movie")


@app.get("/item/{item_id}/edit-music", response_class=HTMLResponse)
def edit_music_form(request: Request, item_id: int, q: str = ""):
    item = db.get_item(item_id)
    candidates = music_meta.search_candidates(q) if q else []
    return templates.TemplateResponse("edit_music.html", {
        "request": request, "item": item, "candidates": candidates, "q": q,
        **_base_context(),
        "dedup_running": bool(_dedup_state().get("running")),
        "delete_dup_running": bool(_delete_dup_state().get("running")),
    })


@app.post("/item/{item_id}/edit-music")
def edit_music_save(item_id: int, artist: str = Form(""), album: str = Form(""),
                    title: str = Form(""), track_no: str = Form("")):
    db.update_item(
        item_id,
        artist=artist.strip() or "Desconocido",
        album=album.strip() or "Desconocido",
        detected_title=title.strip() or None,
        track_no=track_no.strip() or None,
        poster_url=None,
        cover_attempts=0,
    )
    threading.Thread(target=watcher.refresh_music_cover, args=(item_id,), daemon=True).start()
    return RedirectResponse("/tab/music", status_code=303)


# ---------------- Acciones globales ----------------

@app.post("/scan")
def manual_scan(from_tab: str = Form("")):
    # En segundo plano: buscar metadatos puede tardar si la red está lenta,
    # así que no bloqueamos la página.
    _start_scan_job()
    # Volvemos a donde estaba el usuario (no siempre a Películas).
    if from_tab in ("movie", "series", "music"):
        return RedirectResponse(f"/tab/{from_tab}", status_code=303)
    if from_tab in ("history", "settings"):
        return RedirectResponse(f"/{from_tab}", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/jellyfin/refresh-full")
def jellyfin_full():
    ok, message = jellyfin.refresh_full()
    return RedirectResponse(f"/settings?saved=false&msg={message}", status_code=303)


def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
