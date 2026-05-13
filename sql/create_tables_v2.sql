-- ============================================================
-- 주식 자동 거래 시스템 v2 DB 스키마
-- 핵심 원칙: 종목명은 컬럼명이 아닌 row 값으로 저장 (Long format)
-- Supabase SQL Editor에서 순서대로 실행하세요
-- ============================================================


-- ============================================================
-- 1. stock_universe: 종목 마스터
--    종목 추가/제거 = 이 테이블 행 추가/is_active 변경만으로 완료
--    코드 수정 및 DB 스키마 변경 불필요
-- ============================================================
CREATE TABLE IF NOT EXISTS stock_universe (
    id          BIGSERIAL PRIMARY KEY,
    ticker      VARCHAR(10)  UNIQUE NOT NULL,
    name_ko     VARCHAR(50)  NOT NULL,
    exchange    VARCHAR(10)  NOT NULL,           -- NASD / NYSE / AMEX
    sector      VARCHAR(50),
    is_active   BOOLEAN      NOT NULL DEFAULT true,
    is_etf      BOOLEAN      NOT NULL DEFAULT false,
    added_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 기본 종목 데이터 (나스닥 25 + ETF 2)
INSERT INTO stock_universe (ticker, name_ko, exchange, sector, is_active, is_etf) VALUES
    ('AAPL',  '애플',                  'NASD', 'Technology',        true, false),
    ('MSFT',  '마이크로소프트',         'NASD', 'Technology',        true, false),
    ('AMZN',  '아마존',                'NASD', 'Consumer Cyclical', true, false),
    ('GOOGL', '구글 A',                'NASD', 'Communication',     true, false),
    ('GOOG',  '구글 C',                'NASD', 'Communication',     true, false),
    ('META',  '메타',                  'NASD', 'Communication',     true, false),
    ('TSLA',  '테슬라',                'NASD', 'Consumer Cyclical', true, false),
    ('NVDA',  '엔비디아',              'NASD', 'Technology',        true, false),
    ('COST',  '코스트코',              'NASD', 'Consumer Defensive',true, false),
    ('NFLX',  '넷플릭스',              'NASD', 'Communication',     true, false),
    ('PYPL',  '페이팔',                'NASD', 'Financial Services',true, false),
    ('INTC',  '인텔',                  'NASD', 'Technology',        true, false),
    ('CSCO',  '시스코',                'NASD', 'Technology',        true, false),
    ('CMCSA', '컴캐스트',              'NASD', 'Communication',     true, false),
    ('PEP',   '펩시코',                'NASD', 'Consumer Defensive',true, false),
    ('AMGN',  '암젠',                  'NASD', 'Healthcare',        true, false),
    ('HON',   '허니웰 인터내셔널',     'NASD', 'Industrials',       true, false),
    ('SBUX',  '스타벅스',              'NASD', 'Consumer Cyclical', true, false),
    ('MDLZ',  '몬델리즈',              'NASD', 'Consumer Defensive',true, false),
    ('MU',    '마이크론',              'NASD', 'Technology',        true, false),
    ('AVGO',  '브로드컴',              'NASD', 'Technology',        true, false),
    ('ADBE',  '어도비',                'NASD', 'Technology',        true, false),
    ('TXN',   '텍사스 인스트루먼트',   'NASD', 'Technology',        true, false),
    ('AMD',   'AMD',                   'NASD', 'Technology',        true, false),
    ('AMAT',  '어플라이드 머티리얼즈', 'NASD', 'Technology',        true, false),
    ('SPY',   'S&P 500 ETF',           'AMEX', 'ETF',               true, true),
    ('QQQ',   'QQQ ETF',               'NASD', 'ETF',               true, true)
ON CONFLICT (ticker) DO NOTHING;


-- ============================================================
-- 2. stock_daily_prices: 일별 주가 (Long format)
--    종목 추가 시 이 테이블에 새 ticker의 행만 추가하면 됨
-- ============================================================
CREATE TABLE IF NOT EXISTS stock_daily_prices (
    id      BIGSERIAL   PRIMARY KEY,
    date    DATE        NOT NULL,
    ticker  VARCHAR(10) NOT NULL REFERENCES stock_universe(ticker),
    open    NUMERIC,
    high    NUMERIC,
    low     NUMERIC,
    close   NUMERIC     NOT NULL,
    volume  BIGINT,
    UNIQUE(date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_stock_prices_ticker_date ON stock_daily_prices (ticker, date DESC);
CREATE INDEX IF NOT EXISTS idx_stock_prices_date       ON stock_daily_prices (date DESC);


-- ============================================================
-- 3. economic_indicators: 경제 지표 (Wide format 유지)
--    경제 지표는 거의 변경되지 않으므로 Wide format이 적합
-- ============================================================
CREATE TABLE IF NOT EXISTS economic_indicators (
    id   BIGSERIAL PRIMARY KEY,
    date DATE      UNIQUE NOT NULL,

    -- FRED 지표
    inflation_10y       NUMERIC,   -- 10년 기대 인플레이션율
    yield_spread_10y2y  NUMERIC,   -- 장단기 금리차 (10Y-2Y)
    fed_rate            NUMERIC,   -- 기준금리
    consumer_sentiment  NUMERIC,   -- 미시간대 소비자 심리지수
    unemployment        NUMERIC,   -- 실업률
    treasury_2y         NUMERIC,   -- 2년 만기 미국 국채 수익률
    treasury_10y        NUMERIC,   -- 10년 만기 미국 국채 수익률
    financial_stress    NUMERIC,   -- 금융스트레스지수
    pce                 NUMERIC,   -- 개인 소비 지출
    cpi                 NUMERIC,   -- 소비자 물가지수
    mortgage_30y        NUMERIC,   -- 30년 고정금리 모기지
    dollar_trade_index  NUMERIC,   -- 미국 무역가중 달러 환율
    m2                  NUMERIC,   -- 통화 공급량 M2
    household_debt      NUMERIC,   -- 가계 부채 비율
    gdp                 NUMERIC,   -- 실질 GDP
    nasdaq_composite    NUMERIC,   -- 나스닥 종합지수 (FRED)

    -- Yahoo Finance: 주요 지수 및 시장 지표
    sp500               NUMERIC,   -- S&P 500 지수
    gold                NUMERIC,   -- 금 가격
    dollar_index        NUMERIC,   -- 달러 인덱스 (DX-Y.NYB)
    nasdaq100           NUMERIC,   -- 나스닥 100
    spy                 NUMERIC,   -- S&P 500 ETF
    qqq                 NUMERIC,   -- QQQ ETF
    iwm                 NUMERIC,   -- 러셀 2000 ETF
    dia                 NUMERIC,   -- 다우 존스 ETF
    vix                 NUMERIC,   -- VIX 지수
    nikkei225           NUMERIC,   -- 닛케이 225
    shanghai            NUMERIC,   -- 상해종합
    hangseng            NUMERIC,   -- 항셍
    ftse                NUMERIC,   -- 영국 FTSE
    dax                 NUMERIC,   -- 독일 DAX
    cac40               NUMERIC,   -- 프랑스 CAC 40
    agg                 NUMERIC,   -- 미국 전체 채권시장 ETF
    tip                 NUMERIC,   -- TIPS ETF
    lqd                 NUMERIC,   -- 투자등급 회사채 ETF
    jpy_usd             NUMERIC,   -- 달러/엔
    cny_usd             NUMERIC,   -- 달러/위안
    vnq                 NUMERIC,   -- 미국 리츠 ETF

    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_economic_date ON economic_indicators (date DESC);


-- ============================================================
-- 4. stock_signals: 기술적 지표 분석 결과 (Long format)
--    기존 stock_recommendations 대체
-- ============================================================
CREATE TABLE IF NOT EXISTS stock_signals (
    id               BIGSERIAL   PRIMARY KEY,
    date             DATE        NOT NULL,
    ticker           VARCHAR(10) NOT NULL,
    sma20            NUMERIC,
    sma50            NUMERIC,
    rsi              NUMERIC,
    macd             NUMERIC,
    signal_line      NUMERIC,
    adx              NUMERIC,
    atr              NUMERIC,
    volume_ratio     NUMERIC,
    daily_change_pct NUMERIC,
    golden_cross     BOOLEAN     DEFAULT false,
    macd_buy_signal  BOOLEAN     DEFAULT false,
    composite_score  NUMERIC,
    UNIQUE(date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_stock_signals_date   ON stock_signals (date DESC);
CREATE INDEX IF NOT EXISTS idx_stock_signals_ticker ON stock_signals (ticker);


-- ============================================================
-- 5. stock_predictions: ML 예측 결과 (Long format)
--    기존 predicted_stocks + stock_analysis_results 통합
-- ============================================================
CREATE TABLE IF NOT EXISTS stock_predictions (
    id               BIGSERIAL   PRIMARY KEY,
    date             DATE        NOT NULL,
    ticker           VARCHAR(10) NOT NULL,
    predicted_price  NUMERIC,
    actual_price     NUMERIC,
    rise_probability NUMERIC,    -- 예측 상승률 (%)
    accuracy         NUMERIC,    -- 모델 정확도 (%)
    mae              NUMERIC,
    mse              NUMERIC,
    rmse             NUMERIC,
    mape             NUMERIC,
    model_version    VARCHAR(20) DEFAULT '1.0',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_stock_predictions_date   ON stock_predictions (date DESC);
CREATE INDEX IF NOT EXISTS idx_stock_predictions_ticker ON stock_predictions (ticker);


-- ============================================================
-- 6. ticker_sentiment: 뉴스 감성 분석
-- ============================================================
CREATE TABLE IF NOT EXISTS ticker_sentiment (
    id              BIGSERIAL   PRIMARY KEY,
    ticker          VARCHAR(10) UNIQUE NOT NULL,
    sentiment_score NUMERIC,
    article_count   INTEGER     DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);


-- ============================================================
-- 7. trade_records: 매매 기록
-- ============================================================
CREATE TABLE IF NOT EXISTS trade_records (
    id                BIGSERIAL   PRIMARY KEY,
    ticker            TEXT        NOT NULL,
    stock_name        TEXT,
    buy_price         FLOAT8      NOT NULL,
    buy_date          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    quantity          INTEGER     NOT NULL,
    holding_quantity  INTEGER     DEFAULT 0,
    exchange_code     TEXT,
    atr               FLOAT8,
    take_profit_price FLOAT8,
    stop_loss_price   FLOAT8,
    status            TEXT        NOT NULL DEFAULT 'holding',
    -- 상태값: buy_ordered / holding / sell_ordered / sold / buy_failed
    sell_price        FLOAT8,
    sell_date         TIMESTAMPTZ,
    sell_reason       TEXT,
    buy_reason        TEXT,
    profit_loss       FLOAT8,
    profit_loss_pct   FLOAT8,
    composite_score   FLOAT8,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trade_records_status ON trade_records (status);
CREATE INDEX IF NOT EXISTS idx_trade_records_ticker ON trade_records (ticker);


-- ============================================================
-- 8. access_tokens: KIS API 토큰 캐시
-- ============================================================
CREATE TABLE IF NOT EXISTS access_tokens (
    id              BIGSERIAL   PRIMARY KEY,
    token_type      TEXT        NOT NULL DEFAULT 'kis_mock',
    access_token    TEXT        NOT NULL,
    expiration_time TEXT,
    expires_at      TIMESTAMPTZ,
    is_active       BOOLEAN     DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE access_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE access_tokens ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT true;
ALTER TABLE access_tokens ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE access_tokens ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();


-- ============================================================
-- 9. llm_decision_logs: LLM 매수 판단 기록
-- ============================================================
CREATE TABLE IF NOT EXISTS llm_decision_logs (
    id               BIGSERIAL      PRIMARY KEY,
    decision_date    DATE           NOT NULL,
    ticker           VARCHAR(20)    NOT NULL,
    stock_name       VARCHAR(100),
    decision         VARCHAR(10)    NOT NULL,   -- BUY / HOLD / FAIL
    reason           TEXT,
    market_analysis  TEXT,
    composite_score  DECIMAL(10,4),
    rise_probability DECIMAL(10,2),
    rsi              DECIMAL(10,2),
    adx              DECIMAL(10,2),
    vix_value        DECIMAL(10,2),
    created_at       TIMESTAMPTZ    DEFAULT NOW(),
    updated_at       TIMESTAMPTZ    DEFAULT NOW(),
    UNIQUE(decision_date, ticker)
);


-- ============================================================
-- 10. notification_logs: Telegram 알림 로그
-- ============================================================
CREATE TABLE IF NOT EXISTS notification_logs (
    id         BIGSERIAL   PRIMARY KEY,
    type       VARCHAR(30) NOT NULL,   -- buy / sell / error / daily_report / vix_alert / ml_retrain
    message    TEXT,
    ticker     VARCHAR(10),
    created_at TIMESTAMPTZ DEFAULT NOW()
);


-- ============================================================
-- 11. system_config: 시스템 설정 KV Store
--     코드 배포 없이 파라미터 조정 가능
-- ============================================================
CREATE TABLE IF NOT EXISTS system_config (
    key         VARCHAR(50) PRIMARY KEY,
    value       TEXT        NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO system_config (key, value, description) VALUES
    ('vix_halt_threshold',   '35',    'VIX 이 값 초과 시 매수 전면 중단'),
    ('vix_sell_threshold',   '40',    'VIX 이 값 초과 시 매도 신호 강화'),
    ('min_composite_score',  '0.3',   '매수 최소 복합 점수'),
    ('min_rise_probability', '3',     '매수 최소 상승확률 (%)'),
    ('position_size_pct',    '10',    '종목당 투자 비중 (%)'),
    ('max_positions',        '5',     '최대 보유 종목 수'),
    ('max_sector_positions', '3',     '동일 섹터 최대 보유 종목 수'),
    ('correlation_limit',    '0.7',   '기존 보유 종목과 상관계수 임계치(초과 시 진입 금지)'),
    ('buy_window_start_et',  '10:30', '매수 시작 시간 (미국 동부시간 ET)'),
    ('buy_window_end_et',    '11:00', '매수 종료 시간 (미국 동부시간 ET)'),
    ('buy_once_per_day',     'true',  'true이면 하루 1회만 자동매수 실행, false이면 매수 시간대에 5분마다 실행'),
    ('daily_max_loss_pct',   '3',     '일일 최대 손실 한도 (%), 초과 시 당일 매매 중단'),
    ('mdd_soft_limit_pct',   '5',     '누적 MDD 소프트 한도 (%), 초과 시 포지션 축소'),
    ('mdd_hard_limit_pct',   '10',    '누적 MDD 하드 한도 (%), 초과 시 신규 매수 중단'),
    ('mdd_soft_position_multiplier', '0.5', 'MDD 소프트 한도 초과 시 포지션 배수'),
    ('min_adv20_usd',        '5000000', '유동성 필터: 최소 20일 평균 거래대금(USD)'),
    ('min_price_usd',        '5',     '유동성 필터: 최소 주가(USD)'),
    ('promotion_oos_sharpe_min', '0.8', '모델 승격 최소 OOS Sharpe'),
    ('promotion_oos_mdd_max', '12', '모델 승격 최대 OOS MDD(%)'),
    ('promotion_oos_trades_min', '80', '모델 승격 최소 OOS 거래수'),
    ('rollback_sharpe_threshold', '0.2', '롤백 트리거: 20D Sharpe 임계치'),
    ('rollback_consecutive_days', '10', '롤백 트리거: 연속 일수(약 2주)'),
    ('take_profit_atr_mult', '2.5',   'ATR 기반 익절 배수'),
    ('stop_loss_atr_mult',   '1.5',   'ATR 기반 손절 배수'),
    ('take_profit_fixed_pct','5',     'ATR 없을 때 고정 익절 (%)'),
    ('stop_loss_fixed_pct',  '7',     'ATR 없을 때 고정 손절 (%)'),
    ('lookback_days',        '180',   '기술적 지표 계산 기준 과거 일수')
ON CONFLICT (key) DO NOTHING;
