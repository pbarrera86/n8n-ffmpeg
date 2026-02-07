FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    ca-certificates \
    fonts-dejavu-core \
    fonts-noto-core \
    fonts-noto-extra \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f -v

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py

# Directorio para audio local (montable como volumen)
RUN mkdir -p /audio

# ✅ Mejor pasar FFMPEG_API_KEY por docker-compose / variables de entorno.
ENV AUDIO_DIR="/audio"
ENV FONT_FILE="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
