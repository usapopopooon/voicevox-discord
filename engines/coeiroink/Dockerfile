FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    git curl wget build-essential libssl-dev zlib1g-dev \
    libbz2-dev libreadline-dev libsqlite3-dev libffi-dev \
    liblzma-dev libsndfile1 cmake \
    && rm -rf /var/lib/apt/lists/*

# Python 3.8.10 via pyenv
ENV PYENV_ROOT=/opt/pyenv
RUN git clone https://github.com/pyenv/pyenv.git $PYENV_ROOT && \
    $PYENV_ROOT/bin/pyenv install 3.8.10 && \
    $PYENV_ROOT/bin/pyenv global 3.8.10
ENV PATH="$PYENV_ROOT/versions/3.8.10/bin:$PATH"

# coeiroink engine (VOICEVOX engine fork)
RUN git clone -b c-1.6.0+v-0.12.3 https://github.com/shirowanisan/voicevox_engine.git /opt/coeiroink_engine
WORKDIR /opt/coeiroink_engine

RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir espnet resampy typeguard==2.13.3 typing-extensions==4.5.0

EXPOSE 50031

CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "50031", "--voicelib_dir", "/opt/coeiroink_engine"]
