"""Comparador y limpieza incremental de archivos en el NAS.

Permite escanear carpetas elegidas por el usuario (o todo /volume1) de forma
incremental y acumulativa en SQLite, pausar/reanudar/detener guardando el avance,
encontrar archivos pesados, duplicados idénticos (verificados con SHA-256 completo
antes de mover a papelera), posibles duplicados por nombre, mapa de espacio por
carpeta, previsualizar/comparar audiovisuales y mover a papelera registrando
siempre qué copia se conservó y en qué ruta.
"""
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from datetime import datetime

from . import catalog, config, db

ALLOWED_BASE = os.environ.get("NAS_COMPARATOR_BASE", "/volume1")
TRASH_DIR_SETTING = "comparator_trash_dir"
LAST_STATE_SETTING = "comparator_last_state"
DEFAULT_TRASH_DIRNAME = "_papelera_nas_organizer"

VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".webm", ".flv", ".3gp", ".vob", ".ogv",
}
AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".alac", ".aiff",
}
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".tiff", ".tif", ".avif",
}

SKIP_DIR_NAMES = {
    "@eadir", "#recycle", "@tmp", ".trash", "$recycle.bin",
    ".deletedbytmm", "deletedbytmm", DEFAULT_TRASH_DIRNAME,
    "@appdata", "@database", "@download", "@img_bkp_cache",
    "@s2s", "@sharesnap", "@synologycloudsync", "@synologydrive",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS comparator_roots (
    root_path    TEXT PRIMARY KEY,
    last_scanned TEXT,
    last_status  TEXT DEFAULT 'ok',
    file_count   INTEGER DEFAULT 0,
    total_bytes  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS comparator_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT UNIQUE NOT NULL,
    root_path       TEXT NOT NULL,
    filename        TEXT NOT NULL,
    norm_name       TEXT NOT NULL,
    ext             TEXT NOT NULL,
    kind            TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    mtime           REAL NOT NULL DEFAULT 0,
    fast_hash       TEXT,
    full_sha256     TEXT,
    duration_sec    REAL,
    width           INTEGER,
    height          INTEGER,
    video_codec     TEXT,
    audio_summary   TEXT,
    probed          INTEGER DEFAULT 0,
    last_seen       REAL DEFAULT 0,
    trashed         INTEGER DEFAULT 0,
    trashed_from    TEXT,
    trashed_at      TEXT,
    kept_copy_path  TEXT,
    trash_reason    TEXT,
    ignore_similar  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS comparator_ignored_groups (
    group_key  TEXT PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_comp_size ON comparator_files(size_bytes);
CREATE INDEX IF NOT EXISTS idx_comp_hash ON comparator_files(fast_hash);
CREATE INDEX IF NOT EXISTS idx_comp_norm ON comparator_files(norm_name);
CREATE INDEX IF NOT EXISTS idx_comp_root ON comparator_files(root_path);
CREATE INDEX IF NOT EXISTS idx_comp_trashed ON comparator_files(trashed);
CREATE INDEX IF NOT EXISTS idx_comp_ident ON comparator_files(trashed, size_bytes, fast_hash);
"""

_FILE_MIGRATIONS = {
    "full_sha256": "TEXT",
    "kept_copy_path": "TEXT",
    "trash_reason": "TEXT",
    "ignore_similar": "INTEGER DEFAULT 0",
}

_STATE_LOCK = threading.Lock()
_JOB_LOCK = threading.Lock()
_BUSY_LOCK = threading.Lock()
_BUSY_FILE_IDS = set()
_DB_INIT_LOCK = threading.Lock()
_DB_INITIALIZED = False
_PAUSE_EVENT = threading.Event()
_PAUSE_EVENT.set()  # set = no pausado (corriendo)
_STOP_EVENT = threading.Event()

_STATE = {
    "running": False,
    "paused": False,
    "stopping": False,
    "phase": "idle",        # idle | scanning | hashing | done | stopped | error
    "target": "",
    "message": "",
    "seen": 0,
    "added": 0,
    "updated": 0,
    "skipped": 0,
    "hashed": 0,
    "hash_total": 0,
    "current": "",
    "errors": 0,
    "min_scan_mb": 0,
    "started_at": "",
    "finished_at": "",
    "updated_at": 0.0,
}

# Estado del limpiador en lote de duplicados exactos (verificado SHA-256 -> Papelera)
_CLEAN_LOCK = threading.Lock()
_CLEAN_JOB_LOCK = threading.Lock()
_CLEAN_STOP_EVENT = threading.Event()
_CLEAN_STATE = {
    "running": False,
    "stopping": False,
    "phase": "idle",        # idle | running | done | stopped | error
    "message": "",
    "groups_done": 0,
    "groups_total": 0,
    "moved_count": 0,
    "moved_bytes": 0,
    "skipped_count": 0,
    "errors": 0,
    "current": "",
    "finished_at": "",
    "updated_at": 0.0,
}

_NOISE_RE = re.compile(
    r"\b(1080p|720p|480p|2160p|4k|uhd|hdr|bluray|blu-ray|bdrip|brrip|webrip|web-dl|"
    r"webdl|hdrip|dvdrip|x264|x265|h264|h265|hevc|ac3|aac|dts|dual|latino|castellano|"
    r"espanol|spanish|ingles|english|subtitulada|vose|multi|copy|copia)\b",
    re.I,
)


def init_db(force=False):
    global _DB_INITIALIZED
    if _DB_INITIALIZED and not force:
        return
    with _DB_INIT_LOCK:
        if _DB_INITIALIZED and not force:
            return
        with db._lock, db.get_conn() as conn:
            conn.executescript(_SCHEMA)
            existing_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(comparator_files)").fetchall()
            }
            for col, coltype in _FILE_MIGRATIONS.items():
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE comparator_files ADD COLUMN {col} {coltype}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_comp_similar ON comparator_files(trashed, ignore_similar, norm_name)"
            )
            # Limpia automáticamente cualquier archivo interno de sistema Synology (@* / #*)
            # que pudiera haberse colado en una versión anterior.
            conn.execute(
                "DELETE FROM comparator_files WHERE path LIKE '%/@%' OR path LIKE '%/#%' OR path LIKE '%\\@%' OR path LIKE '%\\#%'"
            )
            conn.execute(
                "DELETE FROM comparator_roots WHERE root_path LIKE '%/@%' OR root_path LIKE '%/#%'"
            )
        _DB_INITIALIZED = True


def _persist_state():
    """Guarda un resumen ligero del último estado en SQLite para sobrevivir reinicios."""
    try:
        with _STATE_LOCK:
            snap = {
                "running": bool(_STATE.get("running")),
                "phase": _STATE.get("phase", "idle"),
                "target": _STATE.get("target", ""),
                "message": _STATE.get("message", ""),
                "seen": int(_STATE.get("seen") or 0),
                "added": int(_STATE.get("added") or 0),
                "updated": int(_STATE.get("updated") or 0),
                "skipped": int(_STATE.get("skipped") or 0),
                "errors": int(_STATE.get("errors") or 0),
                "min_scan_mb": _STATE.get("min_scan_mb", 0),
                "started_at": _STATE.get("started_at", ""),
                "finished_at": _STATE.get("finished_at", ""),
            }
        db.set_setting(LAST_STATE_SETTING, json.dumps(snap, ensure_ascii=False))
    except Exception:
        pass


def clear_index():
    """Vacía todo el registro del comparador (NO borra ningún archivo del disco)."""
    if _STATE.get("running") or _CLEAN_STATE.get("running"):
        return False, "Detén el proceso activo antes de vaciar el registro."
    with db._lock, db.get_conn() as conn:
        conn.execute("DELETE FROM comparator_files WHERE trashed = 0")
        conn.execute("DELETE FROM comparator_roots")
        conn.execute("DELETE FROM comparator_ignored_groups")
    _set_state(
        message="Registro vaciado (tus archivos en el NAS no se tocaron).",
        phase="idle",
        seen=0,
        added=0,
        updated=0,
        skipped=0,
    )
    _persist_state()
    return True, "Registro vaciado (tus archivos en el NAS no se tocaron)."


def recover_state():
    """Recupera el último estado guardado al arrancar el contenedor."""
    raw = db.get_setting(LAST_STATE_SETTING, "") or ""
    saved = {}
    if raw:
        try:
            saved = json.loads(raw)
        except Exception:
            saved = {}
    with _STATE_LOCK:
        _STATE["running"] = False
        _STATE["paused"] = False
        _STATE["stopping"] = False
        if saved.get("running"):
            target = saved.get("target") or allowed_base()
            _STATE["phase"] = "stopped"
            _STATE["target"] = target
            _STATE["seen"] = int(saved.get("seen") or 0)
            _STATE["added"] = int(saved.get("added") or 0)
            _STATE["updated"] = int(saved.get("updated") or 0)
            _STATE["skipped"] = int(saved.get("skipped") or 0)
            _STATE["message"] = (
                f"El contenedor se reinició mientras escaneaba {target}. "
                "Lo registrado hasta ese momento quedó guardado; puedes volver a escanear esa carpeta para continuar sin repetir."
            )
        elif saved.get("message"):
            _STATE["phase"] = saved.get("phase") or "idle"
            _STATE["target"] = saved.get("target") or ""
            _STATE["message"] = saved.get("message") or ""
            _STATE["seen"] = int(saved.get("seen") or 0)
            _STATE["added"] = int(saved.get("added") or 0)
            _STATE["updated"] = int(saved.get("updated") or 0)
            _STATE["skipped"] = int(saved.get("skipped") or 0)
            _STATE["finished_at"] = saved.get("finished_at") or ""
        else:
            _STATE["phase"] = "idle"
    _PAUSE_EVENT.set()
    _STOP_EVENT.clear()
    with _CLEAN_LOCK:
        _CLEAN_STATE["running"] = False
        _CLEAN_STATE["stopping"] = False
        _CLEAN_STATE["phase"] = "idle"
    _CLEAN_STOP_EVENT.clear()


def allowed_base():
    base = os.environ.get("NAS_COMPARATOR_BASE", ALLOWED_BASE) or "/volume1"
    return os.path.normpath(base)


def trash_dir():
    custom = (db.get_setting(TRASH_DIR_SETTING, "") or "").strip()
    if custom and is_allowed_path(custom):
        return os.path.normpath(custom)
    return os.path.join(allowed_base(), DEFAULT_TRASH_DIRNAME)


def set_trash_dir(path):
    path = (path or "").strip()
    if not path:
        db.set_setting(TRASH_DIR_SETTING, "")
        return True, trash_dir()
    if not is_allowed_path(path):
        return False, f"La papelera debe estar dentro de {allowed_base()}."
    db.set_setting(TRASH_DIR_SETTING, os.path.normpath(path))
    return True, os.path.normpath(path)


def is_allowed_path(path):
    if not path:
        return False
    try:
        rp = os.path.realpath(path)
        rb = os.path.realpath(allowed_base())
    except (OSError, TypeError, ValueError):
        return False
    return rp == rb or rp.startswith(rb + os.sep)


def _trash_dir_for_file(src):
    """Devuelve la carpeta de papelera en el MISMO volumen/carpeta compartida que `src`.

    En Synology Btrfs cada carpeta compartida (/volume1/homes, /volume1/video, etc.) es
    un subvolumen distinto: mover a /volume1/_papelera_nas_organizer obligaría a copiar
    gigabytes byte a byte. Poniendo _papelera_nas_organizer dentro de la misma carpeta
    compartida, `os.replace()` es INSTANTÁNEO (0 ms, sin copiar datos).
    """
    custom = (db.get_setting(TRASH_DIR_SETTING, "") or "").strip()
    if custom and is_allowed_path(custom):
        return os.path.normpath(custom)
    try:
        base = os.path.realpath(allowed_base())
        rsrc = os.path.realpath(src)
        if rsrc.startswith(base + os.sep):
            rel = rsrc[len(base) + 1:]
            parts = [p for p in rel.split(os.sep) if p]
            if len(parts) >= 2:
                same_share_trash = os.path.join(base, parts[0], DEFAULT_TRASH_DIRNAME)
                if is_allowed_path(same_share_trash):
                    return os.path.normpath(same_share_trash)
    except (OSError, TypeError, ValueError):
        pass
    return trash_dir()


def _is_inside_trash(path):
    if not path:
        return False
    try:
        rp = os.path.realpath(path)
        rt = os.path.realpath(trash_dir())
    except (OSError, TypeError, ValueError):
        return False
    if rp.startswith(rt + os.sep):
        return True
    if not is_allowed_path(rp):
        return False
    parts = [p.lower() for p in rp.split(os.sep) if p]
    return DEFAULT_TRASH_DIRNAME.lower() in parts[:-1]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _classify_kind(ext):
    ext = (ext or "").lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    return "other"


def normalize_name(filename):
    stem = os.path.splitext(filename or "")[0]
    s = "".join(c for c in unicodedata.normalize("NFD", stem) if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"\(\d+\)$", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = s.replace("_", " ").replace(".", " ").replace("-", " ")
    s = _NOISE_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or stem.lower().strip()


def format_bytes(value):
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
    return f"{n:.2f} {unit}" if unit in ("GB", "TB") else f"{n:.1f} {unit}"


def format_duration(seconds):
    try:
        sec = int(float(seconds or 0))
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _set_state(**kwargs):
    with _STATE_LOCK:
        _STATE.update(kwargs)
        _STATE["updated_at"] = time.time()


def status():
    with _STATE_LOCK:
        st = dict(_STATE)
    st["visible"] = bool(st["running"] or st["message"])
    return st


def _set_clean_state(**kwargs):
    with _CLEAN_LOCK:
        _CLEAN_STATE.update(kwargs)
        _CLEAN_STATE["updated_at"] = time.time()


def clean_status():
    with _CLEAN_LOCK:
        st = dict(_CLEAN_STATE)
    st["visible"] = bool(st["running"] or st["message"])
    st["moved_text"] = format_bytes(st.get("moved_bytes"))
    return st


# ---------------- Navegador de carpetas bajo /volume1 ----------------

def list_top_shares():
    """Lista las carpetas compartidas reales de primer nivel bajo /volume1 (sin @* / #*)."""
    base = allowed_base()
    if not os.path.isdir(base):
        return []
    t_real = os.path.realpath(trash_dir())
    shares = []
    try:
        with os.scandir(base) as it:
            entries = sorted(
                (e for e in it if e.is_dir(follow_symlinks=False)),
                key=lambda e: e.name.lower(),
            )
        for e in entries:
            low = e.name.lower()
            if low in SKIP_DIR_NAMES or low.startswith((".", "@", "#")):
                continue
            try:
                if os.path.realpath(e.path) == t_real:
                    continue
            except OSError:
                pass
            shares.append({"name": e.name, "path": os.path.normpath(e.path)})
    except OSError:
        pass
    return shares


def browse_folders(path=""):
    base = allowed_base()
    if not path or not is_allowed_path(path) or not os.path.isdir(path):
        path = base
    else:
        path = os.path.normpath(path)

    parent = None
    if os.path.realpath(path) != os.path.realpath(base):
        cand = os.path.dirname(path)
        if is_allowed_path(cand):
            parent = cand

    quick = [{"name": os.path.basename(base) or base, "path": base}]
    seen_q = {os.path.realpath(base)}
    raw_roots = [p.strip() for p in (config.get("library_roots") or "").split(",") if p.strip()]
    dl = (config.get("downloads_dir") or "").strip()
    if dl:
        raw_roots.append(dl)
    for r in raw_roots:
        if is_allowed_path(r) and os.path.isdir(r):
            rr = os.path.realpath(r)
            if rr not in seen_q:
                seen_q.add(rr)
                quick.append({"name": os.path.basename(r) or r, "path": os.path.normpath(r)})

    items = []
    t_real = os.path.realpath(trash_dir())
    if os.path.isdir(path):
        try:
            with os.scandir(path) as it:
                entries = sorted(
                    (e for e in it if e.is_dir(follow_symlinks=False)),
                    key=lambda e: e.name.lower(),
                )
            for e in entries:
                low = e.name.lower()
                if low in SKIP_DIR_NAMES or low.startswith((".", "@", "#")):
                    continue
                if os.path.realpath(e.path) == t_real:
                    continue
                has_children = False
                try:
                    with os.scandir(e.path) as sub_it:
                        for se in sub_it:
                            if se.is_dir(follow_symlinks=False) and not se.name.startswith((".", "@", "#")):
                                has_children = True
                                break
                except OSError:
                    pass
                items.append({"name": e.name, "path": e.path, "has_children": has_children})
        except OSError:
            pass

    return {"path": path, "parent": parent, "roots": quick, "items": items}


# ---------------- Escaneo incremental con Pausa / Reanudar / Detener ----------------

def start_scan(folder="", scan_all=False, only_media=True, min_scan_mb=50):
    target = allowed_base() if scan_all else (folder or "").strip()
    if not target:
        return False, "Elige una carpeta o pulsa Escanear todo."
    if not is_allowed_path(target) or not os.path.isdir(target):
        return False, f"La carpeta no existe o está fuera de {allowed_base()}."

    try:
        min_mb = max(0, int(float(min_scan_mb or 0)))
    except (TypeError, ValueError):
        min_mb = 50

    target = os.path.normpath(target)
    if not _JOB_LOCK.acquire(blocking=False):
        return False, "Ya hay un escaneo en curso. Puedes pausarlo o detenerlo primero."

    _STOP_EVENT.clear()
    _PAUSE_EVENT.set()
    mode_label = "audiovisuales" if only_media else "todos los archivos"
    size_label = f" (≥ {min_mb} MB)" if min_mb > 0 else " (todos los tamaños)"
    _set_state(
        running=True,
        paused=False,
        stopping=False,
        phase="scanning",
        target=target,
        message=f"Escaneando {mode_label}{size_label} en {target}...",
        seen=0,
        added=0,
        updated=0,
        skipped=0,
        hashed=0,
        hash_total=0,
        current=target,
        errors=0,
        min_scan_mb=min_mb,
        started_at=_now(),
        finished_at="",
    )
    _persist_state()

    t = threading.Thread(
        target=_scan_worker,
        args=(target, bool(only_media), min_mb),
        name="nas-comparator-scan",
        daemon=True,
    )
    t.start()
    return True, f"Escaneo iniciado en {target}."


def pause_scan():
    with _STATE_LOCK:
        if not _STATE["running"]:
            return False, "No hay ningún escaneo activo."
        _PAUSE_EVENT.clear()
        _STATE["paused"] = True
        _STATE["message"] = f"En pausa ({_STATE['seen']} revisados). Pulsa Reanudar para seguir o Detener para guardar y salir."
        _STATE["updated_at"] = time.time()
    return True, "Escaneo en pausa."


def resume_scan():
    with _STATE_LOCK:
        if not _STATE["running"]:
            return False, "No hay ningún escaneo activo."
        _STATE["paused"] = False
        _STATE["message"] = f"Reanudando escaneo en {_STATE['target']}..."
        _STATE["updated_at"] = time.time()
        _PAUSE_EVENT.set()
    return True, "Escaneo reanudado."


def stop_scan():
    with _STATE_LOCK:
        if not _STATE["running"]:
            return False, "No hay ningún escaneo activo."
        _STATE["stopping"] = True
        _STATE["paused"] = False
        _STATE["message"] = "Deteniendo y guardando el avance..."
        _STATE["updated_at"] = time.time()
    _STOP_EVENT.set()
    _PAUSE_EVENT.set()
    return True, "Deteniendo escaneo (se guardará el avance)."


def _wait_if_paused():
    while not _PAUSE_EVENT.is_set():
        if _STOP_EVENT.is_set():
            return False
        time.sleep(0.2)
    return not _STOP_EVENT.is_set()


def _skip_dir(name, full_path, trash_real):
    low = (name or "").lower()
    if low in SKIP_DIR_NAMES or low.startswith(("@", "#", ".")):
        return True
    try:
        if os.path.realpath(full_path) == trash_real:
            return True
    except OSError:
        pass
    return False


def _fast_partial_hash(path, size_bytes, chunk=65536):
    """Hash parcial rápido (inicio + medio + fin + tamaño).

    Lee como máximo 192 KB por archivo, así que incluso en archivos de 10 GB
    tarda pocos milisegundos sin saturar el disco del NAS.
    """
    h = hashlib.sha256()
    h.update(str(size_bytes).encode("ascii"))
    with open(path, "rb") as f:
        if size_bytes <= chunk * 3:
            h.update(f.read())
        else:
            h.update(f.read(chunk))
            f.seek(max(0, (size_bytes // 2) - (chunk // 2)))
            h.update(f.read(chunk))
            f.seek(max(0, size_bytes - chunk))
            h.update(f.read(chunk))
    return h.hexdigest()[:32]


def _full_sha256(path, chunk_size=1024 * 1024):
    """Calcula el hash SHA-256 COMPLETO (byte a byte) de un archivo."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _get_or_compute_full_sha256(item):
    """Devuelve SHA-256 completo verificado en disco (usa caché solo si tamaño y mtime no cambiaron)."""
    path = item["path"]
    st = os.stat(path)
    size_b = int(st.st_size)
    mtime = float(st.st_mtime)
    cached = (item.get("full_sha256") or "").strip()
    if cached and int(item.get("size_bytes") or 0) == size_b and abs(float(item.get("mtime") or 0) - mtime) < 1.0:
        return cached, size_b
    digest = _full_sha256(path)
    with db._lock, db.get_conn() as conn:
        conn.execute(
            "UPDATE comparator_files SET full_sha256=?, size_bytes=?, mtime=? WHERE id=?",
            (digest, size_b, mtime, item["id"]),
        )
    return digest, size_b


def _is_readable_file(path):
    """Comprueba que el archivo existe, no está vacío si tiene peso y se puede leer."""
    try:
        if not os.path.isfile(path):
            return False
        with open(path, "rb") as f:
            f.read(4096)
        return True
    except OSError:
        return False


def _flush_batch(upserts, seen_paths, scan_ts):
    if not upserts and not seen_paths:
        return
    with db._lock, db.get_conn() as conn:
        if upserts:
            conn.executemany(
                """
                INSERT INTO comparator_files
                    (path, root_path, filename, norm_name, ext, kind, size_bytes, mtime, last_seen, trashed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(path) DO UPDATE SET
                    root_path  = excluded.root_path,
                    filename   = excluded.filename,
                    norm_name  = excluded.norm_name,
                    ext        = excluded.ext,
                    kind       = excluded.kind,
                    size_bytes = excluded.size_bytes,
                    mtime      = excluded.mtime,
                    fast_hash  = CASE
                        WHEN comparator_files.size_bytes = excluded.size_bytes
                         AND ABS(comparator_files.mtime - excluded.mtime) < 1.0
                        THEN comparator_files.fast_hash
                        ELSE NULL
                    END,
                    full_sha256 = CASE
                        WHEN comparator_files.size_bytes = excluded.size_bytes
                         AND ABS(comparator_files.mtime - excluded.mtime) < 1.0
                        THEN comparator_files.full_sha256
                        ELSE NULL
                    END,
                    probed     = CASE
                        WHEN comparator_files.size_bytes = excluded.size_bytes
                         AND ABS(comparator_files.mtime - excluded.mtime) < 1.0
                        THEN comparator_files.probed
                        ELSE 0
                    END,
                    last_seen  = excluded.last_seen,
                    trashed    = 0
                """,
                upserts,
            )
        if seen_paths:
            conn.executemany(
                "UPDATE comparator_files SET last_seen=?, root_path=? WHERE path=?",
                [(scan_ts, rp, p) for p, rp in seen_paths],
            )


def _refresh_root_stats(target, status_label):
    if not target:
        return
    prefix = target.rstrip(os.sep) + os.sep
    with db._lock, db.get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt, COALESCE(SUM(size_bytes), 0) AS total
            FROM comparator_files
            WHERE trashed = 0 AND (root_path = ? OR path LIKE ?)
            """,
            (target, prefix + "%"),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO comparator_roots (root_path, last_scanned, last_status, file_count, total_bytes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(root_path) DO UPDATE SET
                last_scanned = excluded.last_scanned,
                last_status  = excluded.last_status,
                file_count   = excluded.file_count,
                total_bytes  = excluded.total_bytes
            """,
            (target, _now(), status_label, int(row["cnt"] or 0), int(row["total"] or 0)),
        )


def _scan_worker(target, only_media=True, min_scan_mb=50):
    scan_ts = time.time()
    trash_real = os.path.realpath(trash_dir())
    min_bytes = max(0, int(min_scan_mb or 0)) * 1024 * 1024
    seen = 0
    added = 0
    updated = 0
    skipped = 0
    errors = 0
    stopped = False

    try:
        prefix = target.rstrip(os.sep) + os.sep
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT path, size_bytes, mtime FROM comparator_files "
                "WHERE trashed = 0 AND (root_path = ? OR path LIKE ?)",
                (target, prefix + "%"),
            ).fetchall()
        existing = {r["path"]: (int(r["size_bytes"] or 0), float(r["mtime"] or 0.0)) for r in rows}

        upserts = []
        seen_unchanged = []

        for dirpath, dirs, files in os.walk(target):
            if not _wait_if_paused():
                stopped = True
                break
            dirs[:] = sorted(
                d for d in dirs if not _skip_dir(d, os.path.join(dirpath, d), trash_real)
            )
            _set_state(current=dirpath)
            for name in files:
                if not _wait_if_paused():
                    stopped = True
                    break
                if name.startswith("."):
                    continue
                ext = os.path.splitext(name)[1].lower()
                kind = _classify_kind(ext)
                if only_media and kind == "other":
                    continue
                fpath = os.path.join(dirpath, name)
                try:
                    st = os.stat(fpath)
                except OSError:
                    errors += 1
                    continue

                size_b = int(st.st_size)
                if min_bytes > 0 and size_b < min_bytes:
                    continue

                seen += 1
                mtime = float(st.st_mtime)
                prev = existing.get(fpath)
                if prev and prev[0] == size_b and abs(prev[1] - mtime) < 1.0:
                    skipped += 1
                    seen_unchanged.append((fpath, target))
                else:
                    if prev:
                        updated += 1
                    else:
                        added += 1
                    upserts.append((
                        fpath, target, name, normalize_name(name), ext, kind, size_b, mtime, scan_ts
                    ))

                if len(upserts) + len(seen_unchanged) >= 20:
                    _flush_batch(upserts, seen_unchanged, scan_ts)
                    upserts.clear()
                    seen_unchanged.clear()

                if seen % 200 == 0:
                    _refresh_root_stats(target, "scanning")

                if seen % 15 == 0:
                    _set_state(
                        seen=seen,
                        added=added,
                        updated=updated,
                        skipped=skipped,
                        errors=errors,
                        current=fpath,
                        message=(
                            f"Escaneando {target}: {seen} archivo(s) "
                            f"({added} nuevos, {updated} cambiados, {skipped} sin cambios)..."
                        ),
                    )
            if stopped:
                break

        _flush_batch(upserts, seen_unchanged, scan_ts)
        upserts.clear()
        seen_unchanged.clear()

        # Solo quitamos del registro archivos que realmente YA NO EXISTEN en disco
        # (nunca borramos registros solo porque ahora escaneaste con filtro >= 50 MB).
        if not stopped and existing:
            gone = []
            for old_path in existing:
                if not os.path.exists(old_path):
                    gone.append((old_path,))
            if gone:
                with db._lock, db.get_conn() as conn:
                    conn.executemany(
                        "DELETE FROM comparator_files WHERE trashed = 0 AND path = ?",
                        gone,
                    )

        # Fase 2: calcular hash parcial SOLO en archivos con tamaño duplicado y sin hash.
        hashed = 0
        hash_total = 0
        if not stopped:
            with db.get_conn() as conn:
                candidates = conn.execute(
                    """
                    SELECT id, path, size_bytes
                    FROM comparator_files
                    WHERE trashed = 0
                      AND size_bytes > 0
                      AND (fast_hash IS NULL OR fast_hash = '')
                      AND size_bytes IN (
                          SELECT size_bytes
                          FROM comparator_files
                          WHERE trashed = 0 AND size_bytes > 0
                          GROUP BY size_bytes
                          HAVING COUNT(*) > 1
                      )
                    ORDER BY size_bytes DESC
                    """
                ).fetchall()
            hash_total = len(candidates)
            if hash_total:
                _set_state(
                    phase="hashing",
                    seen=seen,
                    added=added,
                    updated=updated,
                    skipped=skipped,
                    hashed=0,
                    hash_total=hash_total,
                    message=f"Comparando huellas rápidas en {hash_total} archivo(s) del mismo tamaño...",
                )
                hash_batch = []
                for idx, row in enumerate(candidates, start=1):
                    if not _wait_if_paused():
                        stopped = True
                        break
                    try:
                        fh = _fast_partial_hash(row["path"], int(row["size_bytes"]))
                        hash_batch.append((fh, row["id"]))
                        hashed += 1
                    except OSError:
                        errors += 1
                    if len(hash_batch) >= 20:
                        with db._lock, db.get_conn() as conn:
                            conn.executemany(
                                "UPDATE comparator_files SET fast_hash=? WHERE id=?",
                                hash_batch,
                            )
                        hash_batch.clear()
                    if idx % 10 == 0 or idx == hash_total:
                        _set_state(
                            hashed=hashed,
                            hash_total=hash_total,
                            errors=errors,
                            current=row["path"],
                            message=f"Verificando posibles idénticos ({idx}/{hash_total})...",
                        )
                if hash_batch:
                    with db._lock, db.get_conn() as conn:
                        conn.executemany(
                            "UPDATE comparator_files SET fast_hash=? WHERE id=?",
                            hash_batch,
                        )

        _refresh_root_stats(target, "stopped" if stopped else "ok")
        if stopped:
            msg = (
                f"Escaneo detenido y guardado: {seen} revisados "
                f"({added} nuevos, {updated} cambiados, {skipped} ya registrados). "
                "Puedes retomarlo cuando quieras sin repetir lo guardado."
            )
            phase = "stopped"
        else:
            msg = (
                f"Escaneo terminado en {target}: {seen} archivo(s) "
                f"({added} nuevos, {updated} cambiados, {skipped} sin cambios"
                + (f", {hashed} huellas calculadas" if hashed else "")
                + ")."
            )
            phase = "done"

        _set_state(
            running=False,
            paused=False,
            stopping=False,
            phase=phase,
            seen=seen,
            added=added,
            updated=updated,
            skipped=skipped,
            hashed=hashed,
            hash_total=hash_total,
            current="",
            errors=errors,
            finished_at=_now(),
            message=msg,
        )
        _persist_state()
    except Exception as exc:
        _refresh_root_stats(target, "error")
        _set_state(
            running=False,
            paused=False,
            stopping=False,
            phase="error",
            current="",
            errors=errors + 1,
            finished_at=_now(),
            message=f"Error durante el escaneo: {exc}",
        )
        _persist_state()
    finally:
        _PAUSE_EVENT.set()
        _STOP_EVENT.clear()
        try:
            _JOB_LOCK.release()
        except RuntimeError:
            pass


def forget_root(root_path):
    """Quita una carpeta del registro SQLite SIN tocar ningún archivo del disco."""
    root_path = (root_path or "").strip()
    if not root_path:
        return False
    prefix = root_path.rstrip(os.sep) + os.sep
    with db._lock, db.get_conn() as conn:
        conn.execute(
            "DELETE FROM comparator_files WHERE trashed = 0 AND (root_path = ? OR path LIKE ?)",
            (root_path, prefix + "%"),
        )
        conn.execute("DELETE FROM comparator_roots WHERE root_path = ?", (root_path,))
    return True


# ---------------- Recomendación inteligente de qué copia conservar ----------------

def _library_prefixes():
    raw = [p.strip() for p in (config.get("library_roots") or "").split(",") if p.strip()]
    out = []
    for r in raw:
        nr = os.path.normpath(r).rstrip(os.sep) + os.sep
        out.append(nr.lower())
    return out


def _downloads_prefix():
    dl = (config.get("downloads_dir") or "").strip()
    if not dl:
        return ""
    return (os.path.normpath(dl).rstrip(os.sep) + os.sep).lower()


def _keeper_sort_key(file_dict, lib_prefixes=None, dl_prefix=None):
    """Menor tupla = mejor candidato para CONSERVAR."""
    if lib_prefixes is None:
        lib_prefixes = _library_prefixes()
    if dl_prefix is None:
        dl_prefix = _downloads_prefix()
    p_low = (file_dict.get("path") or "").lower()
    in_lib = any(p_low.startswith(lp) for lp in lib_prefixes)
    in_dl = bool(dl_prefix and p_low.startswith(dl_prefix)) or ("jdownloader" in p_low) or ("descarga" in p_low)
    # 0 = biblioteca ordenada, 1 = otra carpeta normal, 2 = carpeta de descargas/temp
    tier = 0 if (in_lib and not in_dl) else (2 if in_dl else 1)
    # Preferir el más antiguo (copia original) y ruta más corta/limpia
    mtime = float(file_dict.get("mtime") or 0)
    return (tier, mtime if mtime > 0 else 9e18, len(p_low), p_low)


def _mark_recommended_keeper(decorated_files):
    if not decorated_files:
        return None
    lib_p = _library_prefixes()
    dl_p = _downloads_prefix()
    best = min(decorated_files, key=lambda f: _keeper_sort_key(f, lib_p, dl_p))
    for f in decorated_files:
        f["recommended_keep"] = (f["id"] == best["id"])
    return best


# ---------------- Consultas para la vista ----------------

def _decorate(row):
    d = dict(row)
    d["size_text"] = format_bytes(d.get("size_bytes"))
    d["duration_text"] = format_duration(d.get("duration_sec"))
    d["dir_path"] = os.path.dirname(d.get("path") or "")
    res = ""
    if d.get("width") and d.get("height"):
        res = f"{d['width']}x{d['height']}"
    parts = [x for x in [res, (d.get("video_codec") or "").upper(), d["duration_text"]] if x]
    d["media_text"] = " · ".join(parts)
    try:
        d["mtime_text"] = datetime.fromtimestamp(float(d.get("mtime") or 0)).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, TypeError):
        d["mtime_text"] = ""
    return d


def summary_stats():
    with db.get_conn() as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(size_bytes), 0) AS total "
            "FROM comparator_files WHERE trashed = 0"
        ).fetchone()
        ident = conn.execute(
            """
            SELECT COUNT(*) AS groups_cnt,
                   COALESCE(SUM((c - 1) * size_bytes), 0) AS reclaimable
            FROM (
                SELECT size_bytes, fast_hash, COUNT(*) AS c
                FROM comparator_files
                WHERE trashed = 0 AND size_bytes > 0 AND fast_hash IS NOT NULL AND fast_hash != ''
                GROUP BY size_bytes, fast_hash
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()
        same_name = conn.execute(
            """
            SELECT COUNT(*) AS groups_cnt
            FROM (
                SELECT norm_name
                FROM comparator_files
                WHERE trashed = 0
                  AND COALESCE(ignore_similar, 0) = 0
                  AND norm_name != ''
                  AND norm_name NOT IN (SELECT group_key FROM comparator_ignored_groups)
                GROUP BY norm_name
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()
        tr = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(size_bytes), 0) AS total "
            "FROM comparator_files WHERE trashed = 1"
        ).fetchone()
    return {
        "total_files": int(totals["cnt"] or 0),
        "total_bytes": int(totals["total"] or 0),
        "total_text": format_bytes(totals["total"]),
        "identical_groups": int(ident["groups_cnt"] or 0),
        "reclaimable_bytes": int(ident["reclaimable"] or 0),
        "reclaimable_text": format_bytes(ident["reclaimable"]),
        "same_name_groups": int(same_name["groups_cnt"] or 0),
        "trashed_count": int(tr["cnt"] or 0),
        "trashed_text": format_bytes(tr["total"]),
    }


def list_roots():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM comparator_roots ORDER BY last_scanned DESC"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["size_text"] = format_bytes(d.get("total_bytes"))
        out.append(d)
    return out


def _filter_clauses(q="", kind="", root="", min_mb=0):
    where = ["trashed = 0"]
    params = []
    if q:
        like = f"%{q.strip()}%"
        where.append("(filename LIKE ? OR path LIKE ? OR norm_name LIKE ?)")
        params.extend([like, like, like])
    if kind in ("video", "audio", "image", "other"):
        where.append("kind = ?")
        params.append(kind)
    if root:
        prefix = root.rstrip(os.sep) + os.sep
        where.append("(root_path = ? OR path LIKE ?)")
        params.extend([root, prefix + "%"])
    try:
        mb = float(min_mb or 0)
    except (TypeError, ValueError):
        mb = 0.0
    if mb > 0:
        where.append("size_bytes >= ?")
        params.append(int(mb * 1024 * 1024))
    return " AND ".join(where), params


def list_heaviest(q="", kind="", root="", min_mb=0, limit=150):
    clause, params = _filter_clauses(q, kind, root, min_mb)
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM comparator_files WHERE {clause} ORDER BY size_bytes DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return [_decorate(r) for r in rows]


def list_identical_groups(q="", kind="", root="", min_mb=0, limit=60):
    clause, params = _filter_clauses(q, kind, root, min_mb)
    with db.get_conn() as conn:
        groups = conn.execute(
            f"""
            SELECT size_bytes, fast_hash, COUNT(*) AS cnt
            FROM comparator_files
            WHERE {clause} AND size_bytes > 0 AND fast_hash IS NOT NULL AND fast_hash != ''
            GROUP BY size_bytes, fast_hash
            HAVING COUNT(*) > 1
            ORDER BY ((COUNT(*) - 1) * size_bytes) DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        out = []
        for g in groups:
            items = conn.execute(
                "SELECT * FROM comparator_files "
                "WHERE trashed = 0 AND size_bytes = ? AND fast_hash = ? "
                "ORDER BY path ASC",
                (g["size_bytes"], g["fast_hash"]),
            ).fetchall()
            decorated = [_decorate(x) for x in items]
            keeper = _mark_recommended_keeper(decorated)
            # Ponemos el recomendado primero para lectura rápida
            decorated.sort(key=lambda f: (0 if f.get("recommended_keep") else 1, f["path"]))
            reclaim = (len(decorated) - 1) * int(g["size_bytes"])
            out.append({
                "key": g["fast_hash"],
                "size_bytes": int(g["size_bytes"]),
                "label": decorated[0]["filename"] if decorated else "",
                "count": len(decorated),
                "size_text": format_bytes(g["size_bytes"]),
                "reclaimable_text": format_bytes(reclaim),
                "recommended_id": keeper["id"] if keeper else 0,
                "recommended_path": keeper["path"] if keeper else "",
                "files": decorated,
            })
    return out


def list_same_name_groups(q="", kind="", root="", min_mb=0, limit=60):
    clause, params = _filter_clauses(q, kind, root, min_mb)
    with db.get_conn() as conn:
        groups = conn.execute(
            f"""
            SELECT norm_name, COUNT(*) AS cnt, SUM(size_bytes) AS total_size
            FROM comparator_files
            WHERE {clause}
              AND COALESCE(ignore_similar, 0) = 0
              AND norm_name != ''
              AND norm_name NOT IN (SELECT group_key FROM comparator_ignored_groups)
            GROUP BY norm_name
            HAVING COUNT(*) > 1
            ORDER BY total_size DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        out = []
        for g in groups:
            items = conn.execute(
                "SELECT * FROM comparator_files "
                "WHERE trashed = 0 AND COALESCE(ignore_similar, 0) = 0 AND norm_name = ? "
                "ORDER BY size_bytes DESC, path ASC",
                (g["norm_name"],),
            ).fetchall()
            decorated = [_decorate(x) for x in items]
            out.append({
                "key": g["norm_name"],
                "label": g["norm_name"],
                "count": len(decorated),
                "total_text": format_bytes(g["total_size"]),
                "files": decorated,
            })
    return out


def list_folder_usage(limit=40):
    """Mapa de espacio: agrupa los archivos registrados por carpeta principal (hasta 2 niveles bajo /volume1)."""
    base = allowed_base().rstrip(os.sep)
    buckets = {}
    total_all = 0
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT path, size_bytes FROM comparator_files WHERE trashed = 0"
        ).fetchall()
    for r in rows:
        p = r["path"] or ""
        sz = int(r["size_bytes"] or 0)
        total_all += sz
        rel = p[len(base):].lstrip(os.sep) if p.startswith(base) else p.lstrip(os.sep)
        parts = [x for x in rel.split(os.sep) if x]
        # Agrupamos hasta 2 niveles de carpeta (ej. /volume1/video/peliculas) si hay subcarpetas
        if len(parts) >= 3:
            folder = os.path.join(base, parts[0], parts[1])
        elif len(parts) >= 2:
            folder = os.path.join(base, parts[0])
        else:
            folder = base
        b = buckets.setdefault(folder, {"path": folder, "count": 0, "bytes": 0})
        b["count"] += 1
        b["bytes"] += sz

    ranked = sorted(buckets.values(), key=lambda x: x["bytes"], reverse=True)[:limit]
    for item in ranked:
        item["size_text"] = format_bytes(item["bytes"])
        item["pct"] = round((item["bytes"] / total_all) * 100, 1) if total_all > 0 else 0.0
    return ranked


def anonymous_summary():
    """Genera un resumen 100% anónimo (SIN nombres de archivos ni rutas reales) para diagnóstico."""
    st = summary_stats()
    with db.get_conn() as conn:
        by_kind = conn.execute(
            "SELECT kind, COUNT(*) AS c, COALESCE(SUM(size_bytes), 0) AS s "
            "FROM comparator_files WHERE trashed = 0 GROUP BY kind ORDER BY s DESC"
        ).fetchall()
    folders = list_folder_usage(limit=10)
    lines = [
        "=== RESUMEN ANÓNIMO DE ESPACIO EN NAS (sin nombres ni rutas) ===",
        f"Archivos registrados: {st['total_files']}",
        f"Espacio registrado: {st['total_text']}",
        f"Duplicados exactos: {st['identical_groups']} grupos ({st['reclaimable_text']} ahorrables)",
        f"Posibles duplicados (mismo nombre): {st['same_name_groups']} grupos",
        f"En papelera: {st['trashed_count']} archivo(s) ({st['trashed_text']})",
        "",
        "Por tipo:",
    ]
    for k in by_kind:
        lines.append(f"  - {k['kind']}: {k['c']} archivo(s) · {format_bytes(k['s'])}")
    if folders:
        lines.append("")
        lines.append("Top carpetas (anonimizadas):")
        for idx, f in enumerate(folders, start=1):
            lines.append(f"  - Carpeta_{idx}: {f['count']} archivo(s) · {f['size_text']} ({f['pct']}%)")
    return "\n".join(lines)


def list_trashed(limit=250):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM comparator_files WHERE trashed = 1 ORDER BY trashed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_decorate(r) for r in rows]


def get_file(file_id):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM comparator_files WHERE id = ?", (file_id,)).fetchone()
    return _decorate(row) if row else None


# ---------------- Limpieza segura con doble candado (SHA-256 -> Papelera) ----------------

def ignore_same_name_group(norm_name):
    norm_name = (norm_name or "").strip()
    if not norm_name:
        return False, "Grupo no válido."
    with db._lock, db.get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO comparator_ignored_groups(group_key) VALUES (?)",
            (norm_name,),
        )
    return True, "Grupo ocultado («No son duplicados»). Tus archivos no se tocaron."


def exclude_from_similar(file_id):
    """Saca un archivo puntual de 'Decidir similares' (cuando fue mal agrupado) sin tocar el disco."""
    item = get_file(file_id)
    if not item:
        return False, "Archivo no encontrado en el registro."
    with db._lock, db.get_conn() as conn:
        conn.execute(
            "UPDATE comparator_files SET ignore_similar = 1 WHERE id = ?",
            (int(file_id),),
        )
    return True, f"Quitado de «Decidir similares» (tu archivo en el NAS no se tocó): {item['filename']}"


def trash_same_name_group(norm_name):
    """Mueve TODOS los archivos activos de un grupo de 'Decidir similares' a la Papelera."""
    norm_name = (norm_name or "").strip()
    if not norm_name:
        return False, "Grupo no válido."
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, root_path, size_bytes FROM comparator_files "
            "WHERE trashed = 0 AND COALESCE(ignore_similar, 0) = 0 AND norm_name = ?",
            (norm_name,),
        ).fetchall()
    if not rows:
        return False, "No hay archivos activos en este grupo."
    moved = 0
    moved_bytes = 0
    touched_roots = set()
    for r in rows:
        ok, _ = trash_file(
            r["id"],
            kept_copy_path="",
            reason="Grupo completo enviado a papelera por ti",
            refresh_stats=False,
        )
        if ok:
            moved += 1
            moved_bytes += int(r["size_bytes"] or 0)
            if r["root_path"]:
                touched_roots.add(r["root_path"])
    for rp in touched_roots:
        _refresh_root_stats(rp, "ok")
    if moved == 0:
        return False, "No se pudo mover ningún archivo a la papelera."
    catalog.invalidate_build()
    return True, f"Listo: {moved} archivo(s) ({format_bytes(moved_bytes)}) movidos a la Papelera."


def trash_identical_group(fast_hash, size_bytes):
    """Mueve TODOS los archivos activos de un grupo de 'Idénticos' a la Papelera."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, root_path, size_bytes FROM comparator_files "
            "WHERE trashed = 0 AND size_bytes = ? AND fast_hash = ?",
            (int(size_bytes), fast_hash),
        ).fetchall()
    if not rows:
        return False, "No hay archivos activos en este grupo."
    moved = 0
    moved_bytes = 0
    touched_roots = set()
    for r in rows:
        ok, _ = trash_file(
            r["id"],
            kept_copy_path="",
            reason="Grupo idéntico completo enviado a papelera por ti",
            refresh_stats=False,
        )
        if ok:
            moved += 1
            moved_bytes += int(r["size_bytes"] or 0)
            if r["root_path"]:
                touched_roots.add(r["root_path"])
    for rp in touched_roots:
        _refresh_root_stats(rp, "ok")
    if moved == 0:
        return False, "No se pudo mover ningún archivo a la papelera."
    catalog.invalidate_build()
    return True, f"Listo: {moved} archivo(s) ({format_bytes(moved_bytes)}) movidos a la Papelera."


def trash_files_batch(file_ids, keep_id=None, reason="Descartado al comparar lado a lado"):
    """Mueve una lista de archivos a Papelera (opcionalmente registrando qué copia se conservó)."""
    keep_path = ""
    if keep_id:
        keeper = get_file(int(keep_id))
        if not keeper or keeper.get("trashed") or not _is_readable_file(keeper["path"]):
            return False, "La copia a conservar no está disponible en disco. No se movió nada."
        keep_path = keeper["path"]

    moved = 0
    moved_bytes = 0
    touched_roots = set()
    for fid in file_ids:
        if keep_id and int(fid) == int(keep_id):
            continue
        item = get_file(int(fid))
        if not item or item.get("trashed"):
            continue
        ok, _ = trash_file(int(fid), kept_copy_path=keep_path, reason=reason, refresh_stats=False)
        if ok:
            moved += 1
            moved_bytes += int(item.get("size_bytes") or 0)
            if item.get("root_path"):
                touched_roots.add(item["root_path"])
    for rp in touched_roots:
        _refresh_root_stats(rp, "ok")
    if moved == 0:
        return False, "No se movió ningún archivo (puede que ya estuvieran en la papelera)."
    catalog.invalidate_build()
    if keep_path:
        return True, f"Movido(s) {moved} archivo(s) ({format_bytes(moved_bytes)}) a Papelera. Conservado: {keep_path}"
    return True, f"Movido(s) {moved} archivo(s) ({format_bytes(moved_bytes)}) a Papelera."


def clean_identical_group(keep_id, fast_hash, size_bytes):
    """Conserva `keep_id` y mueve a Papelera los demás del mismo grupo SOLO tras verificar SHA-256 completo."""
    keeper = get_file(keep_id)
    if not keeper or keeper.get("trashed"):
        return False, "La copia elegida para conservar ya no está disponible."
    keep_path = keeper["path"]
    if not is_allowed_path(keep_path) or not _is_readable_file(keep_path):
        return False, "No pude verificar que la copia a conservar sea legible en disco. No se tocó nada."

    with db.get_conn() as conn:
        siblings = conn.execute(
            "SELECT * FROM comparator_files WHERE trashed = 0 AND size_bytes = ? AND fast_hash = ? AND id != ?",
            (int(size_bytes), fast_hash, int(keep_id)),
        ).fetchall()
    if not siblings:
        return False, "No hay copias sobrantes en este grupo."

    try:
        keep_sha, keep_size = _get_or_compute_full_sha256(keeper)
    except OSError as exc:
        return False, f"Error leyendo la copia a conservar: {exc}"

    keep_real = os.path.realpath(keep_path)
    moved = 0
    moved_bytes = 0
    skipped = 0

    for row in siblings:
        cand = dict(row)
        cand_path = cand["path"]
        if not is_allowed_path(cand_path) or not os.path.isfile(cand_path):
            skipped += 1
            continue
        # Nunca tocar si apunta físicamente al mismo archivo (symlink / mismo realpath)
        if os.path.realpath(cand_path) == keep_real:
            skipped += 1
            continue
        try:
            cand_sha, cand_size = _get_or_compute_full_sha256(cand)
        except OSError:
            skipped += 1
            continue
        # Doble candado: tamaño y SHA-256 COMPLETO deben coincidir al 100%
        if cand_size != keep_size or cand_sha != keep_sha:
            skipped += 1
            continue
        # Verificación final: la copia conservada sigue viva en disco
        if not os.path.isfile(keep_path):
            return False, "Se detuvo por seguridad: la copia a conservar dejó de estar disponible."
        ok, _ = trash_file(
            cand["id"],
            kept_copy_path=keep_path,
            reason="Duplicado exacto (SHA-256 100% verificado)",
        )
        if ok:
            moved += 1
            moved_bytes += cand_size
        else:
            skipped += 1

    if moved == 0:
        return False, "Ningún archivo pasó la verificación SHA-256 completa; no se movió nada."
    msg = (
        f"Listo: {moved} copia(s) idéntica(s) ({format_bytes(moved_bytes)}) movidas a Papelera tras verificar SHA-256. "
        f"Copia conservada: {keep_path}"
    )
    if skipped:
        msg += f" ({skipped} omitida(s) por seguridad)."
    return True, msg


def start_clean_all_identical():
    """Lanza un trabajo en segundo plano que verifica SHA-256 completo de todos los grupos idénticos
    y mueve las copias sobrantes a la Papelera (conservando siempre la copia recomendada)."""
    if _STATE.get("running"):
        return False, "Espera a que termine el escaneo antes de limpiar duplicados."
    if not _CLEAN_JOB_LOCK.acquire(blocking=False):
        return False, "Ya hay una limpieza de duplicados en curso."

    _CLEAN_STOP_EVENT.clear()
    _set_clean_state(
        running=True,
        stopping=False,
        phase="running",
        message="Preparando verificación SHA-256 de duplicados exactos...",
        groups_done=0,
        groups_total=0,
        moved_count=0,
        moved_bytes=0,
        skipped_count=0,
        errors=0,
        current="",
        finished_at="",
    )
    t = threading.Thread(target=_clean_all_worker, name="nas-comparator-clean", daemon=True)
    t.start()
    return True, "Limpieza segura iniciada (verificando SHA-256 completo antes de mover a Papelera)."


def stop_clean_all():
    with _CLEAN_LOCK:
        if not _CLEAN_STATE["running"]:
            return False, "No hay ninguna limpieza en curso."
        _CLEAN_STATE["stopping"] = True
        _CLEAN_STATE["message"] = "Deteniendo limpieza al terminar el archivo actual..."
    _CLEAN_STOP_EVENT.set()
    return True, "Deteniendo limpieza..."


def _clean_all_worker():
    moved_total = 0
    moved_bytes_total = 0
    skipped_total = 0
    errors_total = 0
    stopped = False

    try:
        groups = list_identical_groups(limit=2000)
        total = len(groups)
        _set_clean_state(
            groups_total=total,
            message=f"Verificando {total} grupo(s) de duplicados exactos (SHA-256 completo)...",
        )
        for idx, g in enumerate(groups, start=1):
            if _CLEAN_STOP_EVENT.is_set():
                stopped = True
                break
            keep_id = g["recommended_id"]
            keeper = get_file(keep_id)
            if not keeper or not _is_readable_file(keeper["path"]):
                errors_total += 1
                _set_clean_state(groups_done=idx, errors=errors_total)
                continue
            keep_path = keeper["path"]
            keep_real = os.path.realpath(keep_path)
            _set_clean_state(
                current=keep_path,
                message=f"Verificando grupo {idx}/{total} (SHA-256 completo)...",
            )
            try:
                keep_sha, keep_size = _get_or_compute_full_sha256(keeper)
            except OSError:
                errors_total += 1
                _set_clean_state(groups_done=idx, errors=errors_total)
                continue

            for f in g["files"]:
                if f["id"] == keep_id:
                    continue
                if _CLEAN_STOP_EVENT.is_set():
                    stopped = True
                    break
                cand = get_file(f["id"])
                if not cand or cand.get("trashed") or not os.path.isfile(cand["path"]):
                    skipped_total += 1
                    continue
                if os.path.realpath(cand["path"]) == keep_real:
                    skipped_total += 1
                    continue
                try:
                    cand_sha, cand_size = _get_or_compute_full_sha256(cand)
                except OSError:
                    errors_total += 1
                    continue
                if cand_size != keep_size or cand_sha != keep_sha:
                    skipped_total += 1
                    continue
                if not os.path.isfile(keep_path):
                    errors_total += 1
                    break
                ok, _ = trash_file(
                    cand["id"],
                    kept_copy_path=keep_path,
                    reason="Duplicado exacto (SHA-256 100% verificado)",
                )
                if ok:
                    moved_total += 1
                    moved_bytes_total += cand_size
                else:
                    errors_total += 1

            _set_clean_state(
                groups_done=idx,
                moved_count=moved_total,
                moved_bytes=moved_bytes_total,
                skipped_count=skipped_total,
                errors=errors_total,
            )

        msg = (
            f"Limpieza {'detenida' if stopped else 'terminada'}: "
            f"{moved_total} copia(s) movidas a Papelera ({format_bytes(moved_bytes_total)} listos para liberar). "
            "Todas las copias conservadas fueron verificadas y sus rutas quedaron registradas en Papelera."
        )
        _set_clean_state(
            running=False,
            stopping=False,
            phase="stopped" if stopped else "done",
            current="",
            finished_at=_now(),
            message=msg,
        )
    except Exception as exc:
        _set_clean_state(
            running=False,
            stopping=False,
            phase="error",
            current="",
            errors=errors_total + 1,
            finished_at=_now(),
            message=f"Error en limpieza segura: {exc}",
        )
    finally:
        _CLEAN_STOP_EVENT.clear()
        try:
            _CLEAN_JOB_LOCK.release()
        except RuntimeError:
            pass


def keep_one_in_same_name_group(keep_id, norm_name):
    """El usuario eligió conservar `keep_id` y mover a Papelera los demás del mismo nombre."""
    keeper = get_file(keep_id)
    if not keeper or keeper.get("trashed"):
        return False, "El archivo elegido para conservar no está disponible."
    keep_path = keeper["path"]
    if not is_allowed_path(keep_path) or not _is_readable_file(keep_path):
        return False, "No pude comprobar que la copia elegida sea legible en disco. No se movió nada."

    with db.get_conn() as conn:
        others = conn.execute(
            "SELECT id, root_path, path, size_bytes FROM comparator_files "
            "WHERE trashed = 0 AND COALESCE(ignore_similar, 0) = 0 AND norm_name = ? AND id != ?",
            (norm_name, int(keep_id)),
        ).fetchall()
    if not others:
        return False, "No hay otros archivos en este grupo."

    moved = 0
    moved_bytes = 0
    touched_roots = set()
    for r in others:
        ok, _ = trash_file(
            r["id"],
            kept_copy_path=keep_path,
            reason="Descartado por ti al comparar versiones similares",
            refresh_stats=False,
        )
        if ok:
            moved += 1
            moved_bytes += int(r["size_bytes"] or 0)
            if r["root_path"]:
                touched_roots.add(r["root_path"])
    for rp in touched_roots:
        _refresh_root_stats(rp, "ok")
    if moved:
        catalog.invalidate_build()

    return True, (
        f"Conservaste «{keeper['filename']}» ({keep_path}). "
        f"Se movieron {moved} archivo(s) ({format_bytes(moved_bytes)}) a la Papelera."
    )


# ---------------- Metadatos (ffprobe) y miniaturas (ffmpeg) bajo demanda ----------------

def probe_file(file_id):
    item = get_file(file_id)
    if not item or not os.path.isfile(item["path"]) or not is_allowed_path(item["path"]):
        return item
    if item.get("probed"):
        return item
    if not shutil.which("ffprobe"):
        return item
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_format", "-show_streams",
                "-of", "json", item["path"],
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            return item
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return item

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    vids = [s for s in streams if s.get("codec_type") == "video"]
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    v0 = vids[0] if vids else {}
    duration = None
    try:
        duration = float(fmt.get("duration") or v0.get("duration") or 0) or None
    except (TypeError, ValueError):
        duration = None
    width = int(v0.get("width") or 0) or None
    height = int(v0.get("height") or 0) or None
    vcodec = str(v0.get("codec_name") or "")

    audio_parts = []
    for a in auds[:5]:
        tags = a.get("tags") or {}
        lang = (tags.get("language") or tags.get("LANGUAGE") or "").strip()
        title = (tags.get("title") or "").strip()
        codec = (a.get("codec_name") or "").strip()
        ch = a.get("channels")
        bits = [x for x in [lang, title, codec, f"{ch}ch" if ch else ""] if x]
        if bits:
            audio_parts.append(" ".join(bits))
    audio_summary = " | ".join(audio_parts)

    with db._lock, db.get_conn() as conn:
        conn.execute(
            """
            UPDATE comparator_files
            SET duration_sec=?, width=?, height=?, video_codec=?, audio_summary=?, probed=1
            WHERE id=?
            """,
            (duration, width, height, vcodec, audio_summary, file_id),
        )
    return get_file(file_id)


def generate_thumbs(file_id, count=4):
    """Genera hasta `count` capturas JPEG pequeñas (base64) en momentos clave del video."""
    item = probe_file(file_id)
    if not item or item.get("kind") != "video":
        return []
    path = item["path"]
    if not os.path.isfile(path) or not is_allowed_path(path) or not shutil.which("ffmpeg"):
        return []

    duration = float(item.get("duration_sec") or 0)
    if duration > 10:
        marks = [duration * f for f in (0.15, 0.40, 0.65, 0.85)][:count]
    elif duration > 1:
        marks = [duration * 0.25, duration * 0.75]
    else:
        marks = [0.0]

    thumbs = []
    with tempfile.TemporaryDirectory(prefix="nas_comp_th_") as tmpdir:
        for idx, ts in enumerate(marks):
            out_jpg = os.path.join(tmpdir, f"t{idx}.jpg")
            cmd = [
                "ffmpeg", "-v", "error",
                "-ss", f"{ts:.2f}",
                "-i", path,
                "-frames:v", "1",
                "-vf", "scale=320:-2",
                "-q:v", "6",
                "-y", out_jpg,
            ]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                if os.path.isfile(out_jpg) and os.path.getsize(out_jpg) > 0:
                    with open(out_jpg, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("ascii")
                    thumbs.append({"time": format_duration(ts) or "0:00", "data": f"data:image/jpeg;base64,{b64}"})
            except Exception:
                continue
    return thumbs


def guess_mime(path):
    mt, _ = mimetypes.guess_type(path or "")
    if mt:
        return mt
    ext = os.path.splitext(path or "")[1].lower()
    return {
        ".mkv": "video/x-matroska",
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")


# ---------------- Acciones sobre disco: Papelera / Restaurar / Vaciar / Borrar ----------------

def _unique_dest(dest_dir, filename):
    cand = os.path.join(dest_dir, filename)
    if not os.path.exists(cand):
        return cand
    stem, ext = os.path.splitext(filename)
    i = 1
    while True:
        cand = os.path.join(dest_dir, f"{stem} ({i}){ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


def _acquire_file_op(file_id):
    fid = int(file_id)
    with _BUSY_LOCK:
        if fid in _BUSY_FILE_IDS:
            return False
        _BUSY_FILE_IDS.add(fid)
        return True


def _release_file_op(file_id):
    fid = int(file_id)
    with _BUSY_LOCK:
        _BUSY_FILE_IDS.discard(fid)


def trash_file(file_id, kept_copy_path="", reason="Movido manualmente", refresh_stats=True):
    if not _acquire_file_op(file_id):
        return False, "Este archivo ya se está procesando..."
    try:
        item = get_file(file_id)
        if not item:
            return False, "Archivo no encontrado en el registro."
        if item.get("trashed"):
            return True, "Este archivo ya estaba en la papelera."
        src = item["path"]
        if not is_allowed_path(src):
            return False, "Ruta fuera de los límites permitidos."
        if not os.path.isfile(src):
            with db._lock, db.get_conn() as conn:
                conn.execute("DELETE FROM comparator_files WHERE id=?", (file_id,))
            return True, "El archivo ya no estaba en disco; se quitó del registro."

        # Primero intenta papelera dentro de la MISMA carpeta compartida (rename instantáneo en Btrfs)
        tdir = _trash_dir_for_file(src)
        fallback_tdir = trash_dir()
        if not is_allowed_path(tdir):
            tdir = fallback_tdir
        if not is_allowed_path(tdir):
            return False, "La carpeta de papelera está fuera del volumen permitido."

        dest = None
        last_exc = None
        for cand_dir in (tdir, fallback_tdir) if tdir != fallback_tdir else (tdir,):
            try:
                os.makedirs(cand_dir, exist_ok=True)
                cand_dest = _unique_dest(cand_dir, os.path.basename(src))
                try:
                    os.replace(src, cand_dest)
                except OSError:
                    shutil.move(src, cand_dest)
                dest = cand_dest
                break
            except OSError as exc:
                last_exc = exc
                continue
        if not dest:
            return False, f"No pude mover a la papelera: {last_exc}"

        with db._lock, db.get_conn() as conn:
            conn.execute(
                """
                UPDATE comparator_files
                SET path=?, trashed=1, trashed_from=?, trashed_at=?, kept_copy_path=?, trash_reason=?
                WHERE id=?
                """,
                (dest, src, _now(), kept_copy_path or "", reason or "", file_id),
            )
            conn.execute("DELETE FROM catalog_files WHERE path=?", (src,))
        if refresh_stats:
            catalog.invalidate_build()
            _refresh_root_stats(item["root_path"], "ok")
        return True, f"Movido a papelera: {dest}"
    finally:
        _release_file_op(file_id)


def restore_file(file_id):
    if not _acquire_file_op(file_id):
        return False, "Este archivo ya se está procesando..."
    try:
        item = get_file(file_id)
        if not item or not item.get("trashed"):
            return False, "Archivo no encontrado en la papelera."
        src = item["path"]
        orig = item.get("trashed_from") or ""
        if not orig or not is_allowed_path(src) or not is_allowed_path(orig):
            return False, "Ruta de restauración no válida."
        if not os.path.isfile(src):
            with db._lock, db.get_conn() as conn:
                conn.execute("DELETE FROM comparator_files WHERE id=?", (file_id,))
            return False, "El archivo ya no existe en la papelera."
        if os.path.exists(orig):
            return False, f"Ya existe otro archivo en la ruta original: {orig}"

        try:
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            try:
                os.replace(src, orig)
            except OSError:
                shutil.move(src, orig)
        except OSError as exc:
            return False, f"No pude restaurar: {exc}"

        with db._lock, db.get_conn() as conn:
            conn.execute(
                """
                UPDATE comparator_files
                SET path=?, trashed=0, trashed_from=NULL, trashed_at=NULL, kept_copy_path=NULL, trash_reason=NULL
                WHERE id=?
                """,
                (orig, file_id),
            )
        _refresh_root_stats(item["root_path"], "ok")
        return True, f"Restaurado en {orig}"
    finally:
        _release_file_op(file_id)


def empty_trash():
    """Borra definitivamente SOLO los archivos que están dentro de la carpeta de Papelera."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, path, size_bytes, trashed_from FROM comparator_files WHERE trashed = 1"
        ).fetchall()
    if not rows:
        return True, "La papelera ya está vacía."

    deleted = 0
    freed = 0
    errors = 0
    deleted_ids = []
    catalog_paths = []
    for r in rows:
        fid = r["id"]
        if not _acquire_file_op(fid):
            continue
        try:
            p = r["path"] or ""
            if not _is_inside_trash(p):
                errors += 1
                continue
            try:
                if os.path.isfile(p):
                    os.remove(p)
                deleted += 1
                freed += int(r["size_bytes"] or 0)
                deleted_ids.append((fid,))
                catalog_paths.append((p,))
                if r["trashed_from"]:
                    catalog_paths.append((r["trashed_from"],))
            except OSError:
                errors += 1
        finally:
            _release_file_op(fid)

    if deleted_ids:
        with db._lock, db.get_conn() as conn:
            conn.executemany("DELETE FROM comparator_files WHERE id=?", deleted_ids)
            if catalog_paths:
                conn.executemany("DELETE FROM catalog_files WHERE path=?", catalog_paths)
        catalog.invalidate_build()

    msg = f"Papelera vaciada: {deleted} archivo(s) borrados, {format_bytes(freed)} liberados."
    if errors:
        msg += f" ({errors} no se pudieron borrar)."
    return True, msg


def delete_file_permanent(file_id):
    if not _acquire_file_op(file_id):
        return False, "Este archivo ya se está procesando..."
    try:
        item = get_file(file_id)
        if not item:
            return False, "Archivo no encontrado en el registro."
        path = item["path"]
        if not is_allowed_path(path):
            return False, "Ruta fuera de los límites permitidos."
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            return False, f"No pude borrar el archivo: {exc}"

        with db._lock, db.get_conn() as conn:
            conn.execute("DELETE FROM comparator_files WHERE id=?", (file_id,))
        db.delete_catalog_file(path)
        if item.get("trashed_from"):
            db.delete_catalog_file(item["trashed_from"])
        catalog.invalidate_build()
        _refresh_root_stats(item["root_path"], "ok")
        return True, f"Borrado definitivamente: {path}"
    finally:
        _release_file_op(file_id)
