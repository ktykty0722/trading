import logging
import pandas as pd
from datetime import datetime, timedelta

import pytz
from app.db.supabase import supabase
from stock import collect_economic_data, collect_stock_prices

logger = logging.getLogger(__name__)

STOCK_PRICE_BACKFILL_DAYS = 365
STOCK_PRICE_MIN_HISTORY_DAYS = 180


def _last_weekday(dt: datetime) -> datetime:
    """주말이면 직전 금요일로 조정"""
    while dt.weekday() >= 5:
        dt -= timedelta(days=1)
    return dt


def get_last_updated_date() -> str:
    """
    economic_indicators 테이블에서 마지막 수집 날짜 다음 날을 반환.
    데이터가 없으면 '2006-01-01' 반환.
    """
    try:
        resp = supabase.table("economic_indicators").select("date").order("date", desc=True).limit(1).execute()
        if resp.data:
            last_date = datetime.fromisoformat(resp.data[0]["date"])
            next_date = last_date + timedelta(days=1)
            logger.info(f"마지막 수집 날짜: {last_date.date()}, 다음 수집 시작일: {next_date.date()}")
            return next_date.strftime('%Y-%m-%d')
        logger.info("기존 데이터 없음. 기본 시작 날짜(2006-01-01)로 설정")
        return "2006-01-01"
    except Exception as e:
        logger.error(f"마지막 수집 날짜 조회 오류: {e}")
        return "2006-01-01"


def _get_active_tickers() -> list:
    """stock_universe에서 is_active=true인 종목 목록 반환"""
    try:
        resp = supabase.table("stock_universe").select("ticker, name_ko").eq("is_active", True).execute()
        return [(r["ticker"], r["name_ko"]) for r in resp.data] if resp.data else []
    except Exception as e:
        logger.error(f"종목 목록 조회 오류: {e}")
        return []


def get_stock_price_start_date(tickers: list[tuple[str, str]]) -> str:
    """
    stock_daily_prices는 경제지표와 독립적으로 백필한다.
    최근 1년 범위에서 활성 종목 중 하나라도 히스토리가 부족하면 1년치를 다시 수집한다.
    """
    fallback_start = (datetime.now() - timedelta(days=STOCK_PRICE_BACKFILL_DAYS)).strftime("%Y-%m-%d")
    ticker_symbols = [ticker for ticker, _ in tickers]
    if not ticker_symbols:
        return fallback_start

    try:
        resp = (
            supabase.table("stock_daily_prices")
            .select("date,ticker")
            .in_("ticker", ticker_symbols)
            .gte("date", fallback_start)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            logger.info(f"기존 주가 데이터 없음. 주가 백필 시작일: {fallback_start}")
            return fallback_start

        dates_by_ticker = {ticker: set() for ticker in ticker_symbols}
        latest_date = None
        for row in rows:
            ticker = row.get("ticker")
            date = row.get("date")
            if ticker in dates_by_ticker and date:
                dates_by_ticker[ticker].add(date)
                if latest_date is None or date > latest_date:
                    latest_date = date

        min_history = min((len(dates) for dates in dates_by_ticker.values()), default=0)
        if min_history < STOCK_PRICE_MIN_HISTORY_DAYS:
            logger.info(
                f"주가 히스토리 부족(min={min_history}, required={STOCK_PRICE_MIN_HISTORY_DAYS}). "
                f"주가 백필 시작일: {fallback_start}"
            )
            return fallback_start

        next_date = datetime.fromisoformat(latest_date) + timedelta(days=1)
        logger.info(f"마지막 주가 수집 날짜: {latest_date}, 다음 주가 수집 시작일: {next_date.date()}")
        return next_date.strftime("%Y-%m-%d")
    except Exception as e:
        logger.error(f"마지막 주가 수집 날짜 조회 오류: {e}")
        return fallback_start


def _upsert_economic_indicators(df: pd.DataFrame, storage_end: str):
    """
    경제지표 DataFrame을 economic_indicators 테이블에 upsert.
    storage_end(어제) 이전 날짜만 저장.
    """
    saved = 0
    for date_idx, row in df.iterrows():
        date_str = date_idx.strftime('%Y-%m-%d') if hasattr(date_idx, 'strftime') else str(date_idx)
        if date_str > storage_end:
            continue

        record = {"date": date_str}
        for col, val in row.items():
            if not pd.isna(val):
                record[col] = float(val)

        try:
            existing = supabase.table("economic_indicators").select("id").eq("date", date_str).execute()
            if existing.data:
                # NULL인 컬럼만 업데이트 (기존 값 보존)
                existing_row = supabase.table("economic_indicators").select("*").eq("date", date_str).execute().data[0]
                update = {k: v for k, v in record.items() if k != "date" and existing_row.get(k) is None}
                if update:
                    supabase.table("economic_indicators").update(update).eq("date", date_str).execute()
            else:
                supabase.table("economic_indicators").insert(record).execute()
            saved += 1
        except Exception as e:
            logger.error(f"economic_indicators upsert 오류 ({date_str}): {e}")

    return saved


def _upsert_stock_prices(price_df: pd.DataFrame, storage_end: str):
    """
    종목 주가 DataFrame을 stock_daily_prices 테이블에 upsert.
    """
    if price_df.empty:
        return 0

    rows_to_upsert = []
    for _, row in price_df.iterrows():
        date_str = str(row['date'])
        if date_str > storage_end:
            continue
        payload = {
            "date":   date_str,
            "ticker": row['ticker'],
            "close":  row['close'],
        }
        # 신 스키마: OHLCV 함께 저장. 구 결과(close만)와도 호환되도록 안전 추출.
        for col in ("open", "high", "low", "volume"):
            if col in row and row[col] is not None and not pd.isna(row[col]):
                payload[col] = float(row[col]) if col != "volume" else int(row[col])
        rows_to_upsert.append(payload)

    if not rows_to_upsert:
        return 0

    # Supabase upsert (on_conflict: date,ticker)
    try:
        supabase.table("stock_daily_prices").upsert(rows_to_upsert, on_conflict="date,ticker").execute()
        return len(rows_to_upsert)
    except Exception as e:
        logger.error(f"stock_daily_prices upsert 오류: {e}")
        return 0


async def update_economic_data_in_background(force: bool = False):
    """
    경제지표 + 종목 주가 데이터를 수집하여 Supabase에 저장.
    미국 장 시간(22:30~06:00 KST)에는 force=True 없이 실행하면 건너뜀.
    """
    try:
        logger.info("경제 데이터 업데이트 시작...")

        # 미국 장 시간 체크 (KST 기준 22:30~06:00)
        now = datetime.now()
        h, m = now.hour, now.minute
        is_market_hours = (h > 22 or (h == 22 and m >= 30)) or (h < 6)

        if is_market_hours and not force:
            logger.info(f"현재 시간({h:02d}:{m:02d} KST)은 미국 장 시간. 장 마감 후 수집합니다.")
            return {"success": True, "message": "장 시간 중 건너뜀", "total_records": 0, "updated_records": 0}

        if is_market_hours and force:
            logger.info(f"장 중이지만 강제 수집 모드로 실행합니다.")

        econ_start_date = get_last_updated_date()
        today         = datetime.now().strftime('%Y-%m-%d')
        yesterday     = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

        # 경제지표 수집
        econ_saved = 0
        if econ_start_date > yesterday:
            logger.info(f"경제지표 수집 시작일({econ_start_date}) >= 어제({yesterday}). 경제지표는 최신 상태")
        else:
            econ_df = collect_economic_data(start_date=econ_start_date, end_date=today)
            if econ_df is not None and not econ_df.empty:
                econ_saved = _upsert_economic_indicators(econ_df, storage_end=yesterday)
                logger.info(f"경제지표 {econ_saved}건 저장 완료")
            else:
                logger.warning("경제지표 수집 데이터 없음")

        # 종목 주가 수집
        tickers = _get_active_tickers()
        stock_saved = 0
        if tickers:
            stock_start_date = get_stock_price_start_date(tickers)
            if stock_start_date > yesterday:
                logger.info(f"주가 수집 시작일({stock_start_date}) >= 어제({yesterday}). 주가 데이터는 최신 상태")
            else:
                price_df = collect_stock_prices(start_date=stock_start_date, end_date=today, tickers=tickers)
                stock_saved = _upsert_stock_prices(price_df, storage_end=yesterday)
            logger.info(f"주가 {stock_saved}건 저장 완료")
        else:
            logger.warning("stock_universe에 활성 종목 없음")

        total = econ_saved + stock_saved
        return {
            "success": True,
            "message": "경제 데이터 업데이트 완료",
            "total_records": total,
            "updated_records": total,
        }

    except Exception as e:
        logger.exception(f"경제 데이터 업데이트 오류: {e}")
        raise
