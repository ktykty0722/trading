FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir tensorflow

COPY . .

# VPS cron에서 호출: docker compose --profile ml run --rm ml-trainer
CMD ["python", "predict.py"]
