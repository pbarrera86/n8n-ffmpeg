import os
import uuid
import subprocess
import textwrap
import re
import math
import unicodedata
from typing import List, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pydantic import ConfigDict

import requests
from PIL import Image, ImageDraw, ImageFont

app = FastAPI()

API_KEY   = os.getenv("FFMPEG_API_KEY", "")
AUDIO_DIR = os.getenv("AUDIO_DIR", "/audio")
FONT_FILE = os.getenv("FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FONT_FILE_REGULAR = os.getenv("FONT_FILE_REGULAR", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

# ─────────────────────────────────────────────
# Modelos
# ─────────────────────────────────────────────
class Slide(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str
    durationSec: int = 3


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    caption:  Optional[str]         = None
    slides:   Optional[List[Slide]] = None

    width:  int = 1080
    height: int = 1920
    fps:    int = 30

    fontSize:       int   = 52
    marginX:        int   = 80
    marginY:        int   = 140
    lineWidthChars: int   = 0      # 0 = auto
    maxLines:       int   = 0      # 0 = auto
    lineSpacing:    int   = 20
    boxAlpha:       float = 0.55
    boxBorder:      int   = 40

    output:         Optional[str] = None
    audioLocal:     Optional[str] = None
    audioUrl:       Optional[str] = None
    audioVolume:    float = 0.9
    audioFadeOutSec: int  = 1


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
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
    if t is None:
        return ""
    t = unicodedata.normalize("NFKC", str(t))
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\u00A0", " ")
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", t)
    t = re.sub(r"[\u200B-\u200F\u202A-\u202E\u2060-\u206F]", "", t)
    # Mantener emojis — Pillow sí puede renderizarlos con Noto
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def run(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-3000:])


# ─────────────────────────────────────────────
# Núcleo: renderizar slide como imagen PNG con Pillow
# ─────────────────────────────────────────────
def render_slide_png(
    out_png: str,
    text: str,
    width: int,
    height: int,
    base_font_size: int,
    margin_x: int,
    margin_y: int,
    line_spacing: int,
    box_alpha: int,   # 0-255
    box_border: int,
    max_lines: int,
):
    """
    Renderiza texto centrado horizontal y verticalmente con Pillow.
    Ajusta automáticamente el font_size para que el bloque de texto
    nunca salga del área útil (width - 2*margin_x).
    """
    text = sanitize_text(text)

    # Área útil
    usable_w = width  - 2 * margin_x
    usable_h = height - 2 * margin_y

    # Carga fuente y ajusta tamaño hacia abajo si no cabe
    font_size = base_font_size
    font      = None
    lines     = []

    for attempt in range(12):
        try:
            font = ImageFont.truetype(FONT_FILE, font_size)
        except Exception:
            font = ImageFont.load_default()

        # Word-wrap usando ancho real en píxeles
        words  = text.replace("\n", " \n ").split(" ")
        lines  = []
        current = ""
        for word in words:
            if word == "\n":
                lines.append(current.strip())
                current = ""
                continue
            test = (current + " " + word).strip()
            # mide ancho del texto candidato
            bbox = font.getbbox(test)
            tw   = bbox[2] - bbox[0]
            if tw <= usable_w:
                current = test
            else:
                if current:
                    lines.append(current.strip())
                # si la palabra sola es más ancha, córtala caracter a caracter
                while True:
                    bbox2 = font.getbbox(word)
                    if (bbox2[2] - bbox2[0]) <= usable_w or len(word) <= 1:
                        current = word
                        break
                    word_line = ""
                    for ch in word:
                        btest = font.getbbox(word_line + ch)
                        if (btest[2] - btest[0]) > usable_w:
                            lines.append(word_line)
                            word_line = ch
                        else:
                            word_line += ch
                    current = word_line
                    break
        if current.strip():
            lines.append(current.strip())

        # Elimina líneas vacías al inicio/fin
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()

        # Recorta si supera max_lines
        if max_lines > 0 and len(lines) > max_lines:
            lines = lines[:max_lines]
            if lines:
                lines[-1] = lines[-1].rstrip() + "…"

        # Calcula alto total del bloque
        line_h     = font_size + line_spacing
        block_h    = len(lines) * line_h - line_spacing  # sin spacing después del último

        if block_h <= usable_h:
            break  # cabe → listo

        # No cabe → reduce fuente
        font_size = max(22, font_size - 4)

    # ── Dibuja ──────────────────────────────────────
    img  = Image.new("RGB", (width, height), color=(0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    line_h  = font_size + line_spacing
    block_h = len(lines) * line_h - line_spacing
    block_w = max(
        (font.getbbox(ln)[2] - font.getbbox(ln)[0]) for ln in lines
    ) if lines else usable_w

    # Centrado vertical y horizontal
    start_y = (height - block_h) // 2
    start_x = margin_x  # alineado a la izquierda del margen (texto izquierda)

    # Caja de fondo (semitransparente)
    pad    = box_border
    box_x0 = start_x - pad
    box_y0 = start_y - pad
    box_x1 = start_x + usable_w + pad
    box_y1 = start_y + block_h  + pad

    # Clamp caja al frame
    box_x0 = max(0, box_x0)
    box_y0 = max(0, box_y0)
    box_x1 = min(width,  box_x1)
    box_y1 = min(height, box_y1)

    alpha_val = int(box_alpha * 255)
    draw.rectangle([box_x0, box_y0, box_x1, box_y1], fill=(0, 0, 0, alpha_val))

    # Dibuja cada línea
    for i, line in enumerate(lines):
        y = start_y + i * line_h
        draw.text((start_x, y), line, font=font, fill=(255, 255, 255, 255))

    img.save(out_png, "PNG")


# ─────────────────────────────────────────────
# Convierte PNG → MP4 (duración fija)
# ─────────────────────────────────────────────
def png_to_mp4(png_path: str, out_path: str, duration: int, width: int, height: int, fps: int):
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", png_path,
        "-t", str(duration),
        "-vf", f"scale={width}:{height},format=yuv420p",
        "-c:v", "libx264",
        "-preset", "fast",
        "-r", str(fps),
        "-movflags", "+faststart",
        out_path
    ]
    run(cmd)


# ─────────────────────────────────────────────
# Concatena segmentos con demuxer
# ─────────────────────────────────────────────
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


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "audio_dir": AUDIO_DIR, "font_file": FONT_FILE}


@app.post("/render")
def render_video(payload: VideoRequest, x_api_key: str | None = Header(default=None)):
    if API_KEY:
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized")

    if not os.path.isfile(FONT_FILE):
        raise HTTPException(status_code=500, detail=f"FONT_FILE no existe: {FONT_FILE}")

    slides  = payload.slides or []
    caption = sanitize_text(payload.caption or "")

    if not slides and not caption:
        raise HTTPException(status_code=400, detail="caption or slides is required")

    if not slides:
        slides = [Slide(text=caption, durationSec=15)]

    # Normaliza slides
    norm_slides: List[Tuple[str, int]] = []
    for sld in slides:
        t = sanitize_text(sld.text or "")
        if not t:
            continue
        dur = max(1, min(int(sld.durationSec or 3), 30))
        norm_slides.append((t, dur))

    if not norm_slides:
        raise HTTPException(status_code=400, detail="slides are empty after cleaning")

    total_duration = sum(d for _, d in norm_slides)

    # Clamps defensivos
    margin_x   = max(40,  min(int(payload.marginX),   300))
    margin_y   = max(40,  min(int(payload.marginY),   600))
    box_border = max(0,   min(int(payload.boxBorder),  80))
    font_size  = max(26,  min(int(payload.fontSize),  120))
    max_lines  = max(0,   int(payload.maxLines  or 0))

    # Evita que márgenes dejen sin área útil
    if payload.width  - 2 * margin_x < 200:
        margin_x = (payload.width  - 200) // 2
    if payload.height - 2 * margin_y < 200:
        margin_y = (payload.height - 200) // 2

    workdir  = "/tmp/ffmpeg"
    os.makedirs(workdir, exist_ok=True)
    job_id   = str(uuid.uuid4())
    out_final = os.path.join(workdir, f"{job_id}.mp4")

    seg_paths = []
    png_paths = []

    try:
        for i, (txt, dur) in enumerate(norm_slides):
            png_path = os.path.join(workdir, f"{job_id}_slide_{i:02d}.png")
            seg_path = os.path.join(workdir, f"{job_id}_seg_{i:02d}.mp4")

            render_slide_png(
                out_png       = png_path,
                text          = txt,
                width         = payload.width,
                height        = payload.height,
                base_font_size= font_size,
                margin_x      = margin_x,
                margin_y      = margin_y,
                line_spacing  = int(payload.lineSpacing or 20),
                box_alpha     = float(payload.boxAlpha),
                box_border    = box_border,
                max_lines     = max_lines,
            )
            png_paths.append(png_path)

            png_to_mp4(
                png_path  = png_path,
                out_path  = seg_path,
                duration  = dur,
                width     = payload.width,
                height    = payload.height,
                fps       = payload.fps,
            )
            seg_paths.append(seg_path)

        concat_segments_demuxer(seg_paths, out_final)

        # ── Audio opcional ─────────────────────────────
        audio_path = None

        if payload.audioLocal:
            fname     = safe_filename(payload.audioLocal)
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
            fade_out   = max(0, int(payload.audioFadeOutSec or 0))
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
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                out_with_audio
            ]
            run(cmd)
            out_final = out_with_audio

       safe_out = safe_filename(payload.output or "") or "video"
        if not safe_out.endswith(".mp4"):
            safe_out += ".mp4"
        return FileResponse(out_final, media_type="video/mp4", filename=safe_out)

    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        for p in png_paths:
            try:
                os.remove(p)
            except Exception:
                pass
