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


def search(query, media_type, year=None):
    """Busca en TMDB. media_type: 'movie' o 'series'. Devuelve lista normalizada."""
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
    except requests.RequestException:
        return []
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
