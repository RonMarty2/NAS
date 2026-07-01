"""Lista de deseos: películas/series que quieres conseguir aunque no tengas nada de ellas."""
from . import catalog, db
from .metadata import tmdb


def list_wishlist():
    """Deseos pendientes, ocultando automáticamente los que ya conseguiste
    (para no tener que quitarlos a mano cuando descargas algo de la lista)."""
    owned_movies = catalog.owned_movie_ids()
    owned_series = catalog.owned_series_ids()
    out = []
    for row in db.list_wishlist():
        tmdb_id = int(row["tmdb_id"])
        owned = tmdb_id in (owned_series if row["media_type"] == "series" else owned_movies)
        if not owned:
            out.append(row)
    return out


def add(tmdb_id, media_type, title, year=None, poster_url=None, overview=""):
    tmdb_id = int(tmdb_id)
    media_type = media_type if media_type in ("movie", "series") else "movie"
    db.add_wishlist_item(tmdb_id, media_type, title, year, poster_url, overview)


def remove(item_id):
    db.remove_wishlist_item(item_id)


def search(query, media_type="movie"):
    """Busca en TMDB para elegir qué agregar a la lista de deseos."""
    return tmdb.search(query, media_type)
