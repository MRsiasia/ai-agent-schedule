FROM python:3.11-slim

WORKDIR /app

# 避免 Python 緩衝輸出，讓終端機即時看到 log
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir langgraph-checkpoint-sqlite

COPY src/ ./src/
COPY tests/ ./tests/
COPY data/ ./data/

RUN mkdir -p /app/storage

CMD ["python", "-m", "src.cli"]
