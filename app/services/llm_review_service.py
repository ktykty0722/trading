import json
import logging
import time
from datetime import datetime

import openai
from app.core.config import settings
from app.db.supabase import supabase

logger = logging.getLogger(__name__)

MAX_RETRIES   = 3
RETRY_DELAYS  = [5, 15, 30]
MODELS        = ["gpt-4o", "gpt-4o-mini"]  # gpt-4o 실패 시 gpt-4o-mini 폴백


def _save_llm_decision_logs(candidates: list, decision_map: dict, market_analysis: str, vix_value: float = None):
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        for candidate in candidates:
            ticker = candidate["ticker"]
            decision_data = decision_map.get(ticker, {})
            supabase.table("llm_decision_logs").upsert({
                "decision_date":  today,
                "ticker":         ticker,
                "stock_name":     candidate.get("stock_name"),
                "decision":       decision_data.get("decision", "N/A"),
                "reason":         decision_data.get("reason", ""),
                "market_analysis":market_analysis,
                "composite_score":candidate.get("composite_score"),
                "rise_probability":candidate.get("rise_probability"),
                "rsi":            candidate.get("rsi"),
                "adx":            candidate.get("adx"),
                "vix_value":      vix_value,
                "updated_at":     datetime.now().isoformat(),
            }, on_conflict="decision_date,ticker").execute()
        logger.info(f"LLM 판단 로그 저장 완료: {len(candidates)}건")
    except Exception as e:
        logger.error(f"LLM 판단 로그 저장 실패: {e}")


def review_buy_candidates(candidates: list, vix_value: float = None) -> dict:
    """
    매수 후보 종목을 OpenAI API로 최종 검토합니다.
    LLM은 거부권만 있습니다 (BUY → HOLD로만 변경 가능, 새 종목 추가 불가).
    LLM 호출 실패 시 매수를 차단합니다 (Fail-Close).
    """
    if not settings.OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY가 설정되지 않았습니다. LLM 검토 불가로 매수 차단.")
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

    stock_summaries = []
    for i, c in enumerate(candidates, 1):
        rsi = c.get('rsi', 50)
        rsi_note = '(과매도 반등)' if rsi < 30 else '(강세 진입)' if 50 <= rsi <= 65 else '(매수구간 아님)'
        adx = c.get('adx')
        adx_note = '(강한 추세)' if adx and adx > 25 else '(추세 약함)' if adx and adx < 20 else '(보통)'
        stock_summaries.append(f"""
{i}. {c.get('stock_name', 'N/A')} ({c.get('ticker', 'N/A')})
   - ML 예측: 상승확률 {c.get('rise_probability', 0):.2f}%, 예측가 ${c.get('predicted_price', 0):.2f} (현재가 ${c.get('last_price', 0):.2f})
   - 기술적 지표:
     골든크로스: {'✓' if c.get('golden_cross') else '✗'} (SMA20: {c.get('sma20', 0):.2f}, SMA50: {c.get('sma50', 0):.2f})
     RSI: {rsi:.2f} {rsi_note}
     MACD: {c.get('macd', 0):.4f}, Signal: {c.get('signal', 0):.4f}, 매수신호: {'✓' if c.get('macd_buy_signal') else '✗'}
   - 거래량: 5일 평균 대비 {c.get('volume_ratio', 'N/A')}배
   - ADX(추세강도): {adx} {adx_note}
   - 감성분석: {c.get('sentiment_score', 'N/A')} (기사 {c.get('article_count', 0)}개)
   - 종합점수: {c.get('composite_score', 0):.4f}
     (상승확률: {c.get('rise_score', 0)}, 기술: {c.get('tech_score', 0)}, 거래량: {c.get('volume_score', 0)}, ADX: {c.get('adx_score', 0)}, VIX: {c.get('vix_score', 0)})""")

    today     = datetime.now().strftime("%Y-%m-%d")
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

    client     = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
    last_error = None

    for model in MODELS:
        for attempt in range(MAX_RETRIES):
            try:
                logger.info(f"LLM 호출 시도 {attempt + 1}/{MAX_RETRIES} (모델: {model})")
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    max_tokens=2000,
                    response_format={"type": "json_object"},
                )
                response_text = response.choices[0].message.content.strip()
                response_data = json.loads(response_text)

                decisions       = response_data.get("decisions", [])
                market_analysis = response_data.get("market_analysis", "")
                used_model_note = f" (폴백: {model})" if model != MODELS[0] else ""
                decision_map    = {d["ticker"]: d for d in decisions}

                reviewed, held = [], []
                for candidate in candidates:
                    ticker      = candidate["ticker"]
                    decision    = decision_map.get(ticker, {})
                    llm_decision = decision.get("decision", "HOLD").upper()
                    llm_reason   = decision.get("reason", "LLM 응답 없음")

                    candidate["llm_decision"] = llm_decision
                    candidate["llm_reason"]   = llm_reason

                    if llm_decision == "BUY":
                        reviewed.append(candidate)
                    else:
                        held.append(candidate)
                        logger.info(f"LLM HOLD: {candidate['stock_name']}({ticker}) - {llm_reason}")

                logger.info(f"LLM 검토 완료{used_model_note}: {len(reviewed)} BUY / {len(held)} HOLD")
                logger.info(f"시장 분석: {market_analysis}")

                _save_llm_decision_logs(candidates, decision_map, market_analysis, vix_value)

                return {
                    "reviewed_candidates": reviewed,
                    "held_candidates":     held,
                    "llm_reasoning":       market_analysis + used_model_note,
                    "raw_response":        decisions,
                }

            except json.JSONDecodeError as e:
                logger.warning(f"LLM 응답 JSON 파싱 실패 (시도 {attempt + 1}): {e}")
                last_error = e
                break  # JSON 파싱 실패는 재시도해도 같은 결과 → 다음 모델로

            except openai.RateLimitError as e:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                logger.warning(f"LLM 속도 제한 (시도 {attempt + 1}/{MAX_RETRIES}, {model}): {e} → {delay}초 대기")
                last_error = e
                time.sleep(delay)

            except openai.APIStatusError as e:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                if e.status_code in (429, 503, 529):
                    logger.warning(f"LLM 서버 과부하 (시도 {attempt + 1}/{MAX_RETRIES}, {model}): {e} → {delay}초 대기")
                    last_error = e
                    time.sleep(delay)
                else:
                    logger.error(f"LLM API 에러 (시도 {attempt + 1}, {model}): {e}")
                    last_error = e
                    break  # 다른 API 에러는 재시도 불필요 → 다음 모델로

            except Exception as e:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                logger.warning(f"LLM 호출 실패 (시도 {attempt + 1}/{MAX_RETRIES}, {model}): {e} → {delay}초 대기")
                last_error = e
                time.sleep(delay)

        if model != MODELS[-1]:
            next_model = MODELS[MODELS.index(model) + 1]
            logger.warning(f"{model} 전체 실패. 폴백 모델 {next_model}로 전환합니다.")

    fail_reason = f"LLM 호출 전체 실패 (gpt-4o {MAX_RETRIES}회 + gpt-4o-mini {MAX_RETRIES}회): {last_error}"
    logger.error(fail_reason)

    fail_decision_map = {c["ticker"]: {"decision": "FAIL", "reason": fail_reason} for c in candidates}
    _save_llm_decision_logs(candidates, fail_decision_map, fail_reason, vix_value)

    return {
        "reviewed_candidates": [],
        "held_candidates":     candidates,
        "llm_reasoning":       fail_reason,
        "raw_response":        [],
    }
