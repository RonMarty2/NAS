import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager


DB_PATH = os.environ.get(
    "NAS_AUDITOR_DB",
    os.path.join(os.path.dirname(__file__), "..", "data", "pc_audit.db"),
)

_LOCK = threading.RLock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    root TEXT,
    parent TEXT,
    filename TEXT NOT NULL,
    ext TEXT,
    size_bytes INTEGER DEFAULT 0,
    mtime_ns INTEGER DEFAULT 0,
    last_seen REAL DEFAULT 0,
    missing INTEGER DEFAULT 0,
    analysis_status TEXT DEFAULT 'new',
    analysis_error TEXT,
    analyzed_at TEXT,
    duration_seconds REAL,
    width INTEGER,
    height INTEGER,
    video_codec TEXT,
    audio_json TEXT,
    subtitle_json TEXT,
    audio_summary TEXT,
    flags TEXT,
    verify_status TEXT DEFAULT '',
    verify_error TEXT,
    verified_at TEXT,
    reviewed INTEGER DEFAULT 0,
    category TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    moved_to TEXT
);

CREATE INDEX IF NOT EXISTS idx_media_missing ON media_files(missing);
CREATE INDEX IF NOT EXISTS idx_media_analysis ON media_files(analysis_status);
CREATE INDEX IF NOT EXISTS idx_media_root ON media_files(root);
CREATE INDEX IF NOT EXISTS idx_media_mtime ON media_files(mtime_ns);
"""


MIGRATIONS = {
    "root": "TEXT",
    "parent": "TEXT",
    "ext": "TEXT",
    "last_seen": "REAL DEFAULT 0",
    "missing": "INTEGER DEFAULT 0",
    "analysis_status": "TEXT DEFAULT 'new'",
    "analysis_error": "TEXT",
    "analyzed_at": "TEXT",
    "duration_seconds": "REAL",
    "width": "INTEGER",
    "height": "INTEGER",
    "video_codec": "TEXT",
    "audio_json": "TEXT",
    "subtitle_json": "TEXT",
    "audio_summary": "TEXT",
    "flags": "TEXT",
    "verify_status": "TEXT DEFAULT ''",
    "verify_error": "TEXT",
    "verified_at": "TEXT",
    "reviewed": "INTEGER DEFAULT 0",
    "category": "TEXT DEFAULT ''",
    "notes": "TEXT DEFAULT ''",
    "moved_to": "TEXT",
}


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _LOCK, get_conn() as conn:
        conn.executescript(SCHEMA)
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(media_files)").fetchall()
        }
        for col, decl in MIGRATIONS.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE media_files ADD COLUMN {col} {decl}")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_setting(key, default=""):
    init_db_if_needed()
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    init_db_if_needed()
    with _LOCK, get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def upsert_seen(path, root, stat_result, scan_ts):
    filename = os.path.basename(path)
    parent = os.path.dirname(path)
    ext = os.path.splitext(filename)[1].lower()
    size = int(stat_result.st_size)
    mtime_ns = int(stat_result.st_mtime_ns)
    with _LOCK, get_conn() as conn:
        row = conn.execute(
            "SELECT id,size_bytes,mtime_ns FROM media_files WHERE path=?",
            (path,),
        ).fetchone()
        if row:
            changed = row["size_bytes"] != size or row["mtime_ns"] != mtime_ns
            conn.execute(
                """
                UPDATE media_files
                SET root=?, parent=?, filename=?, ext=?, size_bytes=?, mtime_ns=?,
                    last_seen=?, missing=0,
                    analysis_status=CASE WHEN ? THEN 'changed' ELSE analysis_status END,
                    verify_status=CASE WHEN ? THEN '' ELSE verify_status END
                WHERE id=?
                """,
                (root, parent, filename, ext, size, mtime_ns, scan_ts, changed, changed, row["id"]),
            )
            return "changed" if changed else "same"
        conn.execute(
            """
            INSERT INTO media_files(path,root,parent,filename,ext,size_bytes,mtime_ns,last_seen,missing,analysis_status)
            VALUES(?,?,?,?,?,?,?,?,0,'new')
            """,
            (path, root, parent, filename, ext, size, mtime_ns, scan_ts),
        )
        return "new"


def mark_missing_for_roots(roots, scan_ts):
    with _LOCK, get_conn() as conn:
        for root in roots:
            conn.execute(
                "UPDATE media_files SET missing=1 WHERE root=? AND last_seen<?",
                (root, scan_ts),
            )


def list_files(filters=None, limit=300):
    filters = filters or {}
    clauses = ["1=1"]
    args = []
    q = (filters.get("q") or "").strip()
    if q:
        clauses.append("(filename LIKE ? OR parent LIKE ? OR path LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like, like])
    root = (filters.get("root") or "").strip()
    if root:
        clauses.append("root=?")
        args.append(root)
    flag = (filters.get("flag") or "").strip()
    if flag:
        if flag == "missing":
            clauses.append("missing=1")
        elif flag == "unanalyzed":
            clauses.append("analysis_status IN ('new','changed','error')")
        elif flag == "reviewed":
            clauses.append("reviewed=1")
        else:
            clauses.append("(',' || COALESCE(flags,'') || ',') LIKE ?")
            args.append(f"%,{flag},%")
    category = (filters.get("category") or "").strip()
    if category:
        clauses.append("category=?")
        args.append(category)
    sql = "SELECT * FROM media_files WHERE " + " AND ".join(clauses)
    sql += " ORDER BY missing ASC, analysis_status IN ('new','changed') DESC, filename COLLATE NOCASE LIMIT ?"
    args.append(int(limit))
    with get_conn() as conn:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]


def get_file(file_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM media_files WHERE id=?", (file_id,)).fetchone()
        return dict(row) if row else None


def pending_analysis_ids(limit=25):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id FROM media_files
            WHERE missing=0 AND analysis_status IN ('new','changed','error')
            ORDER BY analysis_status='error', mtime_ns DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [row["id"] for row in rows]


def update_analysis(file_id, result):
    audio = result.get("audio") or []
    subtitles = result.get("subtitles") or []
    flags = ",".join(sorted(set(result.get("flags") or [])))
    with _LOCK, get_conn() as conn:
        conn.execute(
            """
            UPDATE media_files
            SET analysis_status=?, analysis_error=?, analyzed_at=?,
                duration_seconds=?, width=?, height=?, video_codec=?,
                audio_json=?, subtitle_json=?, audio_summary=?, flags=?
            WHERE id=?
            """,
            (
                result.get("status", "ok"),
                result.get("error", ""),
                _now(),
                result.get("duration_seconds"),
                result.get("width"),
                result.get("height"),
                result.get("video_codec", ""),
                json.dumps(audio, ensure_ascii=False),
                json.dumps(subtitles, ensure_ascii=False),
                result.get("audio_summary", ""),
                flags,
                file_id,
            ),
        )


def update_verify(file_id, status, error=""):
    with _LOCK, get_conn() as conn:
        conn.execute(
            "UPDATE media_files SET verify_status=?, verify_error=?, verified_at=? WHERE id=?",
            (status, error, _now(), file_id),
        )


def update_review(file_id, reviewed=None, category=None, notes=None):
    fields = {}
    if reviewed is not None:
        fields["reviewed"] = 1 if reviewed else 0
    if category is not None:
        fields["category"] = category
    if notes is not None:
        fields["notes"] = notes
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [file_id]
    with _LOCK, get_conn() as conn:
        conn.execute(f"UPDATE media_files SET {cols} WHERE id=?", vals)


def mark_moved(file_id, new_path):
    item = get_file(file_id)
    if not item:
        return
    try:
        stat_result = os.stat(new_path)
    except OSError:
        stat_result = None
    with _LOCK, get_conn() as conn:
        conn.execute("DELETE FROM media_files WHERE id=?", (file_id,))
        if stat_result:
            filename = os.path.basename(new_path)
            conn.execute(
                """
                INSERT OR REPLACE INTO media_files(
                    path,root,parent,filename,ext,size_bytes,mtime_ns,last_seen,missing,
                    analysis_status,analysis_error,analyzed_at,duration_seconds,width,height,
                    video_codec,audio_json,subtitle_json,audio_summary,flags,verify_status,
                    verify_error,verified_at,reviewed,category,notes,moved_to
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_path,
                    item.get("root"),
                    os.path.dirname(new_path),
                    filename,
                    os.path.splitext(filename)[1].lower(),
                    int(stat_result.st_size),
                    int(stat_result.st_mtime_ns),
                    time.time(),
                    0,
                    item.get("analysis_status"),
                    item.get("analysis_error"),
                    item.get("analyzed_at"),
                    item.get("duration_seconds"),
                    item.get("width"),
                    item.get("height"),
                    item.get("video_codec"),
                    item.get("audio_json"),
                    item.get("subtitle_json"),
                    item.get("audio_summary"),
                    item.get("flags"),
                    item.get("verify_status"),
                    item.get("verify_error"),
                    item.get("verified_at"),
                    item.get("reviewed"),
                    item.get("category"),
                    item.get("notes"),
                    item.get("path"),
                ),
            )


def roots_seen():
    with get_conn() as conn:
        return [
            row["root"]
            for row in conn.execute(
                "SELECT DISTINCT root FROM media_files WHERE root IS NOT NULL AND root<>'' ORDER BY root"
            ).fetchall()
        ]


def stats():
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN missing=1 THEN 1 ELSE 0 END) AS missing,
              SUM(CASE WHEN analysis_status IN ('new','changed') AND missing=0 THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN analysis_status='error' AND missing=0 THEN 1 ELSE 0 END) AS errors,
              SUM(CASE WHEN reviewed=1 THEN 1 ELSE 0 END) AS reviewed
            FROM media_files
            """
        ).fetchone()
        return dict(row)


def init_db_if_needed():
    if not os.path.exists(DB_PATH):
        init_db()


def _now():
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
