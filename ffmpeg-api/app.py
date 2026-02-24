import os
import uuid
import subprocess
import re
import unicodedata
from typing import List, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pydantic import ConfigDict

import requests
import json
from PIL import Image, ImageDraw, ImageFont

app = FastAPI()

API_KEY           = os.getenv("FFMPEG_API_KEY", "")
AUDIO_DIR         = os.getenv("AUDIO_DIR", "/audio")
IMAGE_DIR         = os.getenv("IMAGE_DIR", "/images")
FONT_FILE         = os.getenv("FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FONT_FILE_REGULAR = os.getenv("FONT_FILE_REGULAR", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


# ─────────────────────────────────────────────
# Modelos
# ─────────────────────────────────────────────
class Slide(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text:         str
    durationSec:  int           = 3
    bgImageUrl:   Optional[str] = None   # URL pública de imagen de fondo para este slide
    bgImageLocal: Optional[str] = None   # nombre de archivo en IMAGE_DIR


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    caption:  Optional[str]         = None
    slides:   Optional[List[Slide]] = None

    width:  int = 1080
    height: int = 1920
    fps:    int = 30

    fontSize:        int   = 52
    marginX:         int   = 80
    marginY:         int   = 140
    lineWidthChars:  int   = 0
    maxLines:        int   = 0
    lineSpacing:     int   = 20
    boxAlpha:        float = 0.55
    boxBorder:       int   = 40

    output:          Optional[str] = None   # nombre del archivo de salida

    # Imagen de fondo global (aplica a slides sin bgImage propio)
    bgImageUrl:      Optional[str] = None
    bgImageLocal:    Optional[str] = None

    # Audio
    audioLocal:      Optional[str] = None   # archivo en AUDIO_DIR
    audioUrl:        Optional[str] = None   # URL pública
    audioVolume:     float = 0.9
    audioFadeOutSec: int   = 1


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


def download_file(url: str, dest_path: str):
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
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def run(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-3000:])


def resolve_bg_image(
    bg_url: Optional[str],
    bg_local: Optional[str],
    workdir: str,
    job_id: str,
    suffix: str,
) -> Optional[str]:
    """Devuelve ruta local de imagen de fondo. Prioridad: URL > local > None."""
    if bg_url:
        dest = os.path.join(workdir, f"{job_id}_bg_{suffix}.jpg")
        try:
            download_file(bg_url, dest)
            return dest
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"bgImageUrl download failed ({suffix}): {e}")
    if bg_local:
        fname = safe_filename(bg_local)
        path  = os.path.join(IMAGE_DIR, fname)
        if not os.path.isfile(path):
            raise HTTPException(status_code=400, detail=f"bgImageLocal not found: {path}")
        return path
    return None


# ─────────────────────────────────────────────
# Renderizar slide como PNG
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
    box_alpha: float,
    box_border: int,
    max_lines: int,
    bg_image_path: Optional[str] = None,
):
    text     = sanitize_text(text)
    usable_w = width  - 2 * margin_x
    usable_h = height - 2 * margin_y

    font_size = base_font_size
    font      = None
    lines     = []

    for attempt in range(12):
        try:
            font = ImageFont.truetype(FONT_FILE, font_size)
        except Exception:
            font = ImageFont.load_default()

        words   = text.replace("\n", " \n ").split(" ")
        lines   = []
        current = ""
        for word in words:
            if word == "\n":
                lines.append(current.strip())
                current = ""
                continue
            test = (current + " " + word).strip()
            bbox = font.getbbox(test)
            tw   = bbox[2] - bbox[0]
            if tw <= usable_w:
                current = test
            else:
                if current:
                    lines.append(current.strip())
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

        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()

        if max_lines > 0 and len(lines) > max_lines:
            lines = lines[:max_lines]
            if lines:
                lines[-1] = lines[-1].rstrip() + "…"

        line_h  = font_size + line_spacing
        block_h = len(lines) * line_h - line_spacing

        if block_h <= usable_h:
            break

        font_size = max(22, font_size - 4)

    # ── Fondo ────────────────────────────────────────
    if bg_image_path and os.path.isfile(bg_image_path):
        try:
            bg  = Image.open(bg_image_path).convert("RGB")
            img = bg.resize((width, height), Image.LANCZOS)
        except Exception:
            img = Image.new("RGB", (width, height), color=(0, 0, 0))
    else:
        img = Image.new("RGB", (width, height), color=(0, 0, 0))

    draw = ImageDraw.Draw(img, "RGBA")

    line_h  = font_size + line_spacing
    block_h = len(lines) * line_h - line_spacing
    start_y = (height - block_h) // 2
    start_x = margin_x

    pad    = box_border
    box_x0 = max(0,      start_x - pad)
    box_y0 = max(0,      start_y - pad)
    box_x1 = min(width,  start_x + usable_w + pad)
    box_y1 = min(height, start_y + block_h  + pad)

    alpha_val = int(box_alpha * 255)
    draw.rectangle([box_x0, box_y0, box_x1, box_y1], fill=(0, 0, 0, alpha_val))

    for i, line in enumerate(lines):
        y = start_y + i * line_h
        draw.text((start_x, y), line, font=font, fill=(255, 255, 255, 255))

    img.save(out_png, "PNG")


# ─────────────────────────────────────────────
# PNG → MP4
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
# Concatena segmentos
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
# Lógica central compartida
# ─────────────────────────────────────────────
def _process(
    payload: VideoRequest,
    audio_file_path: Optional[str],
    x_api_key: Optional[str],
) -> FileResponse:

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

    norm_slides: List[Tuple] = []
    for sld in slides:
        t = sanitize_text(sld.text or "")
        if not t:
            continue
        dur = max(1, min(int(sld.durationSec or 3), 30))
        norm_slides.append((t, dur, sld.bgImageUrl, sld.bgImageLocal))

    if not norm_slides:
        raise HTTPException(status_code=400, detail="slides are empty after cleaning")

    total_duration = sum(d for _, d, *_ in norm_slides)

    margin_x   = max(40,  min(int(payload.marginX),   300))
    margin_y   = max(40,  min(int(payload.marginY),   600))
    box_border = max(0,   min(int(payload.boxBorder),  80))
    font_size  = max(26,  min(int(payload.fontSize),  120))
    max_lines  = max(0,   int(payload.maxLines  or 0))

    if payload.width  - 2 * margin_x < 200:
        margin_x = (payload.width  - 200) // 2
    if payload.height - 2 * margin_y < 200:
        margin_y = (payload.height - 200) // 2

    workdir   = "/tmp/ffmpeg"
    os.makedirs(workdir, exist_ok=True)
    job_id    = str(uuid.uuid4())
    out_final = os.path.join(workdir, f"{job_id}.mp4")

    # Imagen de fondo global
    global_bg = resolve_bg_image(
        payload.bgImageUrl, payload.bgImageLocal, workdir, job_id, "global"
    )

    seg_paths = []
    png_paths = []
    tmp_files = list(filter(None, [audio_file_path]))

    try:
        for i, (txt, dur, slide_bg_url, slide_bg_local) in enumerate(norm_slides):
            png_path = os.path.join(workdir, f"{job_id}_slide_{i:02d}.png")
            seg_path = os.path.join(workdir, f"{job_id}_seg_{i:02d}.mp4")

            # Fondo: propio del slide > global > negro
            slide_bg = resolve_bg_image(
                slide_bg_url, slide_bg_local, workdir, job_id, f"slide{i}"
            ) if (slide_bg_url or slide_bg_local) else global_bg

            render_slide_png(
                out_png        = png_path,
                text           = txt,
                width          = payload.width,
                height         = payload.height,
                base_font_size = font_size,
                margin_x       = margin_x,
                margin_y       = margin_y,
                line_spacing   = int(payload.lineSpacing or 20),
                box_alpha      = float(payload.boxAlpha),
                box_border     = box_border,
                max_lines      = max_lines,
                bg_image_path  = slide_bg,
            )
            png_paths.append(png_path)

            png_to_mp4(
                png_path = png_path,
                out_path = seg_path,
                duration = dur,
                width    = payload.width,
                height   = payload.height,
                fps      = payload.fps,
            )
            seg_paths.append(seg_path)

        concat_segments_demuxer(seg_paths, out_final)

        # ── Resolver audio (prioridad: multipart > audioLocal > audioUrl) ──
        audio_path = audio_file_path

        if not audio_path and payload.audioLocal:
            fname     = safe_filename(payload.audioLocal)
            candidate = os.path.join(AUDIO_DIR, fname)
            if not os.path.isfile(candidate):
                raise HTTPException(status_code=400, detail=f"audioLocal not found: {candidate}")
            audio_path = candidate

        if not audio_path and payload.audioUrl:
            tmp_audio = os.path.join(workdir, f"{job_id}_audio")
            try:
                download_file(payload.audioUrl, tmp_audio)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"audioUrl download failed: {e}")
            audio_path = tmp_audio
            tmp_files.append(tmp_audio)

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

        # ── Nombre del archivo de salida ───────────────
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
        for p in tmp_files:
            try:
                os.remove(p)
            except Exception:
                pass


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "audio_dir": AUDIO_DIR, "image_dir": IMAGE_DIR, "font_file": FONT_FILE}


@app.post("/render")
def render_video(payload: VideoRequest, x_api_key: str | None = Header(default=None)):
    """Endpoint JSON normal (sin audio multipart)."""
    return _process(payload=payload, audio_file_path=None, x_api_key=x_api_key)


@app.post("/render-with-audio")
async def render_video_with_audio(
    payload:    str        = Form(...),
    audio_file: UploadFile = File(...),
    x_api_key:  str | None = Header(default=None),
):
    """
    Endpoint multipart/form-data para subir MP3 desde n8n.
      - payload:    JSON string con los mismos campos que VideoRequest
      - audio_file: archivo MP3 / AAC / WAV
    """
    try:
        data = json.loads(payload)
        req  = VideoRequest(**data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"payload JSON inválido: {e}")

    workdir   = "/tmp/ffmpeg"
    os.makedirs(workdir, exist_ok=True)
    ext       = os.path.splitext(audio_file.filename or ".mp3")[1] or ".mp3"
    tmp_audio = os.path.join(workdir, f"{uuid.uuid4()}_upload{ext}")

    contents = await audio_file.read()
    with open(tmp_audio, "wb") as f:
        f.write(contents)

    return _process(payload=req, audio_file_path=tmp_audio, x_api_key=x_api_key)
