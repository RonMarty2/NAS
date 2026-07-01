"""Capa de base de datos (SQLite, sin ORM para mantenerlo simple)."""
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("NAS_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "nas.db"))
DB_TIMEOUT_SECONDS = int(os.environ.get("NAS_DB_TIMEOUT_SECONDS", "60"))
DB_BUSY_TIMEOUT_MS = int(os.environ.get("NAS_DB_BUSY_TIMEOUT_MS", str(DB_TIMEOUT_SECONDS * 1000)))

_lock = threading.RLock()
_wal_lock = threading.Lock()
_wal_configured = False


def _ensure_dir():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)


def is_locked_error(exc):
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in text or "database table is locked" in text
    )


def _retry_sqlite(fn, attempts=4):
    delay = 0.25
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not is_locked_error(exc) or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 2.0)


class _RetryingConnection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        return _retry_sqlite(lambda: self._conn.execute(*args, **kwargs))

    def executemany(self, *args, **kwargs):
        return _retry_sqlite(lambda: self._conn.executemany(*args, **kwargs))

    def executescript(self, *args, **kwargs):
        return _retry_sqlite(lambda: self._conn.executescript(*args, **kwargs))

    def commit(self):
        return _retry_sqlite(self._conn.commit)

    def rollback(self):
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _configure_wal(conn):
    global _wal_configured
    if _wal_configured:
        return
    with _wal_lock:
        if _wal_configured:
            return
        conn.execute("PRAGMA journal_mode=WAL")
        _wal_configured = True


@contextmanager
def get_conn():
    """Devuelve una conexión SQLite con filas tipo dict.

    Modo WAL: permite leer (cambiar de pestaña) mientras el vigilante escribe,
    así la web no se queda esperando. busy_timeout evita errores 'database locked'.
    """
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    wrapped = _RetryingConnection(conn)
    wrapped.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    wrapped.execute("PRAGMA synchronous=NORMAL")
    try:
        yield wrapped
        wrapped.commit()
    except Exception:
        wrapped.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    original_path  TEXT NOT NULL,
    filename       TEXT NOT NULL,
    size_bytes     INTEGER DEFAULT 0,
    media_type     TEXT DEFAULT 'unknown',   -- movie | series | music | unknown
    status         TEXT DEFAULT 'pending',   -- pending | done | skipped | error
    detected_title TEXT,
    detected_year  INTEGER,
    season         INTEGER,
    episode        INTEGER,
    tmdb_id        INTEGER,
    chosen_title   TEXT,
    chosen_year    INTEGER,
    poster_url     TEXT,
    overview       TEXT,
    -- Campos específicos de música
    artist         TEXT,
    album          TEXT,
    track_no       TEXT,
    media_info     TEXT,            -- resumen JSON de calidad/idioma/códecs
    file_hash      TEXT,            -- SHA-256 para duplicados exactos
    file_hash_size INTEGER,
    dest_folder    TEXT,            -- carpeta base elegida por el usuario
    dest_path      TEXT,
    error          TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    processed_at   TEXT,
    UNIQUE(original_path)
);

-- Índices para que filtrar por estado/tipo no recorra toda la tabla. Importa
-- conforme crece el historial (cientos/miles de filas) en NAS modestos.
CREATE INDEX IF NOT EXISTS idx_items_status      ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_status_type ON items(status, media_type);
CREATE INDEX IF NOT EXISTS idx_items_processed_at ON items(status, processed_at DESC);

CREATE TABLE IF NOT EXISTS catalog_cache (
    cache_key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS catalog_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    size_bytes INTEGER DEFAULT 0,
    mtime_ns INTEGER DEFAULT 0,
    media_type TEXT DEFAULT 'movie',
    tmdb_id INTEGER,
    title TEXT,
    year INTEGER,
    poster_url TEXT,
    overview TEXT,
    quality TEXT,
    langs TEXT,
    last_seen REAL DEFAULT 0,
    missing INTEGER DEFAULT 0,
    source TEXT DEFAULT 'scan'
);

CREATE INDEX IF NOT EXISTS idx_catalog_files_tmdb ON catalog_files(tmdb_id);
CREATE INDEX IF NOT EXISTS idx_catalog_files_missing ON catalog_files(missing);

CREATE TABLE IF NOT EXISTS wishlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id INTEGER NOT NULL,
    media_type TEXT DEFAULT 'movie',
    title TEXT,
    year INTEGER,
    poster_url TEXT,
    overview TEXT,
    added_at TEXT DEFAULT (datetime('now')),
    UNIQUE(tmdb_id, media_type)
);
"""

# Columnas añadidas después de la primera versión (migración para BD existentes).
_MIGRATIONS = {
    "artist": "TEXT",
    "album": "TEXT",
    "track_no": "TEXT",
    "media_info": "TEXT",
    "file_hash": "TEXT",
    "file_hash_size": "INTEGER",
    "dest_folder": "TEXT",
    "quality": "TEXT",
    "langs": "TEXT",
    "match_attempts": "INTEGER",
    "cover_attempts": "INTEGER",
}


def init_db():
    with _lock, get_conn() as conn:
        _configure_wal(conn)
        conn.executescript(SCHEMA)
        # Migración: añade columnas que falten en bases de datos antiguas.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
        for col, coltype in _MIGRATIONS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE items ADD COLUMN {col} {coltype}")
        existing_catalog = {r["name"] for r in conn.execute("PRAGMA table_info(catalog_files)").fetchall()}
        catalog_migrations = {
            "quality": "TEXT",
            "langs": "TEXT",
            "last_seen": "REAL DEFAULT 0",
            "missing": "INTEGER DEFAULT 0",
            "source": "TEXT DEFAULT 'scan'",
            "match_attempts": "INTEGER DEFAULT 0",
            "import_root": "TEXT",
            "season": "INTEGER",
            "episode": "INTEGER",
        }
        for col, coltype in catalog_migrations.items():
            if col not in existing_catalog:
                conn.execute(f"ALTER TABLE catalog_files ADD COLUMN {col} {coltype}")


# ---------------- Ajustes (settings) ----------------

def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, "" if value is None else str(value)),
        )


def all_settings():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# ---------------- Cache del catalogo ----------------

def get_catalog_cache(cache_key):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value, updated_at FROM catalog_cache WHERE cache_key=?",
            (cache_key,),
        ).fetchone()
        return row


def get_catalog_cache_many(cache_keys):
    keys = [k for k in dict.fromkeys(cache_keys) if k]
    if not keys:
        return {}
    out = {}
    with get_conn() as conn:
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT cache_key, value, updated_at FROM catalog_cache WHERE cache_key IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                out[row["cache_key"]] = row
    return out


def list_catalog_cache_keys():
    with get_conn() as conn:
        return [r["cache_key"] for r in conn.execute("SELECT cache_key FROM catalog_cache").fetchall()]


def delete_catalog_cache_keys(keys):
    """Borra entradas de caché en lotes (limpieza de datos ya sin uso)."""
    keys = list(keys)
    if not keys:
        return 0
    with _lock, get_conn() as conn:
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM catalog_cache WHERE cache_key IN ({placeholders})", chunk)
    return len(keys)


def set_catalog_cache(cache_key, value, updated_at=None):
    if updated_at is None:
        updated_at = time.time()
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO catalog_cache(cache_key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (cache_key, value, updated_at),
        )


def upsert_catalog_file(path, filename, size_bytes=0, mtime_ns=0, **fields):
    allowed = {
        "media_type", "tmdb_id", "title", "year", "poster_url", "overview",
        "quality", "langs", "last_seen", "missing", "source", "import_root",
        "season", "episode",
    }
    data = {k: v for k, v in fields.items() if k in allowed}
    data.setdefault("last_seen", time.time())
    data.setdefault("missing", 0)
    data.setdefault("source", "scan")
    cols = ["path", "filename", "size_bytes", "mtime_ns"] + list(data.keys())
    vals = [path, filename, size_bytes, mtime_ns] + list(data.values())
    updates = ", ".join(f"{col}=excluded.{col}" for col in cols if col != "path")
    placeholders = ",".join("?" for _ in cols)
    with _lock, get_conn() as conn:
        conn.execute(
            f"INSERT INTO catalog_files({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(path) DO UPDATE SET {updates}",
            vals,
        )


def update_catalog_file(path, **fields):
    """Actualiza campos concretos de una fila del catálogo (por ruta)."""
    allowed = {"media_type", "tmdb_id", "title", "year", "poster_url", "overview",
               "quality", "langs", "missing", "source", "match_attempts", "season", "episode"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return
    cols = ", ".join(f"{k}=?" for k in data)
    vals = list(data.values()) + [path]
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE catalog_files SET {cols} WHERE path=?", vals)


def delete_catalog_file(path):
    with _lock, get_conn() as conn:
        conn.execute("DELETE FROM catalog_files WHERE path=?", (path,))


def get_catalog_file_by_path(path):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM catalog_files WHERE path=?", (path,)).fetchone()


def touch_catalog_file(path, last_seen=None, import_root=None):
    """Refresca un archivo ya catalogado (visto de nuevo en un re-escaneo sin
    cambios). Si trae import_root y el archivo aún no tenía uno guardado, lo
    rellena aquí: si no, los re-escaneos nunca corrigen archivos antiguos
    (se saltan por 'sin cambios' antes de llegar a guardar la carpeta)."""
    if last_seen is None:
        last_seen = time.time()
    with _lock, get_conn() as conn:
        if import_root:
            conn.execute(
                "UPDATE catalog_files SET last_seen=?, missing=0, "
                "import_root=COALESCE(import_root, ?) WHERE path=?",
                (last_seen, import_root, path),
            )
        else:
            conn.execute(
                "UPDATE catalog_files SET last_seen=?, missing=0 WHERE path=?",
                (last_seen, path),
            )


def list_catalog_files(missing=None):
    q = "SELECT * FROM catalog_files WHERE 1=1"
    args = []
    if missing is not None:
        q += " AND missing=?"
        args.append(1 if missing else 0)
    q += " ORDER BY title COLLATE NOCASE, year"
    with get_conn() as conn:
        return conn.execute(q, args).fetchall()


def mark_catalog_missing_before(scan_ts):
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE catalog_files SET missing=1 WHERE source='scan' AND last_seen<?",
            (scan_ts,),
        )


def mark_catalog_missing_under_root(root, scan_ts):
    root = os.path.normpath(root or "")
    if not root:
        return
    prefixes = {root.rstrip("/\\") + os.sep}
    prefixes.add(root.rstrip("/\\") + ("/" if os.sep == "\\" else "\\"))
    clauses = ["path=?"]
    args = [scan_ts, root]
    for prefix in sorted(prefixes):
        clauses.append("substr(path,1,?)=?")
        args.extend([len(prefix), prefix])
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE catalog_files SET missing=1 "
            "WHERE source='scan' AND last_seen<? AND (" + " OR ".join(clauses) + ")",
            args,
        )


# ---------------- Lista de deseos ----------------

def list_wishlist():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM wishlist ORDER BY added_at DESC").fetchall()


def add_wishlist_item(tmdb_id, media_type, title, year, poster_url, overview):
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO wishlist(tmdb_id, media_type, title, year, poster_url, overview) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(tmdb_id, media_type) DO NOTHING",
            (tmdb_id, media_type, title, year, poster_url, overview),
        )


def remove_wishlist_item(item_id):
    with _lock, get_conn() as conn:
        conn.execute("DELETE FROM wishlist WHERE id=?", (item_id,))


# ---------------- Items (cola y historial) ----------------

def add_item(original_path, filename, size_bytes=0):
    """Inserta un archivo nuevo si no existe. Devuelve el id o None si ya existía."""
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO items(original_path, filename, size_bytes) VALUES(?,?,?)",
            (original_path, filename, size_bytes),
        )
        return cur.lastrowid if cur.rowcount else None


def get_item(item_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def update_item(item_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [item_id]
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE items SET {cols} WHERE id=?", vals)


def list_items(status=None, media_type=None):
    q = "SELECT * FROM items WHERE 1=1"
    args = []
    if status:
        q += " AND status=?"
        args.append(status)
    if media_type:
        q += " AND media_type=?"
        args.append(media_type)
    q += " ORDER BY created_at DESC, id DESC"
    with get_conn() as conn:
        return conn.execute(q, args).fetchall()


def list_recent_done(limit=15):
    """Últimos items movidos, para 'Agregado recientemente' del dashboard.

    Usa LIMIT en SQL (con el índice de processed_at) en vez de traer todo el
    historial a memoria y ordenar en Python: importa en NAS modestos conforme
    crece el historial."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM items WHERE status='done' AND processed_at IS NOT NULL "
            "ORDER BY processed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def delete_item(item_id):
    with _lock, get_conn() as conn:
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))


def reset_processing():
    """Devuelve a 'pending' los items que quedaron 'processing' (p.ej. si el
    contenedor se reinició a mitad de un movimiento). Evita que se queden
    'Moviendo…' para siempre y que la página se recargue sin parar."""
    with _lock, get_conn() as conn:
        conn.execute("UPDATE items SET status='pending' WHERE status='processing'")


def count_processing():
    """Nº de items moviéndose ahora mismo (para saber si seguir sondeando)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM items WHERE status='processing'"
        ).fetchone()["c"]


def pending_counts():
    """Devuelve {media_type: nº pendientes} para mostrar contadores en las pestañas."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT media_type, COUNT(*) AS c FROM items WHERE status='pending' "
            "GROUP BY media_type"
        ).fetchall()
        return {r["media_type"]: r["c"] for r in rows}


def reset_match_attempts():
    """Reinicia el contador de intentos de TMDB para lo pendiente. Se llama al
    guardar ajustes (p.ej. al poner la API key) para que se vuelva a intentar."""
    with _lock, get_conn() as conn:
        conn.execute("UPDATE items SET match_attempts=0 WHERE status='pending'")


def reset_catalog_match_attempts():
    """Reinicia el contador de intentos de TMDB del Catálogo (solo lo que sigue
    sin reconocer). Útil tras mejorar la lógica de búsqueda: lo que falló con
    la lógica vieja se había quedado sin reintentar nunca más."""
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE catalog_files SET match_attempts=0 WHERE tmdb_id IS NULL AND match_attempts>0"
        )
