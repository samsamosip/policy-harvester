FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword curl libmagic1 libgl1 libglib2.0-0 libxcb1 fonts-nanum fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/*

# CPU-only torch for Docling's layout/table models; the default wheel pulls CUDA.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 torchvision==0.29.1

COPY pyproject.toml requirements.lock README.md inha_parser.py revision_resolver.py /app/
RUN python - <<'PY'
import subprocess
import sys
import tomllib

with open("pyproject.toml", "rb") as source:
    dependencies = tomllib.load(source)["project"]["dependencies"]
subprocess.check_call([
    sys.executable, "-m", "pip", "install", "--no-cache-dir",
    "--constraint", "requirements.lock", *dependencies,
])
PY

# Models are baked into the image; nothing is downloaded at runtime.
ENV DOCLING_ARTIFACTS_PATH=/opt/docling-models HF_HUB_OFFLINE=1
RUN HF_HUB_OFFLINE=0 docling-tools models download -o /opt/docling-models layout tableformer

# rhwp renders HWP/HWPX to PDF (pinned, checksummed); it embeds the Korean fonts installed above.
ENV RHWP_VERSION=0.8.7
RUN curl -fsSL https://github.com/edwardkim/rhwp/releases/download/v0.8.7/rhwp-v0.8.7-linux-x86_64.tar.gz -o /tmp/rhwp.tgz && \
    echo "24de2bdaa0b69f86302a27e7fdfb570ad3c53b0ab3a4eac06446adc651dfb988  /tmp/rhwp.tgz" | sha256sum -c - && \
    tar -xzf /tmp/rhwp.tgz -C /tmp && install -m 0755 /tmp/rhwp/rhwp /usr/local/bin/rhwp && \
    rm -rf /tmp/rhwp /tmp/rhwp.tgz

# Swagger UI is served from the image (pinned, checksummed) so /docs needs no CDN and fits the CSP.
ENV SWAGGER_UI_DIR=/opt/swagger-ui
RUN curl -fsSL https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.33.1.tgz -o /tmp/swagger.tgz && \
    echo "b468ff5f49451f194a739bc245c85b28c7d3d33057a8409a9b2369da37e215b9  /tmp/swagger.tgz" | sha256sum -c - && \
    mkdir -p /opt/swagger-ui && \
    tar -xzf /tmp/swagger.tgz -C /opt/swagger-ui --strip-components=1 \
      package/swagger-ui-bundle.js package/swagger-ui.css package/favicon-32x32.png package/LICENSE && \
    rm /tmp/swagger.tgz

COPY src /app/src
RUN pip install --no-cache-dir --no-deps .

# Internal embedding models (ONNX, CPU) are baked in like the Docling models.
ENV ONNX_MODELS_DIR=/opt/embedding-models
RUN HF_HUB_OFFLINE=0 python -c "from policy_harvester.ai.onnx_embeddings import ONNX_MODELS, download; [download(spec) for spec in ONNX_MODELS.values()]" && \
    rm -rf /opt/embedding-models/*/.cache

COPY alembic.ini schema.sql /app/
COPY migrations /app/migrations

RUN adduser --disabled-password --gecos '' --uid 10001 app && \
    mkdir -p /data/objects && chown -R app:app /data
USER app

EXPOSE 8000
CMD ["uvicorn", "policy_harvester.api:app", "--host", "0.0.0.0", "--port", "8000"]
