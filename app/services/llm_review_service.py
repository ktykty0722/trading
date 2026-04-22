import json
import time
from datetime import datetime
import anthropic
from app.core.config import settings
from app.db.supabase import supabase

# 재시도 설정
MAX_RETRIES = 3
RETRY_DELAYS = [5, 15, 30]  # 재시도 간격 (초): 5초 → 15초 → 30초
MODELS = ["claude-opus-4-7", "claude-sonnet-4-6"]  # Opus 실패 시 Sonnet 폴백


def _save_llm_decision_logs(candidates: list, decision_map: dict, market_analysis: str, vix_value: float = None):
    """LLM 판단 결과를 llm_decision_logs 테이블에 저장 (동일 날짜+티커면 업데이트)"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        for candidate in candidates:
            ticker = candidate["ticker"]
            decision_data = decision_map.get(ticker, {})
            supabase.table("llm_decision_logs").upsert({
                "decision_date": today,
                "ticker": ticker,
                "stock_name": candidate.get("stock_name"),
                "decision": decision_data.get("decision", "N/A"),
                "reason": decision_data.get("reason", ""),
                "market_analysis": market_analysis,
                "composite_score": candidate.get("composite_score"),
                "rise_probability": candidate.get("rise_probability"),
                "rsi": candidate.get("rsi"),
                "adx": candidate.get("adx"),
                "vix_value": vix_value,
                "updated_at": datetime.now().isoformat(),
            }, on_conflict="decision_date,ticker").execute()
        print(f"  LLM 판단 로그 저장 완료: {len(candidates)}건")
    except Exception as log_e:
        print(f"  LLM 판단 로그 저장 실패: {log_e}")


def review_buy_candidates(candidates: list, vix_value: float = None) -> dict:
    """
    매수 후보 종목을 Claude API로 최종 검토합니다.
    LLM은 거부권만 있습니다 (BUY → HOLD로만 변경 가능, 새 종목 추가 불가).
    LLM 호출 실패 시 매수를 차단합니다 (Fail-Close).

    Args:
        candidates: get_combined_recommendations_with_technical_and_sentiment()의 results
        vix_value: 현재 VIX 지수

    Returns:
        dict: {
            "reviewed_candidates": [...],  # BUY 판정된 종목만
            "held_candidates": [...],      # HOLD로 제외된 종목
            "llm_reasoning": str,          # LLM 전체 분석
            "raw_response": [...]          # LLM 원본 응답
        }
    """
    if not settings.ANTHROPIC_API_KEY:
        print("  ANTHROPIC_API_KEY가 설정되지 않았습니다. LLM 검토 불가로 매수 차단.")
        return {
            "reviewed_candidates": [],
            "held_candidates": candidates,
            "llm_reasoning": "API 키 미설정으로 매수 차단",
            "raw_response": []
        }

    if not candidates:
        return {
            "reviewed_candidates": [],
            "held_candidates": [],
            "llm_reasoning": "매수 후보 없음",
            "raw_response": []
        }

    # 매수 후보 데이터를 프롬프트용으로 정리
    stock_summaries = []
    for i, c in enumerate(candidates, 1):
        summary = f"""
{i}. {c.get('stock_name', 'N/A')} ({c.get('ticker', 'N/A')})
   - ML 예측: 상승확률 {c.get('rise_probability', 0):.2f}%, 예측가 ${c.get('predicted_price', 0):.2f} (현재가 ${c.get('last_price', 0):.2f})
   - 기술적 지표:
     골든크로스: {'✓' if c.get('golden_cross') else '✗'} (SMA20: {c.get('sma20', 0):.2f}, SMA50: {c.get('sma50', 0):.2f})
     RSI: {c.get('rsi', 0):.2f} {'(과매도 반등)' if c.get('rsi', 50) < 30 else '(강세 진입)' if 50 <= c.get('rsi', 50) <= 65 else '(매수구간 아님)'}
     MACD: {c.get('macd', 0):.4f}, Signal: {c.get('signal', 0):.4f}, 매수신호: {'✓' if c.get('macd_buy_signal') else '✗'}
   - 거래량: 5일 평균 대비 {c.get('volume_ratio', 'N/A')}배
   - ADX(추세강도): {c.get('adx', 'N/A')} {'(강한 추세)' if c.get('adx') and c.get('adx') > 25 else '(추세 약함)' if c.get('adx') and c.get('adx') < 20 else '(보통)'}
   - 감성분석: {c.get('sentiment_score', 'N/A')} (기사 {c.get('article_count', 0)}개)
   - 종합점수: {c.get('composite_score', 0):.4f}
     (상승확률: {c.get('rise_score', 0)}, 기술: {c.get('tech_score', 0)}, 거래량: {c.get('volume_score', 0)}, ADX: {c.get('adx_score', 0)}, VIX: {c.get('vix_score', 0)})"""
        stock_summaries.append(summary)

    today = datetime.now().strftime("%Y-%m-%d")
    stocks_text = "\n".join(stock_summaries)

    prompt = f"""당신은 월스트리트 경력 20년의 미국 주식 트레이딩 전문가이자 최종 의사결정자입니다.

## 당신의 역할
아래 종목들은 자동매매 시스템(팀원)이 ML 예측, 기술적 분석, 감성분석, VIX를 종합하여 매수 후보로 올린 종목입니다.
당신은 팀장으로서 팀원의 분석을 최종 검토하고 BUY 또는 HOLD를 판정합니다.
팀원의 분석이 맞을 수도 있고 틀릴 수도 있으니, 제공된 데이터와 당신의 시장 지식을 종합하여 독립적으로 판단하세요.

## 오늘 날짜
{today}

## 시장 환경
- VIX(공포지수): {vix_value if vix_value else 'N/A'}

## 매수 후보 종목
{stocks_text}

## 검토 기준
아래 항목들을 종합적으로 검토하여 BUY 또는 HOLD를 판정하세요.

### 기술적 지표 검증
- 골든크로스가 발생했지만 현재가가 이동평균선보다 크게 하회하면 유효한 신호인지 의심
- RSI 과매도(< 30)는 반등 기회일 수 있지만, ADX가 약하면(< 20) 추세 없는 횡보일 수 있음
- RSI 과매수(> 70)인 종목이 매수 후보에 포함되었다면 시스템 오류 가능성 → HOLD

### 외부 리스크 확인
- 해당 종목의 실적 발표(Earnings)가 1주일 이내에 예정 → HOLD
- FOMC, CPI 등 주요 매크로 이벤트가 1~2일 내 → HOLD 고려
- 해당 종목의 특이 리스크(CEO 교체, 소송, 규제 등) → HOLD

### 포트폴리오 균형
- 같은 섹터 3개 이상 집중 시 → 가장 약한 종목을 HOLD (전부 HOLD하지 말 것)

## 판정 원칙
- BUY와 HOLD 모두 구체적인 근거를 제시하세요.
- 막연한 불안감이 아닌, 명확한 데이터와 사실에 기반하여 판단하세요.
- 매수할 만한 종목은 매수하고, 위험한 종목은 거부하는 균형 잡힌 판단을 하세요.

## 응답 형식
반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요.
{{
  "market_analysis": "시장 전체에 대한 간단한 분석 (1~2문장)",
  "decisions": [
    {{
      "ticker": "종목 티커",
      "stock_name": "종목명",
      "decision": "BUY 또는 HOLD",
      "reason": "판정 이유 (1~2문장)"
    }}
  ]
}}"""

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    last_error = None

    # 모델별 재시도: Opus 3회 → Sonnet 3회 (총 최대 6회)
    for model in MODELS:
        for attempt in range(MAX_RETRIES):
            try:
                print(f"  LLM 호출 시도 {attempt + 1}/{MAX_RETRIES} (모델: {model})")
                message = client.messages.create(
                    model=model,
                    max_tokens=2000,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}]
                )

                response_text = message.content[0].text.strip()
                # JSON 파싱 (```json ... ``` 래핑 처리)
                if response_text.startswith("```"):
                    response_text = response_text.split("```")[1]
                    if response_text.startswith("json"):
                        response_text = response_text[4:]
                response_data = json.loads(response_text)

                decisions = response_data.get("decisions", [])
                market_analysis = response_data.get("market_analysis", "")
                used_model_note = f" (폴백: {model})" if model != MODELS[0] else ""

                # 티커별 판정 매핑
                decision_map = {d["ticker"]: d for d in decisions}

                reviewed = []
                held = []
                for candidate in candidates:
                    ticker = candidate["ticker"]
                    decision = decision_map.get(ticker, {})
                    llm_decision = decision.get("decision", "HOLD").upper()
                    llm_reason = decision.get("reason", "LLM 응답 없음")

                    candidate["llm_decision"] = llm_decision
                    candidate["llm_reason"] = llm_reason

                    if llm_decision == "BUY":
                        reviewed.append(candidate)
                    else:
                        held.append(candidate)
                        print(f"  LLM HOLD: {candidate['stock_name']}({ticker}) - {llm_reason}")

                print(f"  LLM 검토 완료{used_model_note}: {len(reviewed)} BUY / {len(held)} HOLD")
                print(f"  시장 분석: {market_analysis}")

                # LLM 판단 로그 저장
                _save_llm_decision_logs(candidates, decision_map, market_analysis, vix_value)

                return {
                    "reviewed_candidates": reviewed,
                    "held_candidates": held,
                    "llm_reasoning": market_analysis + used_model_note,
                    "raw_response": decisions
                }

            except json.JSONDecodeError as e:
                print(f"  LLM 응답 JSON 파싱 실패 (시도 {attempt + 1}): {e}")
                print(f"  원본 응답: {response_text[:500]}")
                last_error = e
                # JSON 파싱 실패는 재시도해도 같은 결과일 수 있으므로 바로 다음 모델로
                break

            except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
                last_error = e
                error_code = getattr(e, 'status_code', 0)
                delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else RETRY_DELAYS[-1]

                if error_code == 529 or error_code == 429 or error_code == 503:
                    print(f"  LLM 서버 과부하/속도제한 (시도 {attempt + 1}/{MAX_RETRIES}, 모델: {model}): {e}")
                    print(f"  {delay}초 후 재시도...")
                    time.sleep(delay)
                    continue
                else:
                    # 다른 API 에러는 재시도해도 안 되므로 중단
                    print(f"  LLM API 에러 (시도 {attempt + 1}, 모델: {model}): {e}")
                    break

            except Exception as e:
                last_error = e
                delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else RETRY_DELAYS[-1]
                print(f"  LLM 호출 실패 (시도 {attempt + 1}/{MAX_RETRIES}, 모델: {model}): {e}")
                print(f"  {delay}초 후 재시도...")
                time.sleep(delay)
                continue

        # 현재 모델 전체 실패 → 다음 모델로 폴백
        if model != MODELS[-1]:
            print(f"  {model} 전체 실패. 폴백 모델 {MODELS[MODELS.index(model) + 1]}로 전환합니다.")

    # 모든 모델, 모든 재시도 실패
    fail_reason = f"LLM 호출 전체 실패 (Opus {MAX_RETRIES}회 + Sonnet {MAX_RETRIES}회): {str(last_error)}"
    print(f"  {fail_reason}")

    # 실패 시에도 로그 저장 (사유 기록)
    fail_decision_map = {c["ticker"]: {"decision": "FAIL", "reason": fail_reason} for c in candidates}
    _save_llm_decision_logs(candidates, fail_decision_map, fail_reason, vix_value)

    return {
        "reviewed_candidates": [],
        "held_candidates": candidates,
        "llm_reasoning": fail_reason,
        "raw_response": []
    }
