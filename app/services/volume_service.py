import time
import asyncio
from datetime import datetime
from app.core.config import settings
from app.services.balance_service import get_access_token
import requests

# 나스닥 100 종목 리스트 (2025년 기준)
NASDAQ_100 = [
    "AAPL", "MSFT", "AMZN", "NVDA", "META", "GOOGL", "GOOG", "TSLA", "AVGO", "COST",
    "NFLX", "TMUS", "ASML", "AMD", "PEP", "ADBE", "CSCO", "LIN", "TXN", "INTC",
    "QCOM", "AMGN", "INTU", "CMCSA", "ISRG", "AMAT", "HON", "BKNG", "MU", "LRCX",
    "VRTX", "ADI", "REGN", "KLAC", "PANW", "ADP", "SBUX", "MDLZ", "SNPS", "GILD",
    "MELI", "CDNS", "PYPL", "CTAS", "CRWD", "MAR", "CSX", "NXPI", "ORLY", "MRVL",
    "MNST", "PCAR", "WDAY", "ADSK", "CEG", "DASH", "ROP", "FTNT", "AEP", "CPRT",
    "ROST", "TTD", "PAYX", "FAST", "ODFL", "KDP", "DDOG", "CHTR", "GEHC", "EA",
    "VRSK", "KHC", "EXC", "CTSH", "FANG", "CCEP", "IDXX", "MCHP", "LULU", "BKR",
    "DXCM", "ON", "XEL", "CSGP", "TEAM", "ANSS", "CDW", "ILMN", "WBD", "MDB",
    "BIIB", "ZS", "GFS", "TTWO", "MRNA", "DLTR", "SIRI", "LCID", "RIVN", "ARM",
]

# 주요 종목 (빠른 조회용 - 기존 추천 시스템과 연동)
KEY_STOCKS = [
    "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "NVDA", "COST",
    "NFLX", "PYPL", "INTC", "CSCO", "CMCSA", "PEP", "AMGN", "HON", "SBUX",
    "MDLZ", "MU", "AVGO", "ADBE", "TXN", "AMD", "AMAT",
]


def get_overseas_daily_price(excd, symb, gubn="0", bymd="", modp="0"):
    """해외주식 기간별시세 조회 (일/주/월봉 + 거래량)

    Args:
        excd: 거래소코드 (NAS:나스닥, NYS:뉴욕, AMS:아멕스)
        symb: 종목코드
        gubn: 일/주/월 구분 (0:일, 1:주, 2:월)
        bymd: 조회기준일 (YYYYMMDD, 공백이면 최근)
        modp: 수정주가반영 (0:미반영, 1:반영)

    Returns:
        dict: API 응답 (output1: 현재 시세, output2: 기간별 시세 배열)
              output2 각 항목: xymd(일자), clos(종가), open(시가), high(고가), low(저가),
                              tvol(거래량), tamt(거래대금), diff(전일대비), rate(등락률)
    """
    try:
        access_token = get_access_token()
        url = f"{settings.kis_base_url}/uapi/overseas-price/v1/quotations/dailyprice"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {access_token}",
            "appkey": settings.KIS_APPKEY,
            "appsecret": settings.KIS_APPSECRET,
            "tr_id": "HHDFS76240000",
        }
        params = {
            "AUTH": "",
            "EXCD": excd,
            "SYMB": symb,
            "GUBN": gubn,
            "BYMD": bymd,
            "MODP": modp,
        }
        response = requests.get(url, headers=headers, params=params)
        return response.json()
    except Exception as e:
        print(f"해외주식 기간별시세 조회 오류 ({symb}): {str(e)}")
        raise


def get_stock_volume_info(excd, symb):
    """개별 종목의 현재 거래량 정보 조회

    현재체결가 API에서 거래량(tvol), 거래대금(tamt) 추출
    """
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
        params = {"AUTH": "", "EXCD": excd, "SYMB": symb}
        response = requests.get(url, headers=headers, params=params)
        return response.json()
    except Exception as e:
        print(f"현재체결가 조회 오류 ({symb}): {str(e)}")
        return None


def get_top_volume_stocks(stock_list=None, excd="NAS", top_n=20, delay=0.5):
    """종목 리스트에서 거래량 상위 종목 조회

    Args:
        stock_list: 조회할 종목 리스트 (None이면 NASDAQ_100 사용)
        excd: 거래소코드 (NAS/NYS/AMS)
        top_n: 상위 N개 반환
        delay: API 호출 간 딜레이(초) - 속도제한 방지

    Returns:
        dict: 거래량 상위 종목 정보
    """
    if stock_list is None:
        stock_list = NASDAQ_100

    results = []
    errors = []

    for i, ticker in enumerate(stock_list):
        try:
            result = get_stock_volume_info(excd, ticker)

            if result and result.get("rt_cd") == "0":
                output = result.get("output", {})

                # 거래량/가격 파싱
                tvol = output.get("tvol", "0")
                tamt = output.get("tamt", "0")
                last = output.get("last", "0")
                diff = output.get("diff", "0")
                rate = output.get("rate", "0")
                name = output.get("name", ticker)

                # 빈 문자열 처리
                tvol_int = int(tvol) if tvol and tvol.strip() else 0
                tamt_float = float(tamt) if tamt and tamt.strip() else 0.0
                last_float = float(last) if last and last.strip() else 0.0
                rate_float = float(rate) if rate and rate.strip() else 0.0

                results.append({
                    "ticker": ticker,
                    "name": name,
                    "last_price": last_float,
                    "change_rate": rate_float,
                    "volume": tvol_int,
                    "trade_amount": tamt_float,
                })
            else:
                error_msg = result.get("msg1", "알 수 없는 오류") if result else "응답 없음"
                errors.append({"ticker": ticker, "error": error_msg})

        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})

        # API 속도 제한 방지
        if i < len(stock_list) - 1:
            time.sleep(delay)

    # 거래량 기준 정렬
    results.sort(key=lambda x: x["volume"], reverse=True)

    return {
        "message": f"{len(results)}개 종목 조회 완료 (거래량 상위 {min(top_n, len(results))}개)",
        "query_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exchange": excd,
        "total_queried": len(stock_list),
        "success_count": len(results),
        "error_count": len(errors),
        "top_volumes": results[:top_n],
        "errors": errors if errors else None,
    }


def get_volume_surge_stocks(stock_list=None, excd="NAS", days=5, surge_ratio=2.0, delay=0.5):
    """거래량 급등 종목 감지

    최근 거래량이 N일 평균 대비 surge_ratio배 이상인 종목 필터링

    Args:
        stock_list: 조회할 종목 리스트
        excd: 거래소코드
        days: 평균 거래량 산출 기간 (일)
        surge_ratio: 급등 기준 배수 (기본 2.0 = 평균 대비 2배 이상)
        delay: API 호출 간 딜레이(초)

    Returns:
        dict: 거래량 급등 종목 정보
    """
    if stock_list is None:
        stock_list = KEY_STOCKS  # 빠른 조회를 위해 주요 종목만

    surge_stocks = []
    normal_stocks = []
    errors = []

    for i, ticker in enumerate(stock_list):
        try:
            result = get_overseas_daily_price(excd, ticker, gubn="0")

            if result and result.get("rt_cd") == "0":
                daily_data = result.get("output2", [])

                if len(daily_data) < days + 1:
                    errors.append({"ticker": ticker, "error": f"데이터 부족 ({len(daily_data)}일)"})
                    continue

                # 최근 거래량 (오늘 또는 가장 최근 거래일)
                today_vol = int(daily_data[0].get("tvol", "0") or "0")
                today_price = float(daily_data[0].get("clos", "0") or "0")
                today_rate = float(daily_data[0].get("rate", "0") or "0")

                # N일 평균 거래량 (오늘 제외)
                past_volumes = []
                for d in daily_data[1:days + 1]:
                    vol = int(d.get("tvol", "0") or "0")
                    if vol > 0:
                        past_volumes.append(vol)

                if not past_volumes:
                    errors.append({"ticker": ticker, "error": "과거 거래량 데이터 없음"})
                    continue

                avg_vol = sum(past_volumes) / len(past_volumes)
                vol_ratio = today_vol / avg_vol if avg_vol > 0 else 0

                stock_info = {
                    "ticker": ticker,
                    "today_volume": today_vol,
                    "avg_volume": int(avg_vol),
                    "volume_ratio": round(vol_ratio, 2),
                    "last_price": today_price,
                    "change_rate": today_rate,
                }

                if vol_ratio >= surge_ratio:
                    surge_stocks.append(stock_info)
                else:
                    normal_stocks.append(stock_info)
            else:
                error_msg = result.get("msg1", "알 수 없는 오류") if result else "응답 없음"
                errors.append({"ticker": ticker, "error": error_msg})

        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})

        if i < len(stock_list) - 1:
            time.sleep(delay)

    # 거래량 비율 기준 정렬
    surge_stocks.sort(key=lambda x: x["volume_ratio"], reverse=True)

    return {
        "message": f"{len(surge_stocks)}개 거래량 급등 종목 감지 (기준: {days}일 평균 대비 {surge_ratio}배 이상)",
        "query_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exchange": excd,
        "surge_ratio_threshold": surge_ratio,
        "avg_days": days,
        "surge_stocks": surge_stocks,
        "errors": errors if errors else None,
    }
