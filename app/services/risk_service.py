"""
리스크 관리 서비스.
system_config KV 테이블에서 파라미터를 동적으로 로드하여 매수 전 체크합니다.
코드 재배포 없이 Supabase에서 임계값을 조정할 수 있습니다.
"""
import logging
from datetime import datetime
from datetime import timedelta

import pandas as pd
import pytz
from app.db.supabase import supabase
from app.core.config import settings

logger = logging.getLogger(__name__)


def _get_config(key: str, default: float) -> float:
    """system_config 테이블에서 단일 값 조회. 실패 시 default 반환."""
    try:
        resp = supabase.table("system_config").select("value").eq("key", key).execute()
        if resp.data:
            return float(resp.data[0]["value"])
    except Exception as e:
        logger.warning(f"system_config 읽기 실패 ({key}): {e}")
    return default


# ============================================================
# 1. 일일 최대 손실 한도 체크
# ============================================================
def check_daily_loss_limit() -> tuple[bool, str]:
    """
    오늘 매도된 거래들의 손실 합계가 daily_max_loss_pct를 초과하면 매수 차단.

    Returns:
        (is_safe, description)
        is_safe=False → 매수 중단
    """
    threshold_pct = _get_config("daily_max_loss_pct", 3.0)
    today_et = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")

    try:
        trades = supabase.table("trade_records").select(
            "profit_loss, buy_price, quantity"
        ).eq("status", "sold").gte("sell_date", today_et).execute().data or []

        if not trades:
            return True, "오늘 매도 내역 없음"

        total_loss = sum(
            t["profit_loss"] for t in trades
            if t.get("profit_loss") and t["profit_loss"] < 0
        )
        total_invested = sum(
            t["buy_price"] * t["quantity"]
            for t in trades
            if t.get("buy_price") and t.get("quantity")
        ) or 1.0

        loss_pct = abs(total_loss / total_invested * 100)

        if loss_pct >= threshold_pct:
            reason = f"일일 손실 {loss_pct:.2f}% ≥ 한도 {threshold_pct:.1f}%"
            logger.warning(f"일일 최대 손실 한도 초과: {reason}")
            return False, reason

        return True, f"일일 손실 {loss_pct:.2f}% / 한도 {threshold_pct:.1f}%"

    except Exception as e:
        logger.error(f"일일 손실 한도 체크 오류: {e}")
        return False, f"일일 손실 한도 체크 오류 — 매수 차단: {e}"


# ============================================================
# 2. 최대 보유 종목 수 체크
# ============================================================
def check_max_positions(holding_tickers: set) -> tuple[bool, str]:
    """
    현재 보유/주문 중인 종목 수가 max_positions 이상이면 신규 매수 차단.

    Returns:
        (can_buy_more, description)
    """
    max_pos = int(_get_config("max_positions", 5))
    current = len(holding_tickers)

    if current >= max_pos:
        reason = f"보유 종목 {current}개 = 최대 {max_pos}개"
        logger.info(f"최대 보유 종목 수 도달: {reason}")
        return False, reason

    return True, f"보유 {current}/{max_pos}개"


def check_vix_halt_gate(vix_value: float | None) -> tuple[bool, str]:
    """
    VIX 임계치 초과 시 신규 매수 차단.
    """
    if vix_value is None:
        return False, "VIX 없음 — 신규 매수 차단"

    threshold = _get_config("vix_halt_threshold", settings.VIX_HALT_THRESHOLD)
    if float(vix_value) > threshold:
        reason = f"VIX {float(vix_value):.2f} > 임계치 {threshold:.2f}"
        logger.warning(f"변동성 게이트 차단: {reason}")
        return False, reason

    return True, f"VIX {float(vix_value):.2f} <= {threshold:.2f}"


def check_event_blackout(ticker: str, now_et: datetime | None = None) -> tuple[bool, str]:
    """
    실적/이벤트 블랙아웃 체크.
    - earnings_calendar 또는 stock_events 테이블이 존재하면 event_date ±2일 진입 차단
    """
    now_et = now_et or datetime.now(pytz.timezone(settings.MARKET_TIMEZONE))
    start_date = (now_et.date() - timedelta(days=2)).isoformat()
    end_date = (now_et.date() + timedelta(days=2)).isoformat()

    for table_name in ("earnings_calendar", "stock_events"):
        try:
            rows = (
                supabase.table(table_name)
                .select("ticker, event_date, event_type")
                .eq("ticker", ticker)
                .gte("event_date", start_date)
                .lte("event_date", end_date)
                .limit(1)
                .execute()
                .data
            )
            if rows:
                row = rows[0]
                event_type = row.get("event_type", "event")
                event_date = row.get("event_date")
                return False, f"{event_type} 블랙아웃({event_date})"
        except Exception:
            continue

    return True, "블랙아웃 이벤트 없음"


def check_liquidity_filter(ticker: str) -> tuple[bool, str]:
    """
    유동성 필터:
    - 20일 평균 거래대금(USD) >= 5M
    - 최신 종가 >= $5
    """
    min_adv20 = _get_config("min_adv20_usd", 5_000_000.0)
    min_price = _get_config("min_price_usd", 5.0)

    try:
        rows = (
            supabase.table("stock_daily_prices")
            .select("date, close, volume")
            .eq("ticker", ticker)
            .order("date", desc=True)
            .limit(20)
            .execute()
            .data
            or []
        )
        if len(rows) < 5:
            return False, "유동성 데이터 부족(<5일)"

        adv_values = []
        latest_close = None
        for row in rows:
            close = float(row.get("close") or 0)
            volume = float(row.get("volume") or 0)
            if latest_close is None:
                latest_close = close
            if close > 0 and volume > 0:
                adv_values.append(close * volume)

        if not adv_values or latest_close is None:
            return False, "유동성 계산 불가"

        adv20 = sum(adv_values) / len(adv_values)
        if latest_close < min_price:
            return False, f"최신가 ${latest_close:.2f} < 최소 ${min_price:.2f}"
        if adv20 < min_adv20:
            return False, f"ADV20 ${adv20:,.0f} < 최소 ${min_adv20:,.0f}"

        return True, f"유동성 통과(ADV20 ${adv20:,.0f}, Price ${latest_close:.2f})"
    except Exception as e:
        logger.error(f"유동성 필터 체크 오류 ({ticker}): {e}")
        return False, f"유동성 체크 오류 — 매수 차단: {e}"


def check_correlation_limit(
    ticker: str,
    holding_tickers: set,
    lookback_days: int = 60,
) -> tuple[bool, str]:
    """
    기존 보유 종목과의 상관계수가 임계치를 초과하면 신규 진입 차단.
    """
    if not holding_tickers:
        return True, "보유 종목 없음 — 상관필터 통과"

    corr_limit = _get_config("correlation_limit", 0.7)
    tickers = [ticker] + list(holding_tickers)
    try:
        rows = (
            supabase.table("stock_daily_prices")
            .select("date, ticker, close")
            .in_("ticker", tickers)
            .order("date", desc=True)
            .limit(max(lookback_days * len(tickers), 200))
            .execute()
            .data
            or []
        )
        if not rows:
            return False, "가격 데이터 없음 — 상관필터 차단"

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        pivot = df.pivot_table(index="date", columns="ticker", values="close", aggfunc="last").sort_index()
        returns = pivot.pct_change().dropna()

        if ticker not in returns.columns or len(returns) < 20:
            return False, "상관 계산 샘플 부족 — 매수 차단"

        target_corr = returns.corr()[ticker].drop(labels=[ticker], errors="ignore")
        if target_corr.empty:
            return False, "비교 대상 상관 없음 — 매수 차단"

        max_corr_ticker = target_corr.abs().idxmax()
        max_corr = float(target_corr[max_corr_ticker])
        if abs(max_corr) > corr_limit:
            return False, f"상관계수 제한 초과({max_corr_ticker}: {max_corr:.2f} > {corr_limit:.2f})"
        return True, f"상관필터 통과(최대 |corr|={abs(max_corr):.2f})"
    except Exception as e:
        logger.error(f"상관계수 필터 체크 오류 ({ticker}): {e}")
        return False, f"상관필터 체크 오류 — 매수 차단: {e}"


def get_mdd_risk_state() -> tuple[bool, float, str]:
    """
    누적 MDD 기준 신규진입 허용 여부와 포지션 축소 배수를 반환.
    - hard limit 이하: 신규매수 중단
    - soft limit 이하: 포지션 축소 배수 적용
    """
    soft_limit = _get_config("mdd_soft_limit_pct", 5.0)
    hard_limit = _get_config("mdd_hard_limit_pct", 10.0)
    soft_multiplier = _get_config("mdd_soft_position_multiplier", 0.5)

    try:
        rows = (
            supabase.table("trade_records")
            .select("sell_date, profit_loss, buy_price, quantity")
            .eq("status", "sold")
            .order("sell_date")
            .execute()
            .data
            or []
        )
        if not rows:
            return True, 1.0, "MDD 데이터 없음 — 기본 배수 1.0"

        equity = 100.0
        peak = 100.0
        max_drawdown = 0.0

        for r in rows:
            buy_price = float(r.get("buy_price") or 0)
            qty = float(r.get("quantity") or 0)
            pnl = float(r.get("profit_loss") or 0)
            invested = buy_price * qty
            if invested <= 0:
                continue
            trade_return_pct = (pnl / invested) * 100
            equity *= (1 + trade_return_pct / 100)
            peak = max(peak, equity)
            drawdown_pct = ((equity - peak) / peak) * 100
            max_drawdown = min(max_drawdown, drawdown_pct)

        mdd_abs = abs(max_drawdown)
        if mdd_abs >= hard_limit:
            return False, 0.0, f"MDD {mdd_abs:.2f}% >= 하드한도 {hard_limit:.2f}% (신규매수 중단)"
        if mdd_abs >= soft_limit:
            return True, soft_multiplier, f"MDD {mdd_abs:.2f}% >= 소프트한도 {soft_limit:.2f}% (배수 {soft_multiplier:.2f})"

        return True, 1.0, f"MDD {mdd_abs:.2f}% (정상)"
    except Exception as e:
        logger.error(f"MDD 리스크 상태 계산 오류: {e}")
        return False, 0.0, f"MDD 계산 오류 — 신규 매수 차단: {e}"


# ============================================================
# 3. 섹터 집중도 체크
# ============================================================
def check_sector_concentration(ticker: str, holding_tickers: set) -> tuple[bool, str]:
    """
    대상 종목과 같은 섹터를 이미 max_sector_positions개 이상 보유 중이면 차단.

    Returns:
        (can_buy, description)
    """
    max_sector = int(_get_config("max_sector_positions", 2))

    try:
        target_rows = supabase.table("stock_universe").select("sector").eq("ticker", ticker).execute().data
        if not target_rows or not target_rows[0].get("sector"):
            return True, "섹터 정보 없음 — 통과"

        target_sector = target_rows[0]["sector"]

        if not holding_tickers:
            return True, f"섹터 {target_sector} (0/{max_sector})"

        holdings_info = supabase.table("stock_universe").select("ticker, sector").in_(
            "ticker", list(holding_tickers)
        ).execute().data or []

        sector_count = sum(1 for h in holdings_info if h.get("sector") == target_sector)

        if sector_count >= max_sector:
            reason = f"섹터 '{target_sector}' {sector_count}개 = 최대 {max_sector}개"
            logger.info(f"섹터 집중도 제한: {reason}")
            return False, reason

        return True, f"섹터 {target_sector}: {sector_count}/{max_sector}개"

    except Exception as e:
        logger.error(f"섹터 집중도 체크 오류 ({ticker}): {e}")
        return False, f"섹터 집중도 체크 오류 — 매수 차단: {e}"


# ============================================================
# 4. 포지션 사이징
# ============================================================
def calculate_position_size(available_amount: float, current_price: float) -> int:
    """
    system_config.position_size_pct 기준으로 매수 수량 계산.

    Returns:
        매수 수량 (0이면 매수 불가)
    """
    if current_price <= 0:
        return 0

    pct = _get_config("position_size_pct", 10.0) / 100.0
    invest_amount = available_amount * pct
    quantity = int(invest_amount / current_price)

    logger.debug(
        f"포지션 사이징: 가용금액=${available_amount:.2f}, "
        f"비중={pct*100:.0f}%, 투자금=${invest_amount:.2f}, 수량={quantity}주"
    )
    return quantity
