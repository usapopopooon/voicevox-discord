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

ARG COEIROINK_SPEAKER_UUID=3c37646f-3881-5374-2a83-149267990abc
ARG COEIROINK_META_ZIP_URL=https://www.dropbox.com/scl/fi/2xzh92hf0s4tk7ptltypl/tsukuyomichan-2.0.0.zip?rlkey=sxrzgr3gferkk45o5jzbyvrfy&st=u5d6t01l&dl=1
ARG COEIROINK_STYLE_ZIP_URLS=https://www.dropbox.com/scl/fi/gueh24t2iqwhcouf4y48s/0-1.0.0.zip?rlkey=l0wkgov5x0c2g79o73gmrvjk9&st=dxfgrmgf&dl=1

RUN set -eux; \
    speaker_dir="speaker_info/${COEIROINK_SPEAKER_UUID}"; \
    mkdir -p "$speaker_dir/model"; \
    curl -fsSL "$COEIROINK_META_ZIP_URL" -o /tmp/coeiroink-meta.zip; \
    unzip -q /tmp/coeiroink-meta.zip -d /tmp/coeiroink-meta; \
    meta_dir="$(find /tmp/coeiroink-meta -name metas.json -exec dirname {} \; | head -n 1)"; \
    cp -a "$meta_dir"/. "$speaker_dir"/; \
    for url in $COEIROINK_STYLE_ZIP_URLS; do \
        rm -rf /tmp/coeiroink-style; \
        mkdir -p /tmp/coeiroink-style; \
        curl -fsSL "$url" -o /tmp/coeiroink-style.zip; \
        unzip -q /tmp/coeiroink-style.zip -d /tmp/coeiroink-style; \
        for model_dir in /tmp/coeiroink-style/*; do \
            [ -f "$model_dir/config.yaml" ] || continue; \
            style_id="$(basename "$model_dir")"; \
            mkdir -p "$speaker_dir/model/$style_id"; \
            cp -a "$model_dir"/. "$speaker_dir/model/$style_id"/; \
        done; \
    done; \
    python -c 'import json, sys; from pathlib import Path; speaker_dir = Path(sys.argv[1]); meta_path = speaker_dir / "metas.json"; meta = json.loads(meta_path.read_text(encoding="utf-8")); installed = {p.name for p in (speaker_dir / "model").iterdir() if p.is_dir()}; meta["styles"] = [s for s in meta["styles"] if str(s["styleId"]) in installed]; assert meta["styles"], "No COEIROINK speaker styles were installed"; meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=4), encoding="utf-8")' "$speaker_dir"; \
    rm -rf /tmp/coeiroink-meta /tmp/coeiroink-meta.zip \
        /tmp/coeiroink-style /tmp/coeiroink-style.zip

EXPOSE 50031

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
    CMD curl -fsS http://localhost:50031/speakers >/dev/null || exit 1

CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "50031", "--voicelib_dir", "/opt/coeiroink_engine"]
