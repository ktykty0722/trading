#!/bin/bash
# ML 모델 주간 재학습 스크립트
# VPS cron에 등록: 0 18 * * 0 /path/to/trading/cron/ml-retrain.sh
# (매주 일요일 18:00 UTC = 03:00 KST 월요일)

set -e
COMPOSE_FILE="$(dirname "$0")/../docker-compose.yml"

echo "[$(date)] ML 재학습 시작..."
docker compose -f "$COMPOSE_FILE" --profile ml run --rm ml-trainer

if [ $? -eq 0 ]; then
    echo "[$(date)] ML 재학습 완료"
else
    echo "[$(date)] ML 재학습 실패" >&2
    exit 1
fi
