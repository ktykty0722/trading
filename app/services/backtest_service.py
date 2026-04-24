import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

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


@dataclass
class CostModel:
    slippage_bps_one_way: float
    fee_bps_one_way: float

    @property
    def total_roundtrip_pct(self) -> float:
        # bps -> %
        return (self.slippage_bps_one_way * 2 + self.fee_bps_one_way * 2) / 100.0


class BacktestService:
    def _load_closed_trades(self) -> pd.DataFrame:
        rows = (
            supabase.table("trade_records")
            .select("ticker, buy_date, sell_date, profit_loss_pct, composite_score, status")
            .eq("status", "sold")
            .order("sell_date")
            .execute()
            .data
            or []
        )
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["buy_date"] = pd.to_datetime(df["buy_date"], errors="coerce")
        df["sell_date"] = pd.to_datetime(df["sell_date"], errors="coerce")
        df["profit_loss_pct"] = pd.to_numeric(df["profit_loss_pct"], errors="coerce")
        df["composite_score"] = pd.to_numeric(df["composite_score"], errors="coerce")
        df = df.dropna(subset=["buy_date", "sell_date", "profit_loss_pct"])
        if df.empty:
            return df
        df["holding_days"] = (df["sell_date"].dt.date - df["buy_date"].dt.date).apply(lambda x: x.days)
        return df

    @staticmethod
    def _metrics(returns_pct: pd.Series) -> dict:
        if returns_pct.empty:
            return {"trades": 0, "win_rate": None, "avg_return": None, "sharpe": None, "max_drawdown": None}

        ret = returns_pct / 100.0
        equity = (1 + ret).cumprod()
        peak = equity.cummax()
        drawdown = (equity - peak) / peak

        std = ret.std(ddof=0)
        sharpe = (ret.mean() / std) * np.sqrt(252) if std and std > 0 else None
        win_rate = float((returns_pct > 0).mean() * 100)
        avg_return = float(returns_pct.mean())
        max_drawdown = float(abs(drawdown.min()) * 100) if not drawdown.empty else None

        return {
            "trades": int(len(returns_pct)),
            "win_rate": round(win_rate, 2) if win_rate is not None else None,
            "avg_return": round(avg_return, 4) if avg_return is not None else None,
            "sharpe": round(float(sharpe), 4) if sharpe is not None else None,
            "max_drawdown": round(max_drawdown, 4) if max_drawdown is not None else None,
        }

    def run_phase4_validation(self) -> dict:
        df = self._load_closed_trades()
        if df.empty:
            return {"message": "검증할 체결 데이터가 없습니다.", "results": []}

        cost_model = CostModel(
            slippage_bps_one_way=_get_config("backtest_slippage_bps_one_way", 15.0),
            fee_bps_one_way=_get_config("backtest_fee_bps_one_way", 1.0),
        )
        cost_pct = cost_model.total_roundtrip_pct
        df["net_return_pct"] = df["profit_loss_pct"] - cost_pct

        score_grid = [0.2, 0.3, 0.4, 0.5]
        hold_grid = [5, 7, 10]

        sweep_results = []
        for score_th in score_grid:
            for hold_days in hold_grid:
                subset = df[(df["composite_score"].fillna(-1) >= score_th) & (df["holding_days"] <= hold_days)]
                metrics = self._metrics(subset["net_return_pct"])
                sweep_results.append(
                    {
                        "score_threshold": score_th,
                        "max_holding_days": hold_days,
                        **metrics,
                    }
                )

        # Walk-forward (단순 70/30 분할)
        split_idx = int(len(df) * 0.7)
        in_sample = df.iloc[:split_idx]
        oos = df.iloc[split_idx:]

        walk_forward = {
            "in_sample": self._metrics(in_sample["net_return_pct"]),
            "out_of_sample": self._metrics(oos["net_return_pct"]),
        }

        return {
            "message": "Phase4 백테스트 검증 완료",
            "cost_model": {
                "slippage_bps_one_way": cost_model.slippage_bps_one_way,
                "fee_bps_one_way": cost_model.fee_bps_one_way,
                "roundtrip_cost_pct": round(cost_pct, 4),
            },
            "walk_forward": walk_forward,
            "parameter_sweep": sorted(
                sweep_results,
                key=lambda x: (x["sharpe"] if x["sharpe"] is not None else -999, x["avg_return"] if x["avg_return"] is not None else -999),
                reverse=True,
            ),
        }
