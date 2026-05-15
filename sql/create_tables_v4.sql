-- ============================================================
-- v4 마이그레이션: 인트라데이 매도/청산 로직 분리
-- create_tables_v3.sql 적용 이후 실행
-- ============================================================

-- 1) trade_records: trailing stop을 위한 보유 중 고점 + 청산 사유
ALTER TABLE trade_records
    ADD COLUMN IF NOT EXISTS peak_price       NUMERIC;
ALTER TABLE trade_records
    ADD COLUMN IF NOT EXISTS exit_strategy    TEXT;
ALTER TABLE trade_records
    ADD COLUMN IF NOT EXISTS exit_signal_snapshot JSONB;

-- 2) 인트라데이 청산 파라미터
INSERT INTO system_config (key, value, description) VALUES
    ('intraday_take_pct',          '1.2',   '인트라데이 익절 %'),
    ('intraday_stop_pct',          '0.7',   '인트라데이 손절 %'),
    ('intraday_trailing_pct',      '0.5',   'Trailing stop %, 보유 중 고점 대비'),
    ('intraday_trailing_arm_pct',  '0.6',   'Trailing 활성화 임계: 평가손익이 이 % 넘으면 trailing 가동'),
    ('intraday_max_hold_minutes',  '90',    '시간 청산: 보유 분 경과 시 손익 < arm_pct이면 청산'),
    ('intraday_eod_exit_et',       '15:45', '강제 청산 시각 ET (이후 보유 금지)'),
    ('intraday_exit_panic_pct',    '-1.5',  'SPY/QQQ 당일 변동률 이하면 전량 청산(%)'),
    ('intraday_exit_enabled',      'true',  '인트라데이 청산 루프 동작 여부')
ON CONFLICT (key) DO NOTHING;
