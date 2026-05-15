"""
인트라데이 백테스트 엔진.

intraday_prices 누적 스냅샷을 시계열 replay하면서
인트라데이 매수 룰 → 청산 룰을 그대로 적용하고
거래/요약 통계를 반환한다.

주의:
- intraday_prices가 실제로 누적된 만큼만 백테스트 가능.
- 슬리피지/수수료는 config로 조정 (bps).
- 매수/청산 룰은 intraday_service의 임계값 키를 그대로 읽어 사용.
"""
import logging
import uuid
from datetime import datetime, timedelta, time as dtime
from typing import Optional

import pandas as pd
import pytz

from app.db.supabase import supabase
from app.core.config import settings
from app.services.intraday_service import _cfg, _cfg_f, _cfg_i, _cfg_b, _parse_hhmm  # type: ignore

logger = logging.getLogger("intraday_backtest")


# ============================================================
# 데이터 로드
# ============================================================
def _load_intraday_prices(start: datetime, end: datetime) -> pd.DataFrame:
    rows: list[dict] = []
    offset = 0
    page = 1000
    while True:
        resp = (
            supabase.table("intraday_prices")
            .select("ticker, timestamp, price, volume")
            .gte("timestamp", start.isoformat())
            .lte("timestamp", end.isoformat())
            .order("timestamp", desc=False)
            .range(offset, offset + page - 1)
            .execute()
        )
        chunk = resp.data or []
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    if not rows:
        return pd.DataFrame(columns=["ticker", "timestamp", "price", "volume"])
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)
    df = df.dropna(subset=["price"])
    return df


def _load_daily_closes(start: datetime, end: datetime) -> dict[str, list[tuple]]:
    """
    각 ticker별 (trading_date, close) 시계열을 반환한다.
    백테스트 timestamp별로 '그 ET 날짜보다 이전의 가장 최근 종가'를 찾을 수 있도록 함.
    Look-ahead bias 방지: 단일 latest close가 아닌 시계열 전체를 사용.
    """
    # 백테스트 시작일 기준 충분한 이전 데이터까지 로드 (영업일 공백 대비 30일 버퍼)
    from datetime import date as _date  # local import for typing only
    buffer_start = (start - timedelta(days=30)).date().isoformat()
    end_date = end.date().isoformat()
    rows: list[dict] = []
    offset = 0
    page = 1000
    try:
        while True:
            resp = (
                supabase.table("stock_daily_prices")
                .select("ticker, date, close")
                .gte("date", buffer_start)
                .lte("date", end_date)
                .order("date", desc=False)
                .range(offset, offset + page - 1)
                .execute()
            )
            chunk = resp.data or []
            rows.extend(chunk)
            if len(chunk) < page:
                break
            offset += page
    except Exception:
        return {}

    m: dict[str, list[tuple]] = {}
    for r in rows:
        if not r.get("close"):
            continue
        try:
            d = datetime.fromisoformat(r["date"]).date() if isinstance(r["date"], str) else r["date"]
        except Exception:
            continue
        m.setdefault(r["ticker"], []).append((d, float(r["close"])))
    # 이미 asc 정렬되어 들어옴
    return m


def _prev_close_at(closes_map: dict[str, list[tuple]], ticker: str, ts_et: datetime):
    """ts_et의 ET 날짜보다 strictly 이전의 가장 최근 종가."""
    arr = closes_map.get(ticker)
    if not arr:
        return None
    target = ts_et.date()
    result = None
    for d, c in arr:
        if d < target:
            result = c
        else:
            break
    return result


def _load_rsi_map(start: datetime, end: datetime) -> dict[str, list[tuple]]:
    """ticker별 (signal_date, rsi) 시계열. live의 _today_rsi 동등 효과를 시점-안전하게 재현."""
    buffer_start = (start - timedelta(days=30)).date().isoformat()
    end_date = end.date().isoformat()
    rows: list[dict] = []
    offset = 0
    page = 1000
    try:
        while True:
            resp = (
                supabase.table("stock_signals")
                .select("ticker, date, rsi")
                .gte("date", buffer_start)
                .lte("date", end_date)
                .order("date", desc=False)
                .range(offset, offset + page - 1)
                .execute()
            )
            chunk = resp.data or []
            rows.extend(chunk)
            if len(chunk) < page:
                break
            offset += page
    except Exception:
        return {}
    m: dict[str, list[tuple]] = {}
    for r in rows:
        if r.get("rsi") is None:
            continue
        try:
            d = datetime.fromisoformat(r["date"]).date() if isinstance(r["date"], str) else r["date"]
            m.setdefault(r["ticker"], []).append((d, float(r["rsi"])))
        except Exception:
            continue
    return m


def _rsi_at(rsi_map: dict[str, list[tuple]], ticker: str, ts_et: datetime):
    arr = rsi_map.get(ticker)
    if not arr:
        return None
    target = ts_et.date()
    result = None
    for d, v in arr:
        if d <= target:
            result = v
        else:
            break
    return result


def _load_universe() -> dict[str, dict]:
    try:
        rows = (
            supabase.table("stock_universe")
            .select("ticker, name_ko, exchange, is_etf, is_active")
            .execute().data or []
        )
        return {r["ticker"]: r for r in rows}
    except Exception:
        return {}


# ============================================================
# 시뮬레이션 헬퍼
# ============================================================
def _is_in_window(ts_et: datetime, start_str: str, end_str: str) -> bool:
    sh, sm = _parse_hhmm(start_str, 9, 45)
    eh, em = _parse_hhmm(end_str, 15, 30)
    cur = ts_et.hour * 60 + ts_et.minute
    return (sh * 60 + sm) <= cur < (eh * 60 + em)


def _is_eod_time(ts_et: datetime, eod_str: str) -> bool:
    eh, em = _parse_hhmm(eod_str, 15, 45)
    return ts_et.hour * 60 + ts_et.minute >= eh * 60 + em


def _compute_signal_from_history(history: pd.DataFrame, ticker: str, prev_close: float, now_utc: datetime, market_chg: Optional[float]) -> dict:
    """history: 단일 ticker의 timestamp/price/volume DataFrame, asc 정렬."""
    if history.empty:
        return {}
    price = float(history.iloc[-1]["price"])
    # 15분 momentum
    cutoff_15 = now_utc - timedelta(minutes=15)
    older = history[history["timestamp"] <= cutoff_15]
    base_price = float(older.iloc[-1]["price"]) if not older.empty else float(history.iloc[0]["price"])
    momentum_15m = ((price - base_price) / base_price * 100.0) if base_price > 0 else 0.0

    # VWAP & day high
    vols = history["volume"].astype(float)
    prices = history["price"].astype(float)
    vsum = vols.sum()
    vwap = float((prices * vols).sum() / vsum) if vsum > 0 else price
    day_high = float(prices.max())
    day_high_breakout = price >= day_high * 0.999

    # 거래량 비율 (최근 5분 vs 이전)
    cutoff_5 = now_utc - timedelta(minutes=5)
    recent = history[history["timestamp"] >= cutoff_5]
    earlier = history[history["timestamp"] < cutoff_5]
    recent_avg = float(recent["volume"].mean()) if not recent.empty else 0.0
    earlier_avg = float(earlier["volume"].mean()) if not earlier.empty else 0.0
    volume_ratio = (recent_avg / earlier_avg) if earlier_avg > 0 else 1.0

    change_prev = ((price - prev_close) / prev_close * 100.0) if prev_close else 0.0
    above_vwap = price >= vwap

    def clamp(x, lo=0.0, hi=1.0):
        return max(lo, min(hi, x))

    momentum_score = clamp(momentum_15m / 1.0)
    volume_score = clamp((volume_ratio - 1.0) / 2.0)
    trend_score = clamp((price - vwap) / max(vwap, 1e-9) / 0.01)
    market_chg_v = market_chg or 0.0
    market_score = clamp(0.5 + market_chg_v / 2.0)
    daily_signal_score = clamp(change_prev / 3.0)

    signal_score = (
        0.30 * momentum_score
        + 0.25 * volume_score
        + 0.20 * trend_score
        + 0.15 * market_score
        + 0.10 * daily_signal_score
    )
    return {
        "price": price,
        "momentum_15m_pct": momentum_15m,
        "volume_ratio": volume_ratio,
        "vwap": vwap,
        "above_vwap": above_vwap,
        "day_high_breakout": day_high_breakout,
        "change_from_prev_close_pct": change_prev,
        "market_chg_pct": market_chg_v,
        "signal_score": signal_score,
    }


def _entry_filter(sig: dict, params: dict) -> tuple[bool, str]:
    if not sig:
        return False, "no signal"
    if sig["signal_score"] < params["min_score"]:
        return False, f"score<{params['min_score']}"
    if sig["change_from_prev_close_pct"] < 0.5:
        return False, "prev_close_chg<0.5%"
    if sig["momentum_15m_pct"] < params["min_momentum_15m_pct"]:
        return False, "momentum"
    if sig["volume_ratio"] < params["min_volume_ratio"]:
        return False, "volume"
    if params["require_above_vwap"] and not sig["above_vwap"]:
        return False, "below_vwap"
    if sig["market_chg_pct"] <= params["market_drop_block_pct"]:
        return False, "market_drop"
    return True, "ok"


def _decide_exit_bt(position: dict, current_price: float, ts_et: datetime, market_chg: Optional[float], params: dict) -> Optional[tuple[str, str]]:
    buy = position["buy_price"]
    pnl_pct = (current_price - buy) / buy * 100.0
    if _is_eod_time(ts_et, params["eod_exit_et"]):
        return ("intraday_eod", f"EOD (pnl={pnl_pct:+.2f}%)")
    if market_chg is not None and market_chg <= params["exit_panic_pct"]:
        return ("intraday_panic", f"market {market_chg:+.2f}%")
    if pnl_pct <= -abs(params["stop_pct"]):
        return ("intraday_stop", f"stop {pnl_pct:+.2f}%")
    peak = position.get("peak_price") or buy
    if peak > buy and (peak - buy) / buy * 100.0 >= params["trailing_arm_pct"]:
        drop = (peak - current_price) / peak * 100.0
        if drop >= params["trailing_pct"]:
            return ("intraday_trail", f"trail -{drop:.2f}% from peak")
    if pnl_pct >= params["take_pct"]:
        return ("intraday_take", f"take {pnl_pct:+.2f}%")
    held_min = (ts_et - position["entry_ts_et"]).total_seconds() / 60.0
    if held_min >= params["max_hold_minutes"] and pnl_pct < params["trailing_arm_pct"]:
        return ("intraday_time", f"time {int(held_min)}min, pnl={pnl_pct:+.2f}%")
    return None


# ============================================================
# 백테스트 main
# ============================================================
def run_intraday_backtest(
    days: int = 5,
    start: Optional[str] = None,
    end: Optional[str] = None,
    fee_bps: float = 5.0,         # 매수+매도 합 round-trip 수수료 (bps)
    slippage_bps: float = 5.0,    # 진입/청산 슬리피지 합 (bps)
    save_to_db: bool = True,
) -> dict:
    """
    intraday_prices를 replay하며 인트라데이 진입/청산 룰을 시뮬레이션.

    Args:
        days: 최근 N일 (start/end가 None이면 사용)
        start, end: 'YYYY-MM-DD' 명시적 기간
        fee_bps, slippage_bps: 비용 모형 (round-trip bps)
        save_to_db: True면 intraday_backtests에 결과 저장

    Returns:
        {"summary": {...}, "trades": [...], "run_id": str}
    """
    market_tz = pytz.timezone(settings.MARKET_TIMEZONE)
    now_utc = datetime.now(pytz.UTC)
    if start:
        start_dt = datetime.fromisoformat(start).replace(tzinfo=pytz.UTC)
    else:
        start_dt = now_utc - timedelta(days=days)
    if end:
        end_dt = datetime.fromisoformat(end).replace(tzinfo=pytz.UTC)
    else:
        end_dt = now_utc

    # 룰 파라미터 로드 (system_config — 현재 설정 그대로)
    params = {
        "min_score": _cfg_f("intraday_min_score", 0.7),
        "min_momentum_15m_pct": _cfg_f("intraday_min_momentum_15m_pct", 0.3),
        "min_volume_ratio": _cfg_f("intraday_min_volume_ratio", 1.5),
        "require_above_vwap": _cfg_b("intraday_require_above_vwap", True),
        "market_drop_block_pct": _cfg_f("intraday_market_drop_block_pct", -0.8),
        "max_entries_per_day": _cfg_i("intraday_max_entries_per_day", 3),
        "cooldown_minutes": _cfg_i("intraday_ticker_cooldown_minutes", 60),
        "start_et": _cfg("intraday_start_et", "09:45"),
        "end_et": _cfg("intraday_end_et", "15:30"),
        "eod_exit_et": _cfg("intraday_eod_exit_et", "15:45"),
        "take_pct": _cfg_f("intraday_take_pct", 1.2),
        "stop_pct": _cfg_f("intraday_stop_pct", 0.7),
        "trailing_pct": _cfg_f("intraday_trailing_pct", 0.5),
        "trailing_arm_pct": _cfg_f("intraday_trailing_arm_pct", 0.6),
        "max_hold_minutes": _cfg_i("intraday_max_hold_minutes", 90),
        "exit_panic_pct": _cfg_f("intraday_exit_panic_pct", -1.5),
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
    }

    df = _load_intraday_prices(start_dt, end_dt)
    if df.empty:
        return {"summary": {"trades": 0, "message": "intraday_prices 데이터 없음 — 백테스트 불가"}, "trades": [], "run_id": ""}

    universe = _load_universe()
    # 시점-안전 prev_close: 각 ts의 ET 날짜보다 strictly 이전 종가만 사용 (look-ahead 차단)
    closes_map = _load_daily_closes(start_dt, end_dt)
    # 시점-안전 RSI: 그날 또는 그 전의 마지막 stock_signals.rsi 사용
    rsi_map = _load_rsi_map(start_dt, end_dt)

    # SPY/QQQ market_chg per timestamp (전일종가 대비, 시점-안전)
    market_tickers = {"SPY", "QQQ"}
    market_df = df[df["ticker"].isin(market_tickers)].copy()

    # 라이브 _recent_snapshots와 동일하게 최근 60분 rolling window 사용
    SIGNAL_WINDOW_MINUTES = 60
    # 라이브 _today_rsi 가드와 동일: rsi > 75면 진입 거부
    RSI_HARD_CAP = 75.0

    # 시뮬: timestamp 오름차순으로 처리
    df = df.sort_values("timestamp")
    timestamps = df["timestamp"].drop_duplicates().tolist()

    open_positions: dict[str, dict] = {}     # ticker → position
    cooldown_until: dict[str, datetime] = {} # ticker → utc datetime
    daily_entry_count: dict[str, int] = {}   # YYYY-MM-DD(ET) → n
    trades: list[dict] = []

    def _market_chg_at(ts: datetime, ts_et: datetime) -> Optional[float]:
        sub = market_df[market_df["timestamp"] <= ts]
        if sub.empty:
            return None
        chg_list = []
        for t in ("SPY", "QQQ"):
            tsub = sub[sub["ticker"] == t]
            if tsub.empty:
                continue
            latest = float(tsub.iloc[-1]["price"])
            prev = _prev_close_at(closes_map, t, ts_et)
            if prev and prev > 0:
                chg_list.append((latest - prev) / prev * 100.0)
        return min(chg_list) if chg_list else None

    for ts in timestamps:
        ts_et = ts.tz_convert(market_tz)
        if ts_et.weekday() > 4:
            continue
        day_key = ts_et.strftime("%Y-%m-%d")

        # 라이브와 동일하게 최근 60분 윈도우만 사용 (look-ahead/장기집계 차단)
        window_start = ts - timedelta(minutes=SIGNAL_WINDOW_MINUTES)
        history = df[(df["timestamp"] >= window_start) & (df["timestamp"] <= ts)]
        snapshot = df[df["timestamp"] == ts]
        market_chg = _market_chg_at(ts, ts_et)

        # ---- 청산 평가 (먼저) ----
        for ticker in list(open_positions.keys()):
            row = snapshot[snapshot["ticker"] == ticker]
            if row.empty:
                continue
            cur_price = float(row.iloc[0]["price"])
            pos = open_positions[ticker]
            # peak 갱신
            pos["peak_price"] = max(pos.get("peak_price") or pos["buy_price"], cur_price)
            decision = _decide_exit_bt(pos, cur_price, ts_et, market_chg, params)
            if decision:
                exit_strategy, reason = decision
                # 슬리피지/수수료 반영
                slip = cur_price * (params["slippage_bps"] / 10000.0 / 2.0)
                sell_eff = cur_price - slip  # 매도 슬리피지 (불리)
                gross_pnl_pct = (sell_eff - pos["buy_price"]) / pos["buy_price"] * 100.0
                net_pnl_pct = gross_pnl_pct - params["fee_bps"] / 100.0  # bps→%
                held_min = (ts_et - pos["entry_ts_et"]).total_seconds() / 60.0
                trades.append({
                    "ticker": ticker,
                    "entry_ts_et": pos["entry_ts_et"].isoformat(),
                    "exit_ts_et": ts_et.isoformat(),
                    "buy_price": round(pos["buy_price"], 4),
                    "sell_price": round(sell_eff, 4),
                    "peak_price": round(pos["peak_price"], 4),
                    "exit_strategy": exit_strategy,
                    "reason": reason,
                    "held_minutes": int(held_min),
                    "gross_pnl_pct": round(gross_pnl_pct, 4),
                    "net_pnl_pct": round(net_pnl_pct, 4),
                })
                del open_positions[ticker]
                cooldown_until[ticker] = ts + timedelta(minutes=params["cooldown_minutes"])

        # ---- 진입 평가 ----
        if _is_in_window(ts_et, params["start_et"], params["end_et"]) and not _is_eod_time(ts_et, params["eod_exit_et"]):
            entries_today = daily_entry_count.get(day_key, 0)
            if entries_today >= params["max_entries_per_day"]:
                continue

            # snapshot 내 각 종목 평가 (market proxy 제외)
            for _, srow in snapshot.iterrows():
                ticker = srow["ticker"]
                if ticker in market_tickers:
                    continue
                u = universe.get(ticker)
                if not u or u.get("is_etf") or not u.get("is_active"):
                    continue
                if ticker in open_positions:
                    continue
                if ticker in cooldown_until and ts < cooldown_until[ticker]:
                    continue

                ticker_history = history[history["ticker"] == ticker]
                prev_close = _prev_close_at(closes_map, ticker, ts_et)
                if not prev_close:
                    continue
                # 라이브 _today_rsi 가드 (rsi > 75면 진입 거부) — 시점-안전 lookup
                rsi_v = _rsi_at(rsi_map, ticker, ts_et)
                if rsi_v is not None and rsi_v > RSI_HARD_CAP:
                    continue
                sig = _compute_signal_from_history(ticker_history, ticker, prev_close, ts, market_chg)
                ok, _why = _entry_filter(sig, params)
                if not ok:
                    continue

                # 진입 (슬리피지 +)
                buy_eff = sig["price"] + sig["price"] * (params["slippage_bps"] / 10000.0 / 2.0)
                open_positions[ticker] = {
                    "buy_price": buy_eff,
                    "peak_price": buy_eff,
                    "entry_ts_et": ts_et,
                    "entry_signal": {k: round(float(v), 4) if isinstance(v, (int, float)) else v
                                     for k, v in sig.items() if k != "above_vwap" and k != "day_high_breakout"},
                }
                daily_entry_count[day_key] = entries_today + 1
                entries_today += 1
                if entries_today >= params["max_entries_per_day"]:
                    break

    # ---- 강제 마감: end_dt 시점에 남은 포지션은 마지막 가격으로 청산 ----
    for ticker, pos in list(open_positions.items()):
        last = df[df["ticker"] == ticker]
        if last.empty:
            continue
        last_row = last.iloc[-1]
        cur_price = float(last_row["price"])
        ts_et = last_row["timestamp"].tz_convert(market_tz)
        slip = cur_price * (params["slippage_bps"] / 10000.0 / 2.0)
        sell_eff = cur_price - slip
        gross = (sell_eff - pos["buy_price"]) / pos["buy_price"] * 100.0
        net = gross - params["fee_bps"] / 100.0
        held_min = (ts_et - pos["entry_ts_et"]).total_seconds() / 60.0
        trades.append({
            "ticker": ticker,
            "entry_ts_et": pos["entry_ts_et"].isoformat(),
            "exit_ts_et": ts_et.isoformat(),
            "buy_price": round(pos["buy_price"], 4),
            "sell_price": round(sell_eff, 4),
            "peak_price": round(pos["peak_price"], 4),
            "exit_strategy": "bt_force_close",
            "reason": "end of backtest window",
            "held_minutes": int(held_min),
            "gross_pnl_pct": round(gross, 4),
            "net_pnl_pct": round(net, 4),
        })

    # ---- 요약 ----
    n = len(trades)
    if n > 0:
        nets = [t["net_pnl_pct"] for t in trades]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]
        # exit_strategy 분포
        ex_dist: dict[str, dict] = {}
        for t in trades:
            k = t["exit_strategy"]
            ex_dist.setdefault(k, {"count": 0, "avg_net_pct": 0.0, "wins": 0})
            ex_dist[k]["count"] += 1
            ex_dist[k]["avg_net_pct"] += t["net_pnl_pct"]
            if t["net_pnl_pct"] > 0:
                ex_dist[k]["wins"] += 1
        for k, v in ex_dist.items():
            v["avg_net_pct"] = round(v["avg_net_pct"] / v["count"], 4)
            v["win_rate_pct"] = round(v["wins"] / v["count"] * 100.0, 2)

        summary = {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / n * 100.0, 2),
            "avg_net_pnl_pct": round(sum(nets) / n, 4),
            "median_net_pnl_pct": round(sorted(nets)[n // 2], 4),
            "best_pct": round(max(nets), 4),
            "worst_pct": round(min(nets), 4),
            "sum_net_pct": round(sum(nets), 4),
            "avg_hold_minutes": int(sum(t["held_minutes"] for t in trades) / n),
            "exit_strategy_distribution": ex_dist,
            "period_start": start_dt.isoformat(),
            "period_end": end_dt.isoformat(),
            "data_points": int(len(df)),
        }
    else:
        summary = {"trades": 0, "message": "조건을 만족하는 진입 없음"}

    run_id = uuid.uuid4().hex[:12]
    if save_to_db:
        try:
            supabase.table("intraday_backtests").insert({
                "run_id": run_id,
                "started_at": now_utc.isoformat(),
                "finished_at": datetime.now(pytz.UTC).isoformat(),
                "period_start": start_dt.date().isoformat(),
                "period_end": end_dt.date().isoformat(),
                "config_snapshot": params,
                "summary": summary,
                "trades": trades,
            }).execute()
        except Exception as e:
            logger.warning(f"intraday_backtests 저장 실패: {e}")

    return {"summary": summary, "trades": trades, "run_id": run_id, "params": params}


def format_backtest_summary(result: dict) -> str:
    """Telegram 메시지용 포맷."""
    s = result.get("summary") or {}
    if not s or s.get("trades", 0) == 0:
        return f"📊 <b>인트라데이 백테스트</b>\n결과: {s.get('message', '데이터 부족')}"
    lines = [
        f"📊 <b>인트라데이 백테스트</b> (run {result.get('run_id', '?')})",
        f"기간: {s['period_start'][:10]} ~ {s['period_end'][:10]}",
        f"데이터: {s['data_points']}개 snapshot",
        "",
        f"거래: <b>{s['trades']}</b>건 | 승률 <b>{s['win_rate_pct']:.1f}%</b>",
        f"평균 net PnL: <b>{s['avg_net_pnl_pct']:+.2f}%</b> (median {s['median_net_pnl_pct']:+.2f}%)",
        f"합산: <b>{s['sum_net_pct']:+.2f}%</b> | best {s['best_pct']:+.2f}% / worst {s['worst_pct']:+.2f}%",
        f"평균 보유: {s['avg_hold_minutes']}분",
        "",
        "<b>청산 사유별</b>",
    ]
    for k, v in sorted(s["exit_strategy_distribution"].items(), key=lambda kv: -kv[1]["count"]):
        lines.append(f"  • <code>{k}</code> ×{v['count']}, avg {v['avg_net_pct']:+.2f}%, win {v['win_rate_pct']:.0f}%")
    return "\n".join(lines)


__all__ = ["run_intraday_backtest", "format_backtest_summary"]
