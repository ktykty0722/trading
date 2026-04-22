-- trade_records: 매매 기록 및 ATR 기반 동적 익절/손절 관리
CREATE TABLE trade_records (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,                    -- 종목 티커 (AAPL, TSLA 등)
    stock_name TEXT,                         -- 한글명 (애플, 테슬라 등)
    buy_price FLOAT8 NOT NULL,              -- 매수가
    buy_date TIMESTAMPTZ NOT NULL DEFAULT NOW(), -- 매수 시점
    quantity INT NOT NULL,                   -- 매수 수량
    exchange_code TEXT,                      -- 거래소 코드 (NASD, NYSE 등)
    atr FLOAT8,                             -- 매수 시점 ATR (14일)
    take_profit_price FLOAT8,               -- 익절가 (buy_price + ATR * 2.5)
    stop_loss_price FLOAT8,                 -- 손절가 (buy_price - ATR * 1.5)
    status TEXT NOT NULL DEFAULT 'holding',  -- holding / sold
    sell_price FLOAT8,                      -- 매도가
    sell_date TIMESTAMPTZ,                  -- 매도 시점
    sell_reason TEXT,                        -- 매도 사유 (take_profit / stop_loss / signal 등)
    profit_loss FLOAT8,                     -- 실현 손익 금액
    profit_loss_pct FLOAT8,                 -- 실현 손익률 (%)
    composite_score FLOAT8,                 -- 매수 시점 종합 점수
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 인덱스
CREATE INDEX idx_trade_records_status ON trade_records (status);
CREATE INDEX idx_trade_records_ticker ON trade_records (ticker);
CREATE INDEX idx_trade_records_buy_date ON trade_records (buy_date DESC);
