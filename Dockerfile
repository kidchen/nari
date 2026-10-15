FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Los_Angeles
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies first so code edits don't invalidate this layer.
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app

# uid 1000 = opc on the OCI VM; /app/data is the nari-data volume.
RUN useradd --create-home --uid 1000 nari && mkdir -p /app/data /app/backups && chown nari /app/data /app/backups
USER nari

# The bot refreshes this file every 30s once Telegram is connected and the scheduler runs.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/nari-heartbeat')<120 else 1)"

# One process only (see docker-compose.yml).
CMD ["python", "-m", "app.main"]
