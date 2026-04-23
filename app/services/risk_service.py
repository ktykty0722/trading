"""
리스크 관리 서비스.
system_config KV 테이블에서 파라미터를 동적으로 로드하여 매수 전 체크합니다.
코드 재배포 없이 Supabase에서 임계값을 조정할 수 있습니다.
"""
import logging
from datetime import datetime

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
        return True, "체크 오류 — 통과"


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
        return True, "VIX 없음 — 통과"

    threshold = _get_config("vix_halt_threshold", settings.VIX_HALT_THRESHOLD)
    if float(vix_value) > threshold:
        reason = f"VIX {float(vix_value):.2f} > 임계치 {threshold:.2f}"
        logger.warning(f"변동성 게이트 차단: {reason}")
        return False, reason

    return True, f"VIX {float(vix_value):.2f} <= {threshold:.2f}"


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
        return True, "체크 오류 — 통과"


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
