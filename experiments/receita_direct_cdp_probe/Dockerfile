FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates libnss3 libcups2t64 libpulse0 libxkbcommon0 libxkbfile1 libx11-6 libxcb1 \
    libxext6 libxfixes3 libglib2.0-0t64 libdbus-1-3 libgbm1 libdrm2 libgl1 libegl1 \
    libasound2t64 libstdc++6 libgcc-s1 libuuid1 libbsd0 libatomic1 libxrender1 \
    libxcursor1 libxi6 libxtst6 libsm6 libice6 libfontconfig1 libfreetype6 libtinfo6 \
    libvulkan1 procps \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/receita-android-home \
    && chmod 1777 /tmp/receita-android-home
ENV HOME=/tmp/receita-android-home

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv python install 3.12.15 && uv venv --python 3.12.15 /opt/direct && uv pip install --python /opt/direct/bin/python --no-progress websocket-client==1.9.2 mycdp==1.4.0 requests==2.34.2 curl_cffi==0.17.0 cryptography==50.0.2 pypdf==6.21.3 && chmod -R a+rX /opt/python /opt/direct
ENV PATH=/opt/direct/bin:/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENV ANDROID_HOME=/opt/android-sdk ANDROID_SDK_ROOT=/opt/android-sdk ANDROID_AVD_HOME=/work/avd PYTHONPATH=/app/src:/app PYTHONUNBUFFERED=1
WORKDIR /work
