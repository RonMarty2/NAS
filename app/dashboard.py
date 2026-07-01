"""Panel resumen: vista general de toda la biblioteca al entrar a la app."""
import os
import shutil
import threading
import time

from . import catalog, config, db
from .metadata import tmdb

# "/" es la página que más se visita (es la raíz), así que cachear evita
# recalcular conteos/discos/recientes en cada carga. Se invalida sola por TTL;
# no hace falta invalidación activa porque el dashboard no necesita ser
# instantáneo al segundo, solo fluido.
_BUILD_TTL = 20.0
_cache = None
_cache_at = 0.0
_cache_lock = threading.Lock()


def _human_size(n):
    n = float(n or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _counts():
    movie_total = len(catalog.owned_movie_entries())
    series_total = len(catalog.owned_series_ids())
    music_total = len(db.list_items(status="done", media_type="music"))
    pending_total = sum(db.pending_counts().values())
    return {
        "movies": movie_total,
        "series": series_total,
        "music": music_total,
        "pending": pending_total,
    }


def _disk_usage():
    """Espacio libre/usado por cada volumen distinto entre tus carpetas de
    biblioteca. Varias carpetas en el mismo disco se agrupan (no se cuentan
    dos veces)."""
    roots = []
    for path in [
        config.get("default_movie_dir"),
        config.get("default_series_dir"),
        config.get("default_music_dir"),
        *[x.strip() for x in config.get("library_roots").split(",") if x.strip()],
    ]:
        if path and path not in roots and os.path.isdir(path):
            roots.append(path)

    seen_devices = {}
    out = []
    for path in roots:
        try:
            st = os.stat(path)
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        dev = st.st_dev
        if dev in seen_devices:
            seen_devices[dev]["paths"].append(path)
            continue
        entry = {
            "paths": [path],
            "total": usage.total,
            "used": usage.total - usage.free,
            "free": usage.free,
            "total_h": _human_size(usage.total),
            "used_h": _human_size(usage.total - usage.free),
            "free_h": _human_size(usage.free),
            "pct_used": round((usage.total - usage.free) / usage.total * 100) if usage.total else 0,
        }
        seen_devices[dev] = entry
        out.append(entry)
    return out


def _recently_added(limit=15):
    """Últimos movidos, con UNA tarjeta por título: las series no aparecen una
    vez por episodio (p.ej. 'The Boys' x9), sino una sola vez."""
    out = []
    seen = set()
    # Traemos más de la cuenta y colapsamos por serie/película para llenar el hueco.
    for it in db.list_recent_done(limit * 8):
        title = it["chosen_title"] or it["detected_title"] or it["filename"]
        # Clave: por serie usamos su id/título (no el episodio); pelis por id/título.
        key = (it["media_type"], it["tmdb_id"] or (title or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "title": title,
            "year": it["chosen_year"] or it["detected_year"],
            "media_type": it["media_type"],
            "poster_url": it["poster_url"],
            "processed_at": it["processed_at"],
        })
        if len(out) >= limit:
            break
    return out


def build():
    global _cache, _cache_at
    snapshot = _cache
    if snapshot is not None and (time.monotonic() - _cache_at) < _BUILD_TTL:
        return snapshot
    with _cache_lock:
        if _cache is not None and (time.monotonic() - _cache_at) < _BUILD_TTL:
            return _cache
        result = {
            "counts": _counts(),
            "disks": _disk_usage(),
            "recent": _recently_added(),
            "tmdb_configured": tmdb.configured(),
        }
        _cache = result
        _cache_at = time.monotonic()
        return result
