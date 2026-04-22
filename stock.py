import requests
import pandas as pd
from datetime import datetime, timedelta
import numpy as np
import time
import os
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("FRED_API_KEY", "")
if not api_key:
    raise ValueError("FRED_API_KEY가 .env 파일에 설정되지 않았습니다.")

# FRED 지표: {series_id: (컬럼명, 주기)}
FRED_INDICATORS = {
    'T10YIE':     ('inflation_10y',      'd'),
    'T10Y2Y':     ('yield_spread_10y2y', 'd'),
    'FEDFUNDS':   ('fed_rate',           'm'),
    'UMCSENT':    ('consumer_sentiment', 'm'),
    'UNRATE':     ('unemployment',       'm'),
    'DGS2':       ('treasury_2y',        'd'),
    'DGS10':      ('treasury_10y',       'd'),
    'STLFSI4':    ('financial_stress',   'w'),
    'PCE':        ('pce',                'm'),
    'CPIAUCSL':   ('cpi',                'm'),
    'MORTGAGE30US': ('mortgage_30y',     'w'),
    'DTWEXBGS':   ('dollar_trade_index', 'm'),
    'M2SL':       ('m2',                 'm'),
    'TDSP':       ('household_debt',     'q'),
    'GDPC1':      ('gdp',                'q'),
    'NASDAQCOM':  ('nasdaq_composite',   'd'),
}

# Yahoo Finance 지표: {컬럼명: ticker}
YFINANCE_INDICATORS = {
    'sp500':          '^GSPC',
    'gold':           'GC=F',
    'dollar_index':   'DX-Y.NYB',
    'nasdaq100':      '^NDX',
    'spy':            'SPY',
    'qqq':            'QQQ',
    'iwm':            'IWM',
    'dia':            'DIA',
    'vix':            '^VIX',
    'nikkei225':      '^N225',
    'shanghai':       '000001.SS',
    'hangseng':       '^HSI',
    'ftse':           '^FTSE',
    'dax':            '^GDAXI',
    'cac40':          '^FCHI',
    'agg':            'AGG',
    'tip':            'TIP',
    'lqd':            'LQD',
    'jpy_usd':        'JPY=X',
    'cny_usd':        'CNY=X',
    'vnq':            'VNQ',
}


def download_yahoo_chart(symbol: str, start_date: str, end_date: str, interval: str = "1d") -> pd.DataFrame:
    """Yahoo Finance Chart API로 종가(Close) 시계열을 가져옵니다."""
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt   = datetime.strptime(end_date,   '%Y-%m-%d')
    delta    = (end_dt - start_dt).days

    if delta <= 30:       range_str = "1mo"
    elif delta <= 90:     range_str = "3mo"
    elif delta <= 180:    range_str = "6mo"
    elif delta <= 365:    range_str = "1y"
    elif delta <= 730:    range_str = "2y"
    elif delta <= 1825:   range_str = "5y"
    else:                 range_str = "max"

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = sess.get(url, params={
        "range": range_str,
        "interval": interval,
        "includePrePost": "false",
        "events": "div|split",
    })
    r.raise_for_status()

    result = r.json().get("chart", {}).get("result", [None])[0]
    if not result:
        raise ValueError(f"No data for symbol: {symbol}")

    timestamps = result["timestamp"]
    closes     = result["indicators"]["quote"][0]["close"]
    date_only  = [pd.Timestamp.fromtimestamp(ts).date() for ts in timestamps]

    df = pd.DataFrame({"Close": closes}, index=pd.DatetimeIndex(date_only))
    if df.index.duplicated().any():
        df = df[~df.index.duplicated(keep='last')]

    df = df[(df.index >= pd.Timestamp(start_date)) & (df.index <= pd.Timestamp(end_date))]
    return df


def collect_economic_data(start_date: str = '2006-01-01', end_date: str = None) -> pd.DataFrame:
    """
    FRED + Yahoo Finance 데이터를 수집해서 DataFrame으로 반환합니다.

    Returns:
        경제지표 DataFrame (인덱스=날짜, 컬럼=economic_indicators 테이블 컬럼명)
    """
    if end_date is None:
        end_date = datetime.today().strftime('%Y-%m-%d')

    print(f"경제 데이터 수집 시작: {start_date} ~ {end_date}")

    # --- FRED ---
    print("FRED 경제 지표 수집 중...")
    fred_frames = []
    for code, (col_name, freq) in FRED_INDICATORS.items():
        resp = requests.get(
            'https://api.stlouisfed.org/fred/series/observations',
            params={
                'series_id':          code,
                'api_key':            api_key,
                'file_type':          'json',
                'observation_start':  start_date,
                'observation_end':    end_date,
                'frequency':          freq,
            }
        )
        if resp.status_code != 200:
            print(f"  FRED {code} 수집 실패: {resp.status_code}")
            continue

        data = resp.json().get('observations', [])
        if not data:
            print(f"  FRED {code}: 데이터 없음")
            continue

        df = pd.DataFrame(data)[['date', 'value']].rename(columns={'value': col_name})
        df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
        df[col_name] = pd.to_numeric(df[col_name], errors='coerce')
        df = df.dropna(subset=[col_name]).set_index('date')
        df = df.resample('D').ffill()
        fred_frames.append(df)
        print(f"  {code} ({col_name}): {len(df)}개")

    # --- Yahoo Finance 지표 ---
    print("\nYahoo Finance 지표 수집 중...")
    yf_frames = []
    for col_name, ticker in YFINANCE_INDICATORS.items():
        try:
            df = download_yahoo_chart(ticker, start_date, end_date)
            df.columns = [col_name]
            df.index = df.index.tz_localize(None)
            yf_frames.append(df)
            print(f"  {ticker} ({col_name}): {len(df)}개")
        except Exception as e:
            print(f"  {ticker} ({col_name}) 수집 오류: {e}")
        time.sleep(1)

    all_frames = fred_frames + yf_frames
    if not all_frames:
        print("수집된 데이터가 없습니다.")
        return pd.DataFrame()

    print("\n데이터프레임 병합 중...")
    result = pd.concat(all_frames, axis=1, join='outer')
    result.replace('.', pd.NA, inplace=True)
    result.sort_index(inplace=True)
    result.ffill(inplace=True)
    result.index = pd.to_datetime(result.index.date)
    result = result[~result.index.duplicated(keep='last')]

    print(f"\n수집 완료: {len(result)}행 × {len(result.columns)}열")
    return result


def collect_stock_prices(start_date: str, end_date: str, tickers: list) -> pd.DataFrame:
    """
    stock_universe의 종목들의 일별 종가를 수집합니다.

    Args:
        tickers: [(ticker, name_ko), ...] 형태의 리스트

    Returns:
        Long format DataFrame with columns: [date, ticker, close]
    """
    print(f"\n종목 주가 수집 중 ({len(tickers)}개)...")
    rows = []
    for ticker, name_ko in tickers:
        try:
            df = download_yahoo_chart(ticker, start_date, end_date)
            if df.empty:
                print(f"  {ticker} ({name_ko}): 데이터 없음")
                continue
            df.index = pd.to_datetime(df.index.date)
            df = df[~df.index.duplicated(keep='last')]
            for date_idx, row in df.iterrows():
                close_val = row['Close']
                if pd.isna(close_val):
                    continue
                rows.append({
                    'date':   date_idx.date(),
                    'ticker': ticker,
                    'close':  float(close_val),
                })
            print(f"  {ticker} ({name_ko}): {len(df)}개")
        except Exception as e:
            print(f"  {ticker} ({name_ko}) 수집 오류: {e}")
        time.sleep(1)

    if not rows:
        return pd.DataFrame(columns=['date', 'ticker', 'close'])

    return pd.DataFrame(rows)


if __name__ == "__main__":
    econ_df = collect_economic_data()
    if not econ_df.empty:
        print("\n=== 경제지표 샘플 ===")
        print(econ_df.tail(3))
