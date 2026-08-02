FROM python:3.8-slim-bullseye

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    SV_CPU_NUM_THREADS=4 \
    VV_OUTPUT_LOG_UTF8=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    cmake \
    curl \
    g++ \
    gcc \
    git \
    libgomp1 \
    libsndfile1 \
    make \
    tar \
    unzip \
    wget \
    && rm -rf /var/lib/apt/lists/*

ARG SHAREVOX_ENGINE_REF=0.2.1
ARG SHAREVOX_ENGINE_VERSION=0.2.1
ARG SHAREVOX_RESOURCE_VERSION=0.2.1
ARG SHAREVOX_CORE_VERSION=0.2.1
ARG SHAREVOX_MODEL_VERSION=0.2.1
ARG ONNXRUNTIME_VERSION=1.12.1
ENV SHAREVOX_ENGINE_REF=${SHAREVOX_ENGINE_REF}
RUN git clone --depth 1 --branch "${SHAREVOX_ENGINE_REF}" \
    https://github.com/SHAREVOX/sharevox_engine.git /opt/sharevox_engine

WORKDIR /opt/sharevox_engine

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -r requirements.txt

RUN sed -i "s/__version__ = \"latest\"/__version__ = \"${SHAREVOX_ENGINE_VERSION}\"/" \
      /opt/sharevox_engine/voicevox_engine/__init__.py

ENV SHAREVOX_ENGINE_VERSION=${SHAREVOX_ENGINE_VERSION}
ENV SHAREVOX_RESOURCE_VERSION=${SHAREVOX_RESOURCE_VERSION}
ENV SHAREVOX_CORE_VERSION=${SHAREVOX_CORE_VERSION}
ENV SHAREVOX_MODEL_VERSION=${SHAREVOX_MODEL_VERSION}
ENV ONNXRUNTIME_VERSION=${ONNXRUNTIME_VERSION}
ENV SHAREVOX_CACHE_ROOT=/opt/sharevox_cache
ENV LD_LIBRARY_PATH=/opt/sharevox_core:/opt/onnxruntime/lib

COPY install_assets.py /usr/local/bin/sharevox_install_assets.py
COPY entrypoint.sh /usr/local/bin/sharevox-entrypoint.sh

RUN mkdir -p /opt/sharevox_cache && \
    echo "/opt/sharevox_core" > /etc/ld.so.conf.d/sharevox_core.conf && \
    echo "/opt/onnxruntime/lib" > /etc/ld.so.conf.d/onnxruntime.conf && \
    chmod +x \
      /usr/local/bin/sharevox_install_assets.py \
      /usr/local/bin/sharevox-entrypoint.sh

EXPOSE 50025

HEALTHCHECK --interval=30s --timeout=5s --start-period=1800s --retries=5 \
    CMD curl -fsS http://localhost:50025/speakers >/dev/null || exit 1

ENTRYPOINT ["sharevox-entrypoint.sh"]
CMD ["python", "run.py", "--voicelib_dir", "/opt/sharevox_core/", "--runtime_dir", "/opt/onnxruntime/lib", "--host", "0.0.0.0", "--port", "50025"]
