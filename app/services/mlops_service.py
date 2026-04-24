import logging
import math
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytz

from app.core.config import settings
from app.db.supabase import supabase

logger = logging.getLogger(__name__)


def _get_config(key: str, default: float) -> float:
    try:
        resp = supabase.table("system_config").select("value").eq("key", key).limit(1).execute()
        if resp.data:
            return float(resp.data[0]["value"])
    except Exception:
        pass
    return default


def should_run_monthly_retrain(now_et: datetime, last_run_date: str | None) -> bool:
    """
    매월 첫째 일요일 13:00 ET에 재학습 실행.
    같은 날짜 중복 실행 방지(last_run_date).
    """
    if last_run_date == now_et.date().isoformat():
        return False

    is_sunday = now_et.weekday() == 6
    is_first_week = 1 <= now_et.day <= 7
    is_target_time = now_et.hour == 13
    return is_sunday and is_first_week and is_target_time


def run_monthly_retrain_job() -> tuple[bool, str]:
    """
    cron/ml-retrain.sh 실행 래퍼.
    """
    script_path = Path(__file__).resolve().parents[2] / "cron" / "ml-retrain.sh"
    if not script_path.exists():
        return False, f"재학습 스크립트 없음: {script_path}"

    try:
        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60 * 20,  # 20분
        )
        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip() or "unknown error"
            return False, f"재학습 실패: {msg[:300]}"
        return True, "월간 재학습 완료"
    except Exception as e:
        return False, f"재학습 실행 오류: {e}"


def evaluate_promotion_gate() -> tuple[bool, str]:
    """
    model_validation_results 최신 후보를 승격 기준으로 평가.
    테이블/데이터가 없으면 skip.
    """
    sharpe_min = _get_config("promotion_oos_sharpe_min", 0.8)
    mdd_max = _get_config("promotion_oos_mdd_max", 12.0)
    trades_min = _get_config("promotion_oos_trades_min", 80.0)

    try:
        rows = (
            supabase.table("model_validation_results")
            .select("model_version, oos_sharpe, oos_mdd, oos_trades, created_at")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            return False, "검증 후보 모델 없음(승격 스킵)"

        row = rows[0]
        sharpe = float(row.get("oos_sharpe") or 0)
        mdd = float(row.get("oos_mdd") or 999)
        trades = float(row.get("oos_trades") or 0)

        passed = sharpe >= sharpe_min and mdd <= mdd_max and trades >= trades_min
        if passed:
            return True, (
                f"승격 통과(model={row.get('model_version')}, "
                f"Sharpe={sharpe:.2f}, MDD={mdd:.2f}, Trades={trades:.0f})"
            )
        return False, (
            f"승격 차단(model={row.get('model_version')}, "
            f"Sharpe={sharpe:.2f}/{sharpe_min:.2f}, "
            f"MDD={mdd:.2f}/{mdd_max:.2f}, Trades={trades:.0f}/{trades_min:.0f})"
        )
    except Exception as e:
        logger.warning(f"승격 게이트 평가 스킵: {e}")
        return False, f"승격 게이트 평가 오류: {e}"


def evaluate_rollback_trigger() -> tuple[bool, str]:
    """
    20거래일 이동 Sharpe 계산 후 최근 10일 연속(약 2주) 0.2 미만이면 롤백 신호.
    """
    sharpe_threshold = _get_config("rollback_sharpe_threshold", 0.2)
    consecutive_days = int(_get_config("rollback_consecutive_days", 10))

    try:
        trades = (
            supabase.table("trade_records")
            .select("sell_date, profit_loss_pct")
            .eq("status", "sold")
            .order("sell_date", desc=True)
            .limit(400)
            .execute()
            .data
            or []
        )
        if not trades:
            return False, "롤백 판정용 실거래 데이터 없음"

        df = pd.DataFrame(trades)
        df["sell_date"] = pd.to_datetime(df["sell_date"]).dt.date
        df["profit_loss_pct"] = pd.to_numeric(df["profit_loss_pct"], errors="coerce")
        df = df.dropna(subset=["profit_loss_pct"]).sort_values("sell_date")
        if df.empty:
            return False, "유효 수익률 데이터 없음"

        daily = df.groupby("sell_date", as_index=False)["profit_loss_pct"].mean()
        returns = daily["profit_loss_pct"] / 100.0

        roll_mean = returns.rolling(20).mean()
        roll_std = returns.rolling(20).std().replace(0, pd.NA)
        sharpe20 = (roll_mean / roll_std) * math.sqrt(252)
        sharpe20 = sharpe20.dropna()
        if len(sharpe20) < consecutive_days:
            return False, "20일 샤프 샘플 부족"

        tail = sharpe20.tail(consecutive_days)
        if (tail < sharpe_threshold).all():
            return True, f"롤백 조건 충족(최근 {consecutive_days}일 20D Sharpe<{sharpe_threshold:.2f})"
        return False, f"롤백 조건 미충족(최근 Sharpe={sharpe20.iloc[-1]:.2f})"
    except Exception as e:
        logger.warning(f"롤백 트리거 평가 오류: {e}")
        return False, f"롤백 평가 오류: {e}"
