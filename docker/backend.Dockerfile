FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 GCF_ROOT=/app

# FFmpeg (avec libass pour les sous-titres) + police Inter utilisée par les presets
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-inter fontconfig \
    && rm -rf /var/lib/apt/lists/* && fc-cache -f

WORKDIR /app
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/app backend/app
RUN pip install -e "backend[url,llm,postgres]"
COPY config config

WORKDIR /app/backend
EXPOSE 8000
CMD ["python", "-m", "app", "serve", "--host", "0.0.0.0", "--port", "8000"]
