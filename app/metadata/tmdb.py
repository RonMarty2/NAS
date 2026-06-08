"""Búsqueda de metadatos en TheMovieDB (TMDB). API gratuita con key.

Devuelve resultados normalizados para películas y series.
"""
import requests

from .. import config

BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w342"


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


def search(query, media_type, year=None):
    """Busca en TMDB. media_type: 'movie' o 'series'. Devuelve lista normalizada."""
    if not configured() or not query:
        return []
    tmdb_type = "movie" if media_type == "movie" else "tv"
    params = {"api_key": _key(), "language": _lang(), "query": query}
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year
    try:
        r = requests.get(f"{BASE}/search/{tmdb_type}", params=params, timeout=15)
        r.raise_for_status()
        results = r.json().get("results", [])
    except requests.RequestException:
        return []
    return [_normalize(x, media_type) for x in results[:10]]


def best_match(query, media_type, year=None):
    """Devuelve la mejor coincidencia (la primera) o None."""
    results = search(query, media_type, year)
    return results[0] if results else None
