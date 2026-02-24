#!/bin/sh
set -e

echo "[piper-tts] starting custom entrypoint"
echo "[piper-tts] PIPER_MODEL_DOWNLOAD_LINK=$PIPER_MODEL_DOWNLOAD_LINK"

if [ -z "$PIPER_MODEL_DOWNLOAD_LINK" ]; then
  echo "[piper-tts] ERROR: PIPER_MODEL_DOWNLOAD_LINK is empty"
  exit 1
fi

mkdir -p /app/models

echo "[piper-tts] downloading model.onnx..."
python -c "import os,urllib.request; u=os.environ['PIPER_MODEL_DOWNLOAD_LINK'].strip(); urllib.request.urlretrieve(u,'/app/models/model.onnx'); print('ok model.onnx')"

echo "[piper-tts] downloading model.onnx.json..."
python -c "import os,urllib.request; u=os.environ['PIPER_MODEL_DOWNLOAD_LINK'].strip(); ju=u.replace('.onnx?download=true','.onnx.json').replace('.onnx?download=1','.onnx.json'); ju = ju if ju!=u else u.replace('.onnx','.onnx.json'); urllib.request.urlretrieve(ju,'/app/models/model.onnx.json'); print('ok model.onnx.json')"

echo "[piper-tts] patching phoneme_type if needed..."
python -c "import json; p='/app/models/model.onnx.json'; cfg=json.load(open(p,'r',encoding='utf-8')); pt=cfg.get('phoneme_type'); cfg['phoneme_type']='espeak' if pt=='PhonemeType.ESPEAK' else pt; json.dump(cfg, open(p,'w',encoding='utf-8'), ensure_ascii=False); print('phoneme_type:', cfg.get('phoneme_type'))"

echo "[piper-tts] files:"
ls -la /app/models

echo "[piper-tts] launching server on :5000"
exec python -m piper.http_server --model /app/models/model.onnx --config /app/models/model.onnx.json --host 0.0.0.0 --port 5000
