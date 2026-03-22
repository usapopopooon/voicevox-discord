FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    git curl wget build-essential libssl-dev zlib1g-dev \
    libbz2-dev libreadline-dev libsqlite3-dev libffi-dev \
    liblzma-dev libsndfile1 cmake \
    && rm -rf /var/lib/apt/lists/*

# Python 3.11 via pyenv
ENV PYENV_ROOT=/opt/pyenv
RUN git clone https://github.com/pyenv/pyenv.git $PYENV_ROOT && \
    $PYENV_ROOT/bin/pyenv install 3.11.3 && \
    $PYENV_ROOT/bin/pyenv global 3.11.3
ENV PATH="$PYENV_ROOT/versions/3.11.3/bin:$PATH"

# sharevox_engine
RUN git clone https://github.com/SHAREVOX/sharevox_engine.git /opt/sharevox_engine
WORKDIR /opt/sharevox_engine

RUN pip install --no-cache-dir -r requirements.txt

# sharevox_core
ARG SHAREVOX_CORE_VERSION=0.2.0
ARG TARGETARCH
RUN if [ "$TARGETARCH" = "arm64" ]; then \
      ARCH=aarch64; \
    else \
      ARCH=x64; \
    fi && \
    wget -q "https://github.com/SHAREVOX/sharevox_core/releases/download/${SHAREVOX_CORE_VERSION}/sharevox_core-linux-${ARCH}-cpu-${SHAREVOX_CORE_VERSION}.zip" -O /tmp/core.zip && \
    mkdir -p /opt/sharevox_core && \
    cd /opt/sharevox_core && \
    python -c "import zipfile; zipfile.ZipFile('/tmp/core.zip').extractall('.')" && \
    rm /tmp/core.zip

# ONNX Runtime
ARG ONNXRUNTIME_VERSION=1.12.1
RUN if [ "$TARGETARCH" = "arm64" ]; then \
      ARCH=aarch64; \
    else \
      ARCH=x64; \
    fi && \
    wget -q "https://github.com/microsoft/onnxruntime/releases/download/v${ONNXRUNTIME_VERSION}/onnxruntime-linux-${ARCH}-${ONNXRUNTIME_VERSION}.tgz" -O /tmp/ort.tgz && \
    mkdir -p /opt/onnxruntime && \
    tar xzf /tmp/ort.tgz -C /opt/onnxruntime --strip-components=1 && \
    rm /tmp/ort.tgz

EXPOSE 50025

CMD ["python3", "run.py", "--voicelib_dir", "/opt/sharevox_core/", "--runtime_dir", "/opt/onnxruntime/lib", "--host", "0.0.0.0", "--port", "50025"]
