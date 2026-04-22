import requests
import json
import time
from datetime import datetime, timedelta
import pytz
from app.core.config import settings
from app.db.supabase import supabase
from threading import Lock
from app.services.auth_service import parse_expiration_date

# 메모리에 토큰 정보 저장 (캐싱)
_token_cache = {
    "access_token": None,
    "expires_at": None
}
_last_refresh_time = 0  # 마지막 토큰 갱신 시간
_refresh_lock = Lock()  # 동시성 방지 락

def get_access_token():
    """한국투자증권 API 접근 토큰 발급 또는 캐시된 토큰 반환"""
    global _token_cache, _last_refresh_time
    
    # 현재 시간
    now = datetime.now(pytz.UTC)
    
    # 메모리에 캐시된 토큰이 있고 유효하면 그것을 사용
    if _token_cache["access_token"] and _token_cache["expires_at"] and now < _token_cache["expires_at"]:
        print("메모리에 캐시된 토큰 사용")
        return _token_cache["access_token"]
    
    # 1분 제한 체크 및 락 획득
    current_time = time.time()
    if current_time - _last_refresh_time < 60:
        time_to_wait = 60 - (current_time - _last_refresh_time)
        print(f"1분 제한으로 {time_to_wait:.1f}초 대기")
        time.sleep(time_to_wait)
    
    with _refresh_lock:  # 동시성 방지
        # 락 획득 후 다시 캐시 확인
        if _token_cache["access_token"] and _token_cache["expires_at"] and now < _token_cache["expires_at"]:
            print("락 내에서 캐시된 토큰 사용")
            return _token_cache["access_token"]
        
        try:
            # 테이블에서 토큰 레코드 조회
            response = supabase.table("access_tokens").select("*").order("created_at", desc=True).limit(1).execute()
            
            if response.data:
                token_data = response.data[0]
                
                # 이 부분을 수정 - auth_service의 parse_expiration_date 함수 사용
                expiration_time = parse_expiration_date(token_data["expiration_time"])
                
                if now < expiration_time:  # 토큰이 아직 유효한 경우
                    print(f"기존 토큰 사용 - 만료까지 남은 시간: {(expiration_time - now)}")
                    _token_cache["access_token"] = token_data["access_token"]
                    _token_cache["expires_at"] = expiration_time
                    _last_refresh_time = current_time
                    return token_data["access_token"]
                
                print("토큰 만료됨, 갱신 필요")
                # 토큰이 만료된 경우 갱신
                token = refresh_token_with_retry(token_data["id"])
                _token_cache["access_token"] = token
                _token_cache["expires_at"] = now + timedelta(days=1)
                _last_refresh_time = current_time
                return token
            else:
                print("토큰 레코드 없음, 새로 생성")
                token = refresh_token_with_retry()
                _token_cache["access_token"] = token
                _token_cache["expires_at"] = now + timedelta(days=1)
                _last_refresh_time = current_time
                return token
                
        except Exception as e:
            print(f"토큰 조회 오류: {str(e)}")
            if _token_cache["access_token"]:
                print("DB 조회 오류 - 메모리에 캐시된 토큰 사용")
                return _token_cache["access_token"]
            raise Exception(f"토큰 발급 실패: {str(e)}")

def refresh_token_with_retry(record_id=None, max_retries=3):
    """토큰 갱신을 재시도하며 처리"""
    for attempt in range(max_retries):
        try:
            url = f"{settings.kis_base_url}/oauth2/tokenP"
            data = {
                "grant_type": "client_credentials",
                "appkey": settings.KIS_APPKEY,
                "appsecret": settings.KIS_APPSECRET
            }
            
            response = requests.post(url, json=data)
            response_data = response.json()
            
            if 'access_token' not in response_data:
                raise Exception(f"토큰 발급 실패: {response_data}")
            
            access_token = response_data["access_token"]
            expires_in = response_data.get("expires_in", 86400)  # 기본값 24시간(초)
            now = datetime.now(pytz.UTC)
            expiration_time = now + timedelta(seconds=expires_in)
            
            token_data = {
                "access_token": access_token,
                "expiration_time": expiration_time.isoformat(),
                "is_active": True
            }
            
            # 레코드 ID가 있으면 업데이트, 없으면 새로 생성
            if record_id:
                supabase.table("access_tokens").update(token_data).eq("id", record_id).execute()
                print("토큰 업데이트 완료")
            else:
                supabase.table("access_tokens").insert(token_data).execute()
                print("새 토큰 레코드 생성 완료")
            
            return access_token
            
        except Exception as e:
            print(f"토큰 갱신 오류 (시도 {attempt+1}/{max_retries}): {str(e)}")
            if "EGW00133" in str(e) and attempt < max_retries - 1:
                print("1분 제한 에러 발생, 61초 대기 후 재시도")
                time.sleep(61)  # 1분 이상 대기
            else:
                raise

def get_domestic_balance():
    """국내주식 잔고 조회"""
    # 토큰 가져오기
    access_token = get_access_token()
    
    url = f"{settings.kis_base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
    
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {access_token}",
        "appkey": settings.KIS_APPKEY,
        "appsecret": settings.KIS_APPSECRET,
        "tr_id": settings.TR_ID  # 국내주식 잔고 조회 TR ID
    }
    
    params = {
        "CANO": settings.KIS_CANO,
        "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    
    max_retries = 2
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, params=params)
            result = response.json()
            
            # API 응답에 오류가 있고, 재시도 가능한 경우
            if 'rt_cd' in result and result['rt_cd'] != '0' and attempt < max_retries - 1:
                msg1 = result.get('msg1', '알 수 없는 오류')
                print(f"API 오류: {result.get('msg_cd', 'N/A')} - {msg1}. 재시도...")
                if "초당" in msg1:
                    time.sleep(2)
                else:
                    access_token = get_access_token()
                    headers["authorization"] = f"Bearer {access_token}"
                    time.sleep(1)
                continue

            return result

        except Exception as e:
            print(f"잔고 조회 중 오류 발생 (시도 {attempt+1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise

def get_overseas_balance(ovrs_excg_cd="NASD"):
    """해외주식 잔고 조회
    
    Args:
        ovrs_excg_cd (str, optional): 거래소 코드. Defaults to "NASD".
            NASD: 나스닥, NYSE: 뉴욕, AMEX: 아멕스
    """
    # 토큰 가져오기
    access_token = get_access_token()
    
    url = f"{settings.kis_base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
    
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {access_token}",
        "appkey": settings.KIS_APPKEY,
        "appsecret": settings.KIS_APPSECRET,
        "tr_id": "VTTS3012R"  # 해외주식 잔고 조회 TR ID
    }
    
    params = {
        "CANO": settings.KIS_CANO,
        "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
        "OVRS_EXCG_CD": ovrs_excg_cd,  # 매개변수로 받은 거래소 코드 사용
        "TR_CRCY_CD": "USD",     # 통화코드 USD
        "CTX_AREA_FK200": "",
        "CTX_AREA_NK200": ""
    }
    
    max_retries = 2
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, params=params)
            result = response.json()
            
            # API 응답에 오류가 있고, 재시도 가능한 경우
            if 'rt_cd' in result and result['rt_cd'] != '0' and attempt < max_retries - 1:
                msg1 = result.get('msg1', '알 수 없는 오류')
                print(f"API 오류: {result.get('msg_cd', 'N/A')} - {msg1}. 재시도...")
                # 초당 거래건수 초과인 경우 대기만, 그 외는 토큰 갱신
                if "초당" in msg1:
                    time.sleep(2)
                else:
                    access_token = get_access_token()
                    headers["authorization"] = f"Bearer {access_token}"
                    time.sleep(1)
                continue

            return result

        except Exception as e:
            print(f"잔고 조회 중 오류 발생 (시도 {attempt+1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise

def get_all_overseas_balances():
    """모든 거래소의 해외주식 잔고 조회"""
    # 주요 거래소 목록
    exchanges = ["NASD", "NYSE", "AMEX"]
    all_holdings = []
    
    for exchange in exchanges:
        try:
            result = get_overseas_balance(exchange)
            
            if result.get("rt_cd") == "0" and "output1" in result:
                holdings = result.get("output1", [])
                if holdings:
                    all_holdings.extend(holdings)
            else:
                print(f"{exchange} 거래소 잔고 조회 실패: {result.get('msg1', '알 수 없는 오류')}")
                
            # API 요청 간 지연 (KIS 초당 거래건수 제한 방지)
            time.sleep(1)
            
        except Exception as e:
            print(f"{exchange} 거래소 잔고 조회 중 오류: {str(e)}")
    
    # 통합된 잔고 정보 반환
    if all_holdings:
        return {
            "rt_cd": "0",
            "msg_cd": "00000",
            "msg1": "모든 거래소 잔고 조회 완료",
            "output1": all_holdings,
            "output2": {}  # 합산 정보는 필요시 계산
        }
    else:
        return {
            "rt_cd": "0",
            "msg_cd": "00000",
            "msg1": "보유 종목이 없습니다.",
            "output1": [],
            "output2": {}
        }

# 추가: 해외주식 예약주문 접수
def overseas_order_resv(order_data):
    """해외주식 예약주문 접수"""
    try:
        access_token = get_access_token()
        url = f"{settings.kis_base_url}/uapi/overseas-stock/v1/trading/order-resv"
        
        # 모의투자 여부 확인
        is_virtual = "openapivts" in settings.kis_base_url
        
        # 매수/매도 여부 및 거래소에 따라 TR_ID 결정
        is_buy = order_data.get("is_buy", True)
        ovrs_excg_cd = order_data.get("OVRS_EXCG_CD", "")
        
        if ovrs_excg_cd in ["NASD", "NYSE", "AMEX"]:  # 미국 주식
            if is_buy:
                tr_id = "VTTT3014U" if is_virtual else "TTTT3014U"  # 미국 매수 예약
            else:
                tr_id = "VTTT3016U" if is_virtual else "TTTT3016U"  # 미국 매도 예약
        else:  # 기타 거래소
            tr_id = "VTTS3013U" if is_virtual else "TTTS3013U"  # 중국/홍콩/일본/베트남 예약
            
            # 중국/홍콩/일본/베트남의 경우 매수/매도 구분 코드 추가
            if not is_buy:
                order_data["SLL_BUY_DVSN_CD"] = "01"  # 매도
            else:
                order_data["SLL_BUY_DVSN_CD"] = "02"  # 매수
        
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {access_token}",
            "appkey": settings.KIS_APPKEY,
            "appsecret": settings.KIS_APPSECRET,
            "tr_id": tr_id
        }
        
        # 필수 파라미터를 포함한 요청 데이터 준비
        request_body = order_data.copy()
        if "is_buy" in request_body:
            del request_body["is_buy"]  # API 요청에는 필요 없는 필드 제거
            
        # 필수 파라미터 설정
        request_body["RVSE_CNCL_DVSN_CD"] = "00"  # 정정취소구분코드 (00: 주문시 필수)
        
        response = requests.post(url, headers=headers, json=request_body)
        result = response.json()
        
        return result
    except Exception as e:
        print(f"예약주문 접수 중 오류 발생: {str(e)}")
        raise

def inquire_psamount(params):
    """해외주식 매수가능금액 조회"""
    try:
        access_token = get_access_token()
        url = f"{settings.kis_base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
        tr_id = "VTTS3007R" if settings.KIS_USE_MOCK else "TTTS3007R"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {access_token}",
            "appkey": settings.KIS_APPKEY,
            "appsecret": settings.KIS_APPSECRET,
            "tr_id": tr_id,
        }
        
        # 기존 파라미터 유지
        base_params = {
            "CANO": params.get("CANO"),
            "ACNT_PRDT_CD": params.get("ACNT_PRDT_CD"),
            "OVRS_EXCG_CD": params.get("OVRS_EXCG_CD"),
            "OVRS_ORD_UNPR": params.get("OVRS_ORD_UNPR"),
            "ITEM_CD": params.get("ITEM_CD"),
            
            # 추가 필수 파라미터
            "AFHR_FLPR_YN": "N",  # 장후플래그여부
            "OFL_YN": "N",        # 오프라인여부
            "INQR_DVSN": "02",    # 조회구분 (02: 상세조회)
            "UNPR_DVSN": "01",    # 단가구분 (01: 기본값)
            "FUND_STTL_ICLD_YN": "N",  # 펀드결제포함여부
            "FNCG_AMT_AUTO_RDPT_YN": "N",  # 융자금액자동상환여부
            "PRCS_DVSN": "00",    # 처리구분 
            "CTX_AREA_FK100": "", # 연속조회검색조건100
            "CTX_AREA_NK100": ""  # 연속조회키100
        }
        
        response = requests.get(url, headers=headers, params=base_params)
        result = response.json()
        
        return result
    except Exception as e:
        print(f"매수가능금액 조회 중 오류 발생: {str(e)}")
        raise

# 추가: 해외주식 현재체결가 조회
def get_current_price(params):
    """해외주식 현재체결가 조회"""
    try:
        access_token = get_access_token()
        url = f"{settings.kis_base_url}/uapi/overseas-price/v1/quotations/price"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {access_token}",
            "appkey": settings.KIS_APPKEY,
            "appsecret": settings.KIS_APPSECRET,
            "tr_id": "HHDFS00000300",
        }
        
        response = requests.get(url, headers=headers, params=params)
        result = response.json()
        
        return result
    except Exception as e:
        print(f"현재체결가 조회 중 오류 발생: {str(e)}")
        raise

def get_overseas_nccs(params):
    """해외주식 미체결내역 조회"""
    try:
        access_token = get_access_token()
        
        # 모의투자에서는 직접 API가 지원되지 않으므로 주문체결내역 API로 대체
        if "openapivts" in settings.kis_base_url:
            # 모의투자 환경에서는 주문체결내역 API 사용
            url = f"{settings.kis_base_url}/uapi/overseas-stock/v1/trading/inquire-order"
            tr_id = "VTTS3035R"  # 모의투자 주문체결내역 TR_ID
        else:
            # 실전투자 환경에서는 미체결내역 API 사용
            url = f"{settings.kis_base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
            tr_id = "TTTS3018R"  # 실전투자 미체결내역 TR_ID
            
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {access_token}",
            "appkey": settings.KIS_APPKEY,
            "appsecret": settings.KIS_APPSECRET,
            "tr_id": tr_id,
        }
        
        response = requests.get(url, headers=headers, params=params)
        result = response.json()
        
        # 모의투자에서는 nccs_qty(미체결수량)가 0보다 큰 항목만 필터링
        if "openapivts" in settings.kis_base_url and 'output' in result and isinstance(result['output'], list):
            result['output'] = [item for item in result['output'] if int(item.get('nccs_qty', 0)) > 0]
        
        return result
    except Exception as e:
        print(f"미체결내역 조회 중 오류 발생: {str(e)}")
        raise

def get_overseas_order_detail(params):
    """해외주식 주문체결내역 조회 (모의투자용 대체 API)"""
    try:
        access_token = get_access_token()
        
        # API 엔드포인트 및 TR_ID 확인
        # v1 대신 v1.0 사용 시도 
        url = f"{settings.kis_base_url}/uapi/overseas-stock/v1/trading/inquire-order"
        tr_id = "VTTS3035R"  # 모의투자 TR_ID
        
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {access_token}",
            "appkey": settings.KIS_APPKEY,
            "appsecret": settings.KIS_APPSECRET,
            "tr_id": tr_id,
        }
        
        # 디버깅 정보
        print(f"API 요청: {url}")
        print(f"헤더: {headers}")
        print(f"파라미터: {params}")
        
        response = requests.get(url, headers=headers, params=params)
        
        # 응답 확인
        print(f"API 응답 상태 코드: {response.status_code}")
        print(f"API 응답 본문: {response.text[:200] if response.text else '비어있음'}")
        
        if response.status_code == 404:
            # 404 오류인 경우 빈 결과 반환
            return {
                "rt_cd": "0",
                "msg_cd": "NODATA",
                "msg1": "모의투자 환경에서는 해당 API를 사용할 수 없습니다.",
                "output": []
            }
        
        if not response.text:
            return {
                "rt_cd": "0",
                "msg_cd": "NODATA",
                "msg1": "응답 데이터가 없습니다.",
                "output": []
            }
        
        try:
            result = response.json()
            # 정상적인 결과 처리
            if 'output' in result and isinstance(result['output'], list):
                result['output'] = [item for item in result['output'] if int(item.get('nccs_qty', 0)) > 0]
            return result
        except ValueError:
            # JSON 파싱 오류 시 빈 결과 반환
            return {
                "rt_cd": "0",
                "msg_cd": "PARSEERR",
                "msg1": "응답 파싱 오류",
                "output": []
            }
    except Exception as e:
        print(f"주문체결내역 조회 중 오류 발생: {str(e)}")
        # 예외 발생 시 빈 결과 반환
        return {
            "rt_cd": "0", 
            "msg_cd": "ERROR",
            "msg1": f"API 호출 오류: {str(e)}",
            "output": []
        }

def get_overseas_order_resv_list(params):
    """해외주식 예약주문 조회"""
    try:
        # 모의투자 환경 확인
        is_virtual = "openapivts" in settings.kis_base_url
        
        if is_virtual:
            # 모의투자에서는 지원되지 않으므로 안내 메시지 반환
            return {
                "rt_cd": "0",
                "msg_cd": "MOCK_UNSUPPORTED",
                "msg1": "모의투자 환경에서는 해외주식 예약주문조회 API를 지원하지 않습니다.",
                "output": []
            }
        
        # 실전투자 환경에서 API 호출
        access_token = get_access_token()
        
        # 거래소 코드에 따라 TR_ID 결정
        ovrs_excg_cd = params.get("OVRS_EXCG_CD", "")
        if ovrs_excg_cd in ["NASD", "NYSE", "AMEX"] or not ovrs_excg_cd:
            # 미국 주식
            tr_id = "TTTT3039R"
        else:
            # 아시아 주식 (일본, 중국, 홍콩, 베트남)
            tr_id = "TTTS3014R"
            
        url = f"{settings.kis_base_url}/uapi/overseas-stock/v1/trading/order-resv-list"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {access_token}",
            "appkey": settings.KIS_APPKEY,
            "appsecret": settings.KIS_APPSECRET,
            "tr_id": tr_id,
        }
        
        # 디버깅 정보
        print(f"예약주문조회 API 요청: {url}")
        print(f"파라미터: {params}")
        
        response = requests.get(url, headers=headers, params=params)
        
        # 응답 확인
        print(f"API 응답 상태 코드: {response.status_code}")
        
        if response.status_code != 200:
            return {
                "rt_cd": "1",
                "msg_cd": f"HTTP_{response.status_code}",
                "msg1": f"API 호출 실패: HTTP {response.status_code}",
                "output": []
            }
        
        if not response.text:
            return {
                "rt_cd": "0",
                "msg_cd": "NODATA",
                "msg1": "응답 데이터가 없습니다.",
                "output": []
            }
        
        try:
            result = response.json()
            return result
        except ValueError:
            return {
                "rt_cd": "1",
                "msg_cd": "PARSEERR",
                "msg1": "응답 파싱 오류",
                "output": []
            }
    except Exception as e:
        print(f"예약주문조회 중 오류 발생: {str(e)}")
        return {
            "rt_cd": "1", 
            "msg_cd": "ERROR",
            "msg1": f"API 호출 오류: {str(e)}",
            "output": []
        }

def order_overseas_stock(order_data):
    """해외주식 주문 실행"""
    try:
        # 토큰 가져오기
        access_token = get_access_token()
        
        # 기본 계좌정보 설정
        if "CANO" not in order_data or not order_data["CANO"]:
            order_data["CANO"] = settings.KIS_CANO
        if "ACNT_PRDT_CD" not in order_data or not order_data["ACNT_PRDT_CD"]:
            order_data["ACNT_PRDT_CD"] = settings.KIS_ACNT_PRDT_CD
            
        # 모의투자 여부 확인
        is_virtual = "openapivts" in settings.kis_base_url
        
        # 매수/매도 여부 확인
        is_buy = order_data.get("is_buy", True)
        
        # 거래소 코드에 따라 tr_id 결정
        ovrs_excg_cd = order_data.get("OVRS_EXCG_CD", "")
        
        # tr_id 결정 (매수/매도 및 거래소에 따라 다름)
        if ovrs_excg_cd in ["NASD", "NYSE", "AMEX"]:
            # 미국 주식
            if is_buy:
                tr_id = "VTTT1002U" if is_virtual else "TTTT1002U"  # 미국 매수
            else:
                tr_id = "VTTT1001U" if is_virtual else "TTTT1006U"  # 미국 매도
        elif ovrs_excg_cd == "TKSE":
            # 일본 주식
            if is_buy:
                tr_id = "VTTS0308U" if is_virtual else "TTTS0308U"  # 일본 매수
            else:
                tr_id = "VTTS0307U" if is_virtual else "TTTS0307U"  # 일본 매도
        elif ovrs_excg_cd == "SHAA":
            # 상해 주식
            if is_buy:
                tr_id = "VTTS0202U" if is_virtual else "TTTS0202U"  # 상해 매수
            else:
                tr_id = "VTTS1005U" if is_virtual else "TTTS1005U"  # 상해 매도
        elif ovrs_excg_cd == "SEHK":
            # 홍콩 주식
            if is_buy:
                tr_id = "VTTS1002U" if is_virtual else "TTTS1002U"  # 홍콩 매수
            else:
                tr_id = "VTTS1001U" if is_virtual else "TTTS1001U"  # 홍콩 매도
        elif ovrs_excg_cd == "SZAA":
            # 심천 주식
            if is_buy:
                tr_id = "VTTS0305U" if is_virtual else "TTTS0305U"  # 심천 매수
            else:
                tr_id = "VTTS0304U" if is_virtual else "TTTS0304U"  # 심천 매도
        elif ovrs_excg_cd in ["HASE", "VNSE"]:
            # 베트남 주식
            if is_buy:
                tr_id = "VTTS0311U" if is_virtual else "TTTS0311U"  # 베트남 매수
            else:
                tr_id = "VTTS0310U" if is_virtual else "TTTS0310U"  # 베트남 매도
        else:
            return {
                "rt_cd": "1",
                "msg_cd": "INVALID_EXCHANGE",
                "msg1": f"지원되지 않는 거래소 코드: {ovrs_excg_cd}",
                "output": {}
            }
        
        # API 요청 URL 및 헤더 설정
        url = f"{settings.kis_base_url}/uapi/overseas-stock/v1/trading/order"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {access_token}",
            "appkey": settings.KIS_APPKEY,
            "appsecret": settings.KIS_APPSECRET,
            "tr_id": tr_id
        }
        
        # 필수 파라미터 준비 (요청 본문에서 is_buy 제거)
        request_body = order_data.copy()
        if "is_buy" in request_body:
            del request_body["is_buy"]
        
        # 기본 값 설정
        if "ORD_SVR_DVSN_CD" not in request_body:
            request_body["ORD_SVR_DVSN_CD"] = "0"
        
        # 주문구분 설정 (기본값: 지정가)
        if "ORD_DVSN" not in request_body:
            request_body["ORD_DVSN"] = "00"  # 지정가
        
        # 디버깅 정보 출력
        print(f"해외주식 주문 API 요청: {url}")
        print(f"헤더: {headers}")
        print(f"요청 본문: {request_body}")
        
        # API 호출
        response = requests.post(url, headers=headers, json=request_body)
        
        # 응답 확인
        print(f"API 응답 상태 코드: {response.status_code}")
        print(f"API 응답 본문: {response.text[:200] if response.text else '비어있음'}")
        
        # 응답 처리
        if response.status_code != 200:
            return {
                "rt_cd": "1",
                "msg_cd": f"HTTP_{response.status_code}",
                "msg1": f"API 호출 실패: HTTP {response.status_code}",
                "output": {}
            }
        
        try:
            result = response.json()
            # 주문 내역을 DB에 저장 (옵션)
            # save_order_history(request_body, result)
            return result
        except ValueError:
            return {
                "rt_cd": "1",
                "msg_cd": "PARSEERR",
                "msg1": "응답 파싱 오류",
                "output": {}
            }
    except Exception as e:
        print(f"해외주식 주문 중 오류 발생: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return {
            "rt_cd": "1", 
            "msg_cd": "ERROR",
            "msg1": f"API 호출 오류: {str(e)}",
            "output": {}
        }

def create_conditional_orders(params):
    """
    특정 가격에 도달했을 때 자동으로 실행되는 조건부 주문 설정
    손절매(stop loss)와 이익실현(take profit) 주문을 동시에 설정
    """
    try:
        # 1. 해외주식 잔고 조회
        balance_result = get_overseas_balance()
        
        if balance_result.get("rt_cd") != "0":
            return {
                "rt_cd": "1",
                "msg_cd": "BALANCE_ERROR",
                "msg1": f"잔고 조회 실패: {balance_result.get('msg1', '알 수 없는 오류')}",
                "output": {}
            }
        
        # 2. 종목 정보 찾기
        pdno = params.get("pdno")
        ovrs_excg_cd = params.get("ovrs_excg_cd")
        
        holdings = balance_result.get("output1", [])
        target_holding = None
        
        for holding in holdings:
            if holding.get("ovrs_pdno") == pdno:
                target_holding = holding
                break
        
        if not target_holding:
            return {
                "rt_cd": "1",
                "msg_cd": "NO_HOLDING",
                "msg1": f"해당 종목({pdno})을 보유하고 있지 않습니다.",
                "output": {}
            }
        
        # 3. 기준 가격, 손절매 가격, 이익실현 가격 계산
        base_price = params.get("base_price")
        if not base_price:
            # 매수 평균단가를 기준 가격으로 사용
            base_price = float(target_holding.get("pchs_avg_pric", "0"))
            
        if base_price <= 0:
            return {
                "rt_cd": "1",
                "msg_cd": "INVALID_PRICE",
                "msg1": "유효하지 않은 기준 가격입니다.",
                "output": {}
            }
        
        # 손절매, Profit Taking 퍼센트 설정
        stop_loss_percent = params.get("stop_loss_percent", -5.0)
        take_profit_percent = params.get("take_profit_percent", 5.0)
        
        # 가격 계산
        stop_loss_price = round(base_price * (1 + stop_loss_percent/100), 2)
        take_profit_price = round(base_price * (1 + take_profit_percent/100), 2)
        
        # 주문 수량 설정 (params에 quantity가 없으면 전체 보유 수량 사용)
        quantity = params.get("quantity", target_holding.get("ord_psbl_qty", "0"))
        
        # 4. 손절매 및 이익실현 주문 생성
        order_results = []
        
        # 손절매 주문 생성 (마이너스이면 실행)
        if stop_loss_percent < 0:
            stop_loss_order = {
                "CANO": settings.KIS_CANO,
                "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
                "PDNO": pdno,
                "OVRS_EXCG_CD": ovrs_excg_cd,
                "FT_ORD_QTY": quantity,
                "FT_ORD_UNPR3": str(stop_loss_price),
                "is_buy": False,  # 매도
                "ORD_DVSN": "00"  # 지정가
            }
            
            stop_loss_result = overseas_order_resv(stop_loss_order)
            stop_loss_result["order_type"] = "stop_loss"
            order_results.append(stop_loss_result)
        
        # 이익실현 주문 생성 (플러스이면 실행)
        if take_profit_percent > 0:
            take_profit_order = {
                "CANO": settings.KIS_CANO,
                "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
                "PDNO": pdno,
                "OVRS_EXCG_CD": ovrs_excg_cd,
                "FT_ORD_QTY": quantity,
                "FT_ORD_UNPR3": str(take_profit_price),
                "is_buy": False,  # 매도
                "ORD_DVSN": "00"  # 지정가
            }
            
            take_profit_result = overseas_order_resv(take_profit_order)
            take_profit_result["order_type"] = "take_profit"
            order_results.append(take_profit_result)
        
        # 5. 결과 반환
        success_count = sum(1 for r in order_results if r.get("rt_cd") == "0")
        
        return {
            "rt_cd": "0" if success_count > 0 else "1",
            "msg_cd": "SUCCESS" if success_count == len(order_results) else "PARTIAL_SUCCESS" if success_count > 0 else "FAILED",
            "msg1": f"{success_count}/{len(order_results)} 주문이 성공적으로 처리되었습니다.",
            "base_price": base_price,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "order_results": order_results
        }
        
    except Exception as e:
        print(f"조건부 주문 생성 중 오류 발생: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return {
            "rt_cd": "1",
            "msg_cd": "ERROR",
            "msg1": f"조건부 주문 생성 중 오류 발생: {str(e)}",
            "output": {}
        }
    