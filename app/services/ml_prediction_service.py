"""
ML 예측 서비스.

predict.py의 학습/예측 로직을 함수 형태로 노출하여
scheduler에서 직접 호출 가능하게 한다.

predict.py는 CLI entrypoint로만 유지하며 내부에서 이 함수를 호출한다.
"""
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


def run_ml_prediction(epochs: Optional[int] = None) -> dict:
    """
    Transformer 기반 가격 예측 학습+추론을 실행하고
    stock_predictions 테이블에 결과를 upsert.

    Args:
        epochs: 학습 epoch 수 (None이면 predict.EPOCHS 기본값 사용)

    Returns:
        {
            "success": bool,
            "message": str,
            "ticker_count": int,
            "average_accuracy": float,
            "duration_seconds": float,
        }
    """
    started = time.time()
    try:
        # predict.py의 헤비 의존성(TensorFlow 등)을 lazy import — 임포트 비용 분리
        import predict  # noqa: WPS433
        import numpy as np  # noqa: WPS433
        from datetime import datetime  # noqa: WPS433
        from sklearn.preprocessing import MinMaxScaler  # noqa: WPS433

        logger.info("ML 예측 실행 시작")

        tickers = predict.load_active_tickers()
        stock_df, econ_df, _dates = predict.prepare_data(tickers)
        econ_cols = list(econ_df.columns)
        valid_tickers = list(stock_df.columns)

        # 스케일링
        stock_scaler = MinMaxScaler()
        econ_scaler = MinMaxScaler()
        stock_scaled = stock_scaler.fit_transform(stock_df.values)
        econ_scaled = econ_scaler.fit_transform(econ_df.values)

        # 학습 샘플 생성
        lookback = predict.LOOKBACK
        horizon = predict.FORECAST_HORIZON
        X_stock, X_econ, y = [], [], []
        for i in range(lookback, len(stock_scaled) - horizon):
            X_stock.append(stock_scaled[i - lookback:i])
            X_econ.append(econ_scaled[i - lookback:i])
            y.append(stock_scaled[i + horizon - 1])

        X_stock = np.array(X_stock)
        X_econ = np.array(X_econ)
        y = np.array(y)

        model = predict.build_model(
            stock_shape=(lookback, len(valid_tickers)),
            econ_shape=(lookback, len(econ_cols)),
            target_size=len(valid_tickers),
        )
        from tensorflow.keras.optimizers import Adam  # noqa: WPS433
        model.compile(optimizer=Adam(learning_rate=predict.LEARNING_RATE),
                      loss="mse", metrics=["mae"])

        ep = int(epochs) if epochs else predict.EPOCHS
        logger.info(f"학습 시작 (epochs={ep}, samples={len(y)})")
        model.fit(
            [X_stock, X_econ], y,
            epochs=ep, batch_size=predict.BATCH_SIZE, verbose=0,
        )

        # 전체 예측
        X_stock_full = np.array(
            [stock_scaled[i - lookback:i] for i in range(lookback, len(stock_scaled))]
        )
        X_econ_full = np.array(
            [econ_scaled[i - lookback:i] for i in range(lookback, len(econ_scaled))]
        )

        pred_scaled = model.predict([X_stock_full, X_econ_full], verbose=0)
        pred_actual = stock_scaler.inverse_transform(pred_scaled)

        pred_len = len(pred_actual)
        actual_raw = stock_df.values[lookback:lookback + pred_len]

        eval_len = pred_len - horizon
        eval_df = predict.evaluate_predictions(
            pred_actual[:eval_len],
            actual_raw[horizon:horizon + eval_len],
            valid_tickers,
        )
        avg_acc = float(eval_df["accuracy"].mean()) if not eval_df.empty else 0.0

        rise_df = predict.compute_rise_probability(
            pred_actual[-1], actual_raw[-1], valid_tickers
        )

        today = datetime.now().strftime("%Y-%m-%d")
        predict.save_predictions(today, valid_tickers, eval_df, rise_df)

        elapsed = time.time() - started
        logger.info(
            f"ML 예측 완료: ticker={len(valid_tickers)}, "
            f"avg_acc={avg_acc:.2f}%, {elapsed:.1f}s"
        )
        return {
            "success": True,
            "message": f"{len(valid_tickers)}종목 예측 완료 (정확도 {avg_acc:.2f}%)",
            "ticker_count": len(valid_tickers),
            "average_accuracy": avg_acc,
            "duration_seconds": round(elapsed, 1),
        }
    except Exception as e:
        elapsed = time.time() - started
        logger.exception(f"ML 예측 실행 실패: {e}")
        return {
            "success": False,
            "message": f"ML 예측 실패: {e}",
            "ticker_count": 0,
            "average_accuracy": 0.0,
            "duration_seconds": round(elapsed, 1),
        }
