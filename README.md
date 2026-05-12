# 주식 자동 거래 시스템 (Algorithmic Stock Trading System)

경제 지표 + 기술적 분석 + 감성 분석 + LLM 최종 검토를 결합한 **해외주식 자동 매매 시스템**.
한국투자증권(KIS) API로 실제 주문하고, Telegram Bot으로 제어·모니터링합니다.

---

## 목차

1. [시스템 개요](#1-시스템-개요)
2. [아키텍처](#2-아키텍처)
3. [사전 준비 (API 키 발급)](#3-사전-준비-api-키-발급)
4. [Supabase DB 설정](#4-supabase-db-설정)
5. [환경 변수 (.env) 설정](#5-환경-변수-env-설정)
6. [로컬 실행 (개발용)](#6-로컬-실행-개발용)
7. [VPS 배포 (Docker)](#7-vps-배포-docker)
8. [Telegram Bot 사용법](#8-telegram-bot-사용법)
9. [파라미터 튜닝 (system_config)](#9-파라미터-튜닝-system_config)
10. [ML 모델 재학습](#10-ml-모델-재학습)
11. [일상 운영 체크리스트](#11-일상-운영-체크리스트)
12. [앞으로 해야 할 것](#12-앞으로-해야-할-것)
13. [문제 해결](#13-문제-해결)

---

## 1. 시스템 개요

### 동작 방식 (하루 흐름, ET 기준)

```
09:00 (KST 22:00 전일)  경제 데이터 수집 (FRED + Yahoo + KIS)
                        → stock_daily_prices, economic_indicators 적재
                        → 기술적 지표 계산 → stock_signals 적재
                        → 감성 분석 → ticker_sentiment 적재

10:30 ~ 10:35  매수 실행
  1) VIX > 35  → 매수 전면 중단 + Telegram 경고
  2) 일일 손실 > 3%  → 매수 중단
  3) 보유 종목 >= 5  → 신규 매수 중단
  4) 복합 점수 >= 0.3 종목 필터링
  5) OpenAI GPT-4o로 최종 검토 (Fail-Close: 애매하면 매수 안함)
  6) 섹터 집중도 체크 (동일 섹터 최대 3종목)
  7) ATR 기반 포지션 사이징 → KIS API 매수 주문
  8) Telegram 매수 알림

09:30 ~ 16:00  매도 모니터링 (1분 간격)
  - 익절(+3%) / 손절(-2%) / 시간 초과(3일) 기준 매도
  - 체결 시 Telegram 매도 알림

17:00  일일 리포트 Telegram 전송

매주 일요일 18:00 UTC (월 03:00 KST)
  - ML 모델 재학습 (Transformer)
  - 완료 후 Telegram 알림
```

### 핵심 특징

- **종목 관리가 DB 기반**: 코드 수정 없이 Telegram `/add NVDA` 명령어로 종목 추가
- **파라미터 튜닝이 DB 기반**: `system_config` 테이블에서 VIX 임계값, 포지션 비중 등 실시간 조정
- **Fail-Close 설계**: LLM 응답 오류/타임아웃 시 기본값은 "매수 안함"
- **Long Format DB**: 종목명이 컬럼이 아닌 row 값 → 종목 추가해도 스키마 변경 없음
- **리스크 3중 방어**: 일일 손실 한도 / 최대 보유 종목 / 섹터 집중도

---

## 2. 아키텍처

```
trading/
├── app/
│   ├── main.py                  # FastAPI 엔트리 + lifespan (스케줄러, 봇 시작)
│   ├── api/routes/              # REST 엔드포인트
│   ├── core/config.py           # 환경변수 로딩 (pydantic-settings)
│   ├── db/supabase.py           # Supabase 클라이언트
│   ├── services/
│   │   ├── balance_service.py       # KIS 주문/잔고/토큰
│   │   ├── economic_service.py      # FRED + Yahoo + KIS 데이터 수집
│   │   ├── stock_recommendation_service.py  # 기술적 지표 계산
│   │   ├── llm_review_service.py    # OpenAI GPT-4o 최종 매수 검토
│   │   ├── risk_service.py          # 리스크 관리 (손실한도/포지션/섹터)
│   │   ├── volume_service.py        # 거래량 필터
│   │   └── auth_service.py          # KIS 토큰 관리
│   ├── telegram_bot/
│   │   ├── bot.py                   # 14개 명령어 핸들러
│   │   └── notifier.py              # 매수/매도/오류 알림 템플릿
│   └── utils/scheduler.py       # APScheduler (매수/매도/일일리포트)
├── predict.py                   # Transformer ML 모델 학습
├── stock.py                     # 데이터 수집 단독 실행 스크립트
├── sql/create_tables_v2.sql     # Long format 스키마 (최초 1회 실행)
├── nginx/nginx.conf             # HTTPS Reverse Proxy
├── cron/ml-retrain.sh           # 주간 ML 재학습 cron
├── Dockerfile                   # FastAPI + Telegram Bot 컨테이너
├── Dockerfile.ml                # ML 학습 전용 컨테이너
├── docker-compose.yml           # app + nginx + ml-trainer
└── .env                         # 환경변수 (직접 작성)
```

### DB 스키마 (Supabase/PostgreSQL)

| 테이블 | 역할 |
|--------|------|
| `stock_universe` | 거래 대상 종목 (ticker, 섹터, is_active) |
| `stock_daily_prices` | 일별 OHLCV (Long format) |
| `economic_indicators` | 경제지표 38종 (Wide format) |
| `stock_signals` | 기술적 지표 + 복합점수 |
| `stock_predictions` | ML 예측 결과 |
| `ticker_sentiment` | 감성 분석 점수 |
| `trade_records` | 실제 체결 이력 |
| `llm_decision_logs` | OpenAI 판단 기록 (감사용) |
| `notification_logs` | Telegram 알림 이력 |
| `system_config` | 런타임 파라미터 KV store |
| `access_tokens` | KIS 토큰 캐시 |

---

## 3. 사전 준비 (API 키 발급)

아래 7가지 키를 발급받아야 합니다. OpenAI를 제외하면 모두 무료입니다.

### 3-1. 한국투자증권 (KIS) — 트레이딩 실행
- 사이트: https://apiportal.koreainvestment.com/
- 절차: 회원가입 → 계좌개설(모의/실전) → API 신청 → AppKey/AppSecret 발급
- **반드시 모의투자부터 시작** (`KIS_USE_MOCK=true`)
- 필요 정보: `KIS_MOCK_APPKEY`, `KIS_MOCK_APPSECRET`, `KIS_MOCK_CANO` (8자리 계좌번호)

### 3-2. Supabase — DB
- 사이트: https://supabase.com/
- 절차: 신규 프로젝트 생성 → Settings → API → `Project URL` + `anon public` key 복사
- 필요 정보: `SUPABASE_URL`, `SUPABASE_KEY`

### 3-3. FRED — 경제 지표 (금리, CPI, VIX 등)
- 사이트: https://fred.stlouisfed.org/docs/api/api_key.html
- 가입 즉시 발급
- 필요 정보: `FRED_API_KEY`

### 3-4. Alpha Vantage — 뉴스 감성 분석
- 사이트: https://www.alphavantage.co/support/#api-key
- 이메일만 입력하면 즉시 발급
- 필요 정보: `ALPHA_VANTAGE_API_KEY`

### 3-5. OpenAI — LLM 최종 검토
- 사이트: https://platform.openai.com/api-keys
- 결제수단 등록 필요 (GPT-4o mini 기준 하루 매수 후보 10종목 검토 시 월 $1 미만)
- 필요 정보: `OPENAI_API_KEY`

### 3-6. Telegram Bot — 알림 + 제어
1. Telegram 앱에서 `@BotFather` 검색 → `/newbot` → 봇 이름 설정 → Token 받기
2. 만든 봇과 대화 시작 (아무 메시지 전송)
3. 브라우저에서 `https://api.telegram.org/bot<TOKEN>/getUpdates` 접속
4. 응답에서 `"chat":{"id":123456789}` 부분의 숫자 복사
- 필요 정보: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

---

## 4. Supabase DB 설정

### 4-1. 테이블 생성
1. Supabase 대시보드 → SQL Editor
2. `sql/create_tables_v2.sql` 내용 전체 복사 → 실행
3. Table Editor에서 11개 테이블 생성 확인

### 4-2. RLS (Row Level Security) 비활성화 (개발용)
각 테이블마다: Table Editor → 테이블 선택 → RLS 토글 OFF.
(운영 시에는 service_role key 사용 + RLS 정책 재설계 권장)

### 4-3. 초기 종목 등록
SQL Editor에서 실행:
```sql
INSERT INTO stock_universe (ticker, name_ko, exchange, sector, is_active) VALUES
('NVDA', '엔비디아', 'NASD', 'Technology', true),
('AAPL', '애플',   'NASD', 'Technology', true),
('MSFT', '마이크로소프트', 'NASD', 'Technology', true),
('GOOGL','구글',   'NASD', 'Communication', true),
('TSLA', '테슬라', 'NASD', 'Consumer',    true);
```

---

## 5. 환경 변수 (.env) 설정

프로젝트 루트에 `.env` 파일을 만들고 아래 내용 채우기.

```bash
# ===== KIS =====
KIS_USE_MOCK=true
KIS_MOCK_APPKEY=발급받은_모의_앱키
KIS_MOCK_APPSECRET=발급받은_모의_앱시크릿
KIS_MOCK_CANO=12345678
KIS_ACNT_PRDT_CD=01

# 실전 전환 시 (처음엔 빈칸 둬도 됨)
KIS_REAL_APPKEY=
KIS_REAL_APPSECRET=
KIS_REAL_CANO=

# ===== Supabase =====
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=eyJhbGciOi...

# ===== 데이터 API =====
FRED_API_KEY=xxxxx
ALPHA_VANTAGE_API_KEY=xxxxx

# ===== LLM =====
OPENAI_API_KEY=sk-proj-xxxxx

# ===== Telegram =====
TELEGRAM_BOT_TOKEN=1234567890:AAH...
TELEGRAM_CHAT_ID=123456789

# ===== 운영 =====
DEBUG=false
CORS_ORIGINS=http://localhost:3000
API_AUTH_TOKEN=충분히_긴_랜덤_토큰
```

**절대 Git에 `.env`를 커밋하지 마세요** (`.gitignore`에 이미 포함되어 있음).

API 호출 시 `Authorization: Bearer <API_AUTH_TOKEN>` 또는 `X-API-Key: <API_AUTH_TOKEN>` 헤더가 필요합니다.
`API_AUTH_TOKEN`이 비어 있으면 `/health`, `/docs` 외 API 접근은 차단됩니다.

---

## 6. 로컬 실행 (개발용)

```bash
# 1. Python 3.12 가상환경
python3.12 -m venv .venv
source .venv/bin/activate

# 2. 의존성 설치
pip install -r requirements.txt

# 3. DB 테이블 생성 (Supabase SQL Editor에서 sql/create_tables_v2.sql 실행)

# 4. 서버 실행
python run.py
```

- FastAPI: http://localhost:8000
- Swagger: http://localhost:8000/docs
- Health: http://localhost:8000/health
- Telegram 봇이 자동으로 백그라운드 실행 → 내 봇에 `/start` 보내보기

---

## 7. VPS 배포 (Docker)

### 7-1. VPS 준비 (Ubuntu 22.04 기준)
```bash
# Docker 설치
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# 로그아웃 후 재로그인

# 코드 업로드
git clone <your-repo-url> ~/trading
cd ~/trading

# .env 작성 (위 섹션 5 참조)
nano .env
```

### 7-2. 실행
```bash
docker compose up -d
docker compose logs -f app       # 로그 확인
docker compose ps                # 상태 확인
```

- `app`: FastAPI + Telegram Bot + 스케줄러 (8000 포트)
- `nginx`: 80/443 리버스 프록시
- `ml-trainer`: profiles=["ml"]이라 평소엔 뜨지 않음

### 7-3. HTTPS 활성화 (도메인 있을 경우)
```bash
# Let's Encrypt 인증서 발급
sudo apt install certbot
sudo certbot certonly --standalone -d yourdomain.com
sudo cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem ./nginx/certs/
sudo cp /etc/letsencrypt/live/yourdomain.com/privkey.pem ./nginx/certs/

# nginx.conf에서 HTTPS 블록 주석 해제 + server_name 수정
nano nginx/nginx.conf

# 재시작
docker compose restart nginx
```

### 7-4. ML 재학습 cron 등록 (일요일 18:00 UTC)
```bash
crontab -e
# 아래 한 줄 추가
0 18 * * 0 /home/your_user/trading/cron/ml-retrain.sh >> /home/your_user/trading/ml-retrain.log 2>&1
```

---

## 8. Telegram Bot 사용법

봇에게 메시지를 보내 거의 모든 조작 가능. **`.env`의 `TELEGRAM_CHAT_ID`에 등록된 사람만** 명령어 실행됨.

### 조회
| 명령어 | 설명 |
|--------|------|
| `/start` | 봇 등록 확인 |
| `/status` | 스케줄러 상태 + 오늘 거래 요약 |
| `/portfolio` | 보유 종목 + 현재 수익률 |
| `/today` | 오늘의 매수 후보 |
| `/history` | 최근 10건 거래 내역 |
| `/config` | `system_config` 현재 파라미터 |
| `/tickers` | 활성 종목 목록 |

### 제어
| 명령어 | 설명 |
|--------|------|
| `/start_buy` | 매수 스케줄러 시작 |
| `/stop_buy` | 매수 스케줄러 중지 |
| `/start_sell` | 매도 스케줄러 시작 |
| `/stop_sell` | 매도 스케줄러 중지 |
| `/buy_now` | 수동 매수 즉시 실행 |
| `/sell_now` | 수동 매도 즉시 실행 |

### 종목 관리
| 명령어 | 예시 | 설명 |
|--------|------|------|
| `/add` | `/add NVDA` | 종목을 `stock_universe`에 추가 (is_active=true) |
| `/remove` | `/remove INTC` | 종목을 비활성화 (is_active=false, 보유 중이면 매도 후 자동 제외) |

### 자동 알림 (명령어 없이 받음)
- 매수/매도 체결 즉시
- VIX > 35 시 경고
- 매일 17:00 ET 일일 리포트
- 시스템 오류 발생 시
- ML 재학습 완료 시

---

## 9. 파라미터 튜닝 (system_config)

코드 재배포 없이 Supabase SQL Editor에서 `system_config.value`만 수정하면 반영됩니다.

| Key | 기본값 | 의미 |
|-----|--------|------|
| `vix_halt_threshold` | 35 | VIX 이 값 초과 시 매수 전면 중단 |
| `min_composite_score` | 0.3 | 매수 최소 복합 점수 |
| `position_size_pct` | 10 | 종목당 투자 비중(%) |
| `max_positions` | 5 | 최대 보유 종목 수 |
| `buy_window_start_et` | 10:30 | 매수 시작 시간 (ET) |
| `buy_window_end_et` | 11:00 | 매수 종료 시간 (ET) |
| `daily_max_loss_pct` | 3 | 일일 손실 한도(%), 초과 시 당일 매매 중단 |
| `max_sector_positions` | 3 | 동일 섹터 최대 보유 수 |

### 예시: 보수적으로 운영하기
```sql
UPDATE system_config SET value='25' WHERE key='vix_halt_threshold';
UPDATE system_config SET value='0.5' WHERE key='min_composite_score';
UPDATE system_config SET value='5' WHERE key='position_size_pct';
UPDATE system_config SET value='3' WHERE key='max_positions';
UPDATE system_config SET value='2' WHERE key='daily_max_loss_pct';
```

---

## 10. ML 모델 재학습

- **모델**: Transformer (`predict.py`)
- **입력**: 과거 60일 OHLCV + 경제지표 → **출력**: 14일 후 예상 가격 + 상승 확률
- **주기**: 매주 일요일 18:00 UTC (월요일 03:00 KST)
- **수동 실행**:
  ```bash
  docker compose --profile ml run --rm ml-trainer
  ```
- 완료 후 결과는 `stock_predictions` 테이블에 저장, Telegram 알림 수신.

---

## 11. 일상 운영 체크리스트

### 매일 아침 (1분)
- Telegram 일일 리포트 확인 (전날 17:00 ET 수신)
- `/portfolio`로 보유 종목 수익률 확인
- `/status`로 스케줄러 정상 동작 확인

### 주 1회
- 월요일 ML 재학습 완료 알림 수신 확인
- 최근 7일 거래 승률 점검 (`/history`)

### 월 1회
- OpenAI / Supabase 사용량 대시보드 확인
- `system_config` 파라미터 전략 재검토
- 승률 낮은 종목 `/remove`로 제거

### 장애 대응
- 오류 알림 수신 시 → VPS SSH 접속 → `docker compose logs app --tail 200`
- KIS 토큰 만료 → 자동 갱신되지만, 문제 시 `access_tokens` 테이블 수동 삭제
- 매매 중단 필요 → Telegram `/stop_buy` + `/stop_sell`

---

## 12. 앞으로 해야 할 것

### 즉시 (실행 전)
- [ ] Supabase에 `sql/create_tables_v2.sql` 실행하고 RLS 비활성화
- [ ] 7종 API 키 발급 및 `.env` 작성
- [ ] `stock_universe`에 원하는 종목 5~10개 INSERT
- [ ] **모의투자 모드**(`KIS_USE_MOCK=true`)로 1~2주 동작 검증

### 1차 안정화 (1개월)
- [ ] 백테스팅 스크립트 작성 — `stock_signals` 과거 데이터로 전략 검증
- [ ] 실제투자 전환 전 **최소 20거래** 모의 체결 기록 확인
- [ ] `llm_decision_logs`에서 OpenAI의 오판 사례 검토 후 프롬프트 개선

### 확장 (2~3개월)
- [ ] Telegram WebApp 대시보드 (portfolio 차트)
- [ ] 국내 주식(코스피/코스닥) 추가
- [ ] 매도 전략 고도화 (고정 익절 대신 trailing stop, 변동성 기반 동적 TP/SL)
- [ ] 포트폴리오 단위 리밸런싱 로직
- [ ] Grafana + Prometheus로 실시간 모니터링

### 권장하지 않는 것
- 하드코딩된 종목 리스트로 돌아가기 (DB 기반 유지)
- LLM 호출 제거 (Fail-Close가 큰 손실을 막아줌)
- 테스트 없이 실전 전환

---

## 13. 문제 해결

### Telegram 봇이 응답 없음
- `.env`의 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 재확인
- `docker compose logs app | grep telegram`
- BotFather에서 봇 살아있는지 확인

### Supabase 저장 실패
- RLS 비활성화 여부 확인
- `SUPABASE_KEY`가 `anon public` 키인지 (service_role 키 아님)

### KIS 주문 실패
- 모의투자 계좌 잔고 0이 아닌지 확인 (모의 예수금 충전 필요)
- `access_tokens` 테이블 행 삭제 후 재시도 → 토큰 재발급
- 미국 주식은 **미국장 개장 중**에만 주문 가능

### OpenAI 호출 실패 시
- **설계상 매수하지 않음** (Fail-Close)
- 결제 한도 도달 / API 키 오류 확인

### ML 학습 실패
- 과거 데이터 부족 (최소 300 거래일 필요)
- `stock_daily_prices`에 해당 ticker row 존재 여부 확인

---

## 라이선스 / 면책

이 프로젝트는 **교육·연구 목적**이며, 제공되는 매매 신호는 투자 조언이 아닙니다. 실제 매매 손실은 전적으로 운영자 책임입니다. 반드시 모의투자로 충분히 검증 후 실전 전환하세요.
