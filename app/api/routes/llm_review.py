from fastapi import APIRouter
from app.services.stock_recommendation_service import StockRecommendationService
from app.services.llm_review_service import review_buy_candidates

router = APIRouter()


@router.post("/review-buy-candidates", summary="LLM 매수 후보 최종 검토")
def llm_review_buy_candidates():
    """
    현재 매수 후보 종목을 Claude API로 최종 검토합니다.
    기존 매수 로직(ML + 기술 + 감성 + 점수)을 통과한 종목에 대해
    LLM이 거부권을 행사합니다 (BUY → HOLD만 가능).
    """
    service = StockRecommendationService()

    # 1. 기존 매수 로직으로 후보 추출
    recommendations = service.get_combined_recommendations_with_technical_and_sentiment()
    candidates = recommendations.get("results", [])

    if not candidates:
        return {
            "message": "매수 후보가 없어 LLM 검토를 건너뜁니다",
            "candidates_before": 0,
            "candidates_after": 0,
            "held": 0,
            "results": [],
            "held_results": [],
            "llm_reasoning": ""
        }

    # VIX 값 추출 (candidates에 이미 포함)
    vix_value = candidates[0].get("vix_value") if candidates else None

    # 2. LLM 검토
    review_result = review_buy_candidates(candidates, vix_value)

    return {
        "message": f"LLM 검토 완료: {len(review_result['reviewed_candidates'])} BUY / {len(review_result['held_candidates'])} HOLD",
        "candidates_before": len(candidates),
        "candidates_after": len(review_result["reviewed_candidates"]),
        "held": len(review_result["held_candidates"]),
        "results": review_result["reviewed_candidates"],
        "held_results": review_result["held_candidates"],
        "llm_reasoning": review_result["llm_reasoning"]
    }
