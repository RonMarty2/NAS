"""Capa de base de datos (SQLite, sin ORM para mantenerlo simple)."""
import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("NAS_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "nas.db"))

_lock = threading.Lock()


def _ensure_dir():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)


@contextmanager
def get_conn():
    """Devuelve una conexión SQLite con filas tipo dict.

    Modo WAL: permite leer (cambiar de pestaña) mientras el vigilante escribe,
    así la web no se queda esperando. busy_timeout evita errores 'database locked'.
    """
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
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
}


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Migración: añade columnas que falten en bases de datos antiguas.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
        for col, coltype in _MIGRATIONS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE items ADD COLUMN {col} {coltype}")


# ---------------- Ajustes (settings) ----------------

def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, "" if value is None else str(value)),
        )


def all_settings():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


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
    with get_conn() as conn:
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


def delete_item(item_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))


def reset_processing():
    """Devuelve a 'pending' los items que quedaron 'processing' (p.ej. si el
    contenedor se reinició a mitad de un movimiento). Evita que se queden
    'Moviendo…' para siempre y que la página se recargue sin parar."""
    with get_conn() as conn:
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
    with get_conn() as conn:
        conn.execute("UPDATE items SET match_attempts=0 WHERE status='pending'")
