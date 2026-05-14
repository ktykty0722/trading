-- ============================================================
-- v3 마이그레이션: 자동 데이터 파이프라인 + 종목 백필 + 인트라데이 엔진
-- create_tables_v2.sql 실행 이후 적용
-- 모든 문장은 멱등(IF NOT EXISTS / ON CONFLICT) 처리
-- ============================================================


-- ============================================================
-- 1. pipeline_runs: 자동 파이프라인 job 실행 로그
-- ============================================================
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id           BIGSERIAL    PRIMARY KEY,
    job_name     TEXT         NOT NULL,
    status       TEXT         NOT NULL,        -- running / success / failed / skipped
    started_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    duration_ms  BIGINT,
    message      TEXT,
    metadata     JSONB
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_job_started
    ON pipeline_runs (job_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status
    ON pipeline_runs (status, started_at DESC);


-- ============================================================
-- 2. ticker_backfill_jobs: /add 후 후속 데이터 백필 큐
-- ============================================================
CREATE TABLE IF NOT EXISTS ticker_backfill_jobs (
    id            BIGSERIAL    PRIMARY KEY,
    ticker        TEXT         NOT NULL,
    exchange      TEXT         NOT NULL,
    status        TEXT         NOT NULL DEFAULT 'pending',
    -- pending / running / done / failed / partial
    requested_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    message       TEXT,
    metadata      JSONB
);

CREATE INDEX IF NOT EXISTS idx_ticker_backfill_status
    ON ticker_backfill_jobs (status, requested_at);
CREATE INDEX IF NOT EXISTS idx_ticker_backfill_ticker
    ON ticker_backfill_jobs (ticker);


-- ============================================================
-- 3. intraday_prices: 장중 가격 스냅샷 (1~5분 간격)
-- ============================================================
CREATE TABLE IF NOT EXISTS intraday_prices (
    id         BIGSERIAL    PRIMARY KEY,
    ticker     TEXT         NOT NULL,
    timestamp  TIMESTAMPTZ  NOT NULL,
    price      NUMERIC      NOT NULL,
    volume     BIGINT,
    source     TEXT,
    UNIQUE(ticker, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_intraday_prices_ticker_time
    ON intraday_prices (ticker, timestamp DESC);


-- ============================================================
-- 4. intraday_signals: 인트라데이 평가 결과
-- ============================================================
CREATE TABLE IF NOT EXISTS intraday_signals (
    id                       BIGSERIAL    PRIMARY KEY,
    ticker                   TEXT         NOT NULL,
    timestamp                TIMESTAMPTZ  NOT NULL,
    price                    NUMERIC,
    change_from_prev_close_pct  NUMERIC,
    momentum_15m_pct         NUMERIC,
    volume_ratio             NUMERIC,
    vwap                     NUMERIC,
    above_vwap               BOOLEAN,
    day_high_breakout        BOOLEAN,
    signal_score             NUMERIC,
    reason                   TEXT,
    created_at               TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE(ticker, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_intraday_signals_time
    ON intraday_signals (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_intraday_signals_ticker_time
    ON intraday_signals (ticker, timestamp DESC);


-- ============================================================
-- 5. trade_records: 스윙/인트라데이 전략 구분 컬럼
-- ============================================================
ALTER TABLE trade_records
    ADD COLUMN IF NOT EXISTS strategy         TEXT DEFAULT 'swing';
ALTER TABLE trade_records
    ADD COLUMN IF NOT EXISTS entry_reason     TEXT;
ALTER TABLE trade_records
    ADD COLUMN IF NOT EXISTS signal_snapshot  JSONB;

CREATE INDEX IF NOT EXISTS idx_trade_records_strategy
    ON trade_records (strategy);


-- ============================================================
-- 6. system_config: 신규 키 추가 (자동 파이프라인 + 인트라데이)
-- ============================================================
INSERT INTO system_config (key, value, description) VALUES
    -- 데이터 파이프라인 자동화
    ('data_pipeline_enabled',     'true',  '자동 데이터 파이프라인 실행 여부'),
    ('economic_update_time_kst',  '06:05', '경제/주가 업데이트 KST 시간'),
    ('technical_signal_time_kst', '06:20', '기술지표 생성 KST 시간'),
    ('ml_prediction_time_kst',    '06:35', 'ML 예측 생성 KST 시간'),
    ('sentiment_update_time_kst', '07:10', '뉴스 감성 분석 KST 시간'),

    -- 종목 백필
    ('ticker_backfill_enabled',     'true', '/add 후 자동 백필 워커 동작 여부'),
    ('ticker_backfill_interval_min','2',    '백필 워커 폴링 주기(분)'),
    ('auto_run_ml_after_add',       'false','/add 직후 ML 재학습 즉시 실행 여부'),
    ('ticker_backfill_days',        '365',  '신규 종목 가격 백필 일수'),

    -- 인트라데이 엔진 (기본 OFF — 보수적)
    ('intraday_enabled',                 'false','인트라데이 매수 엔진 활성화 여부'),
    ('intraday_interval_minutes',        '5',    '인트라데이 평가 주기(분)'),
    ('intraday_start_et',                '09:45','인트라데이 시작 시간 ET'),
    ('intraday_end_et',                  '15:30','인트라데이 종료 시간 ET'),
    ('intraday_max_entries_per_day',     '3',    '인트라데이 일일 최대 신규 진입 수'),
    ('intraday_ticker_cooldown_minutes', '60',   '동일 종목 재평가/재진입 쿨다운(분)'),
    ('intraday_min_score',               '0.7',  '인트라데이 매수 최소 점수'),
    ('intraday_min_volume_ratio',        '1.5',  '평균 대비 최소 거래량 비율'),
    ('intraday_min_momentum_15m_pct',    '0.3',  '15분 모멘텀 최소값(%)'),
    ('intraday_require_above_vwap',      'true', 'VWAP 위에서만 매수 허용'),
    ('intraday_use_llm_review',          'false','인트라데이 후보에 LLM 검토 사용 여부'),
    ('intraday_position_size_pct',       '5',    '인트라데이 진입 비중(%) — 보수적'),
    ('intraday_market_drop_block_pct',   '-0.8', 'SPY/QQQ 당일 변동률 이 값 이하면 매수 차단(%)')
ON CONFLICT (key) DO NOTHING;
