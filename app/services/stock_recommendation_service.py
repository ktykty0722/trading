import pandas as pd
import requests
import time
from datetime import datetime, timedelta
from app.db.supabase import supabase
import numpy as np
from app.core.config import settings
from app.services.balance_service import get_overseas_balance, get_all_overseas_balances
from app.services.volume_service import get_overseas_daily_price

# 한국어 주식명과 티커 심볼 매핑
STOCK_TO_TICKER = {
    "애플": "AAPL",
    "마이크로소프트": "MSFT",
    "아마존": "AMZN",
    "구글 A": "GOOGL",
    "구글 C": "GOOG",
    "메타": "META",
    "테슬라": "TSLA",
    "엔비디아": "NVDA",
    "코스트코": "COST",
    "넷플릭스": "NFLX",
    "페이팔": "PYPL",
    "인텔": "INTC",
    "시스코": "CSCO",
    "컴캐스트": "CMCSA",
    "펩시코": "PEP",
    "암젠": "AMGN",
    "허니웰 인터내셔널": "HON",
    "스타벅스": "SBUX",
    "몬델리즈": "MDLZ",
    "마이크론": "MU",
    "브로드컴": "AVGO",
    "어도비": "ADBE",
    "텍사스 인스트루먼트": "TXN",
    "AMD": "AMD",
    "어플라이드 머티리얼즈": "AMAT",
    "S&P 500 ETF": "SPY",
    "QQQ ETF": "QQQ"
}

# 티커별 거래소 코드 매핑 (NASD=나스닥, NYSE=뉴욕증권거래소, AMEX=아메리칸)
TICKER_TO_EXCHANGE = {
    "AAPL": "NASD", "MSFT": "NASD", "AMZN": "NASD", "GOOGL": "NASD", "GOOG": "NASD",
    "META": "NASD", "TSLA": "NASD", "NVDA": "NASD", "COST": "NASD", "NFLX": "NASD",
    "PYPL": "NASD", "INTC": "NASD", "CSCO": "NASD", "CMCSA": "NASD", "PEP": "NASD",
    "AMGN": "NASD", "HON": "NASD", "SBUX": "NASD", "MDLZ": "NASD", "MU": "NASD",
    "AVGO": "NASD", "ADBE": "NASD", "TXN": "NASD", "AMD": "NASD", "AMAT": "NASD",
    "SPY": "AMEX", "QQQ": "NASD",
}

# 거래소 코드 → KIS API 코드 변환
EXCHANGE_TO_API = {
    "NASD": "NAS",
    "NYSE": "NYS",
    "AMEX": "AMS",
}

class StockRecommendationService:
    def __init__(self):
        # ETF 제외한 컬럼명 리스트
        self.stock_columns = list(STOCK_TO_TICKER.keys())[:-2]
        self.lookback_days = 180  # 6개월 데이터

    def calculate_sma(self, series, period):
        """단순 이동평균(SMA) 계산"""
        return series.rolling(window=period).mean()

    def calculate_ema(self, series, period):
        """지수 이동평균(EMA) 계산"""
        return series.ewm(span=period, adjust=False).mean()

    def calculate_rsi(self, series, period=14):
        """RSI 계산 (Wilder's Smoothing - 업계 표준)"""
        # 비거래일 제거 (ffill로 인한 변동 0인 날 = 가격 변동 없는 중복)
        trading_series = series[series.diff() != 0].copy()
        # 첫 번째 값은 diff가 NaN이므로 포함
        if len(series) > 0:
            trading_series = pd.concat([series.iloc[:1], trading_series]).drop_duplicates()

        if len(trading_series) < period + 1:
            return pd.Series([50] * len(series), index=series.index)

        delta = trading_series.diff().dropna()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)

        # Wilder's Smoothing (EMA with alpha = 1/period)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        # 원본 인덱스에 맞춰 reindex (비거래일은 마지막 거래일 RSI 사용)
        rsi = rsi.reindex(series.index, method='ffill')
        return rsi

    def calculate_macd(self, series, short_period=12, long_period=26, signal_period=9):
        """MACD 및 Signal 라인 계산"""
        short_ema = self.calculate_ema(series, short_period)
        long_ema = self.calculate_ema(series, long_period)
        macd = short_ema - long_ema
        signal = self.calculate_ema(macd, signal_period)
        return macd, signal

    def calculate_atr(self, daily_data, period=14):
        """KIS API 일봉 데이터로 ATR 계산

        Args:
            daily_data: KIS API output2 (최신일이 index 0)
            period: ATR 계산 기간 (기본 14일)

        Returns:
            float: ATR 값, 계산 불가 시 None
        """
        try:
            if len(daily_data) < period + 1:
                return None

            data = list(reversed(daily_data))

            highs = [float(d.get("high", "0") or "0") for d in data]
            lows = [float(d.get("low", "0") or "0") for d in data]
            closes = [float(d.get("clos", "0") or "0") for d in data]

            if any(v == 0 for v in closes[:period + 1]):
                return None

            # True Range 계산
            tr_list = []
            for i in range(1, len(data)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1])
                )
                tr_list.append(tr)

            if len(tr_list) < period:
                return None

            # Wilder's smoothing ATR
            atr = sum(tr_list[:period]) / period
            for i in range(period, len(tr_list)):
                atr = (atr * (period - 1) + tr_list[i]) / period

            return round(atr, 4)
        except Exception as e:
            print(f"  ATR 계산 오류: {e}")
            return None

    def calculate_adx(self, daily_data, period=14):
        """KIS API 일봉 데이터로 ADX 계산

        Args:
            daily_data: KIS API output2 (최신일이 index 0)
            period: ADX 계산 기간 (기본 14일)

        Returns:
            float: ADX 값, 계산 불가 시 None
        """
        try:
            if len(daily_data) < period * 2 + 1:
                return None

            # 최신일이 0번이므로 역순 정렬 (오래된 날짜부터)
            data = list(reversed(daily_data))

            highs = [float(d.get("high", "0") or "0") for d in data]
            lows = [float(d.get("low", "0") or "0") for d in data]
            closes = [float(d.get("clos", "0") or "0") for d in data]

            if any(v == 0 for v in closes[:period * 2]):
                return None

            # True Range, +DM, -DM 계산
            tr_list, plus_dm_list, minus_dm_list = [], [], []
            for i in range(1, len(data)):
                high_diff = highs[i] - highs[i - 1]
                low_diff = lows[i - 1] - lows[i]

                tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else 0
                minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else 0

                tr_list.append(tr)
                plus_dm_list.append(plus_dm)
                minus_dm_list.append(minus_dm)

            # Smoothed TR, +DM, -DM (Wilder's smoothing)
            atr = sum(tr_list[:period])
            plus_di_smooth = sum(plus_dm_list[:period])
            minus_di_smooth = sum(minus_dm_list[:period])

            dx_list = []
            for i in range(period, len(tr_list)):
                atr = atr - (atr / period) + tr_list[i]
                plus_di_smooth = plus_di_smooth - (plus_di_smooth / period) + plus_dm_list[i]
                minus_di_smooth = minus_di_smooth - (minus_di_smooth / period) + minus_dm_list[i]

                if atr == 0:
                    continue

                plus_di = 100 * plus_di_smooth / atr
                minus_di = 100 * minus_di_smooth / atr
                di_sum = plus_di + minus_di

                if di_sum == 0:
                    dx_list.append(0)
                else:
                    dx_list.append(100 * abs(plus_di - minus_di) / di_sum)

            if len(dx_list) < period:
                return None

            # ADX = DX의 이동평균
            adx = sum(dx_list[:period]) / period
            for i in range(period, len(dx_list)):
                adx = (adx * (period - 1) + dx_list[i]) / period

            return round(adx, 2)
        except Exception as e:
            print(f"  ADX 계산 오류: {e}")
            return None

    def generate_technical_recommendations(self):
        """기술적 지표를 기반으로 추천 데이터를 생성하고 Supabase에 저장"""
        # 최근 6개월 데이터만 가져오기
        end_date = datetime.now()
        start_date = end_date - timedelta(days=self.lookback_days)
        start_date_str = start_date.strftime("%Y-%m-%d")

        # Supabase 쿼리에서 컬럼명에 큰따옴표 추가
        quoted_columns = [f'"{col}"' for col in self.stock_columns]
        quoted_columns.append('"날짜"')  # 날짜 컬럼 추가

        response = supabase.table("economic_and_stock_data") \
            .select(*quoted_columns) \
            .gte("날짜", start_date_str) \
            .order("날짜") \
            .execute()

        if not response.data:
            return {"message": "데이터가 없습니다", "data": []}

        # 데이터프레임 생성 (컬럼명은 큰따옴표 제외)
        df = pd.DataFrame(response.data)
        df["날짜"] = pd.to_datetime(df["날짜"])
        df.set_index("날짜", inplace=True)
        df = df.astype(float)
        df.ffill(inplace=True)   # 주말/공휴일 갭 채우기 (RSI 등 rolling 계산에 필수)
        df.bfill(inplace=True)   # 시작부분 NaN 채우기

        recommendations = []
        for stock in self.stock_columns:
            prices = df[stock]

            # 지표 계산
            sma20 = self.calculate_sma(prices, 20)
            sma50 = self.calculate_sma(prices, 50)
            golden_cross = sma20 > sma50
            rsi = self.calculate_rsi(prices)
            macd, signal = self.calculate_macd(prices)
            macd_buy_signal = macd > signal
            rsi_val = rsi.iloc[-1] if not rsi.empty else 50
            rsi_buy = rsi_val <= 65  # ≤65 정상 매수, >65 과열, >80 하드블록
            recommended = golden_cross & rsi_buy & macd_buy_signal

            # 거래량 비율 + ADX 조회 (KIS 일봉 API 1회 호출로 둘 다 계산)
            ticker = STOCK_TO_TICKER.get(stock)
            volume_ratio = None
            adx = None
            daily_change_pct = None
            if ticker:
                try:
                    api_excd = EXCHANGE_TO_API.get(TICKER_TO_EXCHANGE.get(ticker, "NASD"), "NAS")
                    vol_result = get_overseas_daily_price(api_excd, ticker, gubn="0")
                    if vol_result and vol_result.get("rt_cd") == "0":
                        raw_daily_data = vol_result.get("output2", [])
                        # 실제 거래일만 필터링: xymd(날짜)로 주말 제외 + tvol > 0 확인
                        daily_data = []
                        for d in raw_daily_data:
                            xymd = d.get("xymd", "")
                            tvol = int(d.get("tvol", "0") or "0")
                            if xymd and len(xymd) == 8 and tvol > 0:
                                try:
                                    dt = datetime.strptime(xymd, "%Y%m%d")
                                    # 월~금(0~4)만 거래일로 인정
                                    if dt.weekday() < 5:
                                        daily_data.append(d)
                                except ValueError:
                                    pass
                        print(f"  {ticker} 거래일 필터: {len(raw_daily_data)}일 → {len(daily_data)}일 (주말/비거래일 제외)")
                        # 거래량 비율 (최근 완료된 거래일 기준 5일 평균 대비)
                        # 미장 개장 전/시간외 거래는 정규 거래량 대비 극히 적으므로,
                        # 첫 번째 거래일 거래량이 두 번째 대비 1% 미만이면 미완료 거래일로 간주하고 건너뜀
                        if len(daily_data) >= 7:
                            first_vol = int(daily_data[0].get("tvol", "0") or "0")
                            second_vol = int(daily_data[1].get("tvol", "0") or "0")
                            if second_vol > 0 and first_vol < second_vol * 0.01:
                                # 오늘 데이터는 미완료(프리마켓/시간외) → 건너뜀
                                print(f"  {ticker} 오늘({daily_data[0].get('xymd')}) 거래량 {first_vol:,}은 프리마켓/시간외 → 직전 거래일 기준으로 계산")
                                daily_data = daily_data[1:]
                        if len(daily_data) >= 6:
                            today_vol = int(daily_data[0].get("tvol", "0") or "0")
                            past_vols = [int(d.get("tvol", "0") or "0") for d in daily_data[1:6]]
                            past_vols = [v for v in past_vols if v > 0]
                            if past_vols:
                                avg_vol = sum(past_vols) / len(past_vols)
                                volume_ratio = round(today_vol / avg_vol, 2) if avg_vol > 0 else None
                                print(f"  {ticker} volume_ratio={volume_ratio} (day={daily_data[0].get('xymd')}, vol={today_vol:,}, avg={avg_vol:,.0f})")
                        # ADX (거래일 데이터로 계산)
                        adx = self.calculate_adx(daily_data if daily_data else raw_daily_data)
                        # 당일 변동률 계산 (패닉셀 판단용)
                        if len(daily_data) >= 2:
                            today_close = float(daily_data[0].get("clos", "0") or "0")
                            prev_close = float(daily_data[1].get("clos", "0") or "0")
                            if prev_close > 0 and today_close > 0:
                                daily_change_pct = round(((today_close - prev_close) / prev_close) * 100, 2)
                    time.sleep(1.1)  # KIS API 초당 1건 제한 방지
                except Exception as e:
                    print(f"  {ticker} 거래량/ADX 조회 실패: {e}")

            # 가장 최근 날짜의 결과만 저장
            latest_date = df.index[-1]
            if all(pd.notna([sma20[latest_date], sma50[latest_date], rsi[latest_date], macd[latest_date], signal[latest_date]])):
                recommendations.append({
                    "날짜": latest_date.strftime("%Y-%m-%d"),
                    "종목": stock,
                    "SMA20": float(sma20[latest_date]),
                    "SMA50": float(sma50[latest_date]),
                    "골든_크로스": bool(golden_cross[latest_date]),
                    "RSI": float(rsi[latest_date]),
                    "MACD": float(macd[latest_date]),
                    "Signal": float(signal[latest_date]),
                    "MACD_매수_신호": bool(macd_buy_signal[latest_date]),
                    "추천_여부": bool(recommended[latest_date]),
                    "volume_ratio": volume_ratio,
                    "adx": adx,
                    "daily_change_pct": daily_change_pct,
                })

        # 기존 데이터 삭제 후 새 데이터 저장
        try:
            # 전체 데이터 삭제 (항상 TRUE인 조건 사용)
            supabase.table("stock_recommendations").delete().eq("날짜", "1900-01-01").gte("날짜", "1900-01-01").execute()
            
            # 또는 이런 방식도 가능합니다 (모든 레코드와 매치되는 조건)
            supabase.table("stock_recommendations").delete().gte("날짜", "1900-01-01").execute()
            
            # 새 데이터 삽입
            supabase.table("stock_recommendations").insert(recommendations).execute()
        
        except Exception as e:
            print(f"오류 발생: {str(e)}")
            import traceback
            print(traceback.format_exc())  # 상세 스택 트레이스 출력
            raise Exception(f"추천 주식 분석 중 오류: {str(e)}")

        return {"message": f"{len(recommendations)}개의 추천 데이터가 생성되었습니다", "data": recommendations}

    def get_stock_recommendations(self):
        """
        Accuracy가 80% 이상이고 상승 확률이 3% 이상인 추천 주식 목록을 반환합니다.
        상승 확률 기준으로 내림차순 정렬됩니다.
        """
        response = supabase.table("stock_analysis_results").select("*").order("created_at", desc=True).execute()
        if not response.data:
            return {"message": "분석 결과를 찾을 수 없습니다", "recommendations": []}

        df = pd.DataFrame(response.data)
        numeric_columns = ['Accuracy (%)', 'Rise Probability (%)']
        for col in numeric_columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        filtered_df = df[(df['Rise Probability (%)'] >= 2)]
        filtered_df = filtered_df.sort_values(by='Rise Probability (%)', ascending=False)
        result_columns = [
            'Stock', 'Accuracy (%)', 'Rise Probability (%)', 'Last Actual Price',
            'Predicted Future Price', 'Recommendation', 'Analysis'
        ]
        result_df = filtered_df[result_columns]

        recommendations = result_df.to_dict(orient='records')
        return {
            "message": f"{len(recommendations)}개의 추천 주식을 찾았습니다",
            "recommendations": recommendations
        }

    def get_recommendations_with_sentiment(self):
        """
        get_stock_recommendations에서 가져온 추천 주식 중 
        ticker_sentiment_analysis 테이블에서 average_sentiment_score >= 0.15인 주식만 필터링하고,
        두 데이터 소스의 정보를 결합하여 반환합니다.
        """
        stock_recs = self.get_stock_recommendations()
        recommendations = stock_recs.get("recommendations", [])
        if not recommendations:
            return {"message": "추천 주식이 없습니다", "results": []}

        sentiment_response = supabase.table("ticker_sentiment_analysis").select("*").gte("average_sentiment_score", 0.15).execute()
        if not sentiment_response.data:
            return {"message": "감정 분석 데이터가 없습니다", "results": []}

        ticker_to_recommendation = {
            STOCK_TO_TICKER.get(rec["Stock"]): rec 
            for rec in recommendations 
            if rec["Stock"] in STOCK_TO_TICKER
        }
        sentiment_data = {item["ticker"]: item for item in sentiment_response.data}

        results = []
        for ticker, sentiment in sentiment_data.items():
            if ticker in ticker_to_recommendation:
                recommendation = ticker_to_recommendation[ticker]
                combined_data = {
                    "ticker": ticker,
                    "stock_name": recommendation["Stock"],
                    "accuracy": recommendation["Accuracy (%)"],
                    "rise_probability": recommendation["Rise Probability (%)"],
                    "last_actual_price": recommendation["Last Actual Price"],
                    "predicted_future_price": recommendation["Predicted Future Price"],
                    "recommendation": recommendation["Recommendation"],
                    "analysis": recommendation["Analysis"],
                    "average_sentiment_score": sentiment["average_sentiment_score"],
                    "article_count": sentiment["article_count"],
                    "calculation_date": sentiment["calculation_date"]
                }
                results.append(combined_data)

        return {
            "message": f"{len(results)}개의 추천 주식을 분석했습니다",
            "results": results
        }

    def fetch_and_store_sentiment_for_recommendations(self):
        """
        추천 주식과 보유 중인 주식에 대해 뉴스 감정 데이터를 가져오고, Supabase에 저장하며,
        감정 분석과 추천 정보를 통합하여 반환합니다.
        """
        # 추천 주식 목록 가져오기
        stock_recs = self.get_stock_recommendations()
        recommendations = stock_recs.get("recommendations", [])
        
        # 추천 주식의 티커 목록 생성
        recommended_tickers = [STOCK_TO_TICKER.get(rec["Stock"]) for rec in recommendations if rec["Stock"] in STOCK_TO_TICKER]
        
        # 보유 주식 정보 가져오기 (전체 거래소: NASD, NYSE, AMEX)
        balance_result = get_all_overseas_balances()
        holdings = []

        if balance_result.get("rt_cd") == "0" and "output1" in balance_result:
            holdings = balance_result.get("output1", [])
            print(f"보유 주식 정보를 성공적으로 가져왔습니다. 총 {len(holdings)}개 종목 보유 중")
        else:
            print(f"보유 주식 정보를 가져오는데 실패했습니다: {balance_result.get('msg1', '알 수 없는 오류')}")
        
        # 보유 주식의 티커 목록 생성
        holding_tickers = [item.get("ovrs_pdno") for item in holdings if item.get("ovrs_pdno")]
        
        # 추천 주식과 보유 주식의 티커를 합치고 중복 제거
        all_tickers = list(set(recommended_tickers + holding_tickers))
        
        if not all_tickers:
            return {"message": "분석할 티커가 없습니다", "results": []}

        print(f"분석할 티커 목록 ({len(all_tickers)}개): {all_tickers}")

        api_key = settings.ALPHA_VANTAGE_API_KEY
        relevance_threshold = 0.2
        sleep_interval = 5
        yesterday = (datetime.now() - timedelta(days=3)).strftime("%Y%m%dT0000")

        base_url = "https://www.alphavantage.co/query"
        params = {
            "function": "NEWS_SENTIMENT",
            "time_from": yesterday,
            "limit": 100,
            "apikey": api_key
        }

        ticker_to_stock = {ticker: stock for stock, ticker in STOCK_TO_TICKER.items()}
        recommendations_by_ticker = {
            STOCK_TO_TICKER[rec["Stock"]]: rec for rec in recommendations if rec["Stock"] in STOCK_TO_TICKER
        }
        
        # 보유 주식 정보를 ticker로 매핑
        holdings_by_ticker = {item.get("ovrs_pdno"): item for item in holdings if item.get("ovrs_pdno")}

        # 기존 감정 분석 데이터 삭제
        print("기존 감정 분석 데이터 삭제 중...")
        supabase.table("ticker_sentiment_analysis").delete().gte("ticker", "").execute()
        print("기존 감정 분석 데이터 삭제 완료")

        results = []
        for ticker in all_tickers:
            print(f"{ticker} 처리 중...")
            params["tickers"] = ticker

            response = requests.get(base_url, params=params)
            if response.status_code != 200:
                results.append({
                    "ticker": ticker,
                    "stock_name": ticker_to_stock.get(ticker, ticker),  # 티커명이 없으면 티커 자체를 표시
                    "message": "API 호출 실패",
                    "is_recommended": ticker in recommended_tickers,
                    "is_holding": ticker in holding_tickers,
                    "recommendation_info": recommendations_by_ticker.get(ticker, {}),
                    "holding_info": holdings_by_ticker.get(ticker, {})
                })
                time.sleep(sleep_interval)
                continue

            api_data = response.json()
            feed = api_data.get('feed', [])

            articles = [
                float(sentiment['ticker_sentiment_score'])
                for article in feed
                for sentiment in article.get('ticker_sentiment', [])
                if sentiment['ticker'] == ticker and float(sentiment['relevance_score']) >= relevance_threshold
            ]

            if not articles:
                results.append({
                    "ticker": ticker,
                    "stock_name": ticker_to_stock.get(ticker, ticker),
                    "message": "관련 기사 없음",
                    "is_recommended": ticker in recommended_tickers,
                    "is_holding": ticker in holding_tickers,
                    "recommendation_info": recommendations_by_ticker.get(ticker, {}),
                    "holding_info": holdings_by_ticker.get(ticker, {})
                })
                time.sleep(sleep_interval)
                continue

            average_sentiment = sum(articles) / len(articles)
            article_count = len(articles)
            calculation_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 해당 티커에 대한 데이터 추가
            # 테이블 스키마에 맞게 필드 조정
            supabase_data = {
                "ticker": ticker,
                "average_sentiment_score": average_sentiment,
                "article_count": article_count,
                "calculation_date": calculation_date
            }
            supabase.table("ticker_sentiment_analysis").insert(supabase_data).execute()

            results.append({
                "ticker": ticker,
                "stock_name": ticker_to_stock.get(ticker, ticker),
                "average_sentiment_score": average_sentiment,
                "article_count": article_count,
                "is_recommended": ticker in recommended_tickers,
                "is_holding": ticker in holding_tickers,
                "recommendation_info": recommendations_by_ticker.get(ticker, {}),
                "holding_info": holdings_by_ticker.get(ticker, {})
            })
            time.sleep(sleep_interval)

        return {
            "message": f"{len(results)}개의 티커(추천 주식: {len(recommended_tickers)}개, 보유 주식: {len(holding_tickers)}개)를 분석했습니다",
            "results": results
        }

    def get_combined_recommendations_with_technical_and_sentiment(self):
        """
        ML 예측 + 기술적 지표 + 감성분석 + 시장환경을 통합하여 매수 추천 목록을 반환합니다.

        필터링:
        - ML 예측 상승확률 ≥ 2%
        - 기술적 신호 (골든크로스, RSI 매수구간, MACD 매수) 중 2개 이상
        - composite_score ≥ 0.3
        - VIX > 35이면 매수 전면 중단
        """
        try:
            # 1. 기술적 지표 데이터 조회 (전체, 필터 없이)
            tech_response = supabase.table("stock_recommendations").select("*").order("날짜", desc=True).execute()
            if not tech_response.data:
                return {"message": "기술적 지표 데이터가 없습니다", "results": []}

            tech_df = pd.DataFrame(tech_response.data)
            tech_df["골든_크로스"] = tech_df["골든_크로스"].astype(bool)
            tech_df["MACD_매수_신호"] = tech_df["MACD_매수_신호"].astype(bool)
            tech_df["RSI"] = pd.to_numeric(tech_df["RSI"])

            # 2. ML 예측 데이터 조회 (상승확률 ≥ 2%)
            stock_recs = self.get_stock_recommendations()
            recommendations = stock_recs.get("recommendations", [])
            if not recommendations:
                return {"message": "추천 주식이 없습니다", "results": []}

            # 3. 감성분석 데이터 조회 (전체 - 부정적 감성도 반영)
            sentiment_response = supabase.table("ticker_sentiment_analysis").select("*").execute()

            # 4. 데이터 매핑 준비 (pandas NaN → None 변환)
            def _safe_value(v):
                try:
                    return None if pd.isna(v) else v
                except (ValueError, TypeError):
                    return v
            tech_map = {row["종목"]: {k: _safe_value(v) for k, v in row.to_dict().items()} for _, row in tech_df.iterrows()}
            sentiment_map = {item["ticker"]: item for item in sentiment_response.data} if sentiment_response.data else {}
            
            # 5. 결과 통합 (거래량은 DB에서 읽기)
            results = []
            for rec in recommendations:
                stock_name = rec["Stock"]
                if stock_name not in STOCK_TO_TICKER:
                    continue

                ticker = STOCK_TO_TICKER[stock_name]
                tech_data = tech_map.get(stock_name)
                if tech_data is None:
                    continue  # 기술적 지표가 없으면 제외

                sentiment = sentiment_map.get(ticker)

                # 거래량 비율 + ADX는 DB에서 읽기 (generate_technical_recommendations에서 저장됨)
                volume_ratio = tech_data.get("volume_ratio")
                if volume_ratio is not None:
                    volume_ratio = float(volume_ratio)
                adx_value = tech_data.get("adx")
                if adx_value is not None:
                    adx_value = float(adx_value)

                # 통합 데이터 생성
                combined_data = {
                    "ticker": ticker,
                    "stock_name": stock_name,
                    "accuracy": rec["Accuracy (%)"],
                    "rise_probability": rec["Rise Probability (%)"],
                    "last_price": rec["Last Actual Price"],
                    "predicted_price": rec["Predicted Future Price"],
                    "recommendation": rec["Recommendation"],
                    "analysis": rec["Analysis"],
                    "sentiment_score": sentiment["average_sentiment_score"] if sentiment else None,
                    "article_count": sentiment["article_count"] if sentiment else None,
                    "sentiment_date": sentiment["calculation_date"] if sentiment else None,
                    "technical_date": tech_data["날짜"],
                    "sma20": float(tech_data["SMA20"]),
                    "sma50": float(tech_data["SMA50"]),
                    "golden_cross": bool(tech_data["골든_크로스"]),
                    "rsi": float(tech_data["RSI"]),
                    "macd": float(tech_data["MACD"]),
                    "signal": float(tech_data["Signal"]),
                    "macd_buy_signal": bool(tech_data["MACD_매수_신호"]),
                    "technical_recommended": bool(tech_data["추천_여부"]),
                    "volume_ratio": volume_ratio,
                    "adx": adx_value,
                }
                results.append(combined_data)
            
            # 5-1. VIX (시장 공포 지수) 조회 - economic_and_stock_data에서 최신값
            vix_value = None
            try:
                vix_response = supabase.table("economic_and_stock_data").select("*").order("날짜", desc=True).limit(1).execute()
                if vix_response.data and vix_response.data[0].get("VIX 지수") is not None:
                    vix_value = float(vix_response.data[0]["VIX 지수"])
                    print(f"  VIX 지수: {vix_value}")
            except Exception as e:
                print(f"  VIX 조회 실패: {e}")

            # 6. 하드 블록: VIX > 35이면 매수 전면 중단 (극단적 공포장)
            if vix_value is not None and vix_value > 35:
                print(f"  VIX {vix_value:.1f} > 35: 공포장 매수 중단")
                return {"message": f"VIX {vix_value:.1f} - 공포장으로 매수를 중단합니다", "results": []}

            # 7. 매수 후보 필터링 + 종합 점수 계산
            final_results = []
            for item in results:
                # RSI > 80 하드블록: 과매수 구간은 무조건 제외
                rsi = item["rsi"]
                if rsi > 80:
                    print(f"  {item['stock_name']}({item['ticker']}) RSI {rsi:.1f} > 80 과매수 제외")
                    continue

                raw_sentiment = item["sentiment_score"] if item["sentiment_score"] is not None else 0.0
                # 감성점수 정규화: [-1, 1] → [0, 1] (다른 점수와 범위 통일)
                sentiment_score = (raw_sentiment + 1) / 2
                # RSI 매수 적합성 판단
                # < 30: 과매도 반등 (강한 매수 신호)
                # 30~65: 정상 매수 구간
                # > 65: 과열 진입 (매수 부적합)
                # > 80: 하드블록 (위에서 이미 제외)
                rsi_buy = rsi <= 65

                tech_conditions = [item["golden_cross"], rsi_buy, item["macd_buy_signal"]]

                # 기술적 신호 2개 이상이면 매수 후보
                if sum(tech_conditions) < 2:
                    continue

                # --- 정규화된 점수 계산 (모든 항목 0~1 또는 -1~1 범위) ---

                # 상승확률 점수 (0~1 정규화)
                rp = item["rise_probability"]
                if rp < 3:
                    rise_score = 0.2
                elif rp < 5:
                    rise_score = 0.4
                elif rp < 8:
                    rise_score = 0.6
                elif rp < 12:
                    rise_score = 0.8
                else:
                    rise_score = 1.0

                # 기술적 점수 (0~1 정규화, max 3.5)
                tech_conditions_count = (
                    1.5 * item["golden_cross"] +
                    1.0 * rsi_buy +
                    1.0 * item["macd_buy_signal"]
                )
                tech_score = tech_conditions_count / 3.5

                # 거래량 점수 (-0.5~0.6)
                vr = item.get("volume_ratio")
                if vr is None:
                    volume_score = 0.0
                elif vr < 0.5:
                    volume_score = -0.5
                elif vr < 1.0:
                    volume_score = 0.0
                elif vr < 1.5:
                    volume_score = 0.3
                else:
                    volume_score = 0.6

                # ADX 점수 (-0.3~0.4)
                adx = item.get("adx")
                if adx is None:
                    adx_score = 0.0
                elif adx > 25:
                    adx_score = 0.4
                elif adx >= 20:
                    adx_score = 0.0
                else:
                    adx_score = -0.3

                # VIX 점수 (-0.5~0)
                if vix_value is None:
                    vix_score = 0.0
                elif vix_value < 20:
                    vix_score = 0.0
                elif vix_value < 30:
                    vix_score = -0.2
                else:
                    vix_score = -0.5

                # 종합 점수 (정규화된 가중합)
                composite_score = (
                    0.25 * rise_score +
                    0.25 * tech_score +
                    0.20 * sentiment_score +
                    0.15 * volume_score +
                    0.10 * adx_score +
                    0.05 * vix_score
                )

                item["rise_score"] = round(rise_score, 2)
                item["tech_score"] = round(tech_score, 2)
                item["volume_score"] = round(volume_score, 2)
                item["adx_score"] = round(adx_score, 2)
                item["vix_score"] = round(vix_score, 2)
                item["vix_value"] = vix_value
                item["composite_score"] = round(composite_score, 4)

                # 하한선: composite_score 0.3 미만이면 매수 안 함
                if composite_score >= 0.3:
                    final_results.append(item)

            final_results.sort(key=lambda x: x["composite_score"], reverse=True)

            # 8. 결과 반환
            return {
                "message": f"{len(final_results)}개의 매수 추천 주식을 찾았습니다",
                "results": final_results
            }
        
        except Exception as e:
            print(f"오류 발생: {str(e)}")
            import traceback
            print(traceback.format_exc())  # 상세 스택 트레이스 출력
            raise Exception(f"추천 주식 분석 중 오류: {str(e)}")

    def get_stocks_to_sell(self, balance_result=None):
        """
        매도 대상 종목을 식별하는 함수

        매도 조건:
        1. ATR 기반 동적 익절/손절 (trade_records 기준, 없으면 고정비율 폴백)
        2. 기술적 매도 신호 (4개): 데드크로스, RSI>70, MACD매도, 패닉셀(거래량2배+하락3%)
           - ADX > 25이면 필요 신호 수 1개 차감 (신뢰도 보정)
           - 2a: 감성 < -0.15 + 매도 신호 2개 이상 (ADX>25이면 1개)
           - 2b: 매도 신호 3개 이상 (ADX>25이면 2개)
        3. VIX 공포 시장: VIX>30+신호2개, VIX>40+신호1개

        Args:
            balance_result: 이미 조회한 KIS 잔고 결과 (None이면 새로 조회)
        """
        try:
            # 1. 보유 종목 정보 가져오기 (전체 거래소: NASD, NYSE, AMEX)
            if balance_result is None:
                balance_result = get_all_overseas_balances()
            if balance_result.get("rt_cd") != "0" or "output1" not in balance_result:
                return {
                    "message": f"보유 종목 정보를 가져오는데 실패했습니다: {balance_result.get('msg1', '알 수 없는 오류')}",
                    "sell_candidates": []
                }
            
            holdings = balance_result.get("output1", [])
            if not holdings:
                return {
                    "message": "보유 종목이 없습니다",
                    "sell_candidates": []
                }
            
            print(f"보유 종목 정보를 성공적으로 가져왔습니다. 총 {len(holdings)}개 종목 보유 중")
            
            # 2. 티커와 한글명 매핑 생성
            ticker_to_korean = {}
            korean_to_ticker = {}
            
            for item in holdings:
                ticker = item.get("ovrs_pdno")
                name = item.get("ovrs_item_name")
                if ticker and name:
                    ticker_to_korean[ticker] = name
                    korean_to_ticker[name] = ticker
            
            # 3. 기술적 지표 데이터 가져오기
            tech_response = supabase.table("stock_recommendations").select("*").order("날짜", desc=True).execute()
            tech_data = pd.DataFrame(tech_response.data) if tech_response.data else pd.DataFrame()
            
            if not tech_data.empty:
                # 데이터 타입 변환
                tech_data["골든_크로스"] = tech_data["골든_크로스"].astype(bool)
                tech_data["MACD_매수_신호"] = tech_data["MACD_매수_신호"].astype(bool)
                tech_data["RSI"] = pd.to_numeric(tech_data["RSI"])
                
                # 최신 데이터만 필터링 (종목별 가장 최근 날짜의 데이터)
                tech_data = tech_data.sort_values("날짜", ascending=False)
                tech_data = tech_data.drop_duplicates(subset=["종목"], keep="first")
            
            # 4. 감성 분석 데이터 가져오기
            sentiment_response = supabase.table("ticker_sentiment_analysis").select("*").execute()
            sentiment_data = {item["ticker"]: item for item in sentiment_response.data} if sentiment_response.data else {}
            
            # 5. VIX 조회
            vix_value = None
            try:
                vix_response = supabase.table("economic_and_stock_data").select("*").order("날짜", desc=True).limit(1).execute()
                if vix_response.data and vix_response.data[0].get("VIX 지수") is not None:
                    vix_value = float(vix_response.data[0]["VIX 지수"])
                    print(f"  매도 판단용 VIX: {vix_value}")
            except Exception as e:
                print(f"  VIX 조회 실패: {e}")

            # 6. trade_records에서 ATR 기반 익절/손절 기준 조회
            trade_records_map = {}
            try:
                tr_response = supabase.table("trade_records").select("*").eq("status", "holding").execute()
                if tr_response.data:
                    for tr in tr_response.data:
                        trade_records_map[tr["ticker"]] = tr
            except Exception as e:
                print(f"trade_records 조회 실패 (고정 비율 폴백): {e}")

            # 6. 매도 대상 종목 식별
            sell_candidates = []
            ticker_to_stock = {v: k for k, v in STOCK_TO_TICKER.items()}

            for item in holdings:
                ticker = item.get("ovrs_pdno")
                stock_name = item.get("ovrs_item_name")
                purchase_price = float(item.get("pchs_avg_pric", 0))
                current_price = float(item.get("now_pric2", 0))
                quantity = int(item.get("ovrs_cblc_qty", 0))
                exchange_code = item.get("ovrs_excg_cd", "")
                
                # 가격 변동률 계산
                price_change_percent = ((current_price - purchase_price) / purchase_price) * 100 if purchase_price > 0 else 0
                
                # 매도 근거와 신호 수를 추적할 변수들
                sell_reasons = []
                technical_sell_signals = 0
                
                # 조건 1: ATR 기반 동적 익절/손절 (trade_records에서 조회)
                trade_record = trade_records_map.get(ticker)
                if trade_record and trade_record.get("take_profit_price") and trade_record.get("stop_loss_price"):
                    tp_price = float(trade_record["take_profit_price"])
                    sl_price = float(trade_record["stop_loss_price"])
                    if current_price >= tp_price:
                        sell_reasons.append(f"ATR 익절 조건 충족: 현재가 ${current_price:.2f} >= 익절가 ${tp_price:.2f} (구매가 대비 {price_change_percent:.2f}%)")
                    elif current_price <= sl_price:
                        sell_reasons.append(f"ATR 손절 조건 충족: 현재가 ${current_price:.2f} <= 손절가 ${sl_price:.2f} (구매가 대비 {price_change_percent:.2f}%)")
                else:
                    # trade_records에 ATR 정보가 없는 경우 고정 비율 폴백
                    if price_change_percent >= 5:
                        sell_reasons.append(f"익절 조건 충족: 구매가 대비 {price_change_percent:.2f}% 상승 (고정비율)")
                    elif price_change_percent <= -7:
                        sell_reasons.append(f"손절 조건 충족: 구매가 대비 {price_change_percent:.2f}% 하락 (고정비율)")
                
                # 기술적 지표 확인 (티커 기반 매칭: KIS API는 영문명 반환, tech_data는 한글명 사용)
                tech_record = None
                if not tech_data.empty:
                    korean_name = ticker_to_stock.get(ticker)
                    if korean_name:
                        tech_filtered = tech_data[tech_data["종목"] == korean_name]
                        if not tech_filtered.empty:
                            tech_record = tech_filtered.iloc[0].to_dict()
                
                # 조건 2: 기술적 매도 신호 (4개)
                tech_sell_signals_details = []
                if tech_record:
                    # ① 데드 크로스
                    if not tech_record["골든_크로스"]:
                        technical_sell_signals += 1
                        tech_sell_signals_details.append("데드 크로스")

                    # ② RSI > 70 (과매수)
                    if tech_record["RSI"] > 70:
                        technical_sell_signals += 1
                        tech_sell_signals_details.append(f"RSI 과매수({tech_record['RSI']:.2f})")

                    # ③ MACD 매도 신호
                    if not tech_record["MACD_매수_신호"]:
                        technical_sell_signals += 1
                        tech_sell_signals_details.append("MACD 매도 신호")

                    # ④ 패닉셀 감지: 거래량 2배 이상 + 당일 -3% 이상 하락
                    volume_ratio = tech_record.get("volume_ratio")
                    daily_change = tech_record.get("daily_change_pct")
                    if volume_ratio is not None and daily_change is not None and float(volume_ratio) >= 2.0 and float(daily_change) <= -3:
                        technical_sell_signals += 1
                        tech_sell_signals_details.append(f"패닉셀(거래량 {float(volume_ratio):.1f}배, 당일 {float(daily_change):.1f}% 하락)")

                # ADX 보정: ADX > 25이면 필요 신호 수 1개 차감
                adx_value = None
                if tech_record and tech_record.get("adx") is not None:
                    adx_value = float(tech_record["adx"])
                adx_adjustment = 1 if adx_value is not None and adx_value > 25 else 0

                # 감성 분석 데이터 확인
                sentiment_score = None
                if ticker in sentiment_data:
                    sentiment_score = sentiment_data[ticker].get("average_sentiment_score")

                # 조건 2b: 매도 신호 3개 이상 (ADX>25이면 2개 이상)
                required_signals_2b = 3 - adx_adjustment
                if technical_sell_signals >= required_signals_2b:
                    adx_note = f", ADX={adx_value:.1f} 보정" if adx_adjustment else ""
                    sell_reasons.append(f"기술적 매도 신호 {technical_sell_signals}개/{required_signals_2b}개 충족: {', '.join(tech_sell_signals_details)}{adx_note}")

                # 조건 2a: 감성 < -0.15 + 매도 신호 2개 이상 (ADX>25이면 1개 이상)
                elif sentiment_score is not None and sentiment_score < -0.15:
                    required_signals_2a = 2 - adx_adjustment
                    if technical_sell_signals >= required_signals_2a:
                        adx_note = f", ADX={adx_value:.1f} 보정" if adx_adjustment else ""
                        sell_reasons.append(f"부정적 감성({sentiment_score:.2f}) + 매도 신호 {technical_sell_signals}개/{required_signals_2a}개: {', '.join(tech_sell_signals_details)}{adx_note}")

                # 조건 3: VIX 공포 시장
                if vix_value is not None and technical_sell_signals >= 1:
                    if vix_value > 40 and technical_sell_signals >= 1:
                        sell_reasons.append(f"극단적 공포(VIX={vix_value:.1f}) + 매도 신호 {technical_sell_signals}개: {', '.join(tech_sell_signals_details)}")
                    elif vix_value > 30 and technical_sell_signals >= 2:
                        sell_reasons.append(f"공포 시장(VIX={vix_value:.1f}) + 매도 신호 {technical_sell_signals}개: {', '.join(tech_sell_signals_details)}")

                # 매도 대상 판단
                if sell_reasons:
                    sell_candidates.append({
                        "ticker": ticker,
                        "stock_name": stock_name,
                        "purchase_price": purchase_price,
                        "current_price": current_price,
                        "price_change_percent": price_change_percent,
                        "quantity": quantity,
                        "exchange_code": exchange_code,
                        "sell_reasons": sell_reasons,
                        "technical_sell_signals": technical_sell_signals,
                        "technical_sell_details": tech_sell_signals_details if tech_sell_signals_details else None,
                        "sentiment_score": sentiment_score,
                        "adx": adx_value,
                        "vix": vix_value,
                        "technical_data": tech_record
                    })
            
            # 가격 변동률이 큰 순서로 정렬 (절대값 기준)
            sell_candidates.sort(key=lambda x: abs(x["price_change_percent"]), reverse=True)
            
            return {
                "message": f"{len(sell_candidates)}개의 매도 대상 종목을 식별했습니다",
                "sell_candidates": sell_candidates
            }
            
        except Exception as e:
            print(f"매도 대상 종목 식별 중 오류 발생: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return {
                "message": f"매도 대상 종목 식별 중 오류 발생: {str(e)}",
                "sell_candidates": []
            }