import os
import uuid
import subprocess
import textwrap
import re
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import requests

app = FastAPI()

# Tu header en n8n: X-API-Key
API_KEY = os.getenv("FFMPEG_API_KEY", "")

# Carpeta donde montarás mp3 locales (volumen en docker-compose)
AUDIO_DIR = os.getenv("AUDIO_DIR", "/audio")

# Fuente (MUY IMPORTANTE). En Dockerfile instalamos DejaVu + Noto.
FONT_FILE = os.getenv("FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


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
    fontSize: int = 52

    # ✅ márgenes (para ganar ancho y controlar layout desde n8n)
    marginX: int = 40
    marginY: int = 120

    # ✅ wrap: si es 0 o <=0, usamos auto-cálculo
    lineWidthChars: int = 0

    # límite de líneas (para que no se desborde)
    maxLines: int = 12

    lineSpacing: int = 14
    boxAlpha: float = 0.45

    # ✅ IMPORTANTE: boxBorder grande reduce ancho útil
    boxBorder: int = 12

    # -----------------------
    # Audio (opcional, listo)
    # -----------------------
    audioLocal: Optional[str] = None
    audioUrl: Optional[str] = None
    audioVolume: float = 0.9
    audioFadeOutSec: int = 1


@app.get("/health")
def health():
    return {"ok": True, "audio_dir": AUDIO_DIR, "font_file": FONT_FILE}


def safe_filename(name: str) -> str:
    name = (name or "").strip()
    name = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_", ".", " "))
    name = name.replace(" ", "_")
    while "__" in name:
        name = name.replace("__", "_")
    return name[:120]


def download_audio(url: str, dest_path: str):
    r = requests.get(url, timeout=30, stream=True)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)


def sanitize_text(t: str) -> str:
    """
    Evita caracteres que rompen drawtext / fuentes:
    - controles
    - emojis (si la fuente no los soporta salen como □)
    """
    if t is None:
        return ""
    t = t.replace("\r", " ").strip()
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", t)
    # quita emojis fuera de BMP
    t = re.sub(r"[\U00010000-\U0010FFFF]", "", t)
    return t.strip()


def wrap_text(txt: str, width_chars: int, max_lines: int) -> str:
    txt = sanitize_text(txt)
    if not txt:
        return ""

    lines: List[str] = []
    for part in txt.splitlines():
        part = part.strip()
        if not part:
            lines.append("")  # conserva saltos
            continue
        wrapped = textwrap.wrap(part, width=width_chars, break_long_words=False, break_on_hyphens=False)
        lines.extend(wrapped if wrapped else [""])

    # limpia dobles vacíos al inicio/fin
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()

    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[:max_lines]
        if lines:
            last = lines[-1]
            lines[-1] = (last[:-1] + "…") if len(last) > 1 else "…"

    return "\n".join(lines).strip()


def choose_font_size(base: int, text_len: int) -> int:
    """
    Baja fontSize si el texto es largo para evitar cortes.
    """
    if text_len <= 90:
        return base
    if text_len <= 170:
        return max(40, base - 10)
    return max(34, base - 16)


def run(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr)


def auto_line_width_chars(
    width: int,
    margin_x: int,
    box_border: int,
    font_size: int,
) -> int:
    """
    ✅ Calcula automáticamente chars por línea para usar mejor el ancho.
    Fórmula: usable_px / (font_size * factor)
    factor 0.56-0.60 suele funcionar bien.
    """
    # ancho útil real
    usable_px = width - (margin_x * 2) - (box_border * 2)

    # factor aproximado de ancho por carácter vs font_size
    factor = 0.58
    est = int(usable_px / (font_size * factor))

    # clamp razonable
    if est < 22:
        est = 22
    if est > 90:
        est = 90
    return est


def render_segment_mp4(
    out_path: str,
    text: str,
    duration: int,
    width: int,
    height: int,
    fps: int,
    font_size: int,
    margin_x: int,
    margin_y: int,
    line_width_chars: int,
    max_lines: int,
    line_spacing: int,
    box_alpha: float,
    box_border: int,
):
    # ✅ Auto wrap si line_width_chars <= 0
    if not line_width_chars or line_width_chars <= 0:
        line_width_chars = auto_line_width_chars(width, margin_x, box_border, font_size)

    wrapped = wrap_text(text, line_width_chars, max_lines)

    txt_path = out_path + ".txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(wrapped)

    # ✅ Alineación: izquierda pero centrado verticalmente,
    # usando márgenes para ganar ancho.
    # Nota: text_w es el ancho del texto renderizado; usamos marginX y marginY.
    draw = (
        f"drawtext=fontfile={FONT_FILE}:textfile='{txt_path}':reload=0:"
        f"fontcolor=white:fontsize={font_size}:line_spacing={line_spacing}:"
        f"box=1:boxcolor=black@{box_alpha}:boxborderw={box_border}:"
        f"x={margin_x}:y={margin_y}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s={width}x{height}:r={fps}",
        "-t", str(duration),
        "-vf", draw,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-movflags", "+faststart",
        out_path
    ]
    run(cmd)

    try:
        os.remove(txt_path)
    except Exception:
        pass


def concat_segments_demuxer(segment_paths: List[str], out_path: str):
    concat_txt = out_path + ".concat.txt"
    with open(concat_txt, "w", encoding="utf-8") as f:
        for p in segment_paths:
            f.write(f"file '{p}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_txt,
        "-c", "copy",
        "-movflags", "+faststart",
        out_path
    ]
    run(cmd)

    try:
        os.remove(concat_txt)
    except Exception:
        pass


@app.post("/render")
def render_video(payload: VideoRequest, x_api_key: str | None = Header(default=None)):
    # Seguridad simple por header
    if API_KEY:
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized")

    if not os.path.isfile(FONT_FILE):
        raise HTTPException(status_code=500, detail=f"FONT_FILE no existe: {FONT_FILE}")

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
        t = sanitize_text(s.text or "")
        if not t:
            continue
        t = t[:900]
        dur = int(s.durationSec or 3)
        dur = max(1, min(dur, 15))
        norm_slides.append((t, dur))

    if not norm_slides:
        raise HTTPException(status_code=400, detail="slides are empty after cleaning")

    total_duration = sum(d for _, d in norm_slides)

    workdir = "/tmp/ffmpeg"
    os.makedirs(workdir, exist_ok=True)

    job_id = str(uuid.uuid4())
    out_final = os.path.join(workdir, f"{job_id}.mp4")

    seg_paths = []
    try:
        # ✅ Clamp de boxBorder: si llega enorme, te come el ancho
        box_border = int(payload.boxBorder or 0)
        if box_border < 0:
            box_border = 0
        if box_border > 40:
            box_border = 40

        margin_x = int(payload.marginX or 0)
        margin_y = int(payload.marginY or 0)
        margin_x = max(0, min(margin_x, 200))
        margin_y = max(0, min(margin_y, 400))

        for i, (txt, dur) in enumerate(norm_slides):
            seg_path = os.path.join(workdir, f"{job_id}_seg_{i:02d}.mp4")

            fs = choose_font_size(int(payload.fontSize), len(txt))

            render_segment_mp4(
                out_path=seg_path,
                text=txt,
                duration=dur,
                width=payload.width,
                height=payload.height,
                fps=payload.fps,
                font_size=fs,
                margin_x=margin_x,
                margin_y=margin_y,
                line_width_chars=int(payload.lineWidthChars or 0),
                max_lines=int(payload.maxLines or 0),
                line_spacing=int(payload.lineSpacing or 14),
                box_alpha=float(payload.boxAlpha),
                box_border=box_border,
            )
            seg_paths.append(seg_path)

        concat_segments_demuxer(seg_paths, out_final)

        # -----------------------
        # Audio opcional (listo)
        # -----------------------
        audio_path = None

        if payload.audioLocal:
            fname = safe_filename(payload.audioLocal)
            candidate = os.path.join(AUDIO_DIR, fname)
            if not os.path.isfile(candidate):
                raise HTTPException(status_code=400, detail=f"audioLocal not found: {candidate}")
            audio_path = candidate

        elif payload.audioUrl:
            tmp_audio = os.path.join(workdir, f"{job_id}_audio")
            try:
                download_audio(payload.audioUrl, tmp_audio)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"audioUrl download failed: {str(e)}")
            audio_path = tmp_audio

        if audio_path:
            out_with_audio = os.path.join(workdir, f"{job_id}_audio.mp4")

            fade_out = max(0, int(payload.audioFadeOutSec or 0))
            fade_start = max(0, total_duration - fade_out)

            audio_filters = [
                f"atrim=0:{total_duration}",
                "asetpts=PTS-STARTPTS",
                f"volume={float(payload.audioVolume):.3f}",
            ]
            if fade_out > 0:
                audio_filters.append(f"afade=t=out:st={fade_start}:d={fade_out}")

            cmd = [
                "ffmpeg", "-y",
                "-i", out_final,
                "-i", audio_path,
                "-filter_complex", f"[1:a]{','.join(audio_filters)}[aout]",
                "-map", "0:v:0",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                out_with_audio
            ]
            run(cmd)
            out_final = out_with_audio

        return FileResponse(out_final, media_type="video/mp4", filename="video.mp4")

    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        # No borro segmentos aquí para no arriesgar lectura concurrente.
        pass
