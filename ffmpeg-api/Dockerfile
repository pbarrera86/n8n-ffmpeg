FROM python:3.11-slim

# Paquetes del sistema:
# - ffmpeg: render y mezcla audio/video
# - ca-certificates: necesario para descargar MP3 por HTTPS (requests)
# - curl: útil para pruebas/debug
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -r requirements.txt

COPY app.py /app/

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
