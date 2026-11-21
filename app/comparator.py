"""Comparador y limpieza incremental de archivos en el NAS.

Permite escanear carpetas elegidas por el usuario (o todo /volume1) de forma
incremental y acumulativa en SQLite, pausar/reanudar/detener guardando el avance,
encontrar archivos pesados, duplicados idénticos (tamaño + hash parcial rápido)
y mismo nombre, previsualizar/comparar audiovisuales y mover a papelera o borrar
definitivamente.
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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT UNIQUE NOT NULL,
    root_path     TEXT NOT NULL,
    filename      TEXT NOT NULL,
    norm_name     TEXT NOT NULL,
    ext           TEXT NOT NULL,
    kind          TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    mtime         REAL NOT NULL DEFAULT 0,
    fast_hash     TEXT,
    duration_sec  REAL,
    width         INTEGER,
    height        INTEGER,
    video_codec   TEXT,
    audio_summary TEXT,
    probed        INTEGER DEFAULT 0,
    last_seen     REAL DEFAULT 0,
    trashed       INTEGER DEFAULT 0,
    trashed_from  TEXT,
    trashed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_comp_size ON comparator_files(size_bytes);
CREATE INDEX IF NOT EXISTS idx_comp_hash ON comparator_files(fast_hash);
CREATE INDEX IF NOT EXISTS idx_comp_norm ON comparator_files(norm_name);
CREATE INDEX IF NOT EXISTS idx_comp_root ON comparator_files(root_path);
CREATE INDEX IF NOT EXISTS idx_comp_trashed ON comparator_files(trashed);
"""

_STATE_LOCK = threading.Lock()
_JOB_LOCK = threading.Lock()
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
    "started_at": "",
    "finished_at": "",
    "updated_at": 0.0,
}

_NOISE_RE = re.compile(
    r"\b(1080p|720p|480p|2160p|4k|uhd|hdr|bluray|blu-ray|bdrip|brrip|webrip|web-dl|"
    r"webdl|hdrip|dvdrip|x264|x265|h264|h265|hevc|ac3|aac|dts|dual|latino|castellano|"
    r"espanol|spanish|ingles|english|subtitulada|vose|multi|copy|copia)\b",
    re.I,
)


def init_db():
    with db._lock, db.get_conn() as conn:
        conn.executescript(_SCHEMA)


def recover_state():
    with _STATE_LOCK:
        _STATE["running"] = False
        _STATE["paused"] = False
        _STATE["stopping"] = False
        _STATE["phase"] = "idle"
    _PAUSE_EVENT.set()
    _STOP_EVENT.clear()


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


# ---------------- Navegador de carpetas bajo /volume1 ----------------

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

    # Sugerencias rápidas: base + raíces configuradas en NAS Organizer
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
                if low in SKIP_DIR_NAMES or low.startswith(".") or low.startswith("@"):
                    continue
                if os.path.realpath(e.path) == t_real:
                    continue
                has_children = False
                try:
                    with os.scandir(e.path) as sub_it:
                        for se in sub_it:
                            if se.is_dir(follow_symlinks=False) and not se.name.startswith((".", "@")):
                                has_children = True
                                break
                except OSError:
                    pass
                items.append({"name": e.name, "path": e.path, "has_children": has_children})
        except OSError:
            pass

    return {"path": path, "parent": parent, "roots": quick, "items": items}


# ---------------- Escaneo incremental con Pausa / Reanudar / Detener ----------------

def start_scan(folder="", scan_all=False):
    target = allowed_base() if scan_all else (folder or "").strip()
    if not target:
        return False, "Elige una carpeta o pulsa Escanear todo."
    if not is_allowed_path(target) or not os.path.isdir(target):
        return False, f"La carpeta no existe o está fuera de {allowed_base()}."

    target = os.path.normpath(target)
    if not _JOB_LOCK.acquire(blocking=False):
        return False, "Ya hay un escaneo en curso. Puedes pausarlo o detenerlo primero."

    _STOP_EVENT.clear()
    _PAUSE_EVENT.set()
    _set_state(
        running=True,
        paused=False,
        stopping=False,
        phase="scanning",
        target=target,
        message=f"Escaneando {target}...",
        seen=0,
        added=0,
        updated=0,
        skipped=0,
        hashed=0,
        hash_total=0,
        current=target,
        errors=0,
        started_at=_now(),
        finished_at="",
    )

    t = threading.Thread(target=_scan_worker, args=(target,), name="nas-comparator-scan", daemon=True)
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
    _PAUSE_EVENT.set()  # despierta si estaba pausado
    return True, "Deteniendo escaneo (se guardará el avance)."


def _wait_if_paused():
    while not _PAUSE_EVENT.is_set():
        if _STOP_EVENT.is_set():
            return False
        time.sleep(0.2)
    return not _STOP_EVENT.is_set()


def _skip_dir(name, full_path, trash_real):
    low = (name or "").lower()
    if low in SKIP_DIR_NAMES or low.startswith("@eadir") or low.startswith("."):
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


def _scan_worker(target):
    scan_ts = time.time()
    trash_real = os.path.realpath(trash_dir())
    seen = 0
    added = 0
    updated = 0
    skipped = 0
    errors = 0
    stopped = False

    try:
        # Cargamos mapa ligero (path -> (size_bytes, mtime)) de archivos ya registrados
        # bajo esta carpeta para saltarnos en 0ms los que no cambiaron.
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
            for name in files:
                if not _wait_if_paused():
                    stopped = True
                    break
                if name.startswith("."):
                    continue
                fpath = os.path.join(dirpath, name)
                seen += 1
                try:
                    st = os.stat(fpath)
                except OSError:
                    errors += 1
                    continue

                size_b = int(st.st_size)
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
                    ext = os.path.splitext(name)[1].lower()
                    kind = _classify_kind(ext)
                    upserts.append((
                        fpath, target, name, normalize_name(name), ext, kind, size_b, mtime, scan_ts
                    ))

                if len(upserts) + len(seen_unchanged) >= 60:
                    _flush_batch(upserts, seen_unchanged, scan_ts)
                    upserts.clear()
                    seen_unchanged.clear()

                if seen % 25 == 0:
                    _set_state(
                        seen=seen,
                        added=added,
                        updated=updated,
                        skipped=skipped,
                        errors=errors,
                        current=fpath,
                        message=(
                            f"Escaneando {target}: {seen} vistos "
                            f"({added} nuevos, {updated} cambiados, {skipped} sin cambios)..."
                        ),
                    )
            if stopped:
                break

        # Guardamos cualquier lote pendiente (también si se detuvo a mitad).
        _flush_batch(upserts, seen_unchanged, scan_ts)
        upserts.clear()
        seen_unchanged.clear()

        # Solo eliminamos archivos desaparecidos de disco si el escaneo terminó completo.
        if not stopped:
            with db._lock, db.get_conn() as conn:
                conn.execute(
                    "DELETE FROM comparator_files "
                    "WHERE trashed = 0 AND (root_path = ? OR path LIKE ?) AND last_seen < ?",
                    (target, prefix + "%", scan_ts - 1.0),
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
                    if len(hash_batch) >= 25:
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
                WHERE trashed = 0 AND norm_name != ''
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
            reclaim = (len(decorated) - 1) * int(g["size_bytes"])
            out.append({
                "key": g["fast_hash"],
                "label": decorated[0]["filename"] if decorated else "",
                "count": len(decorated),
                "size_text": format_bytes(g["size_bytes"]),
                "reclaimable_text": format_bytes(reclaim),
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
            WHERE {clause} AND norm_name != ''
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
                "WHERE trashed = 0 AND norm_name = ? "
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


def list_trashed(limit=200):
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


# ---------------- Acciones sobre disco: Papelera / Restaurar / Borrar definitivo ----------------

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


def trash_file(file_id):
    item = get_file(file_id)
    if not item:
        return False, "Archivo no encontrado en el registro."
    src = item["path"]
    if not is_allowed_path(src):
        return False, "Ruta fuera de los límites permitidos."
    if not os.path.isfile(src):
        # Ya no existe en disco: limpiamos registro.
        with db._lock, db.get_conn() as conn:
            conn.execute("DELETE FROM comparator_files WHERE id=?", (file_id,))
        return True, "El archivo ya no estaba en disco; se quitó del registro."

    tdir = trash_dir()
    if not is_allowed_path(tdir):
        return False, "La carpeta de papelera está fuera del volumen permitido."
    try:
        os.makedirs(tdir, exist_ok=True)
        dest = _unique_dest(tdir, os.path.basename(src))
        try:
            os.replace(src, dest)
        except OSError:
            shutil.move(src, dest)
    except OSError as exc:
        return False, f"No pude mover a la papelera: {exc}"

    with db._lock, db.get_conn() as conn:
        conn.execute(
            "UPDATE comparator_files SET path=?, trashed=1, trashed_from=?, trashed_at=? WHERE id=?",
            (dest, src, _now(), file_id),
        )
    db.delete_catalog_file(src)
    catalog.invalidate_build()
    _refresh_root_stats(item["root_path"], "ok")
    return True, f"Movido a papelera: {dest}"


def restore_file(file_id):
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
            "UPDATE comparator_files SET path=?, trashed=0, trashed_from=NULL, trashed_at=NULL WHERE id=?",
            (orig, file_id),
        )
    _refresh_root_stats(item["root_path"], "ok")
    return True, f"Restaurado en {orig}"


def delete_file_permanent(file_id):
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
