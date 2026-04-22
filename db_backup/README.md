# Supabase DB 백업

백업일시: 2026-04-11

## 폴더 구조

```
db_backup/
├── README.md                     # 이 파일
├── backup_supabase.py            # 백업 스크립트 (재실행 가능)
├── schema/                       # 테이블 스키마
│   ├── create_all_tables.sql     # 전체 테이블 생성 SQL (수강생용)
│   ├── *_schema.json             # 각 테이블 컬럼 정보
│   └── *.sql                     # 기존 SQL 파일들
└── data/                         # 테이블 데이터
    ├── *.json                    # JSON 형식
    └── *.csv                     # CSV 형식 (엑셀 호환)
```

## 테이블 요약

| 테이블 | 행수 | 설명 |
|--------|------|------|
| economic_and_stock_data | 7,139 | 경제지표 + 주가 (2006~현재, 64컬럼) |
| predicted_stocks | 7,049 | ML 예측값 vs 실제값 (56컬럼) |
| stock_analysis_results | 27 | ML 모델 성능 (종목별 정확도) |
| stock_recommendations | 25 | 기술적 분석 결과 (RSI, MACD, 골든크로스 등) |
| llm_decision_logs | 77 | Claude LLM 매수 판단 기록 |
| trade_records | 10 | 실제 매매 기록 |
| ticker_sentiment_analysis | 7 | 뉴스 감성 분석 결과 |
| access_tokens | 1 | KIS API 토큰 캐시 |

## 수강생 세팅 가이드

### 1단계: Supabase 프로젝트 생성
1. https://supabase.com 에서 무료 계정 생성
2. New Project 생성
3. Project Settings > API에서 URL과 anon key 복사
4. `.env` 파일에 `SUPABASE_URL`과 `SUPABASE_KEY` 입력

### 2단계: 테이블 생성
1. Supabase 대시보드 > SQL Editor 열기
2. `schema/create_all_tables.sql` 내용을 붙여넣기
3. Run 클릭

### 3단계: 초기 데이터 임포트
Supabase 대시보드의 Table Editor에서 CSV를 직접 임포트할 수 있습니다:

1. Table Editor > 해당 테이블 선택
2. Insert > Import data from CSV
3. `data/` 폴더의 해당 CSV 파일 업로드

**필수 임포트 테이블:**
- `economic_and_stock_data` — 시스템 구동에 필수 (경제지표 + 주가 히스토리)
- `stock_analysis_results` — ML 예측 정확도 기준값

**선택 임포트 테이블:**
- `predicted_stocks` — ML 예측 히스토리 (없어도 시스템 동작)
- 나머지 테이블은 시스템이 자동 생성/채움

### 4단계: RLS (Row Level Security) 설정
개발/학습 목적이므로 RLS를 비활성화합니다:

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

또는 모든 접근을 허용하는 policy 추가:

```sql
-- 각 테이블에 대해 실행
CREATE POLICY "Allow all" ON economic_and_stock_data FOR ALL USING (true);
```

## 백업 재실행

```bash
python3 db_backup/backup_supabase.py
```

최신 데이터로 백업을 다시 받고 싶을 때 실행하세요.
