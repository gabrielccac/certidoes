FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    XDG_SESSION_TYPE=x11 \
    HOME=/home/certidoes \
    PYTHONPATH=/app/src \
    CHROME_PATH=/usr/bin/google-chrome

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb xauth x11-utils ca-certificates wget gnupg \
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
COPY src ./src
COPY solved.png ./solved.png
RUN pip install --no-cache-dir . curl_cffi playwright

RUN useradd --create-home --home-dir /home/certidoes --shell /bin/bash certidoes \
    && mkdir -p /app/experiments/profile /app/runs \
    && chown -R certidoes:certidoes /app /home/certidoes

COPY docker/cdp/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER certidoes
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "certidoes.sources.trf1", "--help"]
