from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from app.core.config import settings
from app.services.balance_service import (
    get_domestic_balance, 
    get_overseas_balance,  
    overseas_order_resv, 
    inquire_psamount, 
    get_current_price,
    get_overseas_nccs,
    get_overseas_order_detail,
    get_overseas_order_resv_list,
    order_overseas_stock,
    create_conditional_orders,
)

router = APIRouter()

@router.get("/", summary="국내주식 잔고 조회")
def read_balance():
    try:
        result = get_domestic_balance()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"잔고 조회 중 오류 발생: {str(e)}")

@router.get("/overseas", summary="해외주식 잔고 조회")
def read_balance_overseas():
    """
    해외주식 잔고 조회 API

    ### 응답
    - 성공 시: 해외주식 잔고 정보 반환
    - 실패 시: 오류 메시지와 함께 HTTP 상태 코드 반환
    """
    try:
        result = get_overseas_balance()  # 해외주식 잔고 조회
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"잔고 조회 중 오류 발생: {str(e)}")



# 해외주식 예약주문 접수 요청 모델
class OrderResvRequest(BaseModel):
    pdno: str  # 종목 코드 (예: AAPL)
    ovrs_excg_cd: str  # 거래소 코드 (예: NASD - 나스닥)
    ft_ord_qty: str  # 주문 수량 (예: 1)
    ft_ord_unpr3: str  # 주문 단가 (예: 148.00)
    is_buy: bool = True  # 매수 여부 (True: 매수, False: 매도)
    ord_dvsn: str = "00"  # 주문구분 (00: 지정가, 31: MOO - 미국 매도 예약주문만 가능)

@router.post("/order-resv", summary="해외주식 예약주문 접수")
def order_resv_route(order: OrderResvRequest):
    """
    해외주식 예약주문 접수 API

    미국 예약주문 접수시간
    1) 10:00 ~ 23:20 / 10:00 ~ 22:20 (서머타임 시)
    2) 주문제한 : 16:30 ~ 16:45 경까지 (사유 : 시스템 정산작업시간)
    3) 23:30 정규장으로 주문 전송 (서머타임 시 22:30 정규장 주문 전송)
    4) 미국 거래소 운영시간(한국시간 기준) : 23:30 ~ 06:00 (썸머타임 적용 시 22:30 ~ 05:00)

    ### 입력값 설명
    - **pdno**: 종목 코드 (예: AAPL - 애플 주식)
    - **ovrs_excg_cd**: 거래소 코드 (예: NASD - 나스닥, NYSE - 뉴욕증권거래소)
    - **ft_ord_qty**: 주문 수량 (예: 1 - 1주 매수)
    - **ft_ord_unpr3**: 주문 단가 (예: 148.00 - 달러 단위로 소수점 2자리까지)
    - **is_buy**: 매수 여부 (True: 매수, False: 매도) - 거래소에 따라 알맞은 TR_ID가 자동 지정됨
    - **ord_dvsn**: 주문구분 
        - "00": 지정가 (전 거래소 공통)
        - "31": MOO(장개시시장가) - 미국 매도 예약주문만 가능
    
    ### 유의사항
    - 미국 외 거래소(중국/홍콩/일본/베트남)는 매수/매도 구분을 위해 is_buy 값을 사용합니다.
    - 미국 매도 예약주문에서만 MOO(장개시시장가) 주문이 가능합니다.
    - 지정한 시간에 주문이 자동으로 전송됩니다.
    - 예약주문의 유효기간은 당일입니다.

    ### 응답
    - 성공 시: 주문 접수 결과 반환
    - 실패 시: 오류 메시지와 함께 HTTP 상태 코드 반환
    """
    try:
        order_data = {
            "CANO": settings.KIS_CANO,  # 계좌번호 (환경변수에서 가져옴)
            "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,  # 계좌상품코드 (환경변수에서 가져옴)
            "PDNO": order.pdno,
            "OVRS_EXCG_CD": order.ovrs_excg_cd,
            "FT_ORD_QTY": order.ft_ord_qty,
            "FT_ORD_UNPR3": order.ft_ord_unpr3,
            "is_buy": order.is_buy,  # 매수/매도 여부
            "ORD_DVSN": order.ord_dvsn,  # 주문 구분 (지정가/MOO 등)
            "ORD_SVR_DVSN_CD": "0"  # 주문 서버 구분 코드 (기본값)
        }
        result = overseas_order_resv(order_data)

        if result.get("rt_cd") != "0":
            raise HTTPException(status_code=400, detail=result.get("msg1", "주문 접수 실패"))
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"예약주문 접수 중 오류 발생: {str(e)}")
    
@router.get("/inquire-psamount", summary="해외주식 매수가능금액 조회")
def inquire_psamount_route(
    ovrs_excg_cd: str,
    item_cd: str,
    ovrs_ord_unpr: str
):
    """
    해외주식 매수가능금액 조회 API

    ### 입력값 설명
    - **ovrs_excg_cd**: 거래소 코드 (예: NASD - 나스닥, NYSE - 뉴욕증권거래소)
    - **item_cd**: 종목 코드 (예: AAPL - 애플 주식)
    - **ovrs_ord_unpr**: 주문 단가 (예: 148.00 - 달러 단위로 소수점 2자리까지)

    ### 응답
    - 성공 시: 매수가능 금액 및 수량 반환
    - 실패 시: 오류 메시지와 함께 HTTP 상태 코드 반환
    """
    try:
        params = {
            "CANO": settings.KIS_CANO,  # 계좌번호 (환경변수에서 가져옴)
            "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,  # 계좌상품코드 (환경변수에서 가져옴)
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "ITEM_CD": item_cd,
            "OVRS_ORD_UNPR": ovrs_ord_unpr
        }
        result = inquire_psamount(params)

        if result.get("rt_cd") != "0":
            raise HTTPException(status_code=400, detail=result.get("msg1", "조회 실패"))
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"매수가능금액 조회 중 오류 발생: {str(e)}")
    
@router.get("/quotations/price", summary="해외주식 현재체결가 조회")
def get_current_price_route(
    excd: str,
    symb: str
):
    """
    해외주식 현재체결가 조회 API

    ### 입력값 설명
    - **excd**: 거래소 코드 (예: NAS - 나스닥, NYS - 뉴욕증권거래소)
    - **symb**: 종목 코드 (예: TSLA - 테슬라 주식)

    ### 응답
    - 성공 시: 현재 체결가 반환
    - 실패 시: 오류 메시지와 함께 HTTP 상태 코드 반환
    """
    try:
        params = {
            "EXCD": excd,
            "SYMB": symb
        }
        result = get_current_price(params)

        if result.get("rt_cd") != "0":
            raise HTTPException(status_code=400, detail=result.get("msg1", "조회 실패"))
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"현재체결가 조회 중 오류 발생: {str(e)}")

@router.get("/nccs", summary="해외주식 미체결내역 조회 (모의투자 환경에서는 지원되지 않습니다.)")
def get_overseas_nccs_route(
    ovrs_excg_cd: str = Query(..., description="거래소 코드 (예: NASD - 나스닥, NYSE - 뉴욕증권거래소)"),
    sort_sqn: str = Query("DS", description="정렬순서 (DS: 정순, 그외: 역순)")
):
    """
    해외주식 미체결내역 조회 API
    """
    try:
        # 기본 파라미터 설정
        base_params = {
            "CANO": settings.KIS_CANO,
            "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "SORT_SQN": sort_sqn,
        }
        
        # 환경변수에서 모의투자 여부 확인
        is_virtual = "openapivts" in settings.kis_base_url
        
        if is_virtual:
            # 모의투자: 현재 날짜 기준으로 지난 7일 데이터만 조회
            from datetime import datetime, timedelta
            today = datetime.now()
            # 일주일 전으로 설정 (더 짧은 기간으로 테스트)
            seven_days_ago = today - timedelta(days=7)
            
            params = {
                **base_params,
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "INQR_ST_DT": seven_days_ago.strftime("%Y%m%d"),
                "INQR_END_DT": today.strftime("%Y%m%d"),
            }
            result = get_overseas_order_detail(params)
        else:
            # 실전투자: 미체결내역 API 사용
            params = {
                **base_params,
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            }
            result = get_overseas_nccs(params)
        
        if result.get("rt_cd") != "0" and result.get("rt_cd") != "1":
            raise HTTPException(status_code=400, detail=result.get("msg1", "조회 실패"))
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"미체결내역 조회 중 오류 발생: {str(e)}")

@router.get("/order-resv-list", summary="해외주식 예약주문 조회 (모의투자 환경에서는 지원되지 않습니다.)")
def get_overseas_order_resv_list_route(
    ovrs_excg_cd: str = Query(None, description="거래소 코드 (예: NASD - 나스닥, NYSE - 뉴욕증권거래소)"),
    inqr_strt_dt: str = Query(..., description="조회 시작일자 (YYYYMMDD)"),
    inqr_end_dt: str = Query(..., description="조회 종료일자 (YYYYMMDD)"),
    inqr_dvsn_cd: str = Query("00", description="조회구분코드 (00: 전체, 01: 일반해외주식, 02: 미니스탁)"),
    prdt_type_cd: str = Query("", description="상품유형코드 (공백: 전체, 512: 미국 나스닥, 515: 일본, 등)")
):
    """
    해외주식 예약주문 조회 API

    ### 입력값 설명
    - **ovrs_excg_cd**: 거래소 코드 (예: NASD - 나스닥, NYSE - 뉴욕)
    - **inqr_strt_dt**: 조회 시작일자 (YYYYMMDD 형식)
    - **inqr_end_dt**: 조회 종료일자 (YYYYMMDD 형식)
    - **inqr_dvsn_cd**: 조회구분코드 (00: 전체, 01: 일반해외주식, 02: 미니스탁)
    - **prdt_type_cd**: 상품유형코드 (공백: 전체조회)
    
    ### 응답
    - 성공 시: 예약주문 내역 반환
    - 실패 시: 오류 메시지와 함께 HTTP 상태 코드 반환
    
    ※ 모의투자 환경에서는 이 API가 지원되지 않습니다.
    """
    try:
        # 날짜 형식 검증
        from datetime import datetime
        try:
            start_date = datetime.strptime(inqr_strt_dt, "%Y%m%d")
            end_date = datetime.strptime(inqr_end_dt, "%Y%m%d")
            
            # 종료일이 시작일보다 이전인지 확인
            if end_date < start_date:
                raise HTTPException(status_code=400, detail="종료일은 시작일 이후여야 합니다.")
        except ValueError:
            raise HTTPException(status_code=400, detail="날짜 형식이 올바르지 않습니다. YYYYMMDD 형식으로 입력하세요.")
        
        # 파라미터 설정
        params = {
            "CANO": settings.KIS_CANO,
            "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
            "INQR_STRT_DT": inqr_strt_dt,
            "INQR_END_DT": inqr_end_dt,
            "INQR_DVSN_CD": inqr_dvsn_cd,
            "PRDT_TYPE_CD": prdt_type_cd,
            "OVRS_EXCG_CD": ovrs_excg_cd if ovrs_excg_cd else "",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": ""
        }
        
        from app.services.balance_service import get_overseas_order_resv_list
        result = get_overseas_order_resv_list(params)
        
        # 모의투자 환경에서는 안내 메시지 반환
        if result.get("msg_cd") == "MOCK_UNSUPPORTED":
            return result
        
        if result.get("rt_cd") != "0":
            status_code = 400 if result.get("rt_cd") == "1" else 500
            raise HTTPException(status_code=status_code, detail=result.get("msg1", "예약주문 조회 실패"))
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"예약주문 조회 중 오류 발생: {str(e)}")

# 해외주식 주문 요청 모델
class OrderOverseasRequest(BaseModel):
    pdno: str  # 종목 코드 (예: AAPL)
    ovrs_excg_cd: str  # 거래소 코드 (예: NASD - 나스닥)
    ord_qty: str  # 주문 수량 (예: 1)
    ovrs_ord_unpr: str  # 주문 단가 (예: 148.00)
    is_buy: bool = True  # 매수 여부 (True: 매수, False: 매도)
    ord_dvsn: str = "00"  # 주문구분 (00: 지정가)

@router.post("/order-overseas", summary="해외주식 매수/매도 주문")
def order_overseas_stock_route(request: OrderOverseasRequest):
    """
    해외주식 매수/매도 주문 API
    
    ### 입력값 설명
    - **pdno**: 종목 코드 (예: AAPL - 애플)
    - **ovrs_excg_cd**: 거래소 코드 (예: NASD - 나스닥)
    - **ord_qty**: 주문 수량 (예: 1)
    - **ovrs_ord_unpr**: 주문 단가 (예: 180.00)
    - **is_buy**: 매수 여부 (True: 매수, False: 매도)
    - **ord_dvsn**: 주문구분 (00: 지정가, 그 외 거래소별 문서 참조)
    
    ### 응답
    - 성공 시: 주문 접수 결과 반환
    - 실패 시: 오류 메시지와 함께 HTTP 상태 코드 반환
    """
    try:
        # 주문 데이터 준비
        order_data = {
            "CANO": settings.KIS_CANO,  # 계좌번호 앞 8자리
            "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,  # 계좌번호 뒤 2자리
            "PDNO": request.pdno,  # 종목코드
            "OVRS_EXCG_CD": request.ovrs_excg_cd,  # 해외거래소코드
            "ORD_QTY": request.ord_qty,  # 주문수량
            "OVRS_ORD_UNPR": request.ovrs_ord_unpr,  # 주문단가
            "is_buy": request.is_buy,  # 매수 여부
            "ORD_DVSN": request.ord_dvsn  # 주문구분
        }
        
        # 서비스 함수 호출
        result = order_overseas_stock(order_data)
        
        # 결과 확인
        if result.get("rt_cd") != "0":
            error_msg = result.get("msg1", "주문 처리 중 오류가 발생했습니다")
            raise HTTPException(status_code=400, detail=error_msg)
            
        return result
    except HTTPException:
        raise
    except Exception as e:
        print(f"해외주식 주문 처리 중 오류 발생: {str(e)}")
        raise HTTPException(status_code=500, detail=f"주문 처리 중 오류가 발생했습니다: {str(e)}")

# 조건부 주문 요청 모델
class ConditionalOrderRequest(BaseModel):
    pdno: str  # 종목 코드 (예: AAPL)
    ovrs_excg_cd: str  # 거래소 코드 (예: NASD)
    base_price: float  # 기준 가격
    stop_loss_percent: Optional[float] = None  # 손절매 퍼센트 (예: -5.0)
    take_profit_percent: Optional[float] = None  # 이익실현 퍼센트 (예: 5.0)
    quantity: str  # 주문 수량

@router.post("/conditional-order", summary="조건부 주문 설정")
def conditional_order_route(request: ConditionalOrderRequest):
    """
    특정 가격에 도달했을 때 자동으로 실행되는 조건부 주문 설정
    
    ### 입력값 설명
    - **pdno**: 종목 코드 (예: AAPL)
    - **ovrs_excg_cd**: 거래소 코드 (예: NASD)
    - **base_price**: 기준 가격 (지정하지 않으면 보유 주식의 매수 가격으로 설정됨)
    - **stop_loss_percent**: 손절매 퍼센트 (예: -5.0)
    - **take_profit_percent**: 이익실현 퍼센트 (예: 5.0)
    - **quantity**: 주문 수량
    
    ### 예시
    - base_price가 100달러이고 stop_loss_percent가 -5.0이면, 주가가 95달러에 도달했을 때 매도 주문 실행
    - base_price가 100달러이고 take_profit_percent가 5.0이면, 주가가 105달러에 도달했을 때 매도 주문 실행
    """
    try:
        # 요청 데이터 준비
        params = {
            "pdno": request.pdno,
            "ovrs_excg_cd": request.ovrs_excg_cd,
            "base_price": request.base_price if request.base_price else None,
            "stop_loss_percent": request.stop_loss_percent if request.stop_loss_percent is not None else -5.0,
            "take_profit_percent": request.take_profit_percent if request.take_profit_percent is not None else 5.0,
            "quantity": request.quantity
        }
        
        # 조건부 주문 실행
        result = create_conditional_orders(params)
        
        # 결과 확인
        if result.get("rt_cd") != "0":
            error_msg = result.get("msg1", "조건부 주문 설정 중 오류가 발생했습니다")
            raise HTTPException(status_code=400, detail=error_msg)
            
        return result
    except HTTPException:
        raise
    except Exception as e:
        print(f"조건부 주문 처리 중 오류 발생: {str(e)}")
        raise HTTPException(status_code=500, detail=f"조건부 주문 처리 중 오류가 발생했습니다: {str(e)}")
