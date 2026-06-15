FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    ca-certificates git curl wget build-essential libssl-dev zlib1g-dev \
    libbz2-dev libreadline-dev libsqlite3-dev libffi-dev \
    liblzma-dev libsndfile1 libgomp1 cmake unzip \
    && rm -rf /var/lib/apt/lists/*

# Python 3.8.10 via pyenv
ENV PYENV_ROOT=/opt/pyenv
RUN git clone https://github.com/pyenv/pyenv.git $PYENV_ROOT && \
    $PYENV_ROOT/bin/pyenv install 3.8.10 && \
    $PYENV_ROOT/bin/pyenv global 3.8.10
ENV PATH="$PYENV_ROOT/versions/3.8.10/bin:$PATH"

# coeiroink engine (VOICEVOX engine fork)
ARG COEIROINK_ENGINE_REF=c-1.6.0+v-0.12.3
RUN git clone --depth 1 -b "$COEIROINK_ENGINE_REF" \
    https://github.com/shirowanisan/voicevox_engine.git /opt/coeiroink_engine
WORKDIR /opt/coeiroink_engine

RUN python -m pip install --no-cache-dir --upgrade "pip<25" "setuptools<70" wheel && \
    python -m pip install --no-cache-dir Cython==0.29.24 numpy==1.19.5 && \
    grep '^pyopenjtalk @' requirements.txt > /tmp/requirements-pyopenjtalk.txt && \
    grep -v '^pyopenjtalk @' requirements.txt > /tmp/requirements-engine.txt && \
    python -m pip install --no-cache-dir --no-build-isolation --no-deps \
        -r /tmp/requirements-pyopenjtalk.txt && \
    python -m pip install --no-cache-dir -r /tmp/requirements-engine.txt && \
    sed -i \
        -e 's#git+https://git@github.com/#git+https://github.com/#' \
        -e 's#@espnet-1\.10\.3#@espnet-0.10.3#' \
        requirements-coeiroink-no-deps.txt && \
    python -m pip install --no-cache-dir --no-deps \
        -r requirements-coeiroink-no-deps.txt && \
    python -m pip install --no-cache-dir -r requirements-coeiroink.txt

RUN python -m pip install --no-cache-dir \
    numpy==1.22.4 \
    numba==0.56.4 \
    llvmlite==0.39.1

ARG COEIROINK_SPEAKER_SOURCE=https://coeiroink.com/download
ARG COEIROINK_SPEAKER_PREFIXES=
ENV COEIROINK_SPEAKER_SOURCE=${COEIROINK_SPEAKER_SOURCE}
ENV COEIROINK_SPEAKER_PREFIXES=${COEIROINK_SPEAKER_PREFIXES}
ENV COEIROINK_SPEAKER_INFO_DIR=/opt/coeiroink_engine/speaker_info

COPY install_speakers.py /usr/local/bin/coeiroink_install_speakers.py
COPY manifest.py /usr/local/bin/coeiroink_manifest.py
COPY ensure_openjtalk_dict.py /usr/local/bin/coeiroink_ensure_openjtalk_dict.py
COPY entrypoint.sh /usr/local/bin/coeiroink-entrypoint.sh

RUN mkdir -p /opt/coeiroink_engine/speaker_info && \
    chmod +x \
      /usr/local/bin/coeiroink_install_speakers.py \
      /usr/local/bin/coeiroink_manifest.py \
      /usr/local/bin/coeiroink_ensure_openjtalk_dict.py \
      /usr/local/bin/coeiroink-entrypoint.sh

EXPOSE 50031

HEALTHCHECK --interval=30s --timeout=5s --start-period=3600s --retries=5 \
    CMD curl -fsS http://localhost:50031/speakers >/dev/null || exit 1

ENTRYPOINT ["coeiroink-entrypoint.sh"]
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "50031", "--voicelib_dir", "/opt/coeiroink_engine"]
