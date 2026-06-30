import json
import os
import shutil
import subprocess
import sys


VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts", ".webm"
}
MUSIC_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"}


def is_media_file(path):
    return os.path.splitext(path)[1].lower() in (VIDEO_EXTS | MUSIC_EXTS)


def analyze(path, timeout=25):
    if not shutil.which("ffprobe"):
        return {
            "status": "error",
            "error": "ffprobe no esta instalado en este PC o no esta en PATH.",
            "flags": ["sin_ffprobe"],
        }
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"ffprobe tardo mas de {timeout}s.", "flags": ["timeout"]}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "flags": ["ffprobe_error"]}

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:500]
        return {"status": "error", "error": err or "ffprobe no pudo leer el archivo.", "flags": ["ffprobe_error"]}

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"status": "error", "error": "ffprobe devolvio JSON invalido.", "flags": ["ffprobe_error"]}

    return _parse_probe(path, data)


def verify_full(path, timeout=60 * 60):
    """Optional full read through ffmpeg. Heavy, but runs on the PC."""
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg no esta instalado en este PC o no esta en PATH."
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-map", "0", "-f", "null", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "La validacion completa supero el limite de tiempo."
    except Exception as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or "ffmpeg reporto un error.").strip()[:1000]
    if (proc.stderr or "").strip():
        return False, proc.stderr.strip()[:1000]
    return True, ""


def open_file(path):
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 - local desktop helper by user action.
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
        return
    subprocess.Popen(["xdg-open", path])


def open_folder(path):
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    open_file(folder)


def _parse_probe(path, data):
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    first_video = video_streams[0] if video_streams else {}

    audio = [_stream_summary(s) for s in audio_streams]
    subtitles = [_stream_summary(s) for s in subtitle_streams]
    flags = _flags(path, video_streams, audio, subtitles)

    duration = _float(fmt.get("duration"))
    if duration is None:
        for stream in streams:
            duration = _float(stream.get("duration"))
            if duration is not None:
                break

    return {
        "status": "ok",
        "error": "",
        "duration_seconds": duration,
        "width": _int(first_video.get("width")),
        "height": _int(first_video.get("height")),
        "video_codec": first_video.get("codec_name") or "",
        "audio": audio,
        "subtitles": subtitles,
        "audio_summary": _audio_summary(audio),
        "flags": flags,
    }


def _stream_summary(stream):
    tags = stream.get("tags") or {}
    lang = _clean(tags.get("language") or tags.get("LANGUAGE") or "")
    title = _clean(tags.get("title") or tags.get("handler_name") or "")
    codec = _clean(stream.get("codec_name") or "")
    channels = stream.get("channels")
    return {
        "language": lang,
        "title": title,
        "codec": codec,
        "channels": channels,
        "text": " ".join(x for x in [lang, title, codec] if x),
    }


def _flags(path, video_streams, audio, subtitles):
    flags = []
    name = os.path.basename(path).lower()
    audio_text = " ".join(a.get("text", "") for a in audio).lower()
    sub_text = " ".join(s.get("text", "") for s in subtitles).lower()
    full_text = " ".join([name, audio_text, sub_text])

    if not video_streams and os.path.splitext(path)[1].lower() in VIDEO_EXTS:
        flags.append("sin_video")
    if not audio:
        flags.append("sin_audio")
    if _has_spanish(full_text):
        flags.append("audio_es")
    else:
        flags.append("sin_espanol")
    if _has_spain_spanish(full_text):
        flags.append("posible_castellano")
    if _has_latin_spanish(full_text):
        flags.append("posible_latino")
    if len(audio) > 1:
        flags.append("dual_multi_audio")
    if subtitles:
        flags.append("subtitulos")
    return flags


def _has_spanish(text):
    tokens = [
        "spanish", "espanol", "español", " castellano", " latino",
        " latam", "spa", " es ", "es-419", "es_es", "es-es",
    ]
    padded = f" {text} "
    return any(token in padded for token in tokens)


def _has_spain_spanish(text):
    tokens = ["castellano", "es-es", "es_es", "spain", "espana", "españa"]
    return any(token in text for token in tokens)


def _has_latin_spanish(text):
    tokens = ["latino", "latam", "latin american", "es-419", "mex", "mx"]
    return any(token in text for token in tokens)


def _audio_summary(audio):
    if not audio:
        return "Sin audio detectado"
    chunks = []
    for item in audio[:6]:
        parts = []
        if item.get("language"):
            parts.append(item["language"])
        if item.get("title"):
            parts.append(item["title"])
        if item.get("codec"):
            parts.append(item["codec"])
        if item.get("channels"):
            parts.append(f"{item['channels']}ch")
        chunks.append(" / ".join(parts) or "audio")
    extra = len(audio) - len(chunks)
    if extra > 0:
        chunks.append(f"+{extra}")
    return " | ".join(chunks)


def _clean(value):
    return str(value or "").strip()


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
