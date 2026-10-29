# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

# Self-contained ingest worker: API + headed Chrome under Xvfb.
# Receita uses the separate Redroid service; other sources keep headed Chrome.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    HOME=/home/ingest \
    DISPLAY=:99 \
    XDG_SESSION_TYPE=x11 \
    CHROME_PATH=/usr/bin/google-chrome

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        adb xvfb xauth x11-utils ca-certificates wget gnupg \
        fonts-liberation fonts-noto-color-emoji \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
        libcairo2 libatspi2.0-0 libxshmfence1 libglib2.0-0 libgtk-3-0 \
    && wget -q -O /tmp/google-chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/google-chrome.deb \
    && rm -f /tmp/google-chrome.deb \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix

COPY pyproject.toml .
# Keep the heavy dependency layer cached when application code changes.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print(*d['build-system']['requires'], *d['project']['dependencies'], sep='\n')" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY src ./src
COPY config ./config
COPY solved.png ./solved.png
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

RUN useradd --create-home --home-dir /home/ingest --shell /bin/bash ingest \
    && mkdir -p /app/runs/ingest /app/experiments/profile /tmp/.X11-unix \
    && chown -R ingest:ingest /app /home/ingest \
    && chmod 1777 /tmp/.X11-unix

COPY docker/ingest/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER ingest
EXPOSE 8090
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "certidoes.ingest.worker", "--config", "config/ingest/pessoas.toml", "--serve", "--port", "8090"]
