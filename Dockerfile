FROM python:3.12-slim AS whisper-builder

ARG WHISPER_CPP_VERSION=1.5.5

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates git \
    && git clone --depth 1 --branch "v${WHISPER_CPP_VERSION}" https://github.com/ggerganov/whisper.cpp.git /src/whisper.cpp \
    && make -C /src/whisper.cpp main \
    && rm -rf /var/lib/apt/lists/*

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get -o Acquire::Retries=10 -o Acquire::http::Timeout=60 update \
    && apt-get -o Acquire::Retries=10 -o Acquire::http::Timeout=60 install -y --fix-missing --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY models ./models
COPY --from=whisper-builder /src/whisper.cpp/main /usr/local/bin/whisper-main

CMD ["python", "-m", "app.main", "scheduler"]
