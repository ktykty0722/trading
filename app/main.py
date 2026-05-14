import logging
import secrets
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.api import api_router
from app.core.config import settings
from app.services.economic_service import update_economic_data_in_background
from app.utils.scheduler import (
    start_scheduler, stop_scheduler,
    start_sell_scheduler, stop_sell_scheduler,
    start_economic_data_scheduler, stop_economic_data_scheduler,
    start_daily_report_scheduler,
    start_mlops_scheduler, stop_mlops_scheduler,
    start_data_pipeline_scheduler, stop_data_pipeline_scheduler,
    start_backfill_worker_scheduler, stop_backfill_worker_scheduler,
    start_intraday_scheduler, stop_intraday_scheduler,
)
from app.telegram_bot.bot import start_bot, stop_bot

for noisy_logger in ("httpx", "httpcore"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _startup()
    yield
    stop_scheduler()
    stop_sell_scheduler()
    stop_economic_data_scheduler()
    stop_mlops_scheduler()
    stop_data_pipeline_scheduler()
    stop_backfill_worker_scheduler()
    stop_intraday_scheduler()
    stop_bot()


app = FastAPI(title="주식 분석 및 추천 API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


PUBLIC_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    expected_token = settings.API_AUTH_TOKEN
    if not expected_token:
        return JSONResponse(
            status_code=503,
            content={"detail": "API_AUTH_TOKEN is not configured; API access is disabled."},
        )

    auth_header = request.headers.get("authorization", "")
    bearer_token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
    api_key = request.headers.get("x-api-key", "")

    if not (
        secrets.compare_digest(bearer_token, expected_token)
        or secrets.compare_digest(api_key, expected_token)
    ):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    return await call_next(request)


@app.get("/")
def read_root():
    return {"message": "주식 분석 및 추천 API에 오신 것을 환영합니다"}


@app.get("/health")
def health():
    from app.utils.scheduler import get_scheduler_status
    status = get_scheduler_status()
    return {
        "status": "ok",
        "buy_scheduler":  status["buy_running"],
        "sell_scheduler": status["sell_running"],
        "telegram_bot":   bool(settings.TELEGRAM_BOT_TOKEN),
    }


async def _startup():
    logger.info("서비스 시작: 경제 데이터 초기 수집...")
    await update_economic_data_in_background()
    logger.info("경제 데이터 초기 수집 완료")

    start_scheduler()
    start_sell_scheduler()
    start_economic_data_scheduler()
    start_daily_report_scheduler()
    start_mlops_scheduler()
    start_data_pipeline_scheduler()
    start_backfill_worker_scheduler()
    start_intraday_scheduler()
    start_bot()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
