import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.api import api_router
from app.core.config import settings
from app.services.economic_service import update_economic_data_in_background
from app.utils.scheduler import (
    start_scheduler, stop_scheduler,
    start_sell_scheduler, stop_sell_scheduler,
    start_economic_data_scheduler, stop_economic_data_scheduler,
    start_daily_report_scheduler,
)
from app.telegram_bot.bot import start_bot, stop_bot

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _startup()
    yield
    stop_scheduler()
    stop_sell_scheduler()
    stop_economic_data_scheduler()
    stop_bot()


app = FastAPI(title="주식 분석 및 추천 API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


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
    start_bot()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)