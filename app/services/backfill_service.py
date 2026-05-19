"""
종목 추가(/add) 후속 데이터 백필 서비스.

흐름:
1. /add → stock_universe upsert + ticker_backfill_jobs(pending) insert
2. backfill worker가 N분마다 pending 큐를 폴링:
   - stock_daily_prices 가격 백필 (Yahoo Finance, 최근 365일)
   - stock_signals 단일 ticker 생성
   - ticker_sentiment 단일 ticker 생성
   - (옵션) ML 즉시 재학습 — 기본 OFF
3. Telegram으로 완료/실패 알림
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from app.db.supabase import supabase
from app.telegram_bot.notifier import (
    notify_backfill_done,
    notify_backfill_failed,
)
from stock import collect_stock_prices

logger = logging.getLogger(__name__)


# ============================================================
# system_config helpers
# ============================================================
def _get_config_str(key: str, default: str) -> str:
    try:
        resp = supabase.table("system_config").select("value").eq("key", key).limit(1).execute()
        if resp.data:
            return str(resp.data[0]["value"])
    except Exception:
        pass
    return default


def _get_config_bool(key: str, default: bool) -> bool:
    return _get_config_str(key, "true" if default else "false").strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_config_int(key: str, default: int) -> int:
    try:
        return int(float(_get_config_str(key, str(default))))
    except Exception:
        return default


# ============================================================
# Job 큐
# ============================================================
def enqueue_backfill_job(ticker: str, exchange: str) -> dict:
    """ticker_backfill_jobs 에 pending 행을 추가."""
    ticker = ticker.upper().strip()
    exchange = (exchange or "NASD").upper().strip()
    try:
        existing = (
            supabase.table("ticker_backfill_jobs")
            .select("id, status")
            .eq("ticker", ticker)
            .in_("status", ["pending", "running"])
            .limit(1)
            .execute()
            .data
        )
        if existing:
            return {"success": True, "queued": False, "message": "이미 큐에 존재", "id": existing[0]["id"]}

        resp = supabase.table("ticker_backfill_jobs").insert({
            "ticker": ticker,
            "exchange": exchange,
            "status": "pending",
            "metadata": {"requested_via": "telegram_add"},
        }).execute()
        new_id = resp.data[0]["id"] if resp.data else None
        return {"success": True, "queued": True, "message": "백필 job 예약 완료", "id": new_id}
    except Exception as e:
        logger.exception(f"enqueue_backfill_job 실패 ({ticker}): {e}")
        return {"success": False, "queued": False, "message": f"큐 예약 실패: {e}"}


def _set_job_status(job_id: int, status: str, message: str = "", metadata: Optional[dict] = None) -> None:
    try:
        update = {
            "status": status,
            "message": message[:1000] if message else None,
        }
        if status == "running":
            update["started_at"] = datetime.now().isoformat()
        if status in ("done", "failed", "partial"):
            update["finished_at"] = datetime.now().isoformat()
        if metadata:
            update["metadata"] = metadata
        supabase.table("ticker_backfill_jobs").update(update).eq("id", job_id).execute()
    except Exception as e:
        logger.warning(f"backfill_job {job_id} status 갱신 실패: {e}")


# ============================================================
# 가격 백필 (Yahoo Finance)
# ============================================================
def backfill_stock_prices_for_ticker(ticker: str, name_ko: str, days: int = 365) -> dict:
    """
    Yahoo Finance에서 최근 N일 가격을 가져와 stock_daily_prices에 upsert.

    Returns:
        {"success", "rows_saved", "message"}
    """
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        price_df = collect_stock_prices(start_date=start_date, end_date=end_date, tickers=[(ticker, name_ko)])
        if price_df is None or price_df.empty:
            return {"success": False, "rows_saved": 0, "message": "Yahoo 데이터 없음"}

        rows = []
        for _, row in price_df.iterrows():
            payload = {
                "date": str(row["date"]),
                "ticker": row["ticker"],
                "close": float(row["close"]),
            }
            for col in ("open", "high", "low", "volume"):
                v = row.get(col) if col in row else None
                if v is not None and not pd.isna(v):
                    payload[col] = float(v) if col != "volume" else int(v)
            rows.append(payload)
        supabase.table("stock_daily_prices").upsert(rows, on_conflict="date,ticker").execute()
        return {"success": True, "rows_saved": len(rows), "message": f"{len(rows)}건 저장"}
    except Exception as e:
        logger.exception(f"backfill_stock_prices_for_ticker 실패 ({ticker}): {e}")
        return {"success": False, "rows_saved": 0, "message": f"가격 수집 실패: {e}"}


# ============================================================
# 단일 ticker 백필 파이프라인
# ============================================================
def run_backfill_job(job: dict) -> dict:
    """
    한 건의 백필 job을 처리. 단계별 결과를 metadata에 누적 기록.

    job: {"id", "ticker", "exchange", ...}
    """
    job_id = job["id"]
    ticker = job["ticker"]
    exchange = job.get("exchange", "NASD")

    # 종목 이름 조회
    name_ko = ticker
    try:
        row = (
            supabase.table("stock_universe").select("name_ko").eq("ticker", ticker).limit(1)
            .execute().data
        )
        if row and row[0].get("name_ko"):
            name_ko = row[0]["name_ko"]
    except Exception:
        pass

    _set_job_status(job_id, "running", "백필 시작")
    meta = {"ticker": ticker, "exchange": exchange, "steps": {}}

    # ── 1. 가격 백필 ────────────────────────────────────────
    days = _get_config_int("ticker_backfill_days", 365)
    price_result = backfill_stock_prices_for_ticker(ticker, name_ko, days=days)
    meta["steps"]["prices"] = price_result
    if not price_result["success"]:
        _set_job_status(job_id, "failed", f"가격 백필 실패: {price_result['message']}", metadata=meta)
        notify_backfill_failed(ticker, "stock_daily_prices", price_result["message"])
        return {"success": False, "ticker": ticker, "meta": meta}

    # ── 2. 기술지표 생성 (단일 ticker) ──────────────────────
    signals_result = {"success": False, "message": "skipped"}
    try:
        from app.services.stock_recommendation_service import StockRecommendationService
        svc = StockRecommendationService()
        gen = svc.generate_technical_recommendations(tickers=[ticker])
        signals_result = {
            "success": bool(gen.get("data")),
            "message": gen.get("message", ""),
            "rows": len(gen.get("data", [])),
        }
    except Exception as e:
        logger.warning(f"{ticker} 기술지표 생성 실패: {e}")
        signals_result = {"success": False, "message": f"기술지표 실패: {e}"}
    meta["steps"]["signals"] = signals_result

    # ── 3. 감성 분석 (단일 ticker) ──────────────────────────
    sentiment_result = {"success": False, "message": "skipped"}
    try:
        from app.services.stock_recommendation_service import StockRecommendationService
        svc = StockRecommendationService()
        sent = svc.fetch_and_store_sentiment_for_tickers([ticker])
        first = (sent.get("results") or [{}])[0]
        sentiment_result = {
            "success": True,
            "article_count": first.get("article_count", 0),
            "sentiment_score": first.get("sentiment_score"),
            "message": first.get("message", "ok"),
        }
    except Exception as e:
        logger.warning(f"{ticker} 감성 분석 실패: {e}")
        sentiment_result = {"success": False, "message": f"감성 실패: {e}"}
    meta["steps"]["sentiment"] = sentiment_result

    # ── 4. ML 재학습 (옵션, 기본 OFF) ───────────────────────
    ml_result = {"success": False, "message": "다음 ML job에서 포함 예정"}
    if _get_config_bool("auto_run_ml_after_add", False):
        try:
            from app.services.ml_prediction_service import run_ml_prediction
            ml_result = run_ml_prediction()
        except Exception as e:
            logger.warning(f"{ticker} 즉시 ML 학습 실패: {e}")
            ml_result = {"success": False, "message": f"ML 실패: {e}"}
    meta["steps"]["ml"] = ml_result

    # ── 결과 결정 ──────────────────────────────────────────
    all_ok = price_result["success"] and signals_result.get("success", False)
    status = "done" if all_ok else "partial"
    _set_job_status(job_id, status, "백필 완료" if all_ok else "부분 완료", metadata=meta)

    notify_backfill_done(ticker, {
        "prices": price_result.get("rows_saved", 0),
        "signals": signals_result.get("success", False),
        "sentiment": sentiment_result,
        "ml_after_add": _get_config_bool("auto_run_ml_after_add", False),
    })

    return {"success": all_ok, "ticker": ticker, "meta": meta}


def process_pending_backfill_jobs(max_jobs_per_run: int = 3) -> dict:
    """대기열에서 pending job을 최대 N개 처리. 스케줄러가 N분마다 호출."""
    if not _get_config_bool("ticker_backfill_enabled", True):
        return {"processed": 0, "message": "ticker_backfill_enabled=false"}

    try:
        jobs = (
            supabase.table("ticker_backfill_jobs")
            .select("*")
            .eq("status", "pending")
            .order("requested_at")
            .limit(max_jobs_per_run)
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.error(f"backfill job 조회 실패: {e}")
        return {"processed": 0, "message": f"조회 실패: {e}"}

    if not jobs:
        return {"processed": 0, "message": "pending 없음"}

    processed = 0
    for job in jobs:
        try:
            run_backfill_job(job)
            processed += 1
            time.sleep(2)  # job 간 안전 간격
        except Exception as e:
            logger.exception(f"backfill job {job.get('id')} 처리 실패: {e}")
            _set_job_status(job["id"], "failed", str(e))
            try:
                notify_backfill_failed(job.get("ticker", "?"), "worker", str(e))
            except Exception:
                pass

    return {"processed": processed, "message": f"{processed}건 처리 완료"}


def rebackfill_volumes_for_all(days: int = 365) -> dict:
    """
    모든 active 종목의 stock_daily_prices를 Yahoo Finance로 다시 수집해
    open/high/low/close/volume을 채운다. 기존 close-only 행은 upsert로 보강됨.

    유동성 필터(check_liquidity_filter)가 volume IS NULL이면 모든 매수 후보를
    차단했던 문제를 해결하기 위한 일회성/필요시 수동 보정 함수.
    """
    try:
        rows = (
            supabase.table("stock_universe")
            .select("ticker, name_ko, is_active")
            .eq("is_active", True)
            .execute()
            .data or []
        )
    except Exception as e:
        return {"success": False, "message": f"universe 조회 실패: {e}"}

    tickers = [(r["ticker"], r.get("name_ko") or r["ticker"]) for r in rows]
    if not tickers:
        return {"success": False, "message": "활성 종목 없음"}

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    logger.info(f"rebackfill_volumes: {len(tickers)}개 종목 / {start_date}~{end_date}")

    saved_total = 0
    failed: list[str] = []
    try:
        from stock import collect_stock_prices
        price_df = collect_stock_prices(start_date=start_date, end_date=end_date, tickers=tickers)
    except Exception as e:
        return {"success": False, "message": f"collect_stock_prices 실패: {e}"}

    if price_df is None or price_df.empty:
        return {"success": False, "message": "Yahoo 데이터 없음"}

    # ticker 단위로 묶어 upsert (KIS rate limit 없음 — supabase만 사용)
    for ticker, _name in tickers:
        sub = price_df[price_df["ticker"] == ticker]
        if sub.empty:
            failed.append(ticker)
            continue
        payload_rows = []
        for _, row in sub.iterrows():
            payload = {
                "date": str(row["date"]),
                "ticker": ticker,
                "close": float(row["close"]),
            }
            for col in ("open", "high", "low", "volume"):
                v = row.get(col) if col in row else None
                if v is not None and not pd.isna(v):
                    payload[col] = float(v) if col != "volume" else int(v)
            payload_rows.append(payload)
        if not payload_rows:
            continue
        try:
            supabase.table("stock_daily_prices").upsert(
                payload_rows, on_conflict="date,ticker"
            ).execute()
            saved_total += len(payload_rows)
        except Exception as e:
            logger.warning(f"rebackfill {ticker} upsert 실패: {e}")
            failed.append(ticker)

    return {
        "success": True,
        "tickers": len(tickers),
        "rows_saved": saved_total,
        "failed": failed,
        "message": f"{len(tickers)}종목 / {saved_total}건 저장, 실패 {len(failed)}건",
    }


__all__ = [
    "enqueue_backfill_job",
    "run_backfill_job",
    "process_pending_backfill_jobs",
    "backfill_stock_prices_for_ticker",
    "rebackfill_volumes_for_all",
]
