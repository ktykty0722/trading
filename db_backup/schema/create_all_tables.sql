-- ============================================================
-- Supabase 전체 테이블 생성 SQL
-- 수강생용: 이 파일을 Supabase SQL Editor에서 실행하세요
-- ============================================================

-- 1. economic_and_stock_data: 경제지표 + 주가 데이터 (핵심 데이터)
CREATE TABLE IF NOT EXISTS economic_and_stock_data (
    id BIGSERIAL PRIMARY KEY,
    "날짜" DATE NOT NULL UNIQUE,

    -- Yahoo Finance 지표
    "나스닥 종합지수" FLOAT8,
    "S&P 500 지수" FLOAT8,
    "금 가격" FLOAT8,
    "달러 인덱스" FLOAT8,
    "나스닥 100" FLOAT8,
    "S&P 500 ETF" FLOAT8,
    "QQQ ETF" FLOAT8,
    "러셀 2000 ETF" FLOAT8,
    "다우 존스 ETF" FLOAT8,
    "VIX 지수" FLOAT8,
    "닛케이 225" FLOAT8,
    "상해종합" FLOAT8,
    "항셍" FLOAT8,
    "영국 FTSE" FLOAT8,
    "독일 DAX" FLOAT8,
    "프랑스 CAC 40" FLOAT8,
    "미국 전체 채권시장 ETF" FLOAT8,
    "TIPS ETF" FLOAT8,
    "투자등급 회사채 ETF" FLOAT8,
    "달러/엔" FLOAT8,
    "달러/위안" FLOAT8,
    "미국 리츠 ETF" FLOAT8,

    -- 나스닥 100 상위 종목 주가
    "애플" FLOAT8,
    "마이크로소프트" FLOAT8,
    "아마존" FLOAT8,
    "구글 A" FLOAT8,
    "구글 C" FLOAT8,
    "메타" FLOAT8,
    "테슬라" FLOAT8,
    "엔비디아" FLOAT8,
    "코스트코" FLOAT8,
    "넷플릭스" FLOAT8,
    "페이팔" FLOAT8,
    "인텔" FLOAT8,
    "시스코" FLOAT8,
    "컴캐스트" FLOAT8,
    "펩시코" FLOAT8,
    "암젠" FLOAT8,
    "허니웰 인터내셔널" FLOAT8,
    "스타벅스" FLOAT8,
    "몬델리즈" FLOAT8,
    "마이크론" FLOAT8,
    "브로드컴" FLOAT8,
    "어도비" FLOAT8,
    "텍사스 인스트루먼트" FLOAT8,
    "AMD" FLOAT8,
    "어플라이드 머티리얼즈" FLOAT8,

    -- FRED 경제지표
    "10년 기대 인플레이션율" FLOAT8,
    "장단기 금리차" FLOAT8,
    "기준금리" FLOAT8,
    "미시간대 소비자 심리지수" FLOAT8,
    "실업률" FLOAT8,
    "2년 만기 미국 국채 수익률" FLOAT8,
    "10년 만기 미국 국채 수익률" FLOAT8,
    "금융스트레스지수" FLOAT8,
    "개인 소비 지출" FLOAT8,
    "소비자 물가지수" FLOAT8,
    "5년 변동금리 모기지" FLOAT8,
    "미국 달러 환율" FLOAT8,
    "통화 공급량 M2" FLOAT8,
    "가계 부채 비율" FLOAT8,
    "GDP 성장률" FLOAT8
);

CREATE INDEX IF NOT EXISTS idx_economic_날짜 ON economic_and_stock_data ("날짜");


-- 2. stock_analysis_results: ML 모델 성능 분석 결과
CREATE TABLE IF NOT EXISTS stock_analysis_results (
    id BIGSERIAL PRIMARY KEY,
    "Stock" TEXT NOT NULL,
    "MAE" FLOAT8,
    "MSE" FLOAT8,
    "RMSE" FLOAT8,
    "MAPE (%)" FLOAT8,
    "Accuracy (%)" FLOAT8,
    "Last Actual Price" FLOAT8,
    "Predicted Future Price" FLOAT8,
    "Predicted Change (%)" FLOAT8,
    "Direction" TEXT,
    "Rise Probability (%)" FLOAT8,
    "Analysis Date" TIMESTAMPTZ DEFAULT NOW(),
    "prediction_date" DATE,
    "actual_future_price" FLOAT8
);


-- 3. predicted_stocks: ML 예측값 vs 실제값 (종목별)
CREATE TABLE IF NOT EXISTS predicted_stocks (
    id BIGSERIAL PRIMARY KEY,
    "날짜" DATE NOT NULL,

    -- 각 종목의 예측값과 실제값
    "애플_Predicted" FLOAT8, "애플_Actual" FLOAT8,
    "마이크로소프트_Predicted" FLOAT8, "마이크로소프트_Actual" FLOAT8,
    "아마존_Predicted" FLOAT8, "아마존_Actual" FLOAT8,
    "구글 A_Predicted" FLOAT8, "구글 A_Actual" FLOAT8,
    "구글 C_Predicted" FLOAT8, "구글 C_Actual" FLOAT8,
    "메타_Predicted" FLOAT8, "메타_Actual" FLOAT8,
    "테슬라_Predicted" FLOAT8, "테슬라_Actual" FLOAT8,
    "엔비디아_Predicted" FLOAT8, "엔비디아_Actual" FLOAT8,
    "코스트코_Predicted" FLOAT8, "코스트코_Actual" FLOAT8,
    "넷플릭스_Predicted" FLOAT8, "넷플릭스_Actual" FLOAT8,
    "페이팔_Predicted" FLOAT8, "페이팔_Actual" FLOAT8,
    "인텔_Predicted" FLOAT8, "인텔_Actual" FLOAT8,
    "시스코_Predicted" FLOAT8, "시스코_Actual" FLOAT8,
    "컴캐스트_Predicted" FLOAT8, "컴캐스트_Actual" FLOAT8,
    "펩시코_Predicted" FLOAT8, "펩시코_Actual" FLOAT8,
    "암젠_Predicted" FLOAT8, "암젠_Actual" FLOAT8,
    "허니웰 인터내셔널_Predicted" FLOAT8, "허니웰 인터내셔널_Actual" FLOAT8,
    "스타벅스_Predicted" FLOAT8, "스타벅스_Actual" FLOAT8,
    "몬델리즈_Predicted" FLOAT8, "몬델리즈_Actual" FLOAT8,
    "마이크론_Predicted" FLOAT8, "마이크론_Actual" FLOAT8,
    "브로드컴_Predicted" FLOAT8, "브로드컴_Actual" FLOAT8,
    "어도비_Predicted" FLOAT8, "어도비_Actual" FLOAT8,
    "텍사스 인스트루먼트_Predicted" FLOAT8, "텍사스 인스트루먼트_Actual" FLOAT8,
    "AMD_Predicted" FLOAT8, "AMD_Actual" FLOAT8,
    "어플라이드 머티리얼즈_Predicted" FLOAT8, "어플라이드 머티리얼즈_Actual" FLOAT8,
    "S&P 500 ETF_Predicted" FLOAT8, "S&P 500 ETF_Actual" FLOAT8,
    "QQQ ETF_Predicted" FLOAT8, "QQQ ETF_Actual" FLOAT8
);

CREATE INDEX IF NOT EXISTS idx_predicted_날짜 ON predicted_stocks ("날짜");


-- 4. stock_recommendations: 기술적 분석 결과 (일별)
CREATE TABLE IF NOT EXISTS stock_recommendations (
    id BIGSERIAL PRIMARY KEY,
    "날짜" DATE NOT NULL,
    "종목" TEXT NOT NULL,
    "SMA20" FLOAT8,
    "SMA50" FLOAT8,
    "골든_크로스" BOOLEAN DEFAULT FALSE,
    "RSI" FLOAT8,
    "MACD" FLOAT8,
    "Signal" FLOAT8,
    "MACD_매수_신호" BOOLEAN DEFAULT FALSE,
    "추천_여부" BOOLEAN DEFAULT FALSE,
    "volume_ratio" FLOAT8,
    "adx" FLOAT8,
    "daily_change_pct" FLOAT8
);

CREATE INDEX IF NOT EXISTS idx_recommendations_날짜 ON stock_recommendations ("날짜");


-- 5. ticker_sentiment_analysis: 뉴스 감성 분석 결과
CREATE TABLE IF NOT EXISTS ticker_sentiment_analysis (
    id BIGSERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    average_sentiment_score FLOAT8,
    article_count INTEGER,
    calculation_date TEXT
);


-- 6. trade_records: 매매 기록
CREATE TABLE IF NOT EXISTS trade_records (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    stock_name TEXT,
    buy_price FLOAT8 NOT NULL,
    buy_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    quantity INTEGER NOT NULL,
    exchange_code TEXT,
    atr FLOAT8,
    take_profit_price FLOAT8,
    stop_loss_price FLOAT8,
    status TEXT NOT NULL DEFAULT 'holding',
    sell_price FLOAT8,
    sell_date TIMESTAMPTZ,
    sell_reason TEXT,
    profit_loss FLOAT8,
    profit_loss_pct FLOAT8,
    composite_score FLOAT8,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    holding_quantity INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trade_records_status ON trade_records (status);
CREATE INDEX IF NOT EXISTS idx_trade_records_ticker ON trade_records (ticker);


-- 7. llm_decision_logs: LLM 매수 판단 기록
CREATE TABLE IF NOT EXISTS llm_decision_logs (
    id BIGSERIAL PRIMARY KEY,
    decision_date DATE NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    stock_name VARCHAR(100),
    decision VARCHAR(10) NOT NULL,
    reason TEXT,
    market_analysis TEXT,
    composite_score DECIMAL(10,4),
    rise_probability DECIMAL(10,2),
    rsi DECIMAL(10,2),
    adx DECIMAL(10,2),
    vix_value DECIMAL(10,2),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(decision_date, ticker)
);


-- 8. access_tokens: KIS API 토큰 캐시
CREATE TABLE IF NOT EXISTS access_tokens (
    id BIGSERIAL PRIMARY KEY,
    token_type TEXT NOT NULL DEFAULT 'kis_mock',
    access_token TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
