# AI 주식 자동매매 시스템

ML 예측 + 기술적 분석 + 감성 분석 + LLM 검토를 결합한 미국 주식 자동매매 시스템입니다.

한국투자증권(KIS) API를 통해 실제 매매까지 자동으로 수행합니다.

## 시스템 구조

```
stockTrading/
├── app/
│   ├── main.py                          # FastAPI 앱 진입점
│   ├── api/
│   │   ├── api.py                       # 라우터 통합
│   │   └── routes/                      # API 엔드포인트
│   │       ├── stocks.py                # 주식 분석
│   │       ├── balance.py               # 잔고/주문
│   │       ├── economic.py              # 경제 데이터
│   │       ├── stock_recommendations.py # 추천 종목
│   │       ├── llm_review.py            # LLM 검토
│   │       ├── predict.py               # ML 예측
│   │       └── volume.py                # 거래량 분석
│   ├── services/                        # 비즈니스 로직
│   │   ├── stock_recommendation_service.py  # 기술적 분석 + 매수/매도 판단
│   │   ├── balance_service.py               # KIS API 연동
│   │   ├── economic_service.py              # 경제 데이터 수집
│   │   ├── llm_review_service.py            # Claude LLM 검토
│   │   ├── predict_service.py               # ML 예측
│   │   └── auth_service.py                  # 토큰 관리
│   ├── utils/scheduler.py               # 자동매매 스케줄러
│   ├── core/config.py                   # 환경변수 설정
│   └── db/supabase.py                   # Supabase 클라이언트
├── stock.py                             # 경제지표 + 주가 데이터 수집
├── predict_colab.py                     # ML 예측 모델 (Google Colab용)
├── run.py                               # 서버 실행
├── requirements.txt                     # 패키지 목록
├── .env_dev                             # 환경변수 샘플
├── db_backup/                           # DB 스키마 + 초기 데이터
└── sql/                                 # 테이블 생성 SQL
```

## 동작 흐름

```
1. 데이터 수집    FRED 경제지표 + Yahoo Finance 주가 → Supabase 저장
2. 기술적 분석    SMA, RSI, MACD, ADX, ATR → 매수 신호 판별
3. ML 예측       Transformer 모델 → 상승 확률 예측
4. 감성 분석     AlphaVantage 뉴스 → 감성 점수
5. 종합 점수     6가지 요소 가중 합산 → 매수 후보 선정
6. LLM 검토     Claude API → 최종 거부권 행사
7. 자동 매수     뉴욕 10:30 ET → KIS API 주문
8. 자동 매도     1분마다 체크 → ATR 기반 익절/손절
```

---

## 설치 및 실행 가이드

### 1단계: 프로젝트 클론

```bash
git clone https://gitlab.com/banbu3/banbu-stocktrading-final.git
cd banbu-stocktrading-final
```

### 2단계: Python 설치

Python **3.12** 버전을 권장합니다.

**Mac:**
```bash
brew install python@3.12
```

**Windows:**
- https://www.python.org/downloads/ 에서 Python 3.12 다운로드
- 설치 시 **"Add Python to PATH"** 반드시 체크

**버전 확인:**
```bash
python3 --version
# Python 3.12.x
```

### 3단계: 가상환경 생성 및 패키지 설치

**Mac / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows:**
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

> **참고: TensorFlow 설치**
>
> `requirements.txt`의 `tensorflow-macos`와 `tensorflow-metal`은 Mac 전용입니다.
> 환경에 따라 아래와 같이 설치하세요:
>
> - **Mac (Apple Silicon):** `pip install tensorflow-macos tensorflow-metal`
> - **Mac (Intel):** `pip install tensorflow`
> - **Windows / Linux:** `pip install tensorflow`
>
> ML 예측 기능을 사용하지 않는다면 TensorFlow 설치를 건너뛰어도 서버는 정상 동작합니다.

> **참고: Claude API (anthropic 패키지)**
>
> LLM 최종 검토 기능에 필요합니다. `requirements.txt`에 포함되어 있어 자동 설치됩니다.
> 사용하려면 `.env`에 `ANTHROPIC_API_KEY`를 입력하세요.
> API 키 발급: https://console.anthropic.com

### 4단계: 환경변수 설정

`.env_dev` 파일을 복사해서 `.env`를 만듭니다:

```bash
cp .env_dev .env
```

`.env` 파일을 열어 아래 값들을 입력합니다:

```env
# 한국투자증권 (필수)
KIS_USE_MOCK=true                   # true: 모의투자 / false: 실전투자
KIS_MOCK_APPKEY=발급받은_앱키
KIS_MOCK_APPSECRET=발급받은_앱시크릿
KIS_MOCK_CANO=모의투자_계좌번호

# Supabase (필수)
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=eyJhbGci...

# FRED API (경제지표 수집용, 선택)
# stock.py 파일 내 api_key 변수를 직접 수정하세요

# AlphaVantage (감성분석용, 선택)
ALPHA_VANTAGE_API_KEY=발급받은_키

# Claude API (LLM 검토용, 선택)
ANTHROPIC_API_KEY=sk-ant-...
```

### 5단계: Supabase 데이터베이스 세팅

#### 5-1. Supabase 프로젝트 생성
1. https://supabase.com 에서 무료 계정 생성
2. **New Project** 생성
3. **Project Settings > API**에서 URL과 anon key 복사 → `.env`에 입력

#### 5-2. 테이블 생성
1. Supabase 대시보드 > **SQL Editor** 열기
2. `db_backup/schema/create_all_tables.sql` 내용을 붙여넣고 **Run**

#### 5-3. 초기 데이터 임포트
Supabase의 **Table Editor**에서 CSV를 임포트합니다:

1. Table Editor > 테이블 선택 > **Insert** > **Import data from CSV**
2. `db_backup/data/` 폴더의 CSV 파일 업로드

**필수 임포트:**
- `economic_and_stock_data.csv` — 경제지표 + 주가 히스토리 (시스템 구동 필수)
- `stock_analysis_results.csv` — ML 예측 정확도 기준값

**선택 임포트:**
- `predicted_stocks.csv` — ML 예측 히스토리
- 나머지 테이블은 시스템이 자동 생성합니다

#### 5-4. RLS 비활성화
SQL Editor에서 아래를 실행합니다:

```sql
ALTER TABLE economic_and_stock_data DISABLE ROW LEVEL SECURITY;
ALTER TABLE stock_analysis_results DISABLE ROW LEVEL SECURITY;
ALTER TABLE predicted_stocks DISABLE ROW LEVEL SECURITY;
ALTER TABLE stock_recommendations DISABLE ROW LEVEL SECURITY;
ALTER TABLE ticker_sentiment_analysis DISABLE ROW LEVEL SECURITY;
ALTER TABLE trade_records DISABLE ROW LEVEL SECURITY;
ALTER TABLE llm_decision_logs DISABLE ROW LEVEL SECURITY;
ALTER TABLE access_tokens DISABLE ROW LEVEL SECURITY;
```

### 6단계: 서버 실행

```bash
python run.py
```

정상 실행 시 아래 로그가 출력됩니다:

```
서비스 시작 시 경제 데이터 수집을 즉시 실행합니다...
초기 경제 데이터 수집이 완료되었습니다.
주식 자동매매 스케줄러가 시작되었습니다.
매도 스케줄러가 시작되었습니다. 1분마다 매도 대상을 확인합니다.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

API 문서 확인: http://localhost:8000/docs (Swagger UI)

---

## 외부 API 키 발급 안내

| API | 용도 | 발급 링크 | 비용 |
|-----|------|----------|------|
| 한국투자증권 (KIS) | 주식 매수/매도 주문 | https://apiportal.koreainvestment.com | 무료 |
| Supabase | 데이터베이스 | https://supabase.com | 무료 플랜 |
| FRED | 미국 경제지표 | https://fred.stlouisfed.org/docs/api/api_key.html | 무료 |
| AlphaVantage | 뉴스 감성 분석 | https://www.alphavantage.co/support/#api-key | 무료 (일 25건) |
| Anthropic Claude | LLM 최종 검토 | https://console.anthropic.com | 유료 |

### KIS API 모의투자 설정
1. https://apiportal.koreainvestment.com 에서 회원가입
2. **모의투자** 앱키 발급 (앱키 + 앱시크릿)
3. **모의투자 계좌 개설** (계좌번호 발급)
4. `.env`에 입력

---

## ML 예측 모델 학습 (선택)

ML 예측은 **Google Colab**에서 실행합니다 (GPU 사용):

1. `predict_colab.py`를 Google Colab에 업로드
2. Supabase URL/KEY를 코드에 입력
3. 실행하면 학습 결과가 Supabase `stock_analysis_results`, `predicted_stocks` 테이블에 저장됨

> DB 백업에 이미 학습된 결과가 포함되어 있으므로, ML 학습 없이도 시스템은 동작합니다.

---

## 주의사항

- 이 시스템은 **교육 목적**으로 제작되었습니다
- 실전 투자 시 발생하는 손실에 대해 책임지지 않습니다
- 반드시 **모의투자(`KIS_USE_MOCK=true`)**로 먼저 테스트하세요
- API 키와 시크릿은 절대 외부에 공유하지 마세요
