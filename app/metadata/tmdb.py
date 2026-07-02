"""Búsqueda de metadatos en TheMovieDB (TMDB). API gratuita con key.

Devuelve resultados normalizados para películas y series.
"""
import requests

from .. import config

BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w342"
IMG_ORIGINAL = "https://image.tmdb.org/t/p/original"


def _key():
    return config.get("tmdb_api_key")


def _lang():
    return config.get("metadata_language") or "es-ES"


def configured():
    return bool(_key())


def _normalize(result, media_type):
    """Convierte un resultado crudo de TMDB en nuestro formato común."""
    if media_type == "movie":
        title = result.get("title") or result.get("original_title")
        date = result.get("release_date") or ""
    else:
        title = result.get("name") or result.get("original_name")
        date = result.get("first_air_date") or ""
    year = int(date[:4]) if date[:4].isdigit() else None
    poster = result.get("poster_path")
    return {
        "tmdb_id": result.get("id"),
        "title": title,
        "year": year,
        "poster_url": (IMG + poster) if poster else None,
        "overview": result.get("overview") or "",
        "media_type": media_type,
    }


class TmdbUnavailable(Exception):
    """TMDB no respondió (red caída / servicio saturado).

    Es DISTINTO de 'no hay resultados': antes ambos devolvían lista vacía y
    un fallo de internet quemaba los reintentos de todo el catálogo como si
    TMDB hubiera dicho 'no conozco esa serie'."""


def search(query, media_type, year=None):
    """Busca en TMDB. media_type: 'movie' o 'series'. Devuelve lista normalizada.
    Lanza TmdbUnavailable si la red/servicio falló (no confundir con [])."""
    if not configured() or not query:
        return []
    tmdb_type = "movie" if media_type == "movie" else "tv"
    params = {"api_key": _key(), "language": _lang(), "query": query}
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year
    try:
        r = requests.get(f"{BASE}/search/{tmdb_type}", params=params, timeout=8)
        r.raise_for_status()
        results = r.json().get("results", [])
    except requests.RequestException as exc:
        raise TmdbUnavailable(str(exc))
    return [_normalize(x, media_type) for x in results[:10]]


_LEET = str.maketrans({"1": "i", "3": "e", "4": "a", "0": "o", "5": "s", "7": "t", "8": "b"})


def _deleet(text):
    """Convierte nombres ofuscados con números a letras: 'Str1pt3as3' -> 'Striptease'."""
    return text.translate(_LEET) if text else text


def best_match(query, media_type, year=None):
    """Devuelve la mejor coincidencia (la primera) o None.

    Si no encuentra nada, reintenta con el nombre 'des-leeteado' (por si venía
    ofuscado con números, p.ej. 'Str1pt3as3' -> 'Striptease')."""
    results = search(query, media_type, year)
    if not results:
        deleeted = _deleet(query)
        if deleeted and deleeted != query:
            results = search(deleeted, media_type, year)
    return results[0] if results else None


def _image_url(path):
    return (IMG_ORIGINAL + path) if path else None


def _pick_image(items, preferred_languages=None):
    """Elige una imagen priorizando idioma y votos. Devuelve URL original o None."""
    if not items:
        return None
    langs = preferred_languages or ("es", "en", None)
    ranked = sorted(
        items,
        key=lambda x: (
            (x.get("iso_639_1") not in langs),
            langs.index(x.get("iso_639_1")) if x.get("iso_639_1") in langs else 99,
            -(x.get("vote_average") or 0),
            -(x.get("vote_count") or 0),
        ),
    )
    return _image_url(ranked[0].get("file_path"))


def image_assets(media_type, tmdb_id):
    """Devuelve imágenes locales útiles para Jellyfin: poster, fanart y clearlogo."""
    if not configured() or not tmdb_id:
        return {}
    tmdb_type = "movie" if media_type == "movie" else "tv"
    lang = (_lang() or "es")[:2]
    params = {
        "api_key": _key(),
        "include_image_language": f"{lang},es,en,null",
    }
    try:
        r = requests.get(f"{BASE}/{tmdb_type}/{tmdb_id}/images", params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException:
        return {}
    languages = (lang, "es", "en", None)
    return {
        "poster": _pick_image(data.get("posters"), languages),
        "fanart": _pick_image(data.get("backdrops"), languages),
        "clearlogo": _pick_image(data.get("logos"), languages),
    }


def season_assets(tmdb_id, season_number):
    """Devuelve imágenes para una temporada concreta de una serie."""
    if not configured() or not tmdb_id:
        return {}
    try:
        season = int(season_number)
    except (TypeError, ValueError):
        season = 1
    lang = (_lang() or "es")[:2]
    params = {
        "api_key": _key(),
        "include_image_language": f"{lang},es,en,null",
    }
    try:
        r = requests.get(f"{BASE}/tv/{tmdb_id}/season/{season}/images", params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException:
        return {}
    return {"poster": _pick_image(data.get("posters"), (lang, "es", "en", None))}


def episode_metadata(tmdb_id, season_number, episode_number):
    """Devuelve metadata y miniatura de un episodio concreto de TMDB."""
    if not configured() or not tmdb_id:
        return {}
    try:
        season = int(season_number)
    except (TypeError, ValueError):
        season = 1
    try:
        episode = int(episode_number)
    except (TypeError, ValueError):
        episode = 1
    params = {"api_key": _key(), "language": _lang()}
    try:
        r = requests.get(
            f"{BASE}/tv/{tmdb_id}/season/{season}/episode/{episode}",
            params=params,
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException:
        return {}
    return {
        "tmdb_id": data.get("id"),
        "title": data.get("name") or "",
        "overview": data.get("overview") or "",
        "aired": data.get("air_date") or "",
        "runtime": data.get("runtime"),
        "still_url": _image_url(data.get("still_path")),
    }


def movie_details(tmdb_id):
    """Detalle liviano de una pelicula para el catalogo visual."""
    if not configured() or not tmdb_id:
        return {}
    params = {"api_key": _key(), "language": _lang()}
    try:
        r = requests.get(f"{BASE}/movie/{tmdb_id}", params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException:
        return {}
    collection = data.get("belongs_to_collection") or {}
    companies = data.get("production_companies") or []
    date = data.get("release_date") or ""
    return {
        "tmdb_id": data.get("id"),
        "title": data.get("title") or data.get("original_title") or "",
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "release_date": date,
        "poster_url": (IMG + data["poster_path"]) if data.get("poster_path") else None,
        "overview": data.get("overview") or "",
        "collection": {
            "id": collection.get("id"),
            "name": collection.get("name") or "",
            "poster_url": (IMG + collection["poster_path"]) if collection.get("poster_path") else None,
            "backdrop_url": _image_url(collection.get("backdrop_path")),
        } if collection.get("id") else None,
        "companies": [
            {
                "id": company.get("id"),
                "name": company.get("name") or "",
                "logo_url": (IMG + company["logo_path"]) if company.get("logo_path") else None,
            }
            for company in companies[:5]
        ],
    }


def discover_movies(filters, limit=20):
    """Lista de películas de TMDB según filtros (género, año, estudio, popularidad).

    `filters` son parámetros de /discover/movie (ej. with_genres, with_companies,
    primary_release_date.gte, sort_by). Devuelve resultados ya normalizados."""
    if not configured():
        return []
    params = {
        "api_key": _key(),
        "language": _lang(),
        "include_adult": "false",
        "sort_by": "popularity.desc",
        "vote_count.gte": 100,
        "page": 1,
    }
    params.update(filters or {})
    try:
        r = requests.get(f"{BASE}/discover/movie", params=params, timeout=8)
        r.raise_for_status()
        results = r.json().get("results", [])
    except requests.RequestException:
        return []
    out = []
    for x in results[:limit]:
        norm = _normalize(x, "movie")
        if norm.get("tmdb_id") and norm.get("title"):
            out.append(norm)
    return out


def tv_details(tmdb_id):
    """Detalle de una serie con sus temporadas (nº de episodios por temporada),
    para saber qué falta. Una sola consulta por serie."""
    if not configured() or not tmdb_id:
        return {}
    params = {"api_key": _key(), "language": _lang()}
    try:
        r = requests.get(f"{BASE}/tv/{tmdb_id}", params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException:
        return {}
    date = data.get("first_air_date") or ""
    seasons = []
    for s in data.get("seasons") or []:
        num = s.get("season_number")
        if num is None or num == 0:
            continue  # 0 = especiales, no cuenta como temporada regular
        seasons.append({
            "season_number": num,
            "name": s.get("name") or f"Temporada {num}",
            "episode_count": s.get("episode_count") or 0,
            "air_date": s.get("air_date") or "",
            "poster_url": (IMG + s["poster_path"]) if s.get("poster_path") else None,
        })
    seasons.sort(key=lambda s: s["season_number"])
    return {
        "tmdb_id": data.get("id"),
        "title": data.get("name") or data.get("original_name") or "",
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "poster_url": (IMG + data["poster_path"]) if data.get("poster_path") else None,
        "overview": data.get("overview") or "",
        "seasons": seasons,
    }


def collection_details(collection_id):
    """Partes de una saga/coleccion de TMDB para saber que falta."""
    if not configured() or not collection_id:
        return {}
    params = {"api_key": _key(), "language": _lang()}
    try:
        r = requests.get(f"{BASE}/collection/{collection_id}", params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException:
        return {}
    parts = []
    for part in data.get("parts") or []:
        date = part.get("release_date") or ""
        parts.append({
            "tmdb_id": part.get("id"),
            "title": part.get("title") or part.get("original_title") or "",
            "year": int(date[:4]) if date[:4].isdigit() else None,
            "release_date": date,
            "poster_url": (IMG + part["poster_path"]) if part.get("poster_path") else None,
            "overview": part.get("overview") or "",
        })
    parts.sort(key=lambda p: (p.get("release_date") or "9999", p.get("title") or ""))
    return {
        "id": data.get("id"),
        "name": data.get("name") or "",
        "overview": data.get("overview") or "",
        "poster_url": (IMG + data["poster_path"]) if data.get("poster_path") else None,
        "backdrop_url": _image_url(data.get("backdrop_path")),
        "parts": parts,
    }
