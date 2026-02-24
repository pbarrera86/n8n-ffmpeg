FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    fonts-noto-core \
    fonts-noto-color-emoji \
    fonts-noto-extra \
    fonts-noto-cjk \
    fontconfig \
    ca-certificates \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && \
    pip install --no-cache-dir Pillow

COPY app.py /app/app.py

ENV FFMPEG_API_KEY="puga_ffmpeg_2026_12345"
ENV AUDIO_DIR="/audio"
ENV FONT_FILE="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
ENV FONT_FILE_REGULAR="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
