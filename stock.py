import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
import numpy as np
import time

# FRED API Key 설정
api_key = 'aedfbcd8ba091c740281c0bd8ca93b46'

# FRED에서 제공하는 지표 코드와 명칭
fred_indicators = {
    'T10YIE': '10년 기대 인플레이션율',  # 10년 만기 기대 인플레이션율 (일간)
    'T10Y2Y': '장단기 금리차',  # 10년-2년 국채 수익률 스프레드 (일간)
    'FEDFUNDS': '기준금리',  # 연방기금 금리 (월간)
    'UMCSENT': '미시간대 소비자 심리지수',  # 소비자 신뢰 지수 (월간)
    'UNRATE': '실업률',  # 실업률 (월간)
    # 'USREC': '경기침체',  # 경기침체 지수 (월간) --> 장단기 금리차를 통해 확인 가능.
    'DGS2': '2년 만기 미국 국채 수익률',  # 2년 만기 국채 수익률 (일간)
    'DGS10': '10년 만기 미국 국채 수익률',  # 10년 만기 국채 수익률 (일간)

    # 추가 지표
    'STLFSI4': '금융스트레스지수',  # 금융 스트레스 지수 (주간)
    'PCE': '개인 소비 지출',  # 개인 소비 지출 (월간) -> 소비자물가지수(CPI)나 GDP, 실업률, 인플레이션 기대 등 다른 핵심 지표가 이미 전반적 경기 상황을 반영.
    # 'INDPRO': '산업생산',  # 산업 생산 지수 (월간)
    # 'HOUST': '주택 착공',  # 신규 주택 착공 건수 (월간)
    # 'UNEMPLOY': '실업자수',  # 실업자의 총 수 (월간)
    # 'RSAFS': '소매판매',  # 소매판매 지수 (월간)
    # 'CPIENGSL': '에너지 가격 지수',  # 소비자 물가지수 중 에너지 부문 (월간) -> 소비자물가지수(CPI)나 GDP, 실업률, 인플레이션 기대 등 다른 핵심 지표가 이미 전반적 경기 상황을 반영.
    # 'AHETPI': '임금 성장률',  # 시간당 평균 임금 성장률 (월간)
    # 'PPIACO': '농산물 가격 지수',  # 생산자 물가지수 중 농산물 부문 (월간)
    'CPIAUCSL': '소비자 물가지수',  # 전체 소비자 물가지수 (월간)
    # 'CSUSHPINSA': '주택가격지수',  # 케이스-실러 주택 가격 지수 (월간) -> 5년 변동금리 모기지 (MORTGAGE5US): 부동산 시장이 대상 종목(빅테크) 주가 변동에 단기적으로 큰 영향 미치는지 불분명. 단기간(1주일 후) 예측에서는 영향력 제한적.
    # 'MORTGAGE30US': '30년 고정금리 모기지',  # 30년 만기 고정금리 모기지 금리 (주간)
    # 'MORTGAGE15US': '15년 고정금리 모기지',  # 15년 만기 고정금리 모기지 금리 (주간)
    'MORTGAGE30US': '5년 변동금리 모기지',  # 30년 고정금리 모기지 (주간, MORTGAGE5US 중단으로 대체)
    'DTWEXBGS': '미국 달러 환율',  # 미국 무역가중 환율 (월간, DTWEXM 중단으로 대체)
    'M2SL': '통화 공급량 M2',  # M2 통화 공급량 (월간, M2 시리즈 ID 변경) -> FEDFUNDS나 금리 동향, 달러 인덱스, 금융스트레스지수가 이미 유동성 상황을 대략 파악 가능.
    # 'TEDRATE': 'TED 스프레드',  # 3개월 만기 미국 국채와 유로달러 금리 스프레드 (일간) -> FEDFUNDS나 금리 동향, 달러 인덱스, 금융스트레스지수가 이미 유동성 상황을 대략 파악 가능.
    # 'BAMLH0A0HYM2': '미국 하이일드 채권 스프레드',  # 미국 하이일드 채권과 국채 스프레드 (일간) -> 금융시장 신용위험을 반영하지만, 이미 금융스트레스지수(STLFSI4), 장단기금리차, VIX 등의 지표로 대략적인 위험 선호도나 스트레스 상황 파악 가능.
    # 'BAMLC0A0CM': '미국 회사채 스프레드',  # 미국 회사채와 국채 스프레드 (일간)
    # 'BAMLCC0A0CMTRIV': '미국 회사채 수익률',  # 미국 회사채 수익률 (일간)
    # 'BAMLCC0A1AAATRIV': '미국 회사채 AAA등급 수익률',  # AAA등급 회사채 수익률 (일간)
    # 'BAMLCC0A4BBBTRIV': '미국 회사채 BBB등급 수익률',  # BBB등급 회사채 수익률 (일간)
    # 'BAMLHYH0A0HYM2TRIV': '미국 하이일드 채권 수익률',  # 하이일드 채권 수익률 (일간)
    # 'BAMLHYH0A3CMTRIV': '미국 하이일드 채권 CCC등급 수익률',  # CCC등급 하이일드 채권 수익률 (일간)
    # 'BAMLHE00EHYIEY': '미국 하이일드 채권 기대수익률',  # 하이일드 채권 기대수익률 (일간)

    'TDSP': '가계 부채 비율',  # 가계의 부채 상환 비율을 나타냄 (분기)
    # 'A939RX0Q048SBEA': '실질 GDP 성장률',  # 계절 조정된 연간 실질 GDP 성장률 (분기)
    'GDPC1': 'GDP 성장률',  # 실질 국내총생산 성장률, 물가 조정을 반영 (분기)
    # 'W019RCQ027SBEA': '정부 지출',  # 정부의 총 지출 금액 (분기)
    # 'DRBLACBS': '대출 연체율',  # 기업 대출의 연체율 (분기)

    # 주식시장 관련 추가 지표
    # 'DJIA': '다우존스 산업평균지수',  # 미국 대형 30개 기업의 주가 평균 (일간)
    'NASDAQCOM': '나스닥 종합지수'  # 나스닥 시장 전체 종합 주가 지수 (일간)
}


# Yahoo Finance에서 제공하는 지표와 티커
yfinance_indicators = {
    'S&P 500 지수': '^GSPC',    # S&P 500 지수
    '금 가격': 'GC=F',           # 금 가격 (선물)
    '달러 인덱스': 'DX-Y.NYB',    # 달러 인덱스

    # 추가 지표
    '나스닥 100': '^NDX',           # 나스닥 100 지수
    'S&P 500 ETF': 'SPY',           # S&P 500 추종 ETF
    'QQQ ETF': 'QQQ',               # 나스닥 100 추종 ETF
    '러셀 2000 ETF': 'IWM',         # 러셀 2000 추종 ETF
    '다우 존스 ETF': 'DIA',          # 다우 존스 추종 ETF
    # 'NYSE FANG+ 지수': '^NYFANG'   # NYSE FANG+ 지수
    'VIX 지수': '^VIX',          # ^VIX (변동성 지수, 공포 지수): S&P 500 옵션 가격을 기반으로 앞으로의 시장 변동성 기대치를 반영해 시장 심리를 나타내는 지표.

    # 글로벌 지수
    '닛케이 225': '^N225',          # 일본 닛케이 225 지수
    '상해종합': '000001.SS',        # 중국 상해종합지수
    '항셍': '^HSI',                # 홍콩 항셍지수
    # '유로스톡스 50': '^STOXX50E',   # 유럽 유로스톡스 50 지수
    '영국 FTSE': '^FTSE',          # 영국 FTSE 100 지수
    '독일 DAX': '^GDAXI',          # 독일 DAX 지수
    '프랑스 CAC 40': '^FCHI',       # 프랑스 CAC 40 지수

    '미국 전체 채권시장 ETF': 'AGG',  # iShares 핵심 미국 전체 채권 ETF
    'TIPS ETF': 'TIP',             # iShares TIPS ETF (물가연동국채)
    # '하이일드 채권 ETF': 'HYG',      # iShares iBoxx $ 하이일드 회사채 ETF
    '투자등급 회사채 ETF': 'LQD',     # iShares iBoxx $ 투자등급 회사채 ETF
    # '신흥국 채권 ETF': 'EMB',        # iShares JP모건 USD 신흥시장 채권 ETF

    # 환율
    '달러/엔': 'JPY=X',          # 달러/엔 환율
    '달러/위안': 'CNY=X',         # 달러/위안 환율

    # 리츠(부동산) 관련
    '미국 리츠 ETF': 'VNQ',       # Vanguard Real Estate ETF
    # '모기지 리츠 ETF': 'REM',      # iShares Mortgage Real Estate ETF
}

# 나스닥 100 상위 종목 티커 리스트와 한글 이름
nasdaq_top_100 = [
    ("AAPL", "애플"),                      # 1위, 9.50%
    ("MSFT", "마이크로소프트"),            # 3위, 7.67%
    ("AMZN", "아마존"),                    # 4위, 5.80%
    ("GOOGL", "구글 A"),                   # 10위, 2.58%
    ("GOOG", "구글 C"),                    # 11위, 2.48%
    ("META", "메타"),                      # 6위, 3.79%
    ("TSLA", "테슬라"),                    # 8위, 2.76%
    ("NVDA", "엔비디아"),                  # 2위, 7.95%
    ("COST", "코스트코"),                  # 7위, 2.97%
    ("NFLX", "넷플릭스"),                  # 9위, 2.68%
    ("PYPL", "페이팔"),                    # 51위, 0.46%
    ("INTC", "인텔"),                      # 36위, 0.65%
    ("CSCO", "시스코"),                    # 13위, 1.63%
    ("CMCSA", "컴캐스트"),                 # 27위, 0.88%
    ("PEP", "펩시코"),                     # 15위, 1.35%
    ("AMGN", "암젠"),                      # 23위, 1.06%
    ("HON", "허니웰 인터내셔널"),           # 26위, 0.89%
    ("SBUX", "스타벅스"),                  # 28위, 0.84%
    ("MDLZ", "몬델리즈"),                  # 41위, 0.55%
    ("MU", "마이크론"),                    # 35위, 0.67%
    ("AVGO", "브로드컴"),                  # 5위, 4.00%
    ("ADBE", "어도비"),                    # 17위, 1.23%
    ("TXN", "텍사스 인스트루먼트"),        # 19위, 1.14%
    ("AMD", "AMD"),                        # 24위, 1.04%
    ("AMAT", "어플라이드 머티리얼즈")     # 29위, 0.83%
]

# 결과 데이터프레임을 전역 변수로 정의 (초기에는 None)
result_df = None

# yfinance.py에서 가져온 함수
def download_yahoo_chart(symbol, start_date, end_date, interval="1d"):
    """
    Yahoo Finance Chart API를 통해 주어진 symbol의 종가(Close) 시계열을 가져옵니다.
    - symbol: Yahoo Finance 티커 문자열 (예: "^GSPC", "AAPL")
    - start_date: 시작일 (YYYY-MM-DD)
    - end_date: 종료일 (YYYY-MM-DD)
    - interval: "1d", "1wk", "1mo"
    """
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    })
    
    # 날짜 범위로 변환
    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    delta = end_dt - start_dt
    
    # 범위 문자열 결정 (차이가 1달 이하이면 1mo, 3달 이하이면 3mo, 6달 이하이면 6mo, 그 이상이면 max)
    if delta.days <= 30:
        range_str = "1mo"
    elif delta.days <= 90:
        range_str = "3mo"
    elif delta.days <= 180:
        range_str = "6mo"
    elif delta.days <= 365:
        range_str = "1y"
    elif delta.days <= 730:
        range_str = "2y"
    elif delta.days <= 1825:
        range_str = "5y"
    else:
        range_str = "max"
    
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "range": range_str,
        "interval": interval,
        "includePrePost": "false",
        "events": "div|split"
    }
    
    r = sess.get(url, params=params)
    r.raise_for_status()
    result = r.json().get("chart", {}).get("result", [None])[0]
    if not result:
        raise ValueError(f"No data for symbol: {symbol}")
    
    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    
    # 시작 - 수정된 부분: 날짜만 사용하도록 처리
    # 각 타임스탬프를 datetime으로 변환하고 날짜 부분만 사용
    date_only = [pd.Timestamp.fromtimestamp(ts).date() for ts in timestamps]
    
    # 데이터프레임 생성 시 날짜만 포함하도록 수정
    df = pd.DataFrame({
        "Close": closes
    }, index=pd.DatetimeIndex(date_only))
    
    # 중복된 날짜가 있는 경우 마지막 값만 유지
    if df.index.duplicated().any():
        df = df[~df.index.duplicated(keep='last')]
    # 종료 - 수정된 부분
    
    # 시작일과 종료일 사이의 데이터만 필터링
    df = df[(df.index >= pd.Timestamp(start_date)) & (df.index <= pd.Timestamp(end_date))]
    
    return df

def collect_economic_data(start_date='2006-01-01', end_date=None):
    """
    경제 데이터를 수집하는 메인 함수
    
    Args:
        start_date (str): 데이터 수집 시작 날짜 (YYYY-MM-DD 형식)
        end_date (str, optional): 데이터 수집 종료 날짜. 기본값은 현재 날짜.
    
    Returns:
        pd.DataFrame: 수집된 모든 경제 및 주식 데이터
    """
    global result_df
    
    # end_date가 지정되지 않은 경우 현재 날짜 사용
    if end_date is None:
        end_date = datetime.today().strftime('%Y-%m-%d')
    
    print(f"경제 데이터 수집 시작: {start_date} ~ {end_date}")
    
    # FRED API를 통한 데이터 수집
    print("FRED 경제 지표 수집 중...")
    fred_data_frames = []
    for code, name in fred_indicators.items():
        # 지표별 제공 주기에 따른 요청 주기 설정
        if code in ['FEDFUNDS', 'UMCSENT', 'UNRATE', 'USREC', 'PCE', 'INDPRO',
                    'HOUST', 'UNEMPLOY', 'RSAFS', 'CPIENGSL', 'AHETPI', 'PPIACO', 'CPIAUCSL',
                    'CSUSHPINSA', 'DTWEXBGS', 'M2SL']:
            frequency = 'm'
        elif code in ['STLFSI4', 'MORTGAGE30US', 'MORTGAGE15US']:
            frequency = 'w'
        elif code in ['TDSP', 'A939RX0Q048SBEA', 'GDPC1', 'W019RCQ027SBEA', 'DRBLACBS']:
            frequency = 'q'
        else:
            frequency = 'd'
    
        url = f'https://api.stlouisfed.org/fred/series/observations'
        params = {
            'series_id': code,
            'api_key': api_key,
            'file_type': 'json',
            'observation_start': start_date,
            'observation_end': end_date,
            'frequency': frequency
        }
        response = requests.get(url, params=params)
    
        if response.status_code == 200:
            data = response.json().get('observations', [])
            if data:
                df = pd.DataFrame(data)[['date', 'value']]
                df.columns = ['date', name]
                df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
                fred_data_frames.append(df.set_index('date'))
            else:
                print(f"No data found for indicator {name} ({code}).")
        else:
            print(f"Failed to fetch data for indicator {name} ({code}): {response.status_code}")
    
    # 데이터 빈도에 따른 리샘플링 처리
    for i, df in enumerate(fred_data_frames):
        if df.empty:
            print(f"DataFrame {i} is empty, skipping resampling.")
            continue
        try:
            inferred_freq = df.index.inferred_freq
            # 빈도에 따라 일간 데이터로 변환
            if inferred_freq in ['M', 'MS']:  # 월간 데이터
                fred_data_frames[i] = df.resample('D').ffill()
            elif inferred_freq in ['W', 'W-FRI']:  # 주간 데이터
                fred_data_frames[i] = df.resample('D').ffill()
            elif inferred_freq in ['Q', 'QS-OCT']:  # 분기 데이터
                fred_data_frames[i] = df.resample('D').ffill()
            elif inferred_freq in ['B']:  # 영업일 데이터
                fred_data_frames[i] = df.resample('D').ffill()
            else:
                fred_data_frames[i] = df.resample('D').ffill()
        except Exception as e:
            print(f"Error processing DataFrame {i}: {e}")
    
    # yfinance를 통한 데이터 수집 (yfinance.py의 방식으로 대체)
    print("\nYahoo Finance 지표 데이터 수집 중...")
    yfinance_data_frames = []
    for name, ticker in yfinance_indicators.items():
        try:
            # download_yahoo_chart 함수를 사용하여 데이터 수집
            df = download_yahoo_chart(ticker, start_date, end_date)
            if not df.empty:
                df.columns = [name]  # 'Close' 컬럼명을 지표 이름으로 변경
                df.index = df.index.tz_localize(None)  # 시간대 정보 제거
                yfinance_data_frames.append(df)
                print(f"{name}({ticker}) 수집 완료, {len(df)}개")
            else:
                print(f"No data found for indicator {name} ({ticker}).")
        except Exception as e:
            print(f"Error downloading data for {ticker} ({name}): {e}")
        # 요청 간 간격을 두어 rate limit 방지
        time.sleep(1)
    
    # 나스닥 100 상위 종목 데이터 수집 (yfinance.py의 방식으로 대체)
    print("\n나스닥 100 상위 종목 데이터 수집 중...")
    nasdaq_data_frames = []
    for ticker, name in nasdaq_top_100:
        try:
            # download_yahoo_chart 함수를 사용하여 데이터 수집
            df = download_yahoo_chart(ticker, start_date, end_date)
            if not df.empty:
                df.columns = [name]  # 'Close' 컬럼명을 종목 한글 이름으로 변경
                df.index = df.index.tz_localize(None)  # 시간대 정보 제거
                nasdaq_data_frames.append(df)
                print(f"{name}({ticker}) 수집 완료, {len(df)}개")
            else:
                print(f"No data found for stock {name} ({ticker}).")
        except Exception as e:
            print(f"Error downloading data for {ticker} ({name}): {e}")
        # 요청 간 간격을 두어 rate limit 방지
        time.sleep(1)
    
    # 모든 데이터를 날짜 기준으로 외부 결합하여 하나의 데이터프레임으로 결합
    all_data_frames = fred_data_frames + yfinance_data_frames + nasdaq_data_frames
    if all_data_frames:
        # 중복된 인덱스 처리
        for i, df in enumerate(all_data_frames):
            if df.index.duplicated().any():
                all_data_frames[i] = df[~df.index.duplicated(keep='first')]
        
        # 결합
        print("데이터프레임 병합 중...")
        result_df = pd.concat(all_data_frames, axis=1, join='outer')
    
        # 결측치 및 비정상적인 값 처리
        result_df.replace('.', pd.NA, inplace=True)
        
        # 결측치를 이전 값으로 채움
        result_df.sort_index(inplace=True)
        result_df.ffill(inplace=True)
        
        # 주요 수정: 날짜 인덱스의 시간 부분을 제거하고 일자만 남김
        # 동일 날짜의 데이터가 여러 개 있는 경우, 마지막 데이터만 사용
        print("날짜 인덱스 표준화 중...")
        result_df.index = pd.to_datetime(result_df.index.date)  # 날짜만 남김
        result_df = result_df[~result_df.index.duplicated(keep='last')]  # 중복 날짜 제거, 마지막 값 유지
        
        # 결과 데이터프레임 로그 출력
        print("\n=== 결과 데이터프레임 정보 ===")
        print(f"행 수: {len(result_df)}")
        print(f"열 수: {len(result_df.columns)}")
        print("컬럼 목록:")
        for col in result_df.columns:
            print(f"  - {col}")
        
        print("\n=== 결과 데이터프레임 처음 5행 ===")
        print(result_df.head())
        
        print("\n=== 결과 데이터프레임 마지막 5행 ===")
        print(result_df.tail())
        
        print(f"\n데이터 수집 완료")
        return result_df
    else:
        print("No data collected for any indicators.")
        return None

# 스크립트가 직접 실행될 때만 데이터 수집 진행
if __name__ == "__main__":
    result_df = collect_economic_data()
    # 필요한 경우 CSV로 저장
    # result_df.to_csv('total.csv', index_label="날짜", encoding='utf-8-sig')
