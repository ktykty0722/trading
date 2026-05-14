"""
주가 예측 모델 (Transformer) - VPS 실행용

사용법:
    python predict.py

필요 환경변수 (.env):
    SUPABASE_URL, SUPABASE_KEY
"""
import logging
import os
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.preprocessing import MinMaxScaler
from supabase import create_client, Client

load_dotenv()

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

import tensorflow as tf
from tensorflow.keras.layers import (
    Add, Dense, Dropout, GlobalAveragePooling1D, Input,
    LayerNormalization, MultiHeadAttention,
)
from tensorflow.keras.models import Model
from tensorflow.keras.optimizers import Adam

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================
# Supabase 연결
# ============================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL / SUPABASE_KEY가 .env에 설정되지 않았습니다.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# 하이퍼파라미터
# ============================================================
LOOKBACK          = 90
FORECAST_HORIZON  = 14
EPOCHS            = 50
BATCH_SIZE        = 32
LEARNING_RATE     = 0.0001
MODEL_VERSION     = "2.0"

ECONOMIC_FEATURES = [
    'inflation_10y', 'yield_spread_10y2y', 'fed_rate', 'consumer_sentiment',
    'unemployment', 'treasury_2y', 'treasury_10y', 'financial_stress',
    'pce', 'cpi', 'mortgage_30y', 'dollar_trade_index', 'm2',
    'household_debt', 'gdp', 'nasdaq_composite',
    'sp500', 'gold', 'dollar_index', 'nasdaq100',
    'spy', 'qqq', 'iwm', 'dia', 'vix',
    'nikkei225', 'shanghai', 'hangseng', 'ftse', 'dax', 'cac40',
    'agg', 'tip', 'lqd', 'jpy_usd', 'cny_usd', 'vnq',
]


# ============================================================
# 데이터 로드
# ============================================================
def _fetch_all(table: str, order_col: str, **filters) -> list:
    """페이지네이션으로 전체 데이터 로드"""
    rows, offset, limit = [], 0, 1000
    while True:
        q = supabase.table(table).select("*").order(order_col, desc=False).limit(limit).offset(offset)
        for k, v in filters.items():
            q = q.eq(k, v)
        resp = q.execute()
        if not resp.data:
            break
        rows.extend(resp.data)
        offset += limit
    return rows


def load_active_tickers() -> list[str]:
    """stock_universe에서 is_active=true, is_etf=false 종목 로드"""
    resp = supabase.table("stock_universe").select("ticker").eq("is_active", True).eq("is_etf", False).execute()
    tickers = [r["ticker"] for r in resp.data] if resp.data else []
    if not tickers:
        raise ValueError("활성 종목이 없습니다. stock_universe 테이블을 확인하세요.")
    logger.info(f"활성 종목 {len(tickers)}개 로드: {tickers[:5]}...")
    return tickers


def load_stock_prices(tickers: list[str]) -> pd.DataFrame:
    """stock_daily_prices → Wide format DataFrame (날짜 x 종목)"""
    logger.info("stock_daily_prices 로드 중...")
    rows = _fetch_all("stock_daily_prices", "date")
    if not rows:
        raise ValueError("stock_daily_prices 데이터가 없습니다.")

    df = pd.DataFrame(rows)[["date", "ticker", "close"]]
    df = df[df["ticker"].isin(tickers)]
    df["date"] = pd.to_datetime(df["date"])

    wide = df.pivot(index="date", columns="ticker", values="close")
    wide = wide.sort_index().ffill().bfill()
    # tickers 순서 유지 (없는 종목은 NaN)
    wide = wide.reindex(columns=tickers)
    logger.info(f"주가 데이터: {wide.shape[0]}일 x {wide.shape[1]}종목")
    return wide


def load_economic_data() -> pd.DataFrame:
    """economic_indicators → DataFrame"""
    logger.info("economic_indicators 로드 중...")
    rows = _fetch_all("economic_indicators", "date")
    if not rows:
        raise ValueError("economic_indicators 데이터가 없습니다.")

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    available = [c for c in ECONOMIC_FEATURES if c in df.columns]
    missing = [c for c in ECONOMIC_FEATURES if c not in df.columns]
    if missing:
        logger.warning(f"경제지표 컬럼 누락: {missing}")

    df = df[available].ffill().bfill()
    logger.info(f"경제지표 데이터: {df.shape[0]}일 x {df.shape[1]}컬럼")
    return df


def prepare_data(tickers: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DatetimeIndex]:
    """주가 + 경제지표를 날짜 기준으로 정렬/정합"""
    stock_df = load_stock_prices(tickers)
    econ_df  = load_economic_data()

    common_dates = stock_df.index.intersection(econ_df.index)
    if len(common_dates) < LOOKBACK + FORECAST_HORIZON + 10:
        raise ValueError(f"공통 날짜 부족: {len(common_dates)}일")

    stock_df = stock_df.loc[common_dates]
    econ_df  = econ_df.loc[common_dates]

    # 결측 비율 높은 종목 제거
    valid_tickers = [t for t in tickers if stock_df[t].isna().mean() < 0.5]
    if len(valid_tickers) < len(tickers):
        dropped = set(tickers) - set(valid_tickers)
        logger.warning(f"결측률 50% 초과로 제외된 종목: {dropped}")
    stock_df = stock_df[valid_tickers].ffill().bfill()

    return stock_df, econ_df, common_dates


# ============================================================
# Transformer 모델
# ============================================================
def _transformer_encoder(inputs, num_heads: int, ff_dim: int, dropout: float = 0.1):
    attn = MultiHeadAttention(num_heads=num_heads, key_dim=inputs.shape[-1])(inputs, inputs)
    attn = Dropout(dropout)(attn)
    attn = LayerNormalization(epsilon=1e-6)(Add()([inputs, attn]))

    ffn = Dense(ff_dim, activation="relu")(attn)
    ffn = Dropout(dropout)(Dense(inputs.shape[-1])(ffn))
    return LayerNormalization(epsilon=1e-6)(Add()([attn, ffn]))


def build_model(stock_shape, econ_shape, target_size: int) -> Model:
    stock_in = Input(shape=stock_shape)
    s = stock_in
    for _ in range(4):
        s = _transformer_encoder(s, num_heads=8, ff_dim=256)
    s = Dense(64, activation="relu")(s)

    econ_in = Input(shape=econ_shape)
    e = econ_in
    for _ in range(4):
        e = _transformer_encoder(e, num_heads=8, ff_dim=256)
    e = Dense(64, activation="relu")(e)

    x = Add()([s, e])
    x = Dense(128, activation="relu")(x)
    x = Dropout(0.2)(x)
    x = GlobalAveragePooling1D()(x)
    out = Dense(target_size)(x)

    return Model(inputs=[stock_in, econ_in], outputs=out)


# ============================================================
# 평가 / 분석
# ============================================================
def evaluate_predictions(pred: np.ndarray, actual: np.ndarray, tickers: list[str]) -> pd.DataFrame:
    records = []
    for i, ticker in enumerate(tickers):
        p = pred[:, i]
        a = actual[:, i]
        valid = ~np.isnan(p) & ~np.isnan(a)
        if valid.sum() == 0:
            continue
        p, a = p[valid], a[valid]
        mae  = mean_absolute_error(a, p)
        mse  = mean_squared_error(a, p)
        mape = float(np.mean(np.abs((a - p) / np.where(a == 0, 1e-8, a))) * 100)
        records.append({
            "ticker": ticker,
            "mae":  round(mae,  4),
            "mse":  round(mse,  4),
            "rmse": round(mse ** 0.5, 4),
            "mape": round(mape, 4),
            "accuracy": round(100 - mape, 4),
        })
    return pd.DataFrame(records)


def compute_rise_probability(pred_last: np.ndarray, actual_last: np.ndarray, tickers: list[str]) -> pd.DataFrame:
    records = []
    for i, ticker in enumerate(tickers):
        p, a = pred_last[i], actual_last[i]
        rise_pct = float((p - a) / a * 100) if a != 0 else float("nan")
        records.append({"ticker": ticker, "predicted_price": float(p), "actual_price": float(a), "rise_probability": rise_pct})
    return pd.DataFrame(records)


# ============================================================
# Supabase 저장 (Long format → stock_predictions)
# ============================================================
def save_predictions(today: str, tickers: list[str], eval_df: pd.DataFrame, rise_df: pd.DataFrame):
    merged = pd.merge(eval_df, rise_df, on="ticker", how="outer")
    records = []
    for _, row in merged.iterrows():
        records.append({
            "date":            today,
            "ticker":          row["ticker"],
            "predicted_price": row.get("predicted_price"),
            "actual_price":    row.get("actual_price"),
            "rise_probability":row.get("rise_probability"),
            "accuracy":        row.get("accuracy"),
            "mae":             row.get("mae"),
            "mse":             row.get("mse"),
            "rmse":            row.get("rmse"),
            "mape":            row.get("mape"),
            "model_version":   MODEL_VERSION,
        })

    try:
        supabase.table("stock_predictions").upsert(records, on_conflict="date,ticker").execute()
        logger.info(f"stock_predictions: {len(records)}건 저장 완료 (date={today})")
    except Exception as e:
        logger.error(f"stock_predictions 저장 오류: {e}")


# ============================================================
# 메인
# ============================================================
def main():
    total_start = time.time()
    logger.info(f"TensorFlow {tf.__version__}, GPU: {tf.config.list_physical_devices('GPU')}")

    tickers     = load_active_tickers()
    stock_df, econ_df, dates = prepare_data(tickers)
    econ_cols   = list(econ_df.columns)

    # 스케일링
    stock_scaler = MinMaxScaler()
    econ_scaler  = MinMaxScaler()
    stock_scaled = stock_scaler.fit_transform(stock_df.values)
    econ_scaled  = econ_scaler.fit_transform(econ_df.values)

    # 학습 데이터 생성
    X_stock, X_econ, y = [], [], []
    for i in range(LOOKBACK, len(stock_scaled) - FORECAST_HORIZON):
        X_stock.append(stock_scaled[i - LOOKBACK:i])
        X_econ.append(econ_scaled[i - LOOKBACK:i])
        y.append(stock_scaled[i + FORECAST_HORIZON - 1])

    X_stock = np.array(X_stock)
    X_econ  = np.array(X_econ)
    y       = np.array(y)
    logger.info(f"학습 샘플: {len(y)}개")

    # 모델 빌드 & 학습
    model = build_model(
        stock_shape=(LOOKBACK, len(tickers)),
        econ_shape=(LOOKBACK, len(econ_cols)),
        target_size=len(tickers),
    )
    model.compile(optimizer=Adam(learning_rate=LEARNING_RATE), loss="mse", metrics=["mae"])

    logger.info(f"학습 시작 ({EPOCHS} epochs)")
    t_train = time.time()
    model.fit([X_stock, X_econ], y, epochs=EPOCHS, batch_size=BATCH_SIZE, verbose=1)
    logger.info(f"학습 완료: {time.time() - t_train:.1f}초")

    # 전체 예측
    X_stock_full = np.array([stock_scaled[i - LOOKBACK:i] for i in range(LOOKBACK, len(stock_scaled))])
    X_econ_full  = np.array([econ_scaled[i - LOOKBACK:i]  for i in range(LOOKBACK, len(econ_scaled))])

    pred_scaled = model.predict([X_stock_full, X_econ_full], verbose=0)
    pred_actual = stock_scaler.inverse_transform(pred_scaled)

    pred_len    = len(pred_actual)
    actual_raw  = stock_df.values[LOOKBACK:LOOKBACK + pred_len]

    # 평가 (FORECAST_HORIZON 이후 실제값과 비교)
    eval_len    = pred_len - FORECAST_HORIZON
    eval_df     = evaluate_predictions(
        pred_actual[:eval_len],
        actual_raw[FORECAST_HORIZON:FORECAST_HORIZON + eval_len],
        tickers,
    )
    avg_acc = eval_df["accuracy"].mean() if not eval_df.empty else 0.0
    logger.info(f"평균 정확도: {avg_acc:.2f}%")

    # 마지막 예측값 기반 상승확률 계산
    rise_df = compute_rise_probability(pred_actual[-1], actual_raw[-1], tickers)

    today = datetime.now().strftime("%Y-%m-%d")
    save_predictions(today, tickers, eval_df, rise_df)

    total_time = time.time() - total_start
    logger.info(f"총 소요시간: {total_time:.1f}초 ({total_time / 60:.1f}분)")
    logger.info(f"평균 정확도: {avg_acc:.2f}%")

    logger.info("\n[종목별 상승 예측]")
    for _, row in rise_df.sort_values("rise_probability", ascending=False).iterrows():
        logger.info(f"  {row['ticker']:6s}  현재:{row['actual_price']:8.2f}  예측:{row['predicted_price']:8.2f}  상승률:{row['rise_probability']:+.2f}%")


if __name__ == "__main__":
    # CLI entrypoint — service 모듈을 통해 실행 (scheduler와 동일 경로)
    try:
        from app.services.ml_prediction_service import run_ml_prediction
        result = run_ml_prediction()
        if not result.get("success"):
            logger.error(result.get("message"))
            sys.exit(1)
        logger.info(result.get("message"))
    except ImportError:
        # app 패키지 import 실패 시 기존 main() 폴백
        main()
