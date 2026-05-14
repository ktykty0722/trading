"""
인트라데이 매매 엔진.

스윙 매매(일봉 기반)와 병렬로 동작하는 별도 전략 레이어.

원칙:
- 숫자 룰 기반 후보 생성. LLM은 보조 필터(옵션, 기본 OFF).
- system_config로 ON/OFF 및 임계값 제어.
- 기본값은 보수적: intraday_enabled=false.
- 스윙과 같은 KIS 주문 경로(balance_service), 같은 trade_records 사용.
  단, strategy='intraday'로 구분.

MVP 데이터:
- KIS 현재가 API에서 5분마다 snapshot → intraday_prices
- 전일 종가: stock_daily_prices 최신 행
- VWAP / momentum / day_high: intraday_prices 자체 누적

후보 룰:
1) 전일 종가 대비 > 0.5% 상승
2) 최근 15분 momentum > intraday_min_momentum_15m_pct
3) 거래량 비율 > intraday_min_volume_ratio
4) 현재가 >= VWAP (intraday_require_above_vwap=true일 때)
5) RSI 과열 아님 (stock_signals.rsi <= 75)
6) SPY/QQQ 당일 변동률 <= intraday_market_drop_block_pct 이면 차단
7) 이미 보유/주문 중 아님
8) 같은 종목 쿨다운 시간 내 재진입 금지
9) 일일 최대 진입 제한, max_positions, 일일손실, VIX, MDD 게이트 통과
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import pytz

from app.core.config import settings
from app.db.supabase import supabase
from app.services.balance_service import (
    get_current_price, order_overseas_stock,
    get_all_overseas_balances, inquire_psamount,
)
from app.services.risk_service import (
    check_daily_loss_limit, check_max_positions,
    check_vix_halt_gate, get_mdd_risk_state, calculate_position_size,
)
from app.services.stock_recommendation_service import EXCHANGE_TO_API
from app.telegram_bot.notifier import notify_intraday_order, notify_error

logger = logging.getLogger("intraday")


# ============================================================
# system_config helpers
# ============================================================
def _cfg(key: str, default: str) -> str:
    try:
        row = supabase.table("system_config").select("value").eq("key", key).limit(1).execute().data
        if row:
            return str(row[0].get("value", default))
    except Exception:
        pass
    return default


def _cfg_f(key: str, default: float) -> float:
    try:
        return float(_cfg(key, str(default)))
    except Exception:
        return default


def _cfg_i(key: str, default: int) -> int:
    try:
        return int(float(_cfg(key, str(default))))
    except Exception:
        return default


def _cfg_b(key: str, default: bool) -> bool:
    return _cfg(key, "true" if default else "false").strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_hhmm(value: str, dh: int, dm: int) -> tuple[int, int]:
    try:
        hh, mm = value.split(":")
        return int(hh), int(mm)
    except Exception:
        return dh, dm


# ============================================================
# 시간 게이트
# ============================================================
def _is_intraday_window(now_et: datetime) -> bool:
    sh, sm = _parse_hhmm(_cfg("intraday_start_et", "09:45"), 9, 45)
    eh, em = _parse_hhmm(_cfg("intraday_end_et", "15:30"), 15, 30)
    cur = now_et.hour * 60 + now_et.minute
    return (sh * 60 + sm) <= cur < (eh * 60 + em)


# ============================================================
# 가격 snapshot 수집
# ============================================================
def _fetch_active_universe() -> list[dict]:
    try:
        return (
            supabase.table("stock_universe")
            .select("ticker, name_ko, exchange, is_etf")
            .eq("is_active", True)
            .eq("is_etf", False)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"universe 조회 실패: {e}")
        return []


def collect_intraday_snapshots(universe: list[dict]) -> list[dict]:
    """active universe에 대해 KIS 현재가를 조회, intraday_prices에 저장."""
    now_iso = datetime.now(pytz.UTC).isoformat()
    snapshots = []
    rows_to_insert = []

    for u in universe:
        ticker = u["ticker"]
        exchange = u.get("exchange", "NASD")
        excd = EXCHANGE_TO_API.get(exchange, "NAS")
        try:
            result = get_current_price({"AUTH": "", "EXCD": excd, "SYMB": ticker})
            if result.get("rt_cd") != "0":
                continue
            out = result.get("output", {}) or {}
            last = out.get("last") or "0"
            tvol = out.get("tvol") or "0"
            price = float(last) if last and str(last).strip() not in ("", ".") else 0.0
            volume = int(tvol) if tvol and str(tvol).strip() not in ("", ".") else 0
            if price <= 0:
                continue
            snap = {
                "ticker": ticker,
                "timestamp": now_iso,
                "price": price,
                "volume": volume,
                "source": "kis_price",
                "exchange": exchange,
            }
            snapshots.append(snap)
            rows_to_insert.append({k: v for k, v in snap.items() if k != "exchange"})
            time.sleep(0.6)  # KIS rate limit 보호
        except Exception as e:
            logger.debug(f"{ticker} snapshot 실패: {e}")

    if rows_to_insert:
        try:
            supabase.table("intraday_prices").upsert(rows_to_insert, on_conflict="ticker,timestamp").execute()
        except Exception as e:
            logger.warning(f"intraday_prices 저장 실패: {e}")

    return snapshots


# ============================================================
# 지표 계산 (snapshot 기반 단순 룰)
# ============================================================
def _recent_snapshots(ticker: str, minutes: int = 60) -> list[dict]:
    cutoff = (datetime.now(pytz.UTC) - timedelta(minutes=minutes)).isoformat()
    try:
        return (
            supabase.table("intraday_prices")
            .select("timestamp, price, volume")
            .eq("ticker", ticker)
            .gte("timestamp", cutoff)
            .order("timestamp", desc=False)
            .execute()
            .data or []
        )
    except Exception:
        return []


def _previous_close(ticker: str) -> Optional[float]:
    try:
        rows = (
            supabase.table("stock_daily_prices")
            .select("date, close")
            .eq("ticker", ticker)
            .order("date", desc=True)
            .limit(2)
            .execute()
            .data or []
        )
        # 최신 1개가 오늘일 수도 있으니 더 이전 행을 우선 사용
        if len(rows) >= 2:
            return float(rows[1]["close"])
        if rows:
            return float(rows[0]["close"])
    except Exception:
        pass
    return None


def _today_rsi(ticker: str) -> Optional[float]:
    try:
        rows = (
            supabase.table("stock_signals")
            .select("date, rsi")
            .eq("ticker", ticker)
            .order("date", desc=True)
            .limit(1)
            .execute()
            .data or []
        )
        if rows and rows[0].get("rsi") is not None:
            return float(rows[0]["rsi"])
    except Exception:
        pass
    return None


def _spy_qqq_change_pct() -> Optional[float]:
    """SPY/QQQ 당일 변동률 (snapshot vs 전일 종가). 둘 중 더 낮은 값(약세 기준)."""
    worst = None
    for tkr in ("SPY", "QQQ"):
        snaps = _recent_snapshots(tkr, minutes=15)
        prev = _previous_close(tkr)
        if not snaps or not prev or prev <= 0:
            continue
        latest = snaps[-1]["price"]
        chg = (float(latest) - prev) / prev * 100.0
        worst = chg if worst is None else min(worst, chg)
    return worst


def _compute_signal(ticker: str, snapshot: dict) -> dict:
    """단일 종목의 인트라데이 신호 계산. signal_score 0~1."""
    price = float(snapshot["price"])
    volume_now = int(snapshot.get("volume") or 0)
    history = _recent_snapshots(ticker, minutes=60)

    prev_close = _previous_close(ticker)
    change_prev_pct = ((price - prev_close) / prev_close * 100.0) if prev_close else 0.0

    # 15분 momentum
    momentum_15m = 0.0
    cutoff_15 = datetime.now(pytz.UTC) - timedelta(minutes=15)
    older = [h for h in history if datetime.fromisoformat(h["timestamp"].replace("Z", "+00:00")) <= cutoff_15]
    base_price = float(older[-1]["price"]) if older else (float(history[0]["price"]) if history else price)
    if base_price > 0:
        momentum_15m = (price - base_price) / base_price * 100.0

    # VWAP & day high
    prices_arr = [float(h["price"]) for h in history]
    vols_arr = [int(h.get("volume") or 0) for h in history]
    pv = sum(p * v for p, v in zip(prices_arr, vols_arr) if v > 0)
    vsum = sum(v for v in vols_arr if v > 0)
    vwap = (pv / vsum) if vsum > 0 else price
    day_high = max(prices_arr) if prices_arr else price
    day_high_breakout = price >= day_high * 0.999

    # 거래량 비율: 최근 5분 평균 / 그 이전 평균
    recent_5 = [h for h in history if datetime.fromisoformat(h["timestamp"].replace("Z", "+00:00")) >= (datetime.now(pytz.UTC) - timedelta(minutes=5))]
    earlier = [h for h in history if h not in recent_5]
    recent_vol_avg = (sum(int(h.get("volume") or 0) for h in recent_5) / len(recent_5)) if recent_5 else 0
    earlier_vol_avg = (sum(int(h.get("volume") or 0) for h in earlier) / len(earlier)) if earlier else 0
    volume_ratio = (recent_vol_avg / earlier_vol_avg) if earlier_vol_avg > 0 else 1.0

    above_vwap = price >= vwap

    # 컴포넌트 점수 (모두 0~1로 클램프)
    def clamp(x, lo=0.0, hi=1.0):
        return max(lo, min(hi, x))

    momentum_score = clamp(momentum_15m / 1.0)        # 1%이면 만점
    volume_score = clamp((volume_ratio - 1.0) / 2.0)  # ratio 3.0이면 만점
    trend_score = clamp((price - vwap) / max(vwap, 1e-9) / 0.01)  # vwap 위 1%면 만점
    market_chg = _spy_qqq_change_pct() or 0.0
    market_score = clamp(0.5 + market_chg / 2.0)
    daily_signal_score = clamp(change_prev_pct / 3.0)  # +3%면 만점

    signal_score = (
        0.30 * momentum_score
        + 0.25 * volume_score
        + 0.20 * trend_score
        + 0.15 * market_score
        + 0.10 * daily_signal_score
    )

    reason_parts = [
        f"prev_close_chg={change_prev_pct:+.2f}%",
        f"mom15m={momentum_15m:+.2f}%",
        f"vol_ratio={volume_ratio:.2f}",
        f"vwap={'above' if above_vwap else 'below'}",
        f"market={market_chg:+.2f}%",
    ]
    if day_high_breakout:
        reason_parts.append("day_high")

    return {
        "ticker": ticker,
        "timestamp": snapshot["timestamp"],
        "price": price,
        "change_from_prev_close_pct": round(change_prev_pct, 4),
        "momentum_15m_pct": round(momentum_15m, 4),
        "volume_ratio": round(volume_ratio, 3),
        "vwap": round(vwap, 4),
        "above_vwap": above_vwap,
        "day_high_breakout": day_high_breakout,
        "signal_score": round(signal_score, 4),
        "reason": " | ".join(reason_parts),
        "market_chg_pct": market_chg,
    }


# ============================================================
# 후보 필터
# ============================================================
def _is_candidate(sig: dict, holding_tickers: set, cooldown_tickers: set) -> tuple[bool, str]:
    if sig["ticker"] in holding_tickers:
        return False, "이미 보유/주문 중"
    if sig["ticker"] in cooldown_tickers:
        return False, "쿨다운 중"

    min_score = _cfg_f("intraday_min_score", 0.7)
    if sig["signal_score"] < min_score:
        return False, f"score {sig['signal_score']:.2f} < {min_score:.2f}"

    min_chg = 0.5
    if sig["change_from_prev_close_pct"] < min_chg:
        return False, f"전일대비 {sig['change_from_prev_close_pct']:.2f}% < {min_chg}%"

    min_mom = _cfg_f("intraday_min_momentum_15m_pct", 0.3)
    if sig["momentum_15m_pct"] < min_mom:
        return False, f"momentum_15m {sig['momentum_15m_pct']:.2f}% < {min_mom}%"

    min_vr = _cfg_f("intraday_min_volume_ratio", 1.5)
    if sig["volume_ratio"] < min_vr:
        return False, f"volume_ratio {sig['volume_ratio']:.2f} < {min_vr}"

    if _cfg_b("intraday_require_above_vwap", True) and not sig["above_vwap"]:
        return False, "VWAP 아래"

    rsi = _today_rsi(sig["ticker"])
    if rsi is not None and rsi > 75:
        return False, f"RSI 과열({rsi:.1f})"

    market_block = _cfg_f("intraday_market_drop_block_pct", -0.8)
    if sig.get("market_chg_pct", 0.0) <= market_block:
        return False, f"SPY/QQQ {sig['market_chg_pct']:+.2f}% <= {market_block}%"

    return True, "ok"


# ============================================================
# 쿨다운 집합 조회
# ============================================================
def _cooldown_tickers(now_utc: datetime) -> set:
    minutes = _cfg_i("intraday_ticker_cooldown_minutes", 60)
    cutoff = (now_utc - timedelta(minutes=minutes)).isoformat()
    try:
        rows = (
            supabase.table("intraday_signals")
            .select("ticker, timestamp")
            .gte("timestamp", cutoff)
            .execute()
            .data or []
        )
        return {r["ticker"] for r in rows}
    except Exception:
        return set()


def _today_intraday_order_count() -> int:
    today = datetime.now(pytz.timezone(settings.MARKET_TIMEZONE)).strftime("%Y-%m-%d")
    try:
        rows = (
            supabase.table("trade_records")
            .select("id")
            .eq("strategy", "intraday")
            .gte("buy_date", today)
            .execute()
            .data or []
        )
        return len(rows)
    except Exception:
        return 0


# ============================================================
# 주문 실행
# ============================================================
def _execute_intraday_buy(sig: dict, exchange: str, name_ko: str) -> bool:
    ticker = sig["ticker"]
    api_excd = EXCHANGE_TO_API.get(exchange, "NAS")

    # 현재가 재확인
    price_result = get_current_price({"AUTH": "", "EXCD": api_excd, "SYMB": ticker})
    if price_result.get("rt_cd") != "0":
        logger.warning(f"[intraday] {ticker} 현재가 재조회 실패")
        return False
    last = price_result.get("output", {}).get("last", "0")
    try:
        current_price = float(last)
    except Exception:
        return False
    if current_price <= 0:
        return False

    # 매수가능금액
    time.sleep(0.8)
    ps_result = inquire_psamount({
        "CANO": settings.KIS_CANO,
        "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
        "OVRS_EXCG_CD": exchange,
        "OVRS_ORD_UNPR": str(current_price),
        "ITEM_CD": ticker,
    })
    if ps_result.get("rt_cd") != "0":
        logger.warning(f"[intraday] {ticker} psamount 실패")
        return False
    ps_out = ps_result.get("output", {}) or {}
    avail = float(ps_out.get("frcr_ord_psbl_amt1", 0) or ps_out.get("ovrs_ord_psbl_amt", 0))
    if avail <= 0:
        return False

    # 인트라데이는 별도 비중 사용 (system_config.intraday_position_size_pct)
    pct = _cfg_f("intraday_position_size_pct", 5.0) / 100.0
    invest_amount = avail * pct
    quantity = int(invest_amount / current_price)
    if quantity < 1:
        logger.info(f"[intraday] {ticker} 수량<1 (avail=${avail:.2f}, price=${current_price:.2f})")
        return False

    time.sleep(0.8)
    order_data = {
        "CANO": settings.KIS_CANO,
        "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
        "OVRS_EXCG_CD": exchange,
        "PDNO": ticker,
        "ORD_DVSN": "00",
        "ORD_QTY": str(quantity),
        "OVRS_ORD_UNPR": str(current_price),
        "is_buy": True,
    }
    order_result = order_overseas_stock(order_data)
    if order_result.get("rt_cd") != "0":
        logger.error(f"[intraday] {ticker} 주문 실패: {order_result.get('msg1')}")
        return False

    # trade_records 기록 (strategy=intraday)
    try:
        supabase.table("trade_records").insert({
            "ticker": ticker,
            "stock_name": name_ko or ticker,
            "buy_price": current_price,
            "buy_date": datetime.now(pytz.timezone(settings.MARKET_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S"),
            "quantity": quantity,
            "holding_quantity": 0,
            "exchange_code": exchange,
            "status": "buy_ordered",
            "composite_score": sig["signal_score"],
            "strategy": "intraday",
            "entry_reason": sig["reason"][:500],
            "signal_snapshot": {
                "price": sig["price"],
                "change_from_prev_close_pct": sig["change_from_prev_close_pct"],
                "momentum_15m_pct": sig["momentum_15m_pct"],
                "volume_ratio": sig["volume_ratio"],
                "vwap": sig["vwap"],
                "above_vwap": sig["above_vwap"],
                "day_high_breakout": sig["day_high_breakout"],
                "signal_score": sig["signal_score"],
            },
        }).execute()
    except Exception as e:
        logger.error(f"[intraday] {ticker} trade_records 저장 실패: {e}")

    notify_intraday_order(ticker, name_ko or ticker, current_price, quantity,
                         exchange, sig["signal_score"], sig["reason"])
    logger.info(f"[intraday] {ticker} 매수 접수 {quantity}주 @ ${current_price}")
    return True


# ============================================================
# 메인 사이클
# ============================================================
def run_intraday_cycle() -> dict:
    """1회 평가 사이클. scheduler가 N분마다 호출."""
    if not _cfg_b("intraday_enabled", False):
        return {"executed": False, "reason": "intraday_enabled=false"}

    market_tz = pytz.timezone(settings.MARKET_TIMEZONE)
    now_et = datetime.now(market_tz)
    if now_et.weekday() > 4:
        return {"executed": False, "reason": "weekend"}
    if not _is_intraday_window(now_et):
        return {"executed": False, "reason": "out of intraday window"}

    universe = _fetch_active_universe()
    if not universe:
        return {"executed": False, "reason": "no universe"}

    # 시장 게이트
    safe, reason = check_daily_loss_limit()
    if not safe:
        return {"executed": False, "reason": f"daily_loss: {reason}"}

    # snapshot 수집 (universe + SPY/QQQ)
    market_proxies = [{"ticker": "SPY", "exchange": "AMEX", "name_ko": "SPY"},
                      {"ticker": "QQQ", "exchange": "NASD", "name_ko": "QQQ"}]
    snapshots = collect_intraday_snapshots(universe + market_proxies)
    if not snapshots:
        return {"executed": False, "reason": "no snapshots"}

    # 보유/주문중 ticker
    holding_tickers: set[str] = set()
    try:
        bal = get_all_overseas_balances()
        if bal.get("rt_cd") == "0":
            for it in bal.get("output1", []):
                t = it.get("ovrs_pdno")
                if t and int(it.get("ovrs_cblc_qty", 0)) > 0:
                    holding_tickers.add(t)
        ordered = supabase.table("trade_records").select("ticker").in_(
            "status", ["buy_ordered", "holding", "sell_ordered"]
        ).execute().data or []
        for r in ordered:
            holding_tickers.add(r["ticker"])
    except Exception as e:
        logger.warning(f"[intraday] holdings 조회 실패: {e}")

    # 최대 보유 게이트
    can_buy, mp_reason = check_max_positions(holding_tickers)
    if not can_buy:
        return {"executed": False, "reason": f"max_positions: {mp_reason}"}

    # VIX 게이트
    vix_value = None
    try:
        vrow = supabase.table("economic_indicators").select("vix").order("date", desc=True).limit(1).execute().data
        if vrow and vrow[0].get("vix") is not None:
            vix_value = float(vrow[0]["vix"])
    except Exception:
        pass
    can_trade, vix_reason = check_vix_halt_gate(vix_value)
    if not can_trade:
        return {"executed": False, "reason": f"vix: {vix_reason}"}

    # MDD
    can_mdd, multiplier, mdd_reason = get_mdd_risk_state()
    if not can_mdd:
        return {"executed": False, "reason": f"mdd: {mdd_reason}"}

    # 일일 진입 제한
    today_n = _today_intraday_order_count()
    max_today = _cfg_i("intraday_max_entries_per_day", 3)
    if today_n >= max_today:
        return {"executed": False, "reason": f"max_entries_per_day {today_n}/{max_today}"}

    cooldown = _cooldown_tickers(datetime.now(pytz.UTC))
    # universe snapshot만 신호 계산 (market proxy 제외)
    universe_tickers = {u["ticker"] for u in universe}
    name_map = {u["ticker"]: u.get("name_ko", u["ticker"]) for u in universe}
    exch_map = {u["ticker"]: u.get("exchange", "NASD") for u in universe}

    candidates = []
    signal_rows = []
    for snap in snapshots:
        if snap["ticker"] not in universe_tickers:
            continue
        try:
            sig = _compute_signal(snap["ticker"], snap)
        except Exception as e:
            logger.debug(f"[intraday] {snap['ticker']} 시그널 계산 실패: {e}")
            continue
        signal_rows.append({k: v for k, v in sig.items() if k != "market_chg_pct"})
        ok, _reason = _is_candidate(sig, holding_tickers, cooldown - {snap["ticker"]} if snap["ticker"] not in cooldown else cooldown)
        if ok:
            candidates.append(sig)

    if signal_rows:
        try:
            supabase.table("intraday_signals").upsert(signal_rows, on_conflict="ticker,timestamp").execute()
        except Exception as e:
            logger.warning(f"[intraday] intraday_signals 저장 실패: {e}")

    if not candidates:
        return {"executed": False, "reason": "no candidates", "evaluated": len(signal_rows)}

    candidates.sort(key=lambda x: x["signal_score"], reverse=True)
    placed = 0
    remaining = max_today - today_n

    for sig in candidates:
        if placed >= remaining:
            break
        ticker = sig["ticker"]
        try:
            ok = _execute_intraday_buy(sig, exch_map.get(ticker, "NASD"), name_map.get(ticker, ticker))
            if ok:
                placed += 1
                holding_tickers.add(ticker)
                # 1건 후 max_positions 재체크
                can_more, _r = check_max_positions(holding_tickers)
                if not can_more:
                    break
        except Exception as e:
            logger.exception(f"[intraday] {ticker} 주문 처리 예외: {e}")
            try:
                notify_error("intraday_buy", f"{ticker}: {e}")
            except Exception:
                pass

    return {
        "executed": placed > 0,
        "placed": placed,
        "evaluated": len(signal_rows),
        "candidates": len(candidates),
    }


__all__ = [
    "run_intraday_cycle",
    "collect_intraday_snapshots",
]
