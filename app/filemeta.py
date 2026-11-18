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
_EDITION_RE = re.compile(
    r"\b(Extended(?:\s+Cut|\s+Edition)?|Versi[oó]n\s+Extendida|Director'?s?\s+Cut|"
    r"Uncut|Unrated|IMAX|Remastered| Remasterizada|Special\s+Edition|Edici[oó]n\s+Especial)\b",
    re.I,
)


def inspect_file(path, filename=None, size_bytes=None, allow_probe=True):
    """Devuelve un dict JSON-friendly con datos de nombre y, si existe, ffprobe."""
    filename = filename or os.path.basename(path)
    data = {
        "extension": os.path.splitext(filename)[1].lower().lstrip("."),
        "size_bytes": _safe_int(size_bytes),
    }
    if not data["size_bytes"] and path:
        try:
            data["size_bytes"] = os.path.getsize(path)
        except OSError:
            data["size_bytes"] = 0

    from_name = _from_filename(filename)
    data.update(from_name)

    if allow_probe and path and os.path.exists(path):
        probed = _from_ffprobe(path)
        if probed:
            name_langs = from_name.get("audio_languages") or []
            probe_langs = probed.get("audio_languages") or []
            merged_langs = _merge_audio_languages(name_langs, probe_langs)
            data.update({k: v for k, v in probed.items() if v not in (None, "", [])})
            if merged_langs:
                data["audio_languages"] = merged_langs

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
    if info.get("readable") is False:
        facts.append("⚠️ Archivo dañado / ilegible")
    size = _format_size(info.get("size_bytes"))
    if size:
        facts.append(f"Peso {size}")
    if info.get("quality"):
        facts.append(f"Calidad {info['quality']}")
    if info.get("edition"):
        facts.append(f"🎬 {info['edition']}")
    if info.get("source"):
        facts.append(info["source"])
    if info.get("video_codec"):
        facts.append(f"Video {info['video_codec']}")
    if info.get("bitrate_kbps"):
        mbps = info["bitrate_kbps"] / 1000.0
        facts.append(f"{mbps:.1f} Mbps" if mbps >= 1 else f"{info['bitrate_kbps']} kbps")
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
        facts.append(f"⏱️ {duration}")
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
    edition = _text(guessed.get("edition")) or _match(_EDITION_RE, filename)

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
        data["resolution"] = "2160p" if screen.lower() in ("4k", "uhd") else screen.lower()
    if source:
        data["source"] = _clean_source(source)
    if video:
        data["video_codec"] = video
    if audio:
        data["audio_codec"] = audio
    if edition:
        data["edition"] = edition

    langs = _languages_from_guess(guessed, "language")
    langs = _unique(langs + _languages_from_text(filename))
    if langs:
        data["audio_languages"] = langs
    sub_langs = _languages_from_guess(guessed, "subtitle_language")
    if sub_langs:
        data["subtitle_languages"] = sub_langs

    return data


def _stream_tag(tags, key):
    if not isinstance(tags, dict):
        return ""
    for k, v in tags.items():
        if str(k).lower() == key.lower() and v:
            return str(v).strip()
    return ""


def _stream_language(tags):
    """Deduce el idioma real de una pista de audio/subtítulo mirando primero su
    título ('Español Latino', 'Castellano', 'Spanish (Spain)') y luego su código ISO."""
    title = _stream_tag(tags, "title") or _stream_tag(tags, "handler_name")
    lang_code = _stream_tag(tags, "language")
    low_title = title.lower()
    low_code = lang_code.lower()
    combined = f"{low_title} {low_code}"

    if any(w in combined for w in ("latino", "latam", "es-mx", "es-419", "mexico", "méxico", "hispano")) or re.search(r"\blat\b", low_title):
        return "Latino"
    if any(w in combined for w in ("castellano", "es-es", "españa", "espana", "spain", "europeo")) or re.search(r"\bcast\b", low_title):
        return "Castellano"

    from_title = _languages_from_text(title)
    if from_title:
        return from_title[0]
    return _language_name(lang_code)


def _merge_audio_languages(name_langs, probe_langs):
    """Combina idiomas del nombre y de ffprobe sin perder 'Latino'/'Castellano'
    cuando ffprobe solo reporta el código ISO genérico 'spa' -> 'Español'."""
    name_langs = list(name_langs or [])
    probe_langs = list(probe_langs or [])
    if not probe_langs:
        return name_langs
    if not name_langs:
        return probe_langs
    specific_spanish = [l for l in name_langs if l in ("Latino", "Castellano")]
    out = []
    for lang in probe_langs:
        if lang == "Español" and specific_spanish:
            out.extend(specific_spanish)
        else:
            out.append(lang)
    for lang in name_langs:
        if lang not in out and not (lang == "Español" and any(x in ("Latino", "Castellano") for x in out)):
            out.append(lang)
    return _unique(out)


def _from_ffprobe(path):
    if not path or not os.path.exists(path) or not shutil.which("ffprobe"):
        return {}
    probe = _run_ffprobe(path, timeout=18)
    if not probe:
        return {"readable": False}

    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") != 1), None)
    if not video:
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    if not video and not audios:
        return {"readable": False}

    data = {"readable": True}
    if video:
        width = _safe_int(video.get("width"))
        height = _safe_int(video.get("height"))
        if width and height:
            data["dimensions"] = f"{width}×{height}"
            # Las películas panorámicas (2.39:1) en 1080p miden 1920x800 y en 720p
            # miden 1280x534; mirar también el ancho evita llamarlas '800p' o '534p'.
            if width >= 3600 or height >= 2000:
                data["resolution"] = "2160p"
            elif width >= 1880 or height >= 1000:
                data["resolution"] = "1080p"
            elif width >= 1200 or height >= 680:
                data["resolution"] = "720p"
            elif width >= 800 or height >= 460:
                data["resolution"] = "480p"
            else:
                data["resolution"] = f"{height}p"
        codec = _pretty_codec(video.get("codec_name"))
        if codec:
            data["video_codec"] = codec

    if audios:
        codecs = _unique(_pretty_codec(a.get("codec_name")) for a in audios)
        langs = _unique(_stream_language(a.get("tags") or {}) for a in audios)
        channels = max((_safe_int(a.get("channels")) for a in audios), default=0)
        tracks = []
        for a in audios:
            tags = a.get("tags") or {}
            t_lang = _stream_language(tags) or _stream_tag(tags, "language") or "Pista"
            t_title = _stream_tag(tags, "title")
            t_codec = _pretty_codec(a.get("codec_name"))
            t_ch = _channel_label(_safe_int(a.get("channels"))) if _safe_int(a.get("channels")) else ""
            detail = " ".join(x for x in (t_codec, t_ch) if x)
            label = f"{t_lang}"
            if t_title and t_title.lower() not in t_lang.lower():
                label += f" ({t_title})"
            if detail:
                label += f" [{detail}]"
            tracks.append(label)
        if codecs:
            data["audio_codec"] = ", ".join(codecs[:2])
        if langs:
            data["audio_languages"] = langs
        if channels:
            data["audio_channels"] = _channel_label(channels)
        if tracks:
            data["audio_tracks"] = tracks[:6]

    if subs:
        sub_langs = _unique(_stream_language(s.get("tags") or {}) for s in subs)
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
    if seconds < 60:
        return f"{seconds}s"
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


def resolution_rank(res):
    raw = str(res or "").lower().strip()
    if raw in ("2160p", "4k", "uhd"):
        return 400
    if raw == "1080p":
        return 300
    if raw == "720p":
        return 200
    if raw in ("576p", "480p"):
        return 100
    m = re.match(r"^(\d{3,4})p$", raw)
    if m:
        h = int(m.group(1))
        if h >= 1800:
            return 400
        if h >= 900:
            return 300
        if h >= 650:
            return 200
        return 100
    return 0


def score_version(info):
    """Puntúa una versión según integridad, resolución, idioma (priorizando
    Latino/Español), códec y tasa de bits/peso."""
    info = info or {}
    if info.get("readable") is False or _safe_int(info.get("size_bytes")) <= 0:
        return -1000
    score = resolution_rank(info.get("resolution") or info.get("quality"))
    langs = [str(l).lower() for l in (info.get("audio_languages") or [])]
    if "latino" in langs or "dual" in langs or "multi" in langs:
        score += 75
    elif "español" in langs:
        score += 45
    elif "castellano" in langs:
        score += 20

    codec = str(info.get("video_codec") or "").upper()
    if codec in ("H.265", "AV1"):
        score += 20
    elif codec == "H.264":
        score += 10

    bitrate = _safe_int(info.get("bitrate_kbps"))
    if bitrate:
        score += min(45, bitrate // 300)
    else:
        size_gb = _safe_int(info.get("size_bytes")) / (1024 ** 3)
        score += min(35, int(size_gb * 6))
    return score


def compare_versions(new_info, existing_info):
    """Compara el archivo nuevo frente al existente y explica cuál conviene
    conservar (calidad, idioma, salud, duración/escenas extra)."""
    new_info = new_info or {}
    existing_info = existing_info or {}

    new_score = score_version(new_info)
    ext_score = score_version(existing_info)

    reasons = []
    warnings = []

    new_ok = new_info.get("readable") is not False and _safe_int(new_info.get("size_bytes")) > 0
    ext_ok = existing_info.get("readable") is not False and _safe_int(existing_info.get("size_bytes")) > 0
    if not ext_ok and new_ok:
        warnings.append("⚠️ El archivo que tienes en la biblioteca parece dañado o ilegible (ffprobe falló).")
        reasons.append("El archivo nuevo está íntegro y legible.")
    elif not new_ok and ext_ok:
        warnings.append("⚠️ El archivo nuevo de descargas parece dañado o incompleto (ffprobe falló).")
        reasons.append("El archivo actual de tu biblioteca sí es legible.")

    new_res = new_info.get("resolution") or new_info.get("quality") or "—"
    ext_res = existing_info.get("resolution") or existing_info.get("quality") or "—"
    if resolution_rank(new_res) > resolution_rank(ext_res):
        reasons.append(f"El nuevo tiene mejor resolución ({new_res} vs {ext_res}).")
    elif resolution_rank(ext_res) > resolution_rank(new_res):
        reasons.append(f"El que ya tienes tiene mejor resolución ({ext_res} vs {new_res}).")

    new_langs = new_info.get("audio_languages") or []
    ext_langs = existing_info.get("audio_languages") or []
    if "Castellano" in ext_langs and "Latino" in new_langs:
        warnings.append("🇪🇸 El que tienes está en Castellano (España) y el nuevo trae Español Latino.")
        reasons.append("El nuevo incluye audio en Español Latino.")
    elif "Castellano" in new_langs and "Latino" in ext_langs:
        warnings.append("🇪🇸 El archivo nuevo está en Castellano (España), mientras que el que ya tienes está en Latino.")
    elif "Castellano" in ext_langs and "Latino" not in ext_langs:
        warnings.append("🇪🇸 El archivo actual en tu biblioteca tiene audio en Castellano (España).")
    elif "Castellano" in new_langs and "Latino" not in new_langs:
        warnings.append("🇪🇸 El archivo nuevo tiene audio en Castellano (España).")

    if "Latino" in new_langs and "Latino" not in ext_langs and "Castellano" not in ext_langs:
        reasons.append(f"El nuevo incluye audio Latino ({', '.join(new_langs)}).")
    elif "Latino" in ext_langs and "Latino" not in new_langs and "Castellano" not in new_langs:
        reasons.append(f"El que ya tienes incluye audio Latino ({', '.join(ext_langs)}).")

    new_dur = _safe_int(new_info.get("duration_seconds"))
    ext_dur = _safe_int(existing_info.get("duration_seconds"))
    dur_note = ""
    if new_dur > 0 and ext_dur > 0:
        diff = abs(new_dur - ext_dur)
        diff_min = round(diff / 60)
        if diff >= 120:
            longer = "el nuevo" if new_dur > ext_dur else "el que ya tienes en biblioteca"
            shorter = "el que ya tienes" if new_dur > ext_dur else "el nuevo"
            if min(new_dur, ext_dur) < max(new_dur, ext_dur) * 0.6:
                warnings.append(
                    f"⚠️ Gran diferencia de duración ({_format_duration(new_dur)} vs {_format_duration(ext_dur)}): "
                    f"puede que {shorter} esté cortado/incompleto."
                )
            else:
                warnings.append(
                    f"🎬 Duración distinta ({_format_duration(new_dur)} vs {_format_duration(ext_dur)}): "
                    f"{longer} dura +{diff_min} min más (posible Versión Extendida / escenas extra)."
                )
            dur_note = f"Diferencia de {diff_min} min"

    if new_info.get("edition") or existing_info.get("edition"):
        ed_new = new_info.get("edition") or "Estándar"
        ed_ext = existing_info.get("edition") or "Estándar"
        if ed_new.lower() != ed_ext.lower():
            warnings.append(f"🎬 Edición detectada: existente «{ed_ext}» vs nuevo «{ed_new}».")

    new_br = _safe_int(new_info.get("bitrate_kbps"))
    ext_br = _safe_int(existing_info.get("bitrate_kbps"))
    if resolution_rank(new_res) == resolution_rank(ext_res) and new_br and ext_br:
        if new_br > ext_br * 1.25:
            reasons.append(f"El nuevo tiene mayor tasa de bits ({new_br / 1000:.1f} Mbps vs {ext_br / 1000:.1f} Mbps).")
        elif ext_br > new_br * 1.25:
            reasons.append(f"El que ya tienes tiene mayor tasa de bits ({ext_br / 1000:.1f} Mbps vs {new_br / 1000:.1f} Mbps).")

    if new_score >= ext_score + 10:
        winner = "new"
        headline = "🏆 Recomendado: Reemplazar con el NUEVO (mejor versión)"
    elif ext_score >= new_score + 10:
        winner = "existing"
        headline = "🏆 Recomendado: Mantener el EXISTente (ya tienes una versión mejor o igual)"
    else:
        winner = "tie"
        headline = "⚖️ Calidad muy similar: revisa idioma y duración para decidir"

    def _fmt_br(info):
        br = _safe_int(info.get("bitrate_kbps"))
        if not br:
            return "—"
        return f"{br / 1000:.1f} Mbps" if br >= 1000 else f"{br} kbps"

    def _fmt_health(info):
        if info.get("readable") is False:
            return "⚠️ Dañado / Ilegible"
        if info.get("readable") is True:
            return "✅ Íntegro (verificado)"
        return "Sin verificar"

    rows = [
        {"label": "Salud del archivo", "existing": _fmt_health(existing_info), "new": _fmt_health(new_info)},
        {
            "label": "Resolución",
            "existing": f"{ext_res}" + (f" ({existing_info['dimensions']})" if existing_info.get("dimensions") else ""),
            "new": f"{new_res}" + (f" ({new_info['dimensions']})" if new_info.get("dimensions") else ""),
        },
        {
            "label": "Idiomas de audio",
            "existing": _join(ext_langs) or "No especificado",
            "new": _join(new_langs) or "No especificado",
        },
        {
            "label": "Pistas de audio",
            "existing": " · ".join(existing_info.get("audio_tracks") or []) or _audio_label(existing_info) or "—",
            "new": " · ".join(new_info.get("audio_tracks") or []) or _audio_label(new_info) or "—",
        },
        {
            "label": "Subtítulos",
            "existing": _join(existing_info.get("subtitle_languages")) or "Ninguno",
            "new": _join(new_info.get("subtitle_languages")) or "Ninguno",
        },
        {
            "label": "Duración (escenas)",
            "existing": _format_duration(ext_dur) or "—",
            "new": _format_duration(new_dur) or "—",
            "note": dur_note,
        },
        {
            "label": "Códec de video y Bitrate",
            "existing": f"{existing_info.get('video_codec') or '—'} · {_fmt_br(existing_info)}",
            "new": f"{new_info.get('video_codec') or '—'} · {_fmt_br(new_info)}",
        },
        {
            "label": "Peso y formato",
            "existing": f"{_format_size(existing_info.get('size_bytes')) or '—'} ({(existing_info.get('extension') or '').upper()})",
            "new": f"{_format_size(new_info.get('size_bytes')) or '—'} ({(new_info.get('extension') or '').upper()})",
        },
    ]

    return {
        "winner": winner,
        "headline": headline,
        "reasons": reasons,
        "warnings": warnings,
        "rows": rows,
        "new_score": new_score,
        "existing_score": ext_score,
    }
