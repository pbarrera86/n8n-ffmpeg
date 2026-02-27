import os
import time
import uuid
import base64
import io
from typing import Optional

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from PIL import Image

from diffusers import StableDiffusionPipeline, DPMSolverMultistepScheduler

APP_NAME = "sd-image-api"

MODEL_ID = os.getenv("MODEL_ID", "runwayml/stable-diffusion-v1-5")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/data/outputs")
HF_TOKEN = os.getenv("HF_TOKEN", None)

MAX_IMAGE_SIDE = int(os.getenv("MAX_IMAGE_SIDE", "512"))
DEFAULT_STEPS = int(os.getenv("DEFAULT_STEPS", "20"))
DEFAULT_GUIDANCE = float(os.getenv("DEFAULT_GUIDANCE", "7.0"))

os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title=APP_NAME)

pipe: Optional[StableDiffusionPipeline] = None


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=2000)
    negative_prompt: Optional[str] = Field(
        default="text, watermark, logo, blurry, low quality, bad anatomy, extra fingers"
    )
    width: int = Field(default=512, ge=256, le=1024)
    height: int = Field(default=512, ge=256, le=1024)
    steps: int = Field(default=DEFAULT_STEPS, ge=4, le=50)
    guidance: float = Field(default=DEFAULT_GUIDANCE, ge=0.0, le=20.0)
    seed: Optional[int] = Field(default=None, ge=0, le=2**31 - 1)
    num_images: int = Field(default=1, ge=1, le=4)


class GenerateResponse(BaseModel):
    model_id: str
    took_seconds: float
    seed: int
    images: list[str]  # base64 strings (data:image/png;base64,...)


@app.on_event("startup")
def load_model():
    global pipe

    device = "cpu"
    torch_dtype = torch.float32

    try:
        pipe = StableDiffusionPipeline.from_pretrained(
            MODEL_ID,
            torch_dtype=torch_dtype,
            use_safetensors=True,
            safety_checker=None,
            token=HF_TOKEN,
        )
    except TypeError:
        pipe = StableDiffusionPipeline.from_pretrained(
            MODEL_ID,
            torch_dtype=torch_dtype,
            use_safetensors=True,
            safety_checker=None,
            use_auth_token=HF_TOKEN,
        )
    except Exception as e:
        raise RuntimeError(
            f"No pude cargar el modelo '{MODEL_ID}'. "
            f"Si es runwayml/stable-diffusion-v1-5, quizá debes aceptar términos en HuggingFace "
            f"y/o configurar HF_TOKEN. Error: {e}"
        )

    pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
    pipe.enable_attention_slicing("max")
    pipe = pipe.to(device)


@app.get("/health")
def health():
    return {"ok": True, "model_id": MODEL_ID}


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    global pipe
    if pipe is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet")

    w = min(req.width, MAX_IMAGE_SIDE)
    h = min(req.height, MAX_IMAGE_SIDE)
    w = (w // 8) * 8
    h = (h // 8) * 8

    if w < 256 or h < 256:
        raise HTTPException(status_code=400, detail="width/height too small after limits")

    seed = req.seed if req.seed is not None else int.from_bytes(os.urandom(2), "big")
    gen = torch.Generator(device="cpu").manual_seed(seed)

    start = time.time()

    try:
        out = pipe(
            prompt=req.prompt,
            negative_prompt=req.negative_prompt,
            width=w,
            height=h,
            num_inference_steps=req.steps,
            guidance_scale=req.guidance,
            num_images_per_prompt=req.num_images,
            generator=gen,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation error: {e}")

    images = out.images  # list[PIL.Image]
    b64_images = []

    for img in images:
        if not isinstance(img, Image.Image):
            continue

        # --- CAMBIO: guardar en disco Y devolver base64 ---
        file_id = str(uuid.uuid4())
        filename = f"{file_id}.png"
        path = os.path.join(OUTPUT_DIR, filename)
        img.save(path, format="PNG", optimize=True)

        # Convertir a base64
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        b64_images.append(f"data:image/png;base64,{b64}")

    took = time.time() - start

    return GenerateResponse(
        model_id=MODEL_ID,
        took_seconds=round(took, 3),
        seed=seed,
        images=b64_images,
    )
