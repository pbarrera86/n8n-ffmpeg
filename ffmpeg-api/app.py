import os
import uuid
import subprocess
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI()

API_KEY = os.getenv("FFMPEG_API_KEY", "")

WORKDIR = "/tmp/ffmpeg"


class Slide(BaseModel):
    text: str = Field(..., description="Texto del slide")
    durationSec: int = Field(3, ge=1, le=30, description="Duración por slide en segundos (1-30)")


class VideoRequest(BaseModel):
    # Compatibilidad: antes era requerido, ahora opcional si envías slides
    caption: Optional[str] = None

    # Nuevo: slides dinámicos
    slides: Optional[List[Slide]] = None

    # Defaults
    durationSec: int = 15
    width: int = 1080
    height: int = 1920
    fps: int = 30
    fontSize: int = 48

    # Opcional: nombre deseado del archivo (solo informativo)
    title: Optional[str] = None


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
    # recorta por seguridad
    text = (text or "").strip()
    text = text[:1400]
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

    # ffmpeg concat demuxer
    list_path = os.path.join(WORKDIR, f"{uuid.uuid4()}.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in video_paths:
            # path debe ir entre comillas simples
            f.write(f"file '{p}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c", "copy",
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
    out_path = os.path.join(WORKDIR, f"{job_id}.mp4")

    # 1) Si vienen slides: modo dinámico
    if payload.slides and len(payload.slides) > 0:
        # Filtra slides vacíos
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

        _concat_videos(temp_videos, out_path)
        return FileResponse(out_path, media_type="video/mp4", filename="video.mp4")

    # 2) Compatibilidad: modo antiguo (caption)
    caption = (payload.caption or "").strip()
    if not caption:
        raise HTTPException(status_code=400, detail="caption is required when slides are not provided")

    # si no hay slides, usa durationSec del payload
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
        out_path
    ]
    _run(cmd)

    return FileResponse(out_path, media_type="video/mp4", filename="video.mp4")
