-- trade_records 테이블에 실제 보유 수량 컬럼 추가
-- quantity: 주문 수량 (변경 없음)
-- holding_quantity: KIS 원장 기준 실제 보유 수량 (reconcile 시 동기화)
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS holding_quantity INTEGER DEFAULT 0;
