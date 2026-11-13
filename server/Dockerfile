FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIPER_MODEL=/models/hal.onnx \
    TORCH_HOME=/opt/torch \
    HF_HOME=/opt/hf

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# HAL 9000 Piper voice (campwill/HAL-9000-Piper-TTS), baked into the image
RUN mkdir -p /models \
    && curl -fsSL --retry 3 --connect-timeout 15 --max-time 600 -o /models/hal.onnx \
       https://huggingface.co/campwill/HAL-9000-Piper-TTS/resolve/main/hal.onnx \
    && curl -fsSL --retry 3 --connect-timeout 15 --max-time 60 -o /models/hal.onnx.json \
       https://huggingface.co/campwill/HAL-9000-Piper-TTS/resolve/main/hal.onnx.json

# CPU-only PyTorch for voice conversion (Turkish in HAL's voice)
RUN pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Downloads the Turkish voice, kNN-VC models and HAL reference clips, precomputes HAL's voice
RUN python -m app.vc

RUN useradd -m hal && chown -R hal /srv /models /opt/torch
USER hal

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
