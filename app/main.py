"""Aplicación web (FastAPI) — bandeja de revisión de descargas para Jellyfin."""
import html
import os
import threading
from typing import List

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, duplicates, filemeta, folders, jellyfin, organizer, targets, watcher
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


@app.on_event("startup")
def _startup():
    db.init_db()
    db.reset_processing()  # recupera movimientos que quedaron a medias
    watcher.start_background()


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
    return {it["id"]: targets.inspect(it, defaults[it["id"]]) for it in items}


@app.get("/tab/{media_type}", response_class=HTMLResponse)
def tab(request: Request, media_type: str, dedup: int = 0):
    # Mostramos lo pendiente y lo que se está moviendo (para ver el progreso).
    processing = db.list_items(status="processing", media_type=media_type)
    pending = db.list_items(status="pending", media_type=media_type)
    items = processing + pending

    if media_type == "series":
        # Series: una tarjeta por serie, expandible con sus episodios.
        groups = _group_series(items)
        target_map = {}
        for g in groups:
            g["target"] = targets.inspect_many(g["episodes"], g["default_base"])
            g["duplicate_groups"] = duplicates.comparison_groups(g["episodes"])
            for ep in g["episodes"]:
                target_map[ep["id"]] = targets.inspect(ep, g["default_base"])
        return templates.TemplateResponse("series.html", {
            "request": request, "tabs": TABS, "active": media_type, "page": "tabs",
            "groups": groups, "has_processing": bool(processing) or bool(dedup),
            "deduping": bool(dedup),
            "file_meta": _file_meta_map(items), "duplicate_map": duplicates.analyze(items),
            "target_map": target_map,
        })

    leaves = {it["id"]: organizer.leaf_path(it) for it in items}
    defaults = {it["id"]: (it["dest_folder"] or organizer.default_base(it["media_type"]))
                for it in items}
    return templates.TemplateResponse("index.html", {
        "request": request, "tabs": TABS, "active": media_type,
        "items": items, "page": "tabs",
        "leaves": leaves, "defaults": defaults,
        "file_meta": _file_meta_map(items), "duplicate_map": duplicates.analyze(items),
        "target_map": _target_map(items, defaults),
        "has_processing": bool(processing),
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
        return (
            '<div class="target-warning target-warning-strong">'
            '<strong>Ya existe en destino</strong>'
            f'<span>{summary["exact_count"]} archivo{plural} final{plural} ya existe{"" if summary["exact_count"] == 1 else "n"}. '
            'Si confirmas, la app no pisa nada: creará copia con “(2)”.'
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


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    done = db.list_items(status="done")
    skipped = db.list_items(status="skipped")
    errored = db.list_items(status="error")
    return templates.TemplateResponse("history.html", {
        "request": request, "tabs": TABS, "active": "history",
        "done": done, "skipped": skipped, "errored": errored, "page": "history",
    })


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: bool = False, msg: str = ""):
    return templates.TemplateResponse("settings.html", {
        "request": request, "tabs": TABS, "active": "settings",
        "cfg": config.as_dict(), "saved": saved, "msg": msg, "page": "settings",
    })


@app.post("/settings")
def settings_save(
    downloads_dir: str = Form(""), library_roots: str = Form(""),
    default_movie_dir: str = Form(""), default_series_dir: str = Form(""),
    default_music_dir: str = Form(""),
    tmdb_api_key: str = Form(""), jellyfin_url: str = Form(""),
    jellyfin_api_key: str = Form(""), metadata_language: str = Form("es-MX"),
    min_size_mb: str = Form("10"), junk_patterns: str = Form(""),
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
        "app_url": app_url, "ntfy_server": ntfy_server, "ntfy_topic": ntfy_topic,
        "discord_webhook": discord_webhook, "telegram_token": telegram_token,
        "telegram_chat_id": telegram_chat_id,
    }.items():
        config.set(key, val.strip())
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


def _do_move(item_id):
    """Mueve el archivo en segundo plano (puede tardar si hay que copiar GB) y
    actualiza el estado al terminar. Así la web no se queda congelada."""
    item = db.get_item(item_id)
    if not item:
        return
    ok, dest, message = organizer.move_item(item)
    if ok:
        db.update_item(item_id, status="done", dest_path=dest,
                       processed_at=_now(), error=None)
        jellyfin.refresh_incremental()  # escaneo incremental (solo nuevos)
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

    # Marcamos como "procesando" y movemos en segundo plano (no bloquea la página).
    db.update_item(item_id, dest_folder=target, status="processing", error=None)
    threading.Thread(target=_do_move, args=(item_id,), daemon=True).start()
    return _redirect_to_type(item["media_type"])


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
        db.update_item(item_id, dest_folder=target, status="processing", error=None)
        threading.Thread(target=_do_move, args=(item_id,), daemon=True).start()
    return _redirect_to_type("series")


@app.post("/series/dedup")
def dedup_series(ids: List[int] = Form(...)):
    """Borra en lote los duplicados idénticos (SHA-256), conservando uno de cada.

    Se hace en segundo plano porque puede leer varios GB para verificar."""
    threading.Thread(target=duplicates.delete_all_exact_duplicates,
                     args=(list(ids),), daemon=True).start()
    return RedirectResponse("/tab/series?dedup=1", status_code=303)


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
    if item and os.path.exists(item["original_path"]):
        try:
            os.remove(item["original_path"])
        except OSError:
            pass
    db.delete_item(item_id)
    return _redirect_to_type(mt)


@app.post("/item/{item_id}/delete-duplicate")
def delete_duplicate(item_id: int):
    """Borra solo si hay otro pendiente con mismo destino, tamaño y SHA-256."""
    item = db.get_item(item_id)
    mt = item["media_type"] if item else "movie"
    ok, message = duplicates.delete_exact_duplicate(item_id)
    if not ok and item:
        db.update_item(item_id, error=message)
    return _redirect_to_type(mt)


@app.post("/item/{item_id}/type")
def change_type(item_id: int, media_type: str = Form(...)):
    db.update_item(item_id, media_type=media_type)
    return _redirect_to_type(media_type)


@app.get("/item/{item_id}/search", response_class=HTMLResponse)
def search_form(request: Request, item_id: int, q: str = ""):
    item = db.get_item(item_id)
    results = []
    if q and item and item["media_type"] in ("movie", "series"):
        results = tmdb.search(q, item["media_type"])
    return templates.TemplateResponse("search_results.html", {
        "request": request, "item": item, "results": results, "q": q,
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
    )
    return RedirectResponse("/tab/music", status_code=303)


# ---------------- Acciones globales ----------------

@app.post("/scan")
def manual_scan():
    # En segundo plano: buscar metadatos puede tardar si la red está lenta,
    # así que no bloqueamos la página.
    threading.Thread(target=watcher.scan_once, daemon=True).start()
    return RedirectResponse("/", status_code=303)


@app.post("/jellyfin/refresh-full")
def jellyfin_full():
    ok, message = jellyfin.refresh_full()
    return RedirectResponse(f"/settings?saved=false&msg={message}", status_code=303)


def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
