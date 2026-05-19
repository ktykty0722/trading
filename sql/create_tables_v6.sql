-- ============================================================
-- v6 마이그레이션: 실운영 관찰 결과로 보수적 임계값 조정 + 신규 설정 키
-- (1주 운영: swing은 유동성 필터에서 차단, intraday는 0.7 임계값 미달)
-- create_tables_v5.sql 이후 실행
-- ============================================================

-- 1) 유동성 필터: volume이 채워질 때까지 안전한 디폴트로 완화
--    NOTE: 실거래 전환 시 5000000으로 복귀 권장
UPDATE system_config SET value='500000'
    WHERE key='min_adv20_usd' AND value='5000000';

-- 2) 인트라데이 점수 임계값: 0.7 → 0.6 (운영 max ~0.69 관찰)
UPDATE system_config SET value='0.6'
    WHERE key='intraday_min_score' AND value='0.7';

-- 3) 신규 키 (없으면 추가)
INSERT INTO system_config (key, value, description) VALUES
    ('volume_backfill_required', 'true',
     'true이면 stock_daily_prices.volume이 NULL인 행을 주기적으로 재백필')
ON CONFLICT (key) DO NOTHING;
