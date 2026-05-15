-- ============================================================
-- v5 마이그레이션: 스윙 trailing stop + 인트라데이 백테스트 결과 저장
-- create_tables_v4.sql 적용 이후 실행
-- ============================================================

-- 1) 스윙 trailing stop 파라미터 (기본 OFF — 보수적)
INSERT INTO system_config (key, value, description) VALUES
    ('swing_trailing_enabled',  'false', '스윙 trailing stop 활성화 여부'),
    ('swing_trailing_arm_pct',  '3.0',   '스윙 trailing 활성화 임계 평가손익(%)'),
    ('swing_trailing_pct',      '1.5',   '스윙 peak 대비 하락 청산 임계(%)')
ON CONFLICT (key) DO NOTHING;


-- 2) 인트라데이 백테스트 결과 저장 (선택)
CREATE TABLE IF NOT EXISTS intraday_backtests (
    id              BIGSERIAL    PRIMARY KEY,
    run_id          TEXT         NOT NULL,
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    period_start    DATE,
    period_end      DATE,
    config_snapshot JSONB,
    summary         JSONB,
    trades          JSONB
);

CREATE INDEX IF NOT EXISTS idx_intraday_backtests_started
    ON intraday_backtests (started_at DESC);
