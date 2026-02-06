import os
import uuid
import subprocess
from typing import List, Optional, Literal

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI()

API_KEY = os.getenv("FFMPEG_API_KEY", "")
WORKDIR = "/tmp/ffmpeg"
AUDIO_DIR = os.getenv("AUDIO_DIR", "/audio")  # carpeta montada para audios locales


class Slide(BaseModel):
    text: str = Field(..., description="Texto del slide")
    durationSec: int = Field(3, ge=1, le=30, description="Duración por slide (1-30s)")


class AudioSpec(BaseModel):
    type: Literal["local", "url"] = "local"
    # local: path relativo a AUDIO_DIR (ej: "beats/track1.mp3") o absoluto (si quieres)
    path: Optional[str] = None
    # url: link directo a mp3
    url: Optional[str] = None
    # volumen de 0.0 a 1.0
    volume: float = Field(0.35, ge=0.0, le=1.0)


class VideoRequest(BaseModel):
    caption: Optional[str] = None
    slides: Optional[List[Slide]] = None

    durationSec: int = 15
    width: int = 1080
    height: int = 1920
    fps: int = 30
    fontSize: int = 48

    title: Optional[str] = None
    audio: Optional[AudioSpec] = None


@app.get("/health")
def health():
    return {"ok": True}


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail=e.stderr.decode("utf-8", errors="ignore")
        )


def _safe_write_text(path: str, text: str) -> None:
    text = (text or "").strip()[:1400]
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _render_single_slide(text: str, duration: int, width: int, height: int, fps: int, font_size: int, out_path: str) -> None:
    os.makedirs(WORKDIR, exist_ok=True)
    txt_path = os.path.join(WORKDIR, f"{uuid.uuid4()}.txt")
    _safe_write_text(txt_path, text)

    size = f"{width}x{height}"
    draw = (
        f"drawtext=textfile={txt_path}:reload=1:"
        f"fontcolor=white:fontsize={font_size}:line_spacing=10:"
        f"x=(w-text_w)/2:y=(h-text_h)/2:"
        f"box=1:boxcolor=black@0.55:boxborderw=24"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s={size}:r={fps}:d={duration}",
        "-vf", draw,
        "-t", str(duration),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        out_path
    ]
    _run(cmd)


def _concat_videos(video_paths: list[str], out_path: str) -> None:
    os.makedirs(WORKDIR, exist_ok=True)
    list_path = os.path.join(WORKDIR, f"{uuid.uuid4()}.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in video_paths:
            f.write(f"file '{p}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        out_path
    ]
    _run(cmd)


def _download_to(path: str, url: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 512):
                if chunk:
                    f.write(chunk)


def _resolve_audio(audio: AudioSpec) -> str:
    """Devuelve ruta local a un mp3 listo para usar."""
    if audio.type == "local":
        if not audio.path:
            raise HTTPException(status_code=400, detail="audio.path is required for type=local")
        # Permite path absoluto o relativo a AUDIO_DIR
        p = audio.path
        if not os.path.isabs(p):
            p = os.path.join(AUDIO_DIR, p)
        if not os.path.exists(p):
            raise HTTPException(status_code=400, detail=f"Local audio not found: {p}")
        return p

    if audio.type == "url":
        if not audio.url:
            raise HTTPException(status_code=400, detail="audio.url is required for type=url")
        # Si es link de drive de compartir, intenta convertirlo a descarga directa (solo si es público)
        url = audio.url.strip()
        if "drive.google.com" in url and "uc?export=download" not in url:
            # intenta extraer el file id
            file_id = None
            if "/file/d/" in url:
                try:
                    file_id = url.split("/file/d/")[1].split("/")[0]
                except Exception:
                    file_id = None
            if file_id:
                url = f"https://drive.google.com/uc?export=download&id={file_id}"

        dst = os.path.join(WORKDIR, f"{uuid.uuid4()}.mp3")
        _download_to(dst, url)
        return dst

    raise HTTPException(status_code=400, detail="Unsupported audio.type")


def _merge_audio(video_path: str, audio_path: str, volume: float, out_path: str) -> None:
    """
    Mezcla el audio en el video.
    - Loopea el audio si es más corto (stream_loop -1)
    - Corta al largo del video (-shortest)
    - Convierte audio a AAC para compatibilidad
    """
    vol = max(0.0, min(1.0, float(volume)))
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1",
        "-i", audio_path,
        "-filter:a", f"volume={vol}",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        out_path
    ]
    _run(cmd)


@app.post("/render")
def render_video(payload: VideoRequest, x_api_key: str | None = Header(default=None)):
    # Seguridad simple por header
    if API_KEY:
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized")

    os.makedirs(WORKDIR, exist_ok=True)
    job_id = str(uuid.uuid4())

    video_no_audio = os.path.join(WORKDIR, f"{job_id}_noaudio.mp4")
    final_out = os.path.join(WORKDIR, f"{job_id}.mp4")

    # 1) Modo slides (dinámico)
    if payload.slides and len(payload.slides) > 0:
        slides = [s for s in payload.slides if (s.text or "").strip()]
        if not slides:
            raise HTTPException(status_code=400, detail="slides provided but all texts are empty")

        temp_videos: list[str] = []
        for i, sl in enumerate(slides):
            tmp_out = os.path.join(WORKDIR, f"{job_id}_{i:02d}.mp4")
            _render_single_slide(
                text=sl.text,
                duration=int(sl.durationSec),
                width=int(payload.width),
                height=int(payload.height),
                fps=int(payload.fps),
                font_size=int(payload.fontSize),
                out_path=tmp_out
            )
            temp_videos.append(tmp_out)

        _concat_videos(temp_videos, video_no_audio)

    else:
        # 2) Modo caption (compatibilidad)
        caption = (payload.caption or "").strip()
        if not caption:
            raise HTTPException(status_code=400, detail="caption is required when slides are not provided")

        txt_path = os.path.join(WORKDIR, f"{job_id}.txt")
        _safe_write_text(txt_path, caption)

        size = f"{payload.width}x{payload.height}"
        draw = (
            f"drawtext=textfile={txt_path}:reload=1:"
            f"fontcolor=white:fontsize={payload.fontSize}:line_spacing=10:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:"
            f"box=1:boxcolor=black@0.55:boxborderw=24"
        )

        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=black:s={size}:r={payload.fps}:d={payload.durationSec}",
            "-vf", draw,
            "-t", str(payload.durationSec),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            video_no_audio
        ]
        _run(cmd)

    # 3) Si viene audio, mezclarlo
    if payload.audio:
        audio_path = _resolve_audio(payload.audio)
        _merge_audio(video_no_audio, audio_path, payload.audio.volume, final_out)
    else:
        # sin audio: solo renombra/sale
        final_out = video_no_audio

    return FileResponse(final_out, media_type="video/mp4", filename="video.mp4")
