import os
import uuid
import subprocess
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI()

API_KEY = os.getenv("FFMPEG_API_KEY", "")

class VideoRequest(BaseModel):
    caption: str
    durationSec: int = 15
    width: int = 1080
    height: int = 1920
    fps: int = 30
    fontSize: int = 48

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/render")
def render_video(payload: VideoRequest, x_api_key: str | None = Header(default=None)):
    # Seguridad simple por header
    if API_KEY:
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized")

    caption = (payload.caption or "").strip()
    if not caption:
        raise HTTPException(status_code=400, detail="caption is required")

    # Recorta para evitar captions enormes
    caption = caption[:1400]

    workdir = "/tmp/ffmpeg"
    os.makedirs(workdir, exist_ok=True)

    job_id = str(uuid.uuid4())
    txt_path = os.path.join(workdir, f"{job_id}.txt")
    out_path = os.path.join(workdir, f"{job_id}.mp4")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(caption)

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

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=e.stderr.decode("utf-8", errors="ignore"))

    return FileResponse(out_path, media_type="video/mp4", filename="video.mp4")
