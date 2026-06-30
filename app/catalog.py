"""Catalogo visual: sagas, peliculas que tienes y faltantes."""
import json
import os
import time

from . import config, db, identify
from .metadata import tmdb


STATUS_KEY = "catalog_status"
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60


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


def owned_movie_entries():
    entries = []
    seen = set()
    for item in db.list_items(status="done", media_type="movie"):
        tmdb_id = _int(item["tmdb_id"])
        if not tmdb_id:
            continue
        seen.add(tmdb_id)
        entries.append(_entry_from_item(item, "organizer"))
    for row in db.list_catalog_files(missing=False):
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
    done = 0
    collection_ids = set()

    for item in items:
        if done >= limit:
            break
        tmdb_id = _int(item["tmdb_id"])
        detail = movie_detail(tmdb_id)
        if not detail or _cache_stale(f"movie:{tmdb_id}"):
            detail = tmdb.movie_details(tmdb_id)
            if detail:
                _set_json(f"movie:{tmdb_id}", detail)
        collection = (detail or {}).get("collection") or {}
        if collection.get("id"):
            collection_ids.add(_int(collection["id"]))
        done += 1
        if progress:
            progress({"done": done, "total": total, "current": item["filename"]})

    collection_done = 0
    for collection_id in sorted(x for x in collection_ids if x):
        if done >= limit:
            break
        if _cache_stale(f"collection:{collection_id}"):
            detail = tmdb.collection_details(collection_id)
            if detail:
                _set_json(f"collection:{collection_id}", detail)
        collection_done += 1
        done += 1
        if progress:
            progress({"done": done, "total": total + len(collection_ids), "current": f"Saga {collection_id}"})

    return {
        "done": done,
        "total": total,
        "collections": collection_done,
        "message": f"Catalogo actualizado: {done} consulta(s).",
    }


def build_catalog():
    items = owned_movie_entries()
    owned_by_id = {_int(item["tmdb_id"]): item for item in items if _int(item["tmdb_id"])}
    collections = {}
    standalone = []
    companies = {}
    uncached = 0
    series = _series_entries()

    for item in items:
        tmdb_id = _int(item["tmdb_id"])
        detail = movie_detail(tmdb_id)
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
            collection_detail = collection_detail_cached(collection_id)
            if collection_detail:
                entry["name"] = collection_detail.get("name") or entry["name"]
                entry["poster_url"] = collection_detail.get("poster_url") or entry["poster_url"]
                entry["backdrop_url"] = collection_detail.get("backdrop_url")
                entry["parts"] = [_part_with_owned(part, owned_by_id) for part in collection_detail.get("parts") or []]
            else:
                entry["parts"].append(_part_with_owned(detail, owned_by_id))
        else:
            standalone.append(_detail_to_movie(detail, owned=True, item=item))

    for row in db.list_catalog_files(missing=False):
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
        "imported_total": len(db.list_catalog_files(missing=False)),
        "uncached": uncached,
        "tmdb_configured": tmdb.configured(),
    }


def import_folder(root, enrich_limit=80, progress=None):
    """Importa una carpeta existente de biblioteca bajo demanda.

    Recorre nombres/tamanos/fechas y consulta TMDB solo para una cantidad limitada
    por ejecucion. Asi el usuario puede avanzar por tandas sin castigar el NAS.
    """
    root = (root or "").strip()
    if not root or not os.path.isdir(root):
        return {"scanned": 0, "matched": 0, "message": "La carpeta no existe o no es accesible."}
    scanned = 0
    matched = 0
    skipped = 0
    errors = 0
    enrich_limit = max(0, int(enrich_limit or 0))
    for path in _iter_video_files(root):
        scanned += 1
        try:
            stat = os.stat(path)
            existing = db.get_catalog_file_by_path(path)
            filename = os.path.basename(path)
            changed = not existing or existing["size_bytes"] != stat.st_size or existing["mtime_ns"] != stat.st_mtime_ns
            if existing and not changed and existing["tmdb_id"]:
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
                "last_seen": time.time(),
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


def _series_entries():
    out = []
    seen = set()
    for row in db.list_catalog_files(missing=False):
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
        dirs[:] = [d for d in dirs if not d.startswith("@eaDir")]
        for name in files:
            if os.path.splitext(name)[1].lower() in video_exts:
                yield os.path.join(dirpath, name)


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
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except Exception:
        return None


def _set_json(cache_key, value):
    db.set_catalog_cache(cache_key, json.dumps(value or {}, ensure_ascii=False), time.time())


def _cache_stale(cache_key):
    row = db.get_catalog_cache(cache_key)
    if not row:
        return True
    try:
        updated_at = float(row["updated_at"] or 0)
    except (TypeError, ValueError):
        updated_at = 0
    return (time.time() - updated_at) > CACHE_TTL_SECONDS


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
