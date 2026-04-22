"""
predict_colab.py 실행 후 검증용 코드 (Colab 셀에 붙여넣기)

DB에 저장된 predicted_stocks 데이터를 불러와서
2024~2025 구간의 예측이 실제로 얼마나 맞았는지 검증합니다.
"""

import pandas as pd
import numpy as np
from sklearn.metrics import mean_absolute_error, mean_squared_error

# ============================================================
# 1) predicted_stocks 로드 (이미 predict_colab.py가 저장한 데이터)
# ============================================================
print("predicted_stocks 테이블에서 데이터 로드 중...")

all_data = []
offset = 0
while True:
    response = supabase.table("predicted_stocks").select("*").order("날짜", desc=False).limit(1000).offset(offset).execute()
    if not response.data:
        break
    all_data.extend(response.data)
    offset += 1000

result_data = pd.DataFrame(all_data)
result_data['날짜'] = pd.to_datetime(result_data['날짜'])
print(f"  총 {len(result_data)}개 레코드 로드 완료")
print(f"  기간: {result_data['날짜'].min().date()} ~ {result_data['날짜'].max().date()}")

# ============================================================
# 2) 2024~2025 구간만 필터 (모델이 학습에 사용한 데이터도 포함되므로
#    최근 구간만 보는 게 더 의미 있음)
# ============================================================
target_columns = [
    '애플', '마이크로소프트', '아마존', '구글 A', '구글 C', '메타',
    '테슬라', '엔비디아', '코스트코', '넷플릭스', '페이팔', '인텔', '시스코', '컴캐스트',
    '펩시코', '암젠', '허니웰 인터내셔널', '스타벅스', '몬델리즈', '마이크론', '브로드컴',
    '어도비', '텍사스 인스트루먼트', 'AMD', '어플라이드 머티리얼즈', 'S&P 500 ETF', 'QQQ ETF'
]

forecast_horizon = 14

# 2024년 이후 데이터만 필터
recent = result_data[result_data['날짜'] >= '2024-01-01'].reset_index(drop=True)
print(f"\n  2024~2025 검증 구간: {len(recent)}일")

# ============================================================
# 3) 종목별 검증: 예측 vs 실제 (14일 후)
# ============================================================
print("\n" + "=" * 70)
print("검증 결과: 2024~2025 구간에서 14일 후 예측이 실제와 얼마나 맞았는가?")
print("=" * 70)

metrics = []
for col in target_columns:
    pred_col = f'{col}_Predicted'
    actual_col = f'{col}_Actual'

    if pred_col not in recent.columns or actual_col not in recent.columns:
        continue

    predicted = recent[pred_col]
    actual = recent[actual_col].shift(-forecast_horizon)

    valid = ~predicted.isna() & ~actual.isna()
    predicted = predicted[valid]
    actual = actual[valid]

    if len(predicted) == 0:
        continue

    mae = mean_absolute_error(actual, predicted)
    mape = (abs((actual - predicted) / actual).mean()) * 100
    accuracy = 100 - mape

    # 방향성 정확도: 예측이 "오를 것/내릴 것"을 맞췄는지
    actual_direction = (actual.values > recent[actual_col][valid].values)  # 14일 후 실제로 올랐는지
    pred_direction = (predicted.values > recent[actual_col][valid].values)  # 예측이 오를 것이라 했는지
    direction_accuracy = (actual_direction == pred_direction).mean() * 100

    metrics.append({
        'Stock': col,
        'MAE': round(mae, 2),
        'MAPE (%)': round(mape, 2),
        'Accuracy (%)': round(accuracy, 2),
        'Direction (%)': round(direction_accuracy, 2),
    })

eval_df = pd.DataFrame(metrics)
eval_df = eval_df.sort_values('Accuracy (%)', ascending=False)

print(f"\n{'Stock':>20s} {'Accuracy':>10s} {'Direction':>10s} {'MAE':>10s} {'MAPE':>8s}")
print("-" * 62)
for _, row in eval_df.iterrows():
    print(f"{row['Stock']:>20s} {row['Accuracy (%)']:>9.2f}% {row['Direction (%)']:>9.2f}% {row['MAE']:>10.2f} {row['MAPE (%)']:>7.2f}%")

avg_acc = eval_df['Accuracy (%)'].mean()
avg_dir = eval_df['Direction (%)'].mean()

print("-" * 62)
print(f"{'평균':>20s} {avg_acc:>9.2f}% {avg_dir:>9.2f}%")
print()
print(f"  Accuracy: 가격 오차 기반 정확도 (100 - MAPE)")
print(f"  Direction: 상승/하락 방향을 맞춘 비율")
print()
if avg_dir > 55:
    print(f"  → 방향 예측 {avg_dir:.1f}%: 랜덤(50%)보다 유의미하게 높음")
elif avg_dir > 50:
    print(f"  → 방향 예측 {avg_dir:.1f}%: 랜덤(50%)과 큰 차이 없음")
else:
    print(f"  → 방향 예측 {avg_dir:.1f}%: 랜덤(50%)보다 낮음 - 모델 신뢰도 부족")



