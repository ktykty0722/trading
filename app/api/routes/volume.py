from fastapi import APIRouter, HTTPException, Query
from app.services.volume_service import (
    get_top_volume_stocks,
    get_volume_surge_stocks,
    get_overseas_daily_price,
    NASDAQ_100,
    KEY_STOCKS,
)

router = APIRouter()


@router.get("/top", summary="거래량 상위 종목 조회")
def get_top_volume_route(
    scope: str = Query("key", description="조회 범위 (key: 주요 25종목, full: 나스닥100)"),
    excd: str = Query("NAS", description="거래소코드 (NAS:나스닥, NYS:뉴욕, AMS:아멕스)"),
    top_n: int = Query(20, description="상위 N개 반환", ge=1, le=100),
):
    """
    나스닥 종목들의 거래량을 조회하고 상위 종목을 반환합니다.

    ### 조회 범위
    - **key**: 주요 25종목 (빠름, ~15초)
    - **full**: 나스닥 100종목 (느림, ~60초)

    ### 반환값
    - 종목별 현재가, 등락률, 거래량, 거래대금
    - 거래량 기준 내림차순 정렬
    """
    try:
        stock_list = NASDAQ_100 if scope == "full" else KEY_STOCKS
        result = get_top_volume_stocks(stock_list=stock_list, excd=excd, top_n=top_n)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"거래량 상위 종목 조회 오류: {str(e)}")


@router.get("/surge", summary="거래량 급등 종목 감지")
def get_volume_surge_route(
    scope: str = Query("key", description="조회 범위 (key: 주요 25종목, full: 나스닥100)"),
    excd: str = Query("NAS", description="거래소코드 (NAS:나스닥, NYS:뉴욕, AMS:아멕스)"),
    days: int = Query(5, description="평균 거래량 산출 기간 (일)", ge=2, le=30),
    surge_ratio: float = Query(2.0, description="급등 기준 배수 (기본 2.0 = 평균대비 2배)", ge=1.0),
):
    """
    최근 거래량이 N일 평균 대비 급등한 종목을 감지합니다.

    ### 조회 범위
    - **key**: 주요 25종목 (빠름)
    - **full**: 나스닥 100종목 (느림, 기간별시세 API 사용으로 더 느림)

    ### 급등 기준
    - surge_ratio=2.0 → 평균 대비 2배 이상 거래량인 종목
    - surge_ratio=3.0 → 평균 대비 3배 이상 (더 강한 급등)

    ### 활용
    - 거래량 급등은 주가 방향 전환의 선행 신호가 될 수 있음
    - 자동매매 시 거래량 급등 종목을 우선 매수 대상으로 활용 가능
    """
    try:
        stock_list = NASDAQ_100 if scope == "full" else KEY_STOCKS
        result = get_volume_surge_stocks(
            stock_list=stock_list, excd=excd, days=days, surge_ratio=surge_ratio
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"거래량 급등 종목 감지 오류: {str(e)}")


@router.get("/daily/{ticker}", summary="개별 종목 기간별 시세 조회 (거래량 포함)")
def get_daily_price_route(
    ticker: str,
    excd: str = Query("NAS", description="거래소코드 (NAS:나스닥, NYS:뉴욕, AMS:아멕스)"),
    period: str = Query("0", description="기간 구분 (0:일봉, 1:주봉, 2:월봉)"),
    bymd: str = Query("", description="조회기준일 (YYYYMMDD, 비우면 최근)"),
):
    """
    개별 종목의 기간별 시세를 조회합니다. (종가, 거래량, 거래대금 포함)

    ### 반환값
    - output1: 현재 시세 요약
    - output2: 일별/주별/월별 시세 배열 (거래량 tvol, 거래대금 tamt 포함)
    """
    try:
        result = get_overseas_daily_price(excd, ticker.upper(), gubn=period, bymd=bymd)
        if result.get("rt_cd") != "0":
            raise HTTPException(
                status_code=400, detail=result.get("msg1", "조회 실패")
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"기간별 시세 조회 오류: {str(e)}")
