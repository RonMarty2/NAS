"""Integración con Jellyfin: refresco de biblioteca vía API REST."""
import requests

from . import config


def _headers():
    return {"X-Emby-Token": config.get("jellyfin_api_key")}


def configured():
    return bool(config.get("jellyfin_url") and config.get("jellyfin_api_key"))


def refresh_incremental():
    """Escaneo incremental: Jellyfin solo añade/actualiza lo nuevo. Es lo que se
    llama tras cada archivo movido."""
    if not configured():
        return False, "Jellyfin no está configurado (URL o API key vacías)."
    url = config.get("jellyfin_url").rstrip("/") + "/Library/Refresh"
    try:
        r = requests.post(url, headers=_headers(), timeout=8)
        r.raise_for_status()
        return True, "Escaneo incremental solicitado a Jellyfin."
    except requests.RequestException as e:
        return False, f"No se pudo contactar a Jellyfin: {e}"


def refresh_full():
    """Refresco completo de metadatos de todas las bibliotecas. Bajo demanda
    (botón 'Actualizar todo'). Recorre las bibliotecas y fuerza un ReplaceAllMetadata."""
    if not configured():
        return False, "Jellyfin no está configurado (URL o API key vacías)."
    base = config.get("jellyfin_url").rstrip("/")
    try:
        # Obtiene las carpetas/bibliotecas raíz virtuales
        r = requests.get(f"{base}/Library/VirtualFolders", headers=_headers(), timeout=8)
        r.raise_for_status()
        folders = r.json()
    except requests.RequestException as e:
        return False, f"No se pudo listar bibliotecas: {e}"

    count = 0
    for f in folders:
        item_id = f.get("ItemId")
        if not item_id:
            continue
        try:
            requests.post(
                f"{base}/Items/{item_id}/Refresh",
                headers=_headers(),
                params={
                    "Recursive": "true",
                    "MetadataRefreshMode": "FullRefresh",
                    "ImageRefreshMode": "FullRefresh",
                    "ReplaceAllMetadata": "true",
                },
                timeout=8,
            )
            count += 1
        except requests.RequestException:
            continue
    return True, f"Refresco completo solicitado para {count} biblioteca(s)."
