import os
import uuid
import subprocess
import textwrap
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Para audioUrl (descargar audio). Si no quieres usarlo todavía, puedes quitar requests
# y dejar solo audioLocal.
import requests


app = FastAPI()

API_KEY = os.getenv("FFMPEG_API_KEY", "")

# Carpeta donde montarás mp3 locales (volumen en docker-compose)
AUDIO_DIR = os.getenv("AUDIO_DIR", "/audio")


# -----------------------
# Modelos
# -----------------------
class Slide(BaseModel):
    text: str
    durationSec: int = 3


class VideoRequest(BaseModel):
    # Compatibilidad: si mandas solo caption, se convierte en 1 slide
    caption: Optional[str] = None

    # Nuevo: slides para video dinámico
    slides: Optional[List[Slide]] = None

    width: int = 1080
    height: int = 1920
    fps: int = 30

    # Tipografía / layout
    fontSize: int = 48
    marginX: int = 90
    marginY: int = 140
    lineWidthChars: int = 28

    # -----------------------
    # Audio (opcional)
    # -----------------------
    # 1) audioLocal: nombre de archivo dentro de AUDIO_DIR (ej: "beat.mp3")
    audioLocal: Optional[str] = None

    # 2) audioUrl: url a mp3 (o m4a/wav, ffmpeg intenta manejarlo)
    audioUrl: Optional[str] = None

    # 3) audioVolume: volumen (0.0 a 1.0)
    audioVolume: float = 0.9

    # 4) audioFadeOutSec: aplica un fade out al final (segundos)
    audioFadeOutSec: int = 1


@app.get("/health")
def health():
    return {"ok": True, "audio_dir": AUDIO_DIR}


def wrap_text(txt: str, width_chars: int) -> str:
    txt = (txt or "").strip()
    if not txt:
        return ""
    lines = []
    for part in txt.splitlines():
        part = part.strip()
        if not part:
            lines.append("")
            continue
        lines.append(textwrap.fill(part, width=width_chars))
    return "\n".join(lines).strip()


def safe_filename(name: str) -> str:
    name = (name or "").strip()
    name = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_", ".", " "))
    name = name.replace(" ", "_")
    while "__" in name:
        name = name.replace("__", "_")
    return name[:120]


def download_audio(url: str, dest_path: str):
    # Descarga simple con requests
    r = requests.get(url, timeout=30, stream=True)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)


@app.post("/render")
def render_video(payload: VideoRequest, x_api_key: str | None = Header(default=None)):
    # Seguridad simple por header
    if API_KEY:
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized")

    slides = payload.slides or []
    caption = (payload.caption or "").strip()

    # Regla: o caption o slides
    if not slides and not caption:
        raise HTTPException(status_code=400, detail="caption or slides is required")

    # Si no vienen slides, hacemos 1 slide con caption
    if not slides:
        slides = [Slide(text=caption, durationSec=15)]

    # Normaliza y recorta slides
    norm_slides = []
    for s in slides:
        t = (s.text or "").strip()
        if not t:
            continue
        t = t[:800]
        dur = int(s.durationSec or 3)
        if dur < 1:
            dur = 1
        if dur > 15:
            dur = 15
        norm_slides.append((t, dur))

    if not norm_slides:
        raise HTTPException(status_code=400, detail="slides are empty after cleaning")

    # Duración total del video
    total_duration = sum(d for _, d in norm_slides)

    workdir = "/tmp/ffmpeg"
    os.makedirs(workdir, exist_ok=True)

    job_id = str(uuid.uuid4())
    out_path = os.path.join(workdir, f"{job_id}.mp4")

    size = f"{payload.width}x{payload.height}"

    # -----------------------
    # Generación de video por slides (segmentos + concat)
    # -----------------------
    inputs = []
    filter_parts = []
    concat_inputs = []

    for i, (txt, dur) in enumerate(norm_slides):
        txt_wrapped = wrap_text(txt, payload.lineWidthChars)
        txt_path = os.path.join(workdir, f"{job_id}_{i}.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(txt_wrapped)

        inputs += [
            "-f", "lavfi",
            "-i", f"color=c=black:s={size}:r={payload.fps}:d={dur}"
        ]

        draw = (
            f"drawtext=textfile='{txt_path}':reload=0:"
            f"fontcolor=white:fontsize={payload.fontSize}:line_spacing=18:"
            f"x={payload.marginX}:y={payload.marginY}:"
            f"box=1:boxcolor=black@0.55:boxborderw=30"
        )

        filter_parts.append(f"[{i}:v]{draw},format=yuv420p[v{i}]")
        concat_inputs.append(f"[v{i}]")

    n = len(norm_slides)
    filter_complex_video = ";".join(filter_parts) + ";" + "".join(concat_inputs) + f"concat=n={n}:v=1:a=0[vout]"

    # -----------------------
    # Audio opcional (listo para más adelante)
    # -----------------------
    audio_path = None

    # Preferimos audioLocal si viene, si no audioUrl
    if payload.audioLocal:
        fname = safe_filename(payload.audioLocal)
        candidate = os.path.join(AUDIO_DIR, fname)
        if not os.path.isfile(candidate):
            raise HTTPException(status_code=400, detail=f"audioLocal not found: {candidate}")
        audio_path = candidate

    elif payload.audioUrl:
        # Descarga al workdir
        tmp_audio = os.path.join(workdir, f"{job_id}_audio")
        try:
            download_audio(payload.audioUrl, tmp_audio)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"audioUrl download failed: {str(e)}")
        audio_path = tmp_audio

    # Si hay audio, lo recortamos a duración del video y aplicamos volumen + fade-out
    # Si NO hay audio, solo video.
    cmd = ["ffmpeg", "-y", *inputs]

    if audio_path:
        cmd += ["-i", audio_path]

        # Construimos filter_complex final:
        # 1) video: [vout]
        # 2) audio: [a0] = recorta a total_duration, volumen, fadeout
        # 3) map v y a
        fade_out = max(0, int(payload.audioFadeOutSec or 0))
        # start fade out
        fade_start = max(0, total_duration - fade_out)

        audio_filters = [
            f"atrim=0:{total_duration}",
            "asetpts=PTS-STARTPTS",
            f"volume={float(payload.audioVolume):.3f}",
        ]
        if fade_out > 0:
            audio_filters.append(f"afade=t=out:st={fade_start}:d={fade_out}")

        filter_complex = (
            filter_complex_video
            + f";[1:a]{','.join(audio_filters)}[aout]"
        )

        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", "libx264",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            out_path
        ]
    else:
        cmd += [
            "-filter_complex", filter_complex_video,
            "-map", "[vout]",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            out_path
        ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=e.stderr.decode("utf-8", errors="ignore"))

    return FileResponse(out_path, media_type="video/mp4", filename="video.mp4")
