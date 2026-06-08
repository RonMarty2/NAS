"""Notificaciones opcionales cuando llegan descargas nuevas.

Soporta (todas opcionales, se configuran en Ajustes):
- ntfy  (lo más fácil: instala la app ntfy y elige un "topic")
- Discord (pega la URL de un webhook)
- Telegram (token del bot + chat id)

Es "mejor esfuerzo": si algo falla o no está configurado, no pasa nada.
"""
import requests

from . import config


def _ascii(text):
    """Cabeceras HTTP solo aceptan ASCII; quitamos lo que no lo sea."""
    return text.encode("ascii", "ignore").decode("ascii") or "NAS Organizer"


def notify(title, message, url=None):
    """Envía la notificación a todos los canales configurados."""
    _ntfy(title, message, url)
    _discord(title, message, url)
    _telegram(title, message, url)


def _ntfy(title, message, url):
    topic = config.get("ntfy_topic")
    if not topic:
        return
    server = (config.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    headers = {"Title": _ascii(title)}
    if url:
        headers["Click"] = url
    try:
        requests.post(f"{server}/{topic}", data=message.encode("utf-8"),
                      headers=headers, timeout=8)
    except requests.RequestException:
        pass


def _discord(title, message, url):
    webhook = config.get("discord_webhook")
    if not webhook:
        return
    content = f"**{title}**\n{message}" + (f"\n{url}" if url else "")
    try:
        requests.post(webhook, json={"content": content}, timeout=8)
    except requests.RequestException:
        pass


def _telegram(title, message, url):
    token = config.get("telegram_token")
    chat_id = config.get("telegram_chat_id")
    if not (token and chat_id):
        return
    text = f"{title}\n{message}" + (f"\n{url}" if url else "")
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=8)
    except requests.RequestException:
        pass
