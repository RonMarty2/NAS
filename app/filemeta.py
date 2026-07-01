"""Resumen seguro de datos del archivo para decidir antes de moverlo."""
import json
import os
import re
import shutil
import subprocess

from .identify import guessit_safe


_LANG_ALIASES = {
    "lat": "Latino",
    "latino": "Latino",
    "es": "Español",
    "esp": "Español",
    "spa": "Español",
    "spanish": "Español",
    "espanol": "Español",
    "español": "Español",
    "castellano": "Castellano",
    "en": "Inglés",
    "eng": "Inglés",
    "ing": "Inglés",
    "ingles": "Inglés",
    "inglés": "Inglés",
    "english": "Inglés",
    "ja": "Japonés",
    "jp": "Japonés",
    "jpn": "Japonés",
    "jap": "Japonés",
    "japanese": "Japonés",
    "pt": "Portugués",
    "por": "Portugués",
    "portuguese": "Portugués",
    "fr": "Francés",
    "fre": "Francés",
    "fra": "Francés",
    "french": "Francés",
    "de": "Alemán",
    "ger": "Alemán",
    "deu": "Alemán",
    "german": "Alemán",
    "it": "Italiano",
    "ita": "Italiano",
    "italian": "Italiano",
    "ko": "Coreano",
    "kor": "Coreano",
    "korean": "Coreano",
    "zh": "Chino",
    "chi": "Chino",
    "zho": "Chino",
    "chinese": "Chino",
    "ru": "Ruso",
    "rus": "Ruso",
    "russian": "Ruso",
    "dual": "Dual",
    "multi": "Multi",
}

_SOURCE_RE = re.compile(r"\b(BluRay|BDRip|WEB[-_. ]?DL|WEBRip|HDTV|DVDRip|HDRip|CAM|TS)\b", re.I)
_RES_RE = re.compile(r"\b(2160p|1080p|720p|576p|480p|4K|UHD)\b", re.I)
_VIDEO_RE = re.compile(r"\b(HEVC|H\.?265|x265|H\.?264|x264|AV1|VP9|MPEG[-_. ]?2)\b", re.I)
_AUDIO_RE = re.compile(r"\b(AAC|AC3|EAC3|DDP?5\.1|DTS|FLAC|OPUS|MP3)\b", re.I)


def inspect_file(path, filename=None, size_bytes=None, allow_probe=True):
    """Devuelve un dict JSON-friendly con datos de nombre y, si existe, ffprobe."""
    filename = filename or os.path.basename(path)
    data = {
        "extension": os.path.splitext(filename)[1].lower().lstrip("."),
        "size_bytes": _safe_int(size_bytes),
    }
    if not data["size_bytes"]:
        try:
            data["size_bytes"] = os.path.getsize(path)
        except OSError:
            data["size_bytes"] = 0

    data.update(_from_filename(filename))

    if allow_probe:
        probed = _from_ffprobe(path)
        if probed:
            data.update({k: v for k, v in probed.items() if v not in (None, "", [])})

    data["quality"] = _best_quality(data)
    return {k: v for k, v in data.items() if v not in (None, "", [])}


def to_json(data):
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True)


def from_json(raw):
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def display_info(item):
    """Datos listos para pintar en Jinja. Nunca toca el disco."""
    info = from_json(_get(item, "media_info"))
    if not info:
        info = inspect_file(
            _get(item, "original_path") or "",
            filename=_get(item, "filename") or "",
            size_bytes=_get(item, "size_bytes") or 0,
            allow_probe=False,
        )
    elif not info.get("size_bytes"):
        info["size_bytes"] = _get(item, "size_bytes") or 0

    facts = []
    size = _format_size(info.get("size_bytes"))
    if size:
        facts.append(f"Peso {size}")
    if info.get("quality"):
        facts.append(f"Calidad {info['quality']}")
    if info.get("source"):
        facts.append(info["source"])
    if info.get("video_codec"):
        facts.append(f"Video {info['video_codec']}")
    audio = _audio_label(info)
    if audio:
        facts.append(audio)
    langs = _join(info.get("audio_languages") or info.get("languages"))
    if langs:
        facts.append(f"Audio {langs}")
    subs = _join(info.get("subtitle_languages"))
    if subs:
        facts.append(f"Subs {subs}")
    duration = _format_duration(info.get("duration_seconds"))
    if duration:
        facts.append(duration)
    if info.get("extension"):
        facts.append(info["extension"].upper())

    return {"facts": facts, "raw": info}


def media_is_readable(path):
    """Valida que ffprobe pueda leer al menos un stream de audio o video.

    Devuelve True/False. Si ffprobe no existe, devuelve None para no bloquear el
    flujo; la verificación de duplicado exacto sigue usando tamaño + SHA-256.
    """
    if not shutil.which("ffprobe"):
        return None
    probe = _run_ffprobe(path, timeout=20)
    if not probe:
        return False
    streams = probe.get("streams") or []
    useful = [s for s in streams if s.get("codec_type") in ("video", "audio")]
    if not useful:
        return False
    return True


def _from_filename(filename):
    data = {}
    # guessit con límite de tiempo: era la última llamada sin proteger contra
    # nombres de archivo que la cuelguen (ver identify._run_guarded).
    guessed = guessit_safe(filename)

    screen = _text(guessed.get("screen_size"))
    source = _text(guessed.get("source"))
    video = _pretty_codec(_text(guessed.get("video_codec")))
    audio = _pretty_codec(_text(guessed.get("audio_codec")))

    if not screen:
        screen = _match(_RES_RE, filename)
    source_from_name = _clean_source(_match(_SOURCE_RE, filename))
    if source_from_name and (not source or source.lower() == "web"):
        source = source_from_name
    if not video:
        video = _pretty_codec(_match(_VIDEO_RE, filename))
    if not audio:
        audio = _pretty_codec(_match(_AUDIO_RE, filename))

    if screen:
        data["resolution"] = screen.upper() if screen.lower() in ("4k", "uhd") else screen.lower()
    if source:
        data["source"] = _clean_source(source)
    if video:
        data["video_codec"] = video
    if audio:
        data["audio_codec"] = audio

    langs = _languages_from_guess(guessed, "language")
    langs = _unique(langs + _languages_from_text(filename))
    if langs:
        data["audio_languages"] = langs
    sub_langs = _languages_from_guess(guessed, "subtitle_language")
    if sub_langs:
        data["subtitle_languages"] = sub_langs

    return data


def _from_ffprobe(path):
    probe = _run_ffprobe(path, timeout=18)
    if not probe:
        return {}

    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    data = {}
    if video:
        width = _safe_int(video.get("width"))
        height = _safe_int(video.get("height"))
        if width and height:
            if width >= 3800 or height >= 2000:
                data["resolution"] = "2160p"
            else:
                data["resolution"] = f"{height}p"
        codec = _pretty_codec(video.get("codec_name"))
        if codec:
            data["video_codec"] = codec

    if audios:
        codecs = _unique(_pretty_codec(a.get("codec_name")) for a in audios)
        langs = _unique(_language_name((a.get("tags") or {}).get("language")) for a in audios)
        channels = max((_safe_int(a.get("channels")) for a in audios), default=0)
        if codecs:
            data["audio_codec"] = ", ".join(codecs[:2])
        if langs:
            data["audio_languages"] = langs
        if channels:
            data["audio_channels"] = _channel_label(channels)

    if subs:
        sub_langs = _unique(_language_name((s.get("tags") or {}).get("language")) for s in subs)
        if sub_langs:
            data["subtitle_languages"] = sub_langs

    duration = _safe_float((probe.get("format") or {}).get("duration"))
    if duration:
        data["duration_seconds"] = int(duration)

    bitrate = _safe_int((probe.get("format") or {}).get("bit_rate"))
    if bitrate:
        data["bitrate_kbps"] = int(bitrate / 1000)

    return data


def _run_ffprobe(path, timeout=18):
    if not path or not os.path.exists(path) or not shutil.which("ffprobe"):
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _best_quality(data):
    return data.get("resolution") or data.get("quality")


def _audio_label(info):
    parts = []
    if info.get("audio_codec"):
        parts.append(str(info["audio_codec"]))
    if info.get("audio_channels"):
        parts.append(str(info["audio_channels"]))
    return "Audio " + " ".join(parts) if parts else ""


def _format_size(value):
    size = _safe_int(value)
    if not size:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    n = float(size)
    unit = units[0]
    for unit in units:
        if n < 1024 or unit == units[-1]:
            break
        n /= 1024
    if unit in ("B", "KB"):
        return f"{int(n)} {unit}"
    return f"{n:.1f} {unit}"


def _format_duration(value):
    seconds = _safe_int(value)
    if not seconds:
        return ""
    minutes = seconds // 60
    hours = minutes // 60
    minutes = minutes % 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _channel_label(channels):
    if channels >= 8:
        return "7.1"
    if channels >= 6:
        return "5.1"
    if channels == 2:
        return "2.0"
    if channels == 1:
        return "Mono"
    return f"{channels}ch"


def _languages_from_guess(guessed, key):
    values = []
    raw = guessed.get(key)
    if isinstance(raw, (list, tuple, set)):
        values.extend(raw)
    elif raw:
        values.append(raw)
    return _unique(_language_name(v) for v in values)


def _languages_from_text(text):
    found = []
    bracketed = [m[0] or m[1] for m in re.findall(r"\[([^\]]+)\]|\(([^\)]+)\)", text or "")]
    segments = bracketed or [text or ""]
    for segment in segments:
        normalized = segment.lower().replace("_", " ").replace(".", " ")
        for token in re.findall(r"[a-záéíóúñ]+", normalized):
            if not bracketed and len(token) <= 2:
                continue
            name = _language_name(token)
            if name:
                found.append(name)
    if bracketed:
        return _unique(found)
    for token in re.findall(r"[a-záéíóúñ]+", (text or "").lower()):
        if len(token) <= 2:
            continue
        name = _language_name(token)
        if name:
            found.append(name)
    return _unique(found)


def _language_name(value):
    if not value:
        return ""
    raw = str(value).strip().lower()
    raw = raw.replace("<language [", "").replace("language [", "").replace("]>", "")
    raw = raw.strip("[](){} ")
    return _LANG_ALIASES.get(raw, "")


def _pretty_codec(value):
    if not value:
        return ""
    raw = str(value).strip()
    key = raw.lower().replace(".", "").replace("-", "").replace("_", "")
    return {
        "h264": "H.264",
        "x264": "H.264",
        "avc": "H.264",
        "h265": "H.265",
        "x265": "H.265",
        "hevc": "H.265",
        "aac": "AAC",
        "ac3": "AC3",
        "eac3": "EAC3",
        "dts": "DTS",
        "flac": "FLAC",
        "opus": "Opus",
        "mp3": "MP3",
        "av1": "AV1",
        "vp9": "VP9",
    }.get(key, raw.upper() if len(raw) <= 5 else raw)


def _clean_source(value):
    if not value:
        return ""
    raw = str(value).replace("_", "-").replace(" ", "-")
    low = raw.lower()
    if "web" in low and "dl" in low:
        return "WEB-DL"
    if "webrip" in low:
        return "WEBRip"
    if "bluray" in low or "bdrip" in low:
        return "BluRay"
    return raw.upper() if len(raw) <= 5 else raw


def _match(regex, text):
    m = regex.search(text or "")
    return m.group(1) if m else ""


def _text(value):
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_text(v) for v in value if _text(v))
    return "" if value in (None, "") else str(value)


def _join(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v)
    return ""


def _unique(values):
    result = []
    seen = set()
    for value in values:
        if not value:
            continue
        key = str(value).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(str(value))
    return result


def _safe_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _get(item, key):
    try:
        return item[key]
    except (KeyError, IndexError, TypeError):
        return None
