-- LLM 판단 로그 테이블
CREATE TABLE llm_decision_logs (
    id BIGSERIAL PRIMARY KEY,
    decision_date DATE NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    stock_name VARCHAR(100),
    decision VARCHAR(10) NOT NULL,          -- BUY 또는 HOLD
    reason TEXT,                             -- LLM 판단 이유
    market_analysis TEXT,                    -- LLM 시장 전체 분석
    composite_score DECIMAL(10, 4),          -- 종합 점수
    rise_probability DECIMAL(10, 2),         -- ML 상승확률
    rsi DECIMAL(10, 2),
    adx DECIMAL(10, 2),
    vix_value DECIMAL(10, 2),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(decision_date, ticker)
);

CREATE INDEX idx_llm_decision_logs_date ON llm_decision_logs(decision_date);
CREATE INDEX idx_llm_decision_logs_ticker ON llm_decision_logs(ticker);

-- stock_recommendations 테이블에 당일 변동률 컬럼 추가 (패닉셀 판단용)
ALTER TABLE stock_recommendations ADD COLUMN IF NOT EXISTS daily_change_pct DECIMAL(10, 2);
