# IntentSeal Delegate: one image with the Gateway API, the Delegate service, the Console (served at /), the content
# filter (engine + local classifier, baked in) and the Gemini agents (Google ADK). Runs as an unprivileged user.
#
#   docker compose up --build -d        # gateway + PostgreSQL (docker-compose.yml), Console on http://localhost:8000
#
# Configuration is environment only (.env.example). The Console's read-only key is the build argument VITE_API_KEY and
# must equal the gateway's INTENTSEAL_PUBLIC_KEY (docker-compose.yml passes both).

FROM node:22-slim AS console
WORKDIR /console
COPY apps/console/package.json apps/console/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/console ./
ARG VITE_API_KEY=""
ENV VITE_API_BASE="" VITE_API_KEY=$VITE_API_KEY VITE_PRODUCT_NAME=IntentSeal
RUN npm run build


FROM python:3.11-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY requirements.lock ./
COPY packages ./packages
COPY services ./services
# Runtime dependencies only, at the exact versions of requirements.lock (a constraints file: nothing extra installed).
RUN pip install -c requirements.lock "./packages/core[engine,classifiers,ocr,postgres]" ./packages/sdk ./services/gateway openai
# IntentSeal Delegate's Gemini agents run on Google ADK (it brings google-genai and a newer FastAPI; tested together).
RUN pip install "google-adk==2.12.0"
# Bake the Tier 1 classifier into the image so a cold start never runs without it.
ENV HF_HOME=/opt/models/hf
RUN python -c "from intentseal_core.classifier import LocalClassifier; LocalClassifier()"


FROM python:3.11-slim AS runtime
# OpenCV runtime libraries for the OCR extractor; DejaVu for Unicode PDF rendering in the demo fixtures.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 intentseal \
 && mkdir -p /data && chown intentseal:intentseal /data
COPY --from=build /opt/venv /opt/venv
COPY --from=build --chown=intentseal:intentseal /opt/models /opt/models
WORKDIR /app
COPY config ./config
COPY policies ./policies
COPY eval/reports/*.json ./eval/reports/
COPY --from=console /console/dist ./console
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/models/hf \
    HF_HUB_OFFLINE=1 \
    INTENTSEAL_HOME=/app \
    INTENTSEAL_DB_PATH=/data/intentseal.db \
    INTENTSEAL_POLICY_DIR=/data/policies \
    INTENTSEAL_STORE_PREVIEWS=0 \
    INTENTSEAL_CONSOLE_DIR=/app/console \
    PORT=8000
USER intentseal
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/readyz' % os.environ.get('PORT', '8000'), timeout=4)"
CMD ["python", "-m", "intentseal_gateway"]
