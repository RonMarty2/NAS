import json
import ntpath
import os
import queue
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import media, store


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="NAS Library Auditor")
app.mount("/auditor-static", StaticFiles(directory=str(BASE_DIR / "static")), name="auditor-static")

STATE_LOCK = threading.Lock()
JOB_LOCK = threading.Lock()
ANALYZE_QUEUE = queue.Queue()
VERIFY_QUEUE = queue.Queue()
WORKER_STARTED = False

STATE = {
    "running": False,
    "kind": "",
    "message": "",
    "done": 0,
    "total": 0,
    "current": "",
    "errors": 0,
    "updated_at": 0,
}


@app.on_event("startup")
def _startup():
    store.init_db()
    _start_worker()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", flag: str = "", root: str = "", category: str = ""):
    store.init_db()
    roots_text = store.get_setting("roots", os.environ.get("NAS_AUDIT_ROOTS", ""))
    filters = {"q": q, "flag": flag, "root": root, "category": category}
    items = [_decorate(row) for row in store.list_files(filters, limit=350)]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "roots_text": roots_text,
            "roots_seen": store.roots_seen(),
            "items": items,
            "stats": store.stats(),
            "state": _state(),
            "q": q,
            "flag": flag,
            "root": root,
            "category": category,
            "ffprobe_ok": bool(shutil.which("ffprobe")),
            "ffmpeg_ok": bool(shutil.which("ffmpeg")),
        },
    )


@app.post("/settings/roots")
def save_roots(roots: str = Form("")):
    store.set_setting("roots", roots.strip())
    return RedirectResponse("/", status_code=303)


@app.post("/scan")
def scan():
    roots = _configured_roots()
    if not roots:
        _set_state("scan", "Configura una o mas carpetas primero.", running=False)
        return RedirectResponse("/", status_code=303)
    if not _try_start_job("scan", f"Escaneando cambios en {len(roots)} carpeta(s)..."):
        return RedirectResponse("/", status_code=303)
    threading.Thread(target=_scan_worker, args=(roots,), daemon=True).start()
    return RedirectResponse("/", status_code=303)


@app.post("/analyze")
def analyze(limit: int = Form(25)):
    limit = max(1, min(int(limit or 25), 200))
    ids = store.pending_analysis_ids(limit=limit)
    if not ids:
        _set_state("analyze", "No hay archivos nuevos o modificados por analizar.", running=False)
        return RedirectResponse("/", status_code=303)
    if not _try_start_job("analyze", f"Analizando {len(ids)} archivo(s), uno por uno..."):
        return RedirectResponse("/", status_code=303)
    for file_id in ids:
        ANALYZE_QUEUE.put(file_id)
    return RedirectResponse("/", status_code=303)


@app.post("/analyze/{file_id}")
def analyze_one(file_id: int):
    if not _try_start_job("analyze", "Analizando archivo..."):
        return RedirectResponse("/", status_code=303)
    ANALYZE_QUEUE.put(file_id)
    return RedirectResponse("/", status_code=303)


@app.post("/verify/{file_id}")
def verify_one(file_id: int):
    if not _try_start_job("verify", "Validando archivo completo..."):
        return RedirectResponse("/", status_code=303)
    VERIFY_QUEUE.put(file_id)
    return RedirectResponse("/", status_code=303)


@app.post("/review/{file_id}")
def review(file_id: int, reviewed: str = Form(""), category: str = Form(""), notes: str = Form("")):
    store.update_review(
        file_id,
        reviewed=reviewed == "on",
        category=category.strip(),
        notes=notes.strip(),
    )
    return RedirectResponse("/", status_code=303)


@app.post("/move/{file_id}")
def move(file_id: int, dest_dir: str = Form("")):
    item = store.get_file(file_id)
    if not item:
        _set_state("move", "Archivo no encontrado en el inventario.", running=False)
        return RedirectResponse("/", status_code=303)
    ok, message = _safe_rename_only(item["path"], dest_dir.strip())
    _set_state("move", message, running=False)
    if ok:
        store.mark_moved(file_id, message)
    return RedirectResponse("/", status_code=303)


@app.get("/open/{file_id}")
def open_item(file_id: int):
    item = store.get_file(file_id)
    if item and os.path.exists(item["path"]):
        try:
            media.open_file(item["path"])
            _set_state("open", "Abriendo archivo en este PC.", running=False)
        except Exception as exc:
            _set_state("open", f"No pude abrirlo: {exc}", running=False)
    return RedirectResponse("/", status_code=303)


@app.get("/folder/{file_id}")
def open_folder(file_id: int):
    item = store.get_file(file_id)
    if item:
        try:
            media.open_folder(item["path"])
            _set_state("open", "Abriendo carpeta en este PC.", running=False)
        except Exception as exc:
            _set_state("open", f"No pude abrir la carpeta: {exc}", running=False)
    return RedirectResponse("/", status_code=303)


@app.get("/api/status")
def api_status():
    return _state()


def _scan_worker(roots):
    scan_ts = time.time()
    found = 0
    new = 0
    changed = 0
    errors = 0
    try:
        for root in roots:
            for path in _iter_media(root):
                found += 1
                try:
                    result = store.upsert_seen(path, root, os.stat(path), scan_ts)
                    if result == "new":
                        new += 1
                    elif result == "changed":
                        changed += 1
                    if found % 25 == 0:
                        _set_state(
                            "scan",
                            f"Escaneando... vistos {found}, nuevos {new}, cambiados {changed}.",
                            running=True,
                            done=found,
                            total=0,
                            current=path,
                            errors=errors,
                        )
                except OSError:
                    errors += 1
        store.mark_missing_for_roots(roots, scan_ts)
        _set_state(
            "scan",
            f"Escaneo terminado: {found} archivo(s), {new} nuevo(s), {changed} cambiado(s).",
            running=False,
            done=found,
            total=found,
            current="",
            errors=errors,
        )
    except Exception as exc:
        _set_state("scan", f"Escaneo detenido: {exc}", running=False, errors=errors + 1)
    finally:
        _release_job()


def _worker_loop():
    while True:
        try:
            if not ANALYZE_QUEUE.empty():
                _drain_analyze()
            elif not VERIFY_QUEUE.empty():
                _drain_verify()
            else:
                time.sleep(0.5)
        except Exception as exc:
            _set_state("worker", f"Error de worker: {exc}", running=False, errors=1)
            _release_job()


def _drain_analyze():
    ids = []
    while not ANALYZE_QUEUE.empty():
        ids.append(ANALYZE_QUEUE.get())
    total = len(ids)
    errors = 0
    for idx, file_id in enumerate(ids, start=1):
        item = store.get_file(file_id)
        if not item or item.get("missing"):
            ANALYZE_QUEUE.task_done()
            continue
        _set_state(
            "analyze",
            "Analizando audio/video con ffprobe...",
            running=True,
            done=idx - 1,
            total=total,
            current=item["path"],
            errors=errors,
        )
        result = media.analyze(item["path"])
        if result.get("status") == "error":
            errors += 1
        store.update_analysis(file_id, result)
        ANALYZE_QUEUE.task_done()
    _set_state(
        "analyze",
        f"Analisis terminado: {total - errors} ok, {errors} con problema.",
        running=False,
        done=total,
        total=total,
        current="",
        errors=errors,
    )
    _release_job()


def _drain_verify():
    ids = []
    while not VERIFY_QUEUE.empty():
        ids.append(VERIFY_QUEUE.get())
    total = len(ids)
    errors = 0
    for idx, file_id in enumerate(ids, start=1):
        item = store.get_file(file_id)
        if not item or item.get("missing"):
            VERIFY_QUEUE.task_done()
            continue
        _set_state(
            "verify",
            "Validando archivo completo con ffmpeg. Puede tardar bastante.",
            running=True,
            done=idx - 1,
            total=total,
            current=item["path"],
            errors=errors,
        )
        ok, error = media.verify_full(item["path"])
        if not ok:
            errors += 1
        store.update_verify(file_id, "ok" if ok else "error", error)
        VERIFY_QUEUE.task_done()
    _set_state(
        "verify",
        f"Validacion terminada: {total - errors} ok, {errors} con problema.",
        running=False,
        done=total,
        total=total,
        current="",
        errors=errors,
    )
    _release_job()


def _iter_media(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith("@eaDir")]
        for name in files:
            path = os.path.join(dirpath, name)
            if media.is_media_file(path):
                yield path


def _configured_roots():
    raw = store.get_setting("roots", os.environ.get("NAS_AUDIT_ROOTS", ""))
    roots = []
    for line in raw.replace("\n", ",").split(","):
        root = line.strip().strip('"')
        if root and os.path.isdir(root):
            roots.append(root)
    return roots


def _decorate(row):
    flags = [f for f in (row.get("flags") or "").split(",") if f]
    row["flags_list"] = flags
    row["duration_text"] = _duration(row.get("duration_seconds"))
    row["size_text"] = _bytes(row.get("size_bytes"))
    row["video_text"] = _video_text(row)
    row["status_label"] = _status_label(row)
    row["path_q"] = quote(row.get("path") or "")
    return row


def _video_text(row):
    parts = []
    if row.get("width") and row.get("height"):
        parts.append(f"{row['width']}x{row['height']}")
    if row.get("video_codec"):
        parts.append(str(row["video_codec"]).upper())
    if row.get("duration_seconds"):
        parts.append(_duration(row["duration_seconds"]))
    return " | ".join(parts) or "Sin analizar"


def _status_label(row):
    if row.get("missing"):
        return "No encontrado"
    if row.get("analysis_status") == "ok":
        return "Analizado"
    if row.get("analysis_status") == "error":
        return "Error"
    if row.get("analysis_status") == "changed":
        return "Cambio pendiente"
    return "Nuevo"


def _duration(seconds):
    try:
        seconds = int(float(seconds or 0))
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return ""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _bytes(value):
    n = float(value or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = units[0]
    for unit in units:
        if n < 1024 or unit == units[-1]:
            break
        n /= 1024
    if unit in ("B", "KB"):
        return f"{int(n)} {unit}"
    return f"{n:.1f} {unit}"


def _safe_rename_only(src, dest_dir):
    if not dest_dir:
        return False, "Elige una carpeta destino."
    if not os.path.exists(src):
        return False, "El archivo origen ya no existe."
    if not os.path.isdir(dest_dir):
        return False, "La carpeta destino no existe desde este PC."
    if not _same_volume_or_share(src, dest_dir):
        return False, "Bloqueado: destino parece estar en otro volumen/share. No copio por red."
    dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.exists(dest):
        return False, "Bloqueado: ya existe un archivo con ese nombre en destino."
    try:
        os.replace(src, dest)
    except OSError as exc:
        return False, f"No se pudo mover sin copiar: {exc}"
    return True, dest


def _same_volume_or_share(src, dest_dir):
    src_abs = os.path.abspath(src)
    dest_abs = os.path.abspath(dest_dir)
    src_drive = ntpath.splitdrive(src_abs)[0].lower()
    dest_drive = ntpath.splitdrive(dest_abs)[0].lower()
    if src_drive or dest_drive:
        return src_drive == dest_drive
    try:
        return os.stat(os.path.dirname(src_abs)).st_dev == os.stat(dest_abs).st_dev
    except OSError:
        return False


def _try_start_job(kind, message):
    if not JOB_LOCK.acquire(blocking=False):
        _set_state(kind, "Ya hay una tarea pesada en curso. Espera a que termine.", running=True)
        return False
    _set_state(kind, message, running=True, done=0, total=0, current="", errors=0)
    return True


def _release_job():
    try:
        JOB_LOCK.release()
    except RuntimeError:
        pass


def _start_worker():
    global WORKER_STARTED
    if WORKER_STARTED:
        return
    t = threading.Thread(target=_worker_loop, name="pc-auditor-worker", daemon=True)
    t.start()
    WORKER_STARTED = True


def _set_state(kind, message, running=False, done=0, total=0, current="", errors=0):
    with STATE_LOCK:
        STATE.update(
            {
                "running": bool(running),
                "kind": kind,
                "message": message,
                "done": int(done or 0),
                "total": int(total or 0),
                "current": current or "",
                "errors": int(errors or 0),
                "updated_at": time.time(),
            }
        )


def _state():
    with STATE_LOCK:
        return dict(STATE)
