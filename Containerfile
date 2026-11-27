FROM docker.io/library/python:3.11-slim@sha256:61e01f72680f86a5279ad510818ab7b2ee9f049822589d4e34e33e044360356c

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.lock.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.lock.txt

COPY src/ ./src/
COPY tests/ ./tests/
COPY data/ ./data/
COPY evaluation/ ./evaluation/
RUN mkdir -p /app/storage

CMD ["python", "-m", "src.cli"]
