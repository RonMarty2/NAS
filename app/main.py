"""Aplicación web (FastAPI) — bandeja de revisión de descargas para Jellyfin."""
import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, jellyfin, organizer, watcher
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
    watcher.start_background()


# ---------------- Vistas principales ----------------

@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse("/tab/movie")


@app.get("/tab/{media_type}", response_class=HTMLResponse)
def tab(request: Request, media_type: str):
    items = db.list_items(status="pending", media_type=media_type)
    return templates.TemplateResponse("index.html", {
        "request": request, "tabs": TABS, "active": media_type,
        "items": items, "page": "tabs",
    })


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
    downloads_dir: str = Form(""), movies_dir: str = Form(""),
    series_dir: str = Form(""), music_dir: str = Form(""),
    tmdb_api_key: str = Form(""), jellyfin_url: str = Form(""),
    jellyfin_api_key: str = Form(""), metadata_language: str = Form("es-ES"),
    min_size_mb: str = Form("10"),
):
    for key, val in {
        "downloads_dir": downloads_dir, "movies_dir": movies_dir,
        "series_dir": series_dir, "music_dir": music_dir,
        "tmdb_api_key": tmdb_api_key, "jellyfin_url": jellyfin_url,
        "jellyfin_api_key": jellyfin_api_key, "metadata_language": metadata_language,
        "min_size_mb": min_size_mb,
    }.items():
        config.set(key, val.strip())
    return RedirectResponse("/settings?saved=true", status_code=303)


# ---------------- Acciones sobre items ----------------

def _redirect_to_type(media_type):
    if media_type not in ("movie", "series", "music"):
        media_type = "movie"
    return RedirectResponse(f"/tab/{media_type}", status_code=303)


@app.post("/item/{item_id}/confirm")
def confirm(item_id: int):
    item = db.get_item(item_id)
    if not item:
        return RedirectResponse("/", status_code=303)
    ok, dest, message = organizer.move_item(item)
    if ok:
        db.update_item(item_id, status="done", dest_path=dest,
                       processed_at=_now(), error=None)
        jellyfin.refresh_incremental()  # escaneo incremental (solo nuevos)
    else:
        db.update_item(item_id, status="error", error=message)
    return _redirect_to_type(item["media_type"])


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
    watcher.scan_once()
    return RedirectResponse("/", status_code=303)


@app.post("/jellyfin/refresh-full")
def jellyfin_full():
    ok, message = jellyfin.refresh_full()
    return RedirectResponse(f"/settings?saved=false&msg={message}", status_code=303)


def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
