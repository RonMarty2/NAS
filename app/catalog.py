"""Catalogo visual: sagas, peliculas que tienes y faltantes."""
import json
import os
import threading
import time

from . import config, db, identify
from .metadata import tmdb


STATUS_KEY = "catalog_status"
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
SKIP_DIR_NAMES = {"@eadir", "#recycle", "@tmp", ".trash", "$recycle.bin"}

# Caché del catálogo ya armado. build_catalog recorre toda la tabla y parsea el
# JSON de cada película/saga; con bibliotecas grandes eso es caro y se repetía en
# cada apertura de la pestaña (y en cada recarga del sondeo durante una
# importación). Una caché corta lo hace fluido; se refresca solo tras unos
# segundos o cuando algo cambia (mover una peli, importar). Ver invalidate_build().
_BUILD_TTL = 15.0
_build_cache = None
_build_cache_at = 0.0
_build_lock = threading.Lock()


def invalidate_build():
    """Olvida el catálogo armado para que la próxima vista lo recalcule."""
    global _build_cache
    _build_cache = None


def status():
    raw = db.get_setting(STATUS_KEY, "")
    if not raw:
        return {"visible": False, "running": False, "message": ""}
    try:
        data = json.loads(raw)
    except Exception:
        return {"visible": False, "running": False, "message": ""}
    data["visible"] = bool(data.get("running") or data.get("message"))
    return data


def set_status(data):
    db.set_setting(STATUS_KEY, json.dumps(data or {}, ensure_ascii=False))


def owned_movie_items():
    """Compatibilidad: devuelve items movidos por el organizador."""
    return db.list_items(status="done", media_type="movie")


def owned_movie_entries(done_items=None, catalog_rows=None):
    entries = []
    seen = set()
    done_items = db.list_items(status="done", media_type="movie") if done_items is None else done_items
    catalog_rows = db.list_catalog_files(missing=False) if catalog_rows is None else catalog_rows
    for item in done_items:
        tmdb_id = _int(item["tmdb_id"])
        if not tmdb_id or tmdb_id in seen:
            continue  # una tarjeta por película, aunque haya varias copias/versiones
        seen.add(tmdb_id)
        entries.append(_entry_from_item(item, "organizer"))
    for row in catalog_rows:
        if row["media_type"] != "movie":
            continue
        tmdb_id = _int(row["tmdb_id"])
        if not tmdb_id or tmdb_id in seen:
            continue
        seen.add(tmdb_id)
        entries.append(_entry_from_catalog_file(row))
    return entries


def update_catalog(limit=60, progress=None):
    """Completa cache de peliculas y sagas. Corre bajo demanda."""
    if not tmdb.configured():
        return {"done": 0, "total": 0, "message": "Falta configurar la API key de TMDB."}

    items = owned_movie_entries()
    total = len(items)
    limit = max(0, int(limit or 0))
    if limit <= 0:
        return {"done": 0, "total": total, "collections": 0, "message": "Catalogo actualizado: 0 consulta(s)."}
    queries = 0
    scanned = 0
    collection_ids = set()

    for item in items:
        scanned += 1
        tmdb_id = _int(item["tmdb_id"])
        detail = movie_detail(tmdb_id)
        if (not detail or _cache_stale(f"movie:{tmdb_id}")) and queries < limit:
            detail = tmdb.movie_details(tmdb_id)
            if detail:
                _set_json(f"movie:{tmdb_id}", detail)
            queries += 1
        collection = (detail or {}).get("collection") or {}
        if collection.get("id"):
            collection_ids.add(_int(collection["id"]))
        if progress and scanned % 20 == 0:
            progress({"done": queries, "total": total, "current": item["filename"]})
        if queries >= limit:
            break

    collection_done = 0
    for collection_id in sorted(x for x in collection_ids if x):
        if queries >= limit:
            break
        if _cache_stale(f"collection:{collection_id}"):
            detail = tmdb.collection_details(collection_id)
            if detail:
                _set_json(f"collection:{collection_id}", detail)
            queries += 1
        collection_done += 1
        if progress:
            progress({"done": queries, "total": total + len(collection_ids), "current": f"Saga {collection_id}"})

    return {
        "done": queries,
        "total": total,
        "collections": collection_done,
        "message": f"Catalogo actualizado: {queries} consulta(s).",
    }


def build_catalog(force=False):
    """Catálogo armado, con caché corta para que la pestaña sea fluida."""
    global _build_cache, _build_cache_at
    if not force and _build_cache is not None and (time.monotonic() - _build_cache_at) < _BUILD_TTL:
        return _build_cache
    with _build_lock:
        if not force and _build_cache is not None and (time.monotonic() - _build_cache_at) < _BUILD_TTL:
            return _build_cache
        result = _build_catalog_uncached()
        _build_cache = result
        _build_cache_at = time.monotonic()
        return result


def _build_catalog_uncached():
    done_items = db.list_items(status="done", media_type="movie")
    catalog_rows = db.list_catalog_files(missing=False)
    items = owned_movie_entries(done_items=done_items, catalog_rows=catalog_rows)
    owned_by_id = {_int(item["tmdb_id"]): item for item in items if _int(item["tmdb_id"])}
    movie_cache = db.get_catalog_cache_many(f"movie:{_int(item['tmdb_id'])}" for item in items if _int(item["tmdb_id"]))
    movie_details = {}
    collection_ids = set()
    for item in items:
        tmdb_id = _int(item["tmdb_id"])
        detail = _json_from_row(movie_cache.get(f"movie:{tmdb_id}"))
        movie_details[tmdb_id] = detail
        collection = (detail or {}).get("collection") or {}
        if collection.get("id"):
            collection_ids.add(_int(collection["id"]))
    collection_cache = db.get_catalog_cache_many(f"collection:{collection_id}" for collection_id in collection_ids)
    collections = {}
    standalone = []
    companies = {}
    uncached = 0
    series = _series_entries(catalog_rows)

    for item in items:
        tmdb_id = _int(item["tmdb_id"])
        detail = movie_details.get(tmdb_id)
        if not detail:
            uncached += 1
            standalone.append(_movie_from_item(item, owned=True))
            continue
        _collect_companies(companies, detail)
        collection = detail.get("collection") or {}
        collection_id = _int(collection.get("id"))
        if collection_id:
            entry = collections.setdefault(
                collection_id,
                {
                    "id": collection_id,
                    "name": collection.get("name") or "Saga",
                    "poster_url": collection.get("poster_url") or detail.get("poster_url"),
                    "backdrop_url": collection.get("backdrop_url"),
                    "parts": [],
                    "owned_count": 0,
                    "total_count": 0,
                    "missing_count": 0,
                },
            )
            collection_detail = _json_from_row(collection_cache.get(f"collection:{collection_id}"))
            if collection_detail:
                entry["name"] = collection_detail.get("name") or entry["name"]
                entry["poster_url"] = collection_detail.get("poster_url") or entry["poster_url"]
                entry["backdrop_url"] = collection_detail.get("backdrop_url")
                entry["parts"] = [_part_with_owned(part, owned_by_id) for part in collection_detail.get("parts") or []]
            else:
                entry["parts"].append(_part_with_owned(detail, owned_by_id))
        else:
            standalone.append(_detail_to_movie(detail, owned=True, item=item))

    for row in catalog_rows:
        if row["media_type"] == "movie" and not _int(row["tmdb_id"]):
            uncached += 1
            standalone.append(_movie_from_item(_entry_from_catalog_file(row), owned=True))

    for entry in collections.values():
        unique = {}
        for part in entry["parts"]:
            unique[part["tmdb_id"]] = part
        entry["parts"] = sorted(unique.values(), key=lambda p: (p.get("release_date") or "9999", p.get("title") or ""))
        entry["total_count"] = len(entry["parts"])
        entry["owned_count"] = sum(1 for p in entry["parts"] if p.get("owned"))
        entry["missing_count"] = max(0, entry["total_count"] - entry["owned_count"])

    collections_list = sorted(
        collections.values(),
        key=lambda c: (c["missing_count"] == 0, -c["owned_count"], c["name"].lower()),
    )
    standalone = sorted(standalone, key=lambda m: (m.get("year") or 9999, m.get("title") or ""))
    companies_list = sorted(companies.values(), key=lambda c: (-c["count"], c["name"].lower()))[:18]

    return {
        "collections": collections_list,
        "standalone": standalone[:80],
        "companies": companies_list,
        "series": series[:80],
        "owned_total": len(items),
        "imported_total": len(catalog_rows),
        "uncached": uncached,
        "tmdb_configured": tmdb.configured(),
    }


# Secciones de "Descubre": género, década, estudio y populares/taquilleras.
# Cada una es una consulta a /discover de TMDB, cacheada mucho tiempo (cambian poco).
DISCOVER_SECTIONS = [
    # Populares / taquilleras / mejor valoradas
    {"key": "pop:popular",   "group": "Populares",  "title": "Más populares",      "filters": {"sort_by": "popularity.desc"}},
    {"key": "pop:revenue",   "group": "Populares",  "title": "Más taquilleras",    "filters": {"sort_by": "revenue.desc"}},
    {"key": "pop:top",       "group": "Populares",  "title": "Mejor valoradas",    "filters": {"sort_by": "vote_average.desc", "vote_count.gte": 2000}},
    # Por género (ids de TMDB)
    {"key": "genre:28",   "group": "Géneros", "title": "Acción",           "filters": {"with_genres": "28"}},
    {"key": "genre:12",   "group": "Géneros", "title": "Aventura",         "filters": {"with_genres": "12"}},
    {"key": "genre:16",   "group": "Géneros", "title": "Animación",        "filters": {"with_genres": "16"}},
    {"key": "genre:35",   "group": "Géneros", "title": "Comedia",          "filters": {"with_genres": "35"}},
    {"key": "genre:27",   "group": "Géneros", "title": "Terror",           "filters": {"with_genres": "27"}},
    {"key": "genre:878",  "group": "Géneros", "title": "Ciencia ficción",  "filters": {"with_genres": "878"}},
    {"key": "genre:18",   "group": "Géneros", "title": "Drama",            "filters": {"with_genres": "18"}},
    {"key": "genre:53",   "group": "Géneros", "title": "Suspenso",         "filters": {"with_genres": "53"}},
    {"key": "genre:10751", "group": "Géneros", "title": "Familia",         "filters": {"with_genres": "10751"}},
    # Por década
    {"key": "decade:2020", "group": "Décadas", "title": "De los 2020s", "filters": {"primary_release_date.gte": "2020-01-01", "primary_release_date.lte": "2029-12-31", "sort_by": "revenue.desc"}},
    {"key": "decade:2010", "group": "Décadas", "title": "De los 2010s", "filters": {"primary_release_date.gte": "2010-01-01", "primary_release_date.lte": "2019-12-31", "sort_by": "revenue.desc"}},
    {"key": "decade:2000", "group": "Décadas", "title": "De los 2000s", "filters": {"primary_release_date.gte": "2000-01-01", "primary_release_date.lte": "2009-12-31", "sort_by": "revenue.desc"}},
    {"key": "decade:1990", "group": "Décadas", "title": "De los 90s",   "filters": {"primary_release_date.gte": "1990-01-01", "primary_release_date.lte": "1999-12-31", "sort_by": "revenue.desc"}},
    {"key": "decade:1980", "group": "Décadas", "title": "De los 80s",   "filters": {"primary_release_date.gte": "1980-01-01", "primary_release_date.lte": "1989-12-31", "sort_by": "revenue.desc"}},
    # Por estudio (ids de compañía de TMDB)
    {"key": "studio:420", "group": "Estudios", "title": "Marvel Studios",   "filters": {"with_companies": "420"}},
    {"key": "studio:174", "group": "Estudios", "title": "Warner Bros.",     "filters": {"with_companies": "174"}},
    {"key": "studio:3",   "group": "Estudios", "title": "Pixar",            "filters": {"with_companies": "3"}},
    {"key": "studio:33",  "group": "Estudios", "title": "Universal",        "filters": {"with_companies": "33"}},
    {"key": "studio:2",   "group": "Estudios", "title": "Walt Disney",      "filters": {"with_companies": "2"}},
]
DISCOVER_TTL_SECONDS = 7 * 24 * 60 * 60


def library_duplicates():
    """Películas de la biblioteca que existen en más de un archivo (mismo tmdb_id).

    Devuelve grupos con la película y la lista de copias (ruta, peso, calidad)
    para que el usuario pueda borrar una o reorganizar."""
    by_id = {}
    for row in db.list_catalog_files(missing=False):
        if row["media_type"] != "movie":
            continue
        tmdb_id = _int(row["tmdb_id"])
        if not tmdb_id:
            continue  # sin identificar no podemos saber si es la misma peli
        by_id.setdefault(tmdb_id, []).append(row)

    groups = []
    for tmdb_id, rows in by_id.items():
        # Copias en rutas distintas (mismo archivo movido no cuenta).
        paths = {}
        for r in rows:
            paths.setdefault(r["path"], r)
        if len(paths) < 2:
            continue
        copies = []
        for r in paths.values():
            copies.append({
                "path": r["path"],
                "folder": os.path.dirname(r["path"]),
                "filename": r["filename"],
                "size_bytes": r["size_bytes"] or 0,
                "size_h": _human_size(r["size_bytes"] or 0),
                "quality": r["quality"] or "",
                "langs": r["langs"] or "",
            })
        copies.sort(key=lambda c: (-(c["size_bytes"] or 0), c["path"]))
        first = rows[0]
        groups.append({
            "tmdb_id": tmdb_id,
            "title": first["title"] or first["filename"],
            "year": first["year"],
            "poster_url": first["poster_url"],
            "count": len(copies),
            "copies": copies,
        })
    groups.sort(key=lambda g: (g["title"] or "").lower())
    return groups


def _human_size(n):
    n = float(n or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def owned_movie_ids():
    """Conjunto de tmdb_id de películas que el usuario ya tiene."""
    return {_int(e["tmdb_id"]) for e in owned_movie_entries() if _int(e["tmdb_id"])}


def build_discover():
    """Arma las secciones de 'Descubre' desde la cache (sin llamar a la red).

    Marca cuáles tienes y cuáles te faltan. Devuelve grupos ordenados."""
    owned = owned_movie_ids()
    keys = [f"discover:{s['key']}" for s in DISCOVER_SECTIONS]
    cached = db.get_catalog_cache_many(keys)
    groups = {}
    any_cached = False
    for spec in DISCOVER_SECTIONS:
        movies = _json_from_row(cached.get(f"discover:{spec['key']}")) or []
        if not movies:
            continue
        any_cached = True
        cards = []
        for m in movies:
            tmdb_id = _int(m.get("tmdb_id"))
            cards.append({
                "tmdb_id": tmdb_id,
                "title": m.get("title") or "",
                "year": m.get("year"),
                "poster_url": m.get("poster_url"),
                "owned": tmdb_id in owned,
            })
        owned_count = sum(1 for c in cards if c["owned"])
        groups.setdefault(spec["group"], []).append({
            "title": spec["title"],
            "movies": cards,
            "owned_count": owned_count,
            "missing_count": len(cards) - owned_count,
        })
    ordered = []
    for group_name in ["Populares", "Géneros", "Décadas", "Estudios"]:
        if groups.get(group_name):
            ordered.append({"name": group_name, "sections": groups[group_name]})
    return {"groups": ordered, "any_cached": any_cached}


def update_discover(limit=20, progress=None):
    """Descarga/actualiza las secciones de Descubre que estén vencidas o falten.

    Bounded por `limit` consultas por ejecución para no castigar el NAS."""
    if not tmdb.configured():
        return {"done": 0, "message": "Falta configurar la API key de TMDB."}
    done = 0
    for spec in DISCOVER_SECTIONS:
        if done >= limit:
            break
        cache_key = f"discover:{spec['key']}"
        if not _cache_stale_ttl(cache_key, DISCOVER_TTL_SECONDS):
            continue
        movies = tmdb.discover_movies(spec["filters"], limit=20)
        if movies:
            _set_json(cache_key, movies)
        done += 1
        if progress:
            progress({"done": done, "total": len(DISCOVER_SECTIONS), "current": spec["title"]})
    return {"done": done, "message": f"Descubre actualizado: {done} lista(s)."}


MAX_MATCH_ATTEMPTS = 2


def enrich_unmatched(limit=None, progress=None):
    """Rellena póster/datos de TODAS las películas importadas sin match de TMDB
    (las que salían 'Sin imagen'). Las que TMDB no reconoce tras varios intentos
    se dejan de reintentar (para no repetir en cada actualización). Devuelve
    cuántas se reconocieron nuevas."""
    if not tmdb.configured():
        return 0
    pendientes = [
        row for row in db.list_catalog_files(missing=False)
        if row["media_type"] == "movie" and not _int(row["tmdb_id"])
        and (row["match_attempts"] or 0) < MAX_MATCH_ATTEMPTS
    ]
    if limit:
        pendientes = pendientes[:int(limit)]
    total = len(pendientes)
    matched = 0
    for i, row in enumerate(pendientes):
        title = row["title"] or _fallback_title(row["filename"])
        match = None
        if title:
            try:
                match = tmdb.best_match(title, "movie", row["year"])
            except Exception:
                match = None
        if not match:
            db.update_catalog_file(row["path"], match_attempts=(row["match_attempts"] or 0) + 1)
        else:
            db.update_catalog_file(row["path"], **{
                "tmdb_id": match["tmdb_id"],
                "title": match["title"] or title,
                "year": match["year"] or row["year"],
                "poster_url": match["poster_url"],
                "overview": match["overview"],
            })
            matched += 1
            detail = movie_detail(match["tmdb_id"]) or tmdb.movie_details(match["tmdb_id"])
            if detail:
                _set_json(f"movie:{match['tmdb_id']}", detail)
                collection = detail.get("collection") or {}
                if collection.get("id") and _cache_stale(f"collection:{collection['id']}"):
                    collection_detail = tmdb.collection_details(collection["id"])
                    if collection_detail:
                        _set_json(f"collection:{collection['id']}", collection_detail)
        if progress and i % 10 == 0:
            progress({"done": i + 1, "total": total, "current": title or ""})
    return matched


def import_folder(root, enrich_limit=80, progress=None):
    """Importa una carpeta existente de biblioteca bajo demanda.

    Recorre nombres/tamanos/fechas y consulta TMDB solo para una cantidad limitada
    por ejecucion. Asi el usuario puede avanzar por tandas sin castigar el NAS.
    """
    root = (root or "").strip()
    if not root or not os.path.isdir(root):
        return {"scanned": 0, "matched": 0, "message": "La carpeta no existe o no es accesible."}
    if not _within_catalog_roots(root):
        return {"scanned": 0, "matched": 0, "message": "Esa carpeta esta fuera de las bibliotecas configuradas."}
    scan_ts = time.time()
    scanned = 0
    matched = 0
    skipped = 0
    errors = 0
    enrich_limit = max(0, int(enrich_limit or 0))
    # Precargamos lo ya catalogado en un dict (una consulta) en vez de una
    # consulta por archivo. En bibliotecas grandes eso evita miles de queries.
    existing_by_path = {row["path"]: row for row in db.list_catalog_files()}
    for path in _iter_video_files(root):
        scanned += 1
        try:
            stat = os.stat(path)
            existing = existing_by_path.get(path)
            filename = os.path.basename(path)
            changed = not existing or existing["size_bytes"] != stat.st_size or existing["mtime_ns"] != stat.st_mtime_ns
            if existing and not changed and existing["tmdb_id"]:
                db.touch_catalog_file(path, last_seen=scan_ts)
                skipped += 1
                continue
            ident = identify.identify(path)
            media_type = ident.get("media_type") if ident.get("media_type") in ("movie", "series") else "movie"
            quality, langs = identify.tech_info(filename)
            fields = {
                "media_type": media_type,
                "title": ident.get("title") or _fallback_title(filename),
                "year": ident.get("year"),
                "quality": quality,
                "langs": langs,
                "last_seen": scan_ts,
                "missing": 0,
                "source": "scan",
            }
            if media_type == "movie" and matched < enrich_limit and tmdb.configured():
                match = tmdb.best_match(fields["title"], "movie", fields["year"])
                if match:
                    fields.update({
                        "tmdb_id": match["tmdb_id"],
                        "title": match["title"],
                        "year": match["year"],
                        "poster_url": match["poster_url"],
                        "overview": match["overview"],
                    })
                    detail = movie_detail(match["tmdb_id"]) or tmdb.movie_details(match["tmdb_id"])
                    if detail:
                        _set_json(f"movie:{match['tmdb_id']}", detail)
                        collection = detail.get("collection") or {}
                        if collection.get("id") and _cache_stale(f"collection:{collection['id']}"):
                            collection_detail = tmdb.collection_details(collection["id"])
                            if collection_detail:
                                _set_json(f"collection:{collection['id']}", collection_detail)
                    matched += 1
            db.upsert_catalog_file(
                path,
                filename,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                **fields,
            )
            if progress and scanned % 20 == 0:
                progress({
                    "done": scanned,
                    "total": 0,
                    "current": filename,
                    "message": f"Importando biblioteca: {scanned} vistos, {matched} reconocidos.",
                })
        except Exception:
            errors += 1
    db.mark_catalog_missing_under_root(root, scan_ts)
    return {
        "scanned": scanned,
        "matched": matched,
        "skipped": skipped,
        "errors": errors,
        "message": f"Importacion terminada: {scanned} archivo(s), {matched} reconocido(s), {skipped} ya estaban.",
    }


def suggested_roots():
    roots = []
    for path in [
        config.get("default_movie_dir"),
        config.get("default_series_dir"),
        *[x.strip() for x in config.get("library_roots").split(",") if x.strip()],
    ]:
        if path and path not in roots:
            roots.append(path)
    return roots


def _catalog_roots():
    roots = suggested_roots()
    for path in [x.strip() for x in config.get("library_roots").split(",") if x.strip()]:
        if path and path not in roots:
            roots.append(path)
    return roots


def _within_catalog_roots(path):
    try:
        rp = os.path.realpath(path)
    except (OSError, TypeError):
        return False
    for root in _catalog_roots():
        if not root:
            continue
        try:
            rr = os.path.realpath(root)
        except (OSError, TypeError):
            continue
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def _series_entries(catalog_rows=None):
    out = []
    seen = set()
    catalog_rows = db.list_catalog_files(missing=False) if catalog_rows is None else catalog_rows
    for row in catalog_rows:
        if row["media_type"] != "series":
            continue
        key = (row["title"] or row["filename"]).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "title": row["title"] or row["filename"],
            "year": row["year"],
            "poster_url": row["poster_url"],
            "path": row["path"],
            "quality": row["quality"],
            "langs": row["langs"],
        })
    out.sort(key=lambda x: (x.get("title") or "").lower())
    return out


def movie_detail(tmdb_id):
    return _get_json(f"movie:{tmdb_id}") if tmdb_id else None


def collection_detail_cached(collection_id):
    return _get_json(f"collection:{collection_id}") if collection_id else None


def _part_with_owned(part, owned_by_id):
    tmdb_id = _int(part.get("tmdb_id"))
    item = owned_by_id.get(tmdb_id)
    out = {
        "tmdb_id": tmdb_id,
        "title": part.get("title") or "",
        "year": part.get("year"),
        "release_date": part.get("release_date") or "",
        "poster_url": part.get("poster_url"),
        "overview": part.get("overview") or "",
        "owned": bool(item),
        "dest_path": item["dest_path"] if item else "",
        "langs": item["langs"] if item else "",
        "quality": item["quality"] if item else "",
        "source": item["source"] if item else "",
    }
    return out


def _detail_to_movie(detail, owned=False, item=None):
    return {
        "tmdb_id": _int(detail.get("tmdb_id")),
        "title": detail.get("title") or (_val(item, "title") if item else ""),
        "year": detail.get("year") or (_val(item, "year") if item else None),
        "release_date": detail.get("release_date") or "",
        "poster_url": detail.get("poster_url") or (_val(item, "poster_url") if item else None),
        "owned": owned,
        "dest_path": _val(item, "dest_path") if item else "",
        "langs": _val(item, "langs") if item else "",
        "quality": _val(item, "quality") if item else "",
        "source": _val(item, "source") if item else "",
    }


def _movie_from_item(item, owned=False):
    return {
        "tmdb_id": _int(_val(item, "tmdb_id")),
        "title": _val(item, "title") or _val(item, "chosen_title") or _val(item, "detected_title") or _val(item, "filename"),
        "year": _val(item, "year") or _val(item, "chosen_year") or _val(item, "detected_year"),
        "poster_url": _val(item, "poster_url"),
        "owned": owned,
        "dest_path": _val(item, "dest_path"),
        "langs": _val(item, "langs"),
        "quality": _val(item, "quality"),
        "source": _val(item, "source") or "organizer",
    }


def _entry_from_item(item, source):
    return {
        "tmdb_id": _int(item["tmdb_id"]),
        "filename": item["filename"],
        "title": item["chosen_title"] or item["detected_title"] or item["filename"],
        "year": item["chosen_year"] or item["detected_year"],
        "poster_url": item["poster_url"],
        "overview": item["overview"],
        "dest_path": item["dest_path"] or item["original_path"],
        "langs": item["langs"],
        "quality": item["quality"],
        "source": source,
    }


def _entry_from_catalog_file(row):
    return {
        "tmdb_id": _int(row["tmdb_id"]),
        "filename": row["filename"],
        "title": row["title"] or row["filename"],
        "year": row["year"],
        "poster_url": row["poster_url"],
        "overview": row["overview"],
        "dest_path": row["path"],
        "langs": row["langs"],
        "quality": row["quality"],
        "source": "scan",
    }


def _iter_video_files(root):
    video_exts = config.ext_list("video_exts")
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not _skip_dir(d)]
        for name in files:
            if os.path.splitext(name)[1].lower() in video_exts:
                yield os.path.join(dirpath, name)


def _skip_dir(name):
    low = (name or "").lower()
    return low in SKIP_DIR_NAMES or low.startswith("@eadir")


def _fallback_title(filename):
    return os.path.splitext(filename)[0].replace(".", " ").replace("_", " ").strip()


def _collect_companies(companies, detail):
    for company in detail.get("companies") or []:
        name = company.get("name")
        if not name:
            continue
        entry = companies.setdefault(name, {"name": name, "logo_url": company.get("logo_url"), "count": 0})
        entry["count"] += 1
        if not entry.get("logo_url") and company.get("logo_url"):
            entry["logo_url"] = company.get("logo_url")


def _get_json(cache_key):
    row = db.get_catalog_cache(cache_key)
    return _json_from_row(row)


def _json_from_row(row):
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except Exception:
        return None


def _set_json(cache_key, value):
    db.set_catalog_cache(cache_key, json.dumps(value or {}, ensure_ascii=False), time.time())


def _cache_stale(cache_key):
    return _cache_stale_ttl(cache_key, CACHE_TTL_SECONDS)


def _cache_stale_ttl(cache_key, ttl_seconds):
    row = db.get_catalog_cache(cache_key)
    if not row:
        return True
    try:
        updated_at = float(row["updated_at"] or 0)
    except (TypeError, ValueError):
        updated_at = 0
    return (time.time() - updated_at) > ttl_seconds


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _val(item, key):
    if not item:
        return None
    try:
        return item[key]
    except (KeyError, IndexError, TypeError):
        if isinstance(item, dict):
            return item.get(key)
        return None
