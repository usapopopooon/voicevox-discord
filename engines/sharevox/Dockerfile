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
RUN git clone --depth 1 --branch "${SHAREVOX_ENGINE_REF}" \
    https://github.com/SHAREVOX/sharevox_engine.git /opt/sharevox_engine

WORKDIR /opt/sharevox_engine

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -r requirements.txt

RUN set -eux; \
    for i in 1 2 3 4 5; do \
      python -c "import pyopenjtalk; pyopenjtalk._lazy_init()" && break; \
      if [ "$i" = "5" ]; then exit 1; fi; \
      sleep 5; \
    done

RUN git clone --depth 1 --branch "${SHAREVOX_RESOURCE_VERSION}" \
      https://github.com/SHAREVOX/sharevox_resource.git /tmp/sharevox_resource \
    && rm -rf /opt/sharevox_engine/speaker_info \
    && cp -a /tmp/sharevox_resource/character_info /opt/sharevox_engine/speaker_info \
    && cp -a /tmp/sharevox_resource/engine/README.md /opt/sharevox_engine/README.md \
    && cp -a /tmp/sharevox_resource/engine/engine_manifest.json /opt/sharevox_engine/engine_manifest.json \
    && cp -a /tmp/sharevox_resource/engine/engine_manifest_assets/. /opt/sharevox_engine/engine_manifest_assets/ \
    && rm -rf /tmp/sharevox_resource

# SHAREVOX core
ARG SHAREVOX_CORE_VERSION=0.2.1
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      arm64) core_arch="arm64" ;; \
      *) core_arch="x64" ;; \
    esac; \
    core_asset="sharevox_core-linux-${core_arch}-cpu-${SHAREVOX_CORE_VERSION}"; \
    wget -q -O /tmp/sharevox_core.zip \
      "https://github.com/SHAREVOX/sharevox_core/releases/download/${SHAREVOX_CORE_VERSION}/${core_asset}.zip"; \
    unzip -q /tmp/sharevox_core.zip -d /tmp/sharevox_core; \
    mkdir -p /opt/sharevox_core; \
    cp -a "/tmp/sharevox_core/${core_asset}/." /opt/sharevox_core/; \
    rm -rf /tmp/sharevox_core /tmp/sharevox_core.zip; \
    echo "/opt/sharevox_core" > /etc/ld.so.conf.d/sharevox_core.conf; \
    ldconfig

# ONNX Runtime used by SHAREVOX core
ARG ONNXRUNTIME_VERSION=1.12.1
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      arm64) ort_arch="aarch64" ;; \
      *) ort_arch="x64" ;; \
    esac; \
    wget -q -O /tmp/onnxruntime.tgz \
      "https://github.com/microsoft/onnxruntime/releases/download/v${ONNXRUNTIME_VERSION}/onnxruntime-linux-${ort_arch}-${ONNXRUNTIME_VERSION}.tgz"; \
    mkdir -p /opt/onnxruntime; \
    tar xzf /tmp/onnxruntime.tgz -C /opt/onnxruntime --strip-components=1; \
    rm /tmp/onnxruntime.tgz; \
    echo "/opt/onnxruntime/lib" > /etc/ld.so.conf.d/onnxruntime.conf; \
    ldconfig

# Default SHAREVOX models. The engine copies this directory into its user data
# on first startup, so /speakers works without manual model installation.
ARG SHAREVOX_MODEL_VERSION=0.2.1
RUN set -eux; \
    model_asset="sharevox_model-${SHAREVOX_MODEL_VERSION}"; \
    wget -q -O /tmp/sharevox_model.zip \
      "https://github.com/SHAREVOX/sharevox_core/releases/download/${SHAREVOX_MODEL_VERSION}/${model_asset}.zip"; \
    unzip -q /tmp/sharevox_model.zip -d /tmp/sharevox_model; \
    libraries_json="$(find /tmp/sharevox_model -name libraries.json -type f | head -n 1)"; \
    test -n "${libraries_json}"; \
    model_root="$(dirname "${libraries_json}")"; \
    mkdir -p /opt/sharevox_engine/model; \
    cp -a "${model_root}/." /opt/sharevox_engine/model/; \
    test -f /opt/sharevox_engine/model/libraries.json; \
    rm -rf /tmp/sharevox_model /tmp/sharevox_model.zip

RUN sed -i "s/__version__ = \"latest\"/__version__ = \"${SHAREVOX_ENGINE_VERSION}\"/" \
      /opt/sharevox_engine/voicevox_engine/__init__.py \
    && sed -i "s/\"version\": \"999\\.999\\.999\"/\"version\": \"${SHAREVOX_ENGINE_VERSION}\"/" \
      /opt/sharevox_engine/engine_manifest.json

EXPOSE 50025

CMD ["python", "run.py", "--voicelib_dir", "/opt/sharevox_core/", "--runtime_dir", "/opt/onnxruntime/lib", "--host", "0.0.0.0", "--port", "50025"]
