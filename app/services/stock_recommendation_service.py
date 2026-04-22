import logging
import pandas as pd
import requests
import time
from datetime import datetime, timedelta
from app.db.supabase import supabase
import numpy as np
from app.core.config import settings
from app.services.balance_service import get_overseas_balance, get_all_overseas_balances
from app.services.volume_service import get_overseas_daily_price

logger = logging.getLogger(__name__)

# 거래소 코드 → KIS API 코드 변환 (변경 없음)
EXCHANGE_TO_API = {
    "NASD": "NAS",
    "NYSE": "NYS",
    "AMEX": "AMS",
}


def _load_stock_universe() -> list[dict]:
    """stock_universe 테이블에서 활성 종목 목록을 로드합니다."""
    try:
        resp = supabase.table("stock_universe").select("ticker, name_ko, exchange, is_etf").eq("is_active", True).execute()
        return resp.data or []
    except Exception as e:
        logger.error(f"stock_universe 로드 오류: {e}")
        return []


class StockRecommendationService:

    def __init__(self):
        universe = _load_stock_universe()
        # ETF 제외한 종목 ticker 리스트
        self.tickers = [u["ticker"] for u in universe if not u.get("is_etf", False)]
        # ticker → name_ko 매핑
        self.ticker_to_name = {u["ticker"]: u["name_ko"] for u in universe}
        # ticker → exchange 매핑
        self.ticker_to_exchange = {u["ticker"]: u["exchange"] for u in universe}
        self.lookback_days = 180

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
        """기술적 지표를 계산하고 stock_signals 테이블에 저장"""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=self.lookback_days)
        start_date_str = start_date.strftime("%Y-%m-%d")

        # stock_daily_prices (Long format) → Wide format pivot
        resp = supabase.table("stock_daily_prices") \
            .select("date, ticker, close") \
            .in_("ticker", self.tickers) \
            .gte("date", start_date_str) \
            .order("date") \
            .execute()

        if not resp.data:
            return {"message": "주가 데이터가 없습니다", "data": []}

        raw_df = pd.DataFrame(resp.data)
        raw_df["date"] = pd.to_datetime(raw_df["date"])
        raw_df["close"] = pd.to_numeric(raw_df["close"], errors="coerce")

        # Long → Wide (날짜 × 티커)
        df = raw_df.pivot(index="date", columns="ticker", values="close")
        df.sort_index(inplace=True)
        df.ffill(inplace=True)
        df.bfill(inplace=True)

        signals = []
        for ticker in self.tickers:
            if ticker not in df.columns:
                logger.warning(f"{ticker}: stock_daily_prices에 데이터 없음")
                continue

            prices = df[ticker]

            sma20 = self.calculate_sma(prices, 20)
            sma50 = self.calculate_sma(prices, 50)
            golden_cross = sma20 > sma50
            rsi = self.calculate_rsi(prices)
            macd, signal_line = self.calculate_macd(prices)
            macd_buy_signal = macd > signal_line

            # KIS API에서 거래량/ADX 조회
            volume_ratio = None
            adx = None
            daily_change_pct = None
            try:
                exchange = self.ticker_to_exchange.get(ticker, "NASD")
                api_excd = EXCHANGE_TO_API.get(exchange, "NAS")
                vol_result = get_overseas_daily_price(api_excd, ticker, gubn="0")
                if vol_result and vol_result.get("rt_cd") == "0":
                    raw_daily = vol_result.get("output2", [])
                    # 실제 거래일 필터링 (주말·비정상 제외)
                    daily_data = []
                    for d in raw_daily:
                        xymd = d.get("xymd", "")
                        tvol = int(d.get("tvol", "0") or "0")
                        if xymd and len(xymd) == 8 and tvol > 0:
                            try:
                                if datetime.strptime(xymd, "%Y%m%d").weekday() < 5:
                                    daily_data.append(d)
                            except ValueError:
                                pass
                    # 미완료 거래일(프리마켓) 제거
                    if len(daily_data) >= 2:
                        first_vol  = int(daily_data[0].get("tvol", "0") or "0")
                        second_vol = int(daily_data[1].get("tvol", "0") or "0")
                        if second_vol > 0 and first_vol < second_vol * 0.01:
                            daily_data = daily_data[1:]
                    # 거래량 비율 (5일 평균 대비)
                    if len(daily_data) >= 6:
                        today_vol = int(daily_data[0].get("tvol", "0") or "0")
                        past_vols = [int(d.get("tvol", "0") or "0") for d in daily_data[1:6] if int(d.get("tvol", "0") or "0") > 0]
                        if past_vols:
                            avg_vol = sum(past_vols) / len(past_vols)
                            volume_ratio = round(today_vol / avg_vol, 2) if avg_vol > 0 else None
                    adx = self.calculate_adx(daily_data or raw_daily)
                    if len(daily_data) >= 2:
                        c0 = float(daily_data[0].get("clos", "0") or "0")
                        c1 = float(daily_data[1].get("clos", "0") or "0")
                        if c0 > 0 and c1 > 0:
                            daily_change_pct = round((c0 - c1) / c1 * 100, 2)
                time.sleep(1.1)
            except Exception as e:
                logger.warning(f"{ticker} 거래량/ADX 조회 실패: {e}")

            latest = df.index[-1]
            if all(pd.notna([sma20[latest], sma50[latest], rsi[latest], macd[latest], signal_line[latest]])):
                signals.append({
                    "date":             latest.strftime("%Y-%m-%d"),
                    "ticker":           ticker,
                    "sma20":            float(sma20[latest]),
                    "sma50":            float(sma50[latest]),
                    "golden_cross":     bool(golden_cross[latest]),
                    "rsi":              float(rsi[latest]),
                    "macd":             float(macd[latest]),
                    "signal_line":      float(signal_line[latest]),
                    "macd_buy_signal":  bool(macd_buy_signal[latest]),
                    "volume_ratio":     volume_ratio,
                    "adx":              adx,
                    "daily_change_pct": daily_change_pct,
                })

        # stock_signals 테이블 upsert (date+ticker unique)
        try:
            supabase.table("stock_signals").upsert(signals, on_conflict="date,ticker").execute()
        except Exception as e:
            logger.exception(f"stock_signals 저장 오류: {e}")
            raise

        return {"message": f"{len(signals)}개의 기술적 지표 데이터가 생성되었습니다", "data": signals}

    def get_stock_recommendations(self):
        """
        stock_predictions 테이블에서 상승확률 ≥ 3% 종목을 반환합니다.
        """
        # 가장 최근 예측일 기준 조회
        resp = supabase.table("stock_predictions").select("*").gte("rise_probability", 3).order("rise_probability", desc=True).execute()
        if not resp.data:
            return {"message": "분석 결과를 찾을 수 없습니다", "recommendations": []}

        df = pd.DataFrame(resp.data)
        # 같은 ticker의 가장 최신 예측만 유지
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date", ascending=False).drop_duplicates(subset="ticker")

        recommendations = []
        for _, row in df.iterrows():
            recommendations.append({
                "Stock":                row["ticker"],
                "stock_name":          self.ticker_to_name.get(row["ticker"], row["ticker"]),
                "Accuracy (%)":        row.get("accuracy"),
                "Rise Probability (%)": row.get("rise_probability"),
                "Last Actual Price":   row.get("actual_price"),
                "Predicted Future Price": row.get("predicted_price"),
            })

        return {
            "message": f"{len(recommendations)}개의 추천 주식을 찾았습니다",
            "recommendations": recommendations,
        }

    def get_recommendations_with_sentiment(self):
        """
        ML 추천 주식 중 감성 점수 ≥ 0.15인 종목만 필터링하여 반환합니다.
        """
        stock_recs = self.get_stock_recommendations()
        recommendations = stock_recs.get("recommendations", [])
        if not recommendations:
            return {"message": "추천 주식이 없습니다", "results": []}

        rec_tickers = {r["Stock"] for r in recommendations}
        sentiment_resp = supabase.table("ticker_sentiment").select("*").gte("sentiment_score", 0.15).execute()
        if not sentiment_resp.data:
            return {"message": "감성 분석 데이터가 없습니다", "results": []}

        rec_by_ticker = {r["Stock"]: r for r in recommendations}
        results = []
        for s in sentiment_resp.data:
            ticker = s["ticker"]
            if ticker in rec_tickers:
                rec = rec_by_ticker[ticker]
                results.append({
                    "ticker":                ticker,
                    "stock_name":           self.ticker_to_name.get(ticker, ticker),
                    "accuracy":             rec.get("Accuracy (%)"),
                    "rise_probability":     rec.get("Rise Probability (%)"),
                    "last_actual_price":    rec.get("Last Actual Price"),
                    "predicted_future_price": rec.get("Predicted Future Price"),
                    "sentiment_score":      s["sentiment_score"],
                    "article_count":        s["article_count"],
                    "updated_at":           s["updated_at"],
                })

        return {"message": f"{len(results)}개의 추천 주식을 분석했습니다", "results": results}

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

        alpha_key = settings.ALPHA_VANTAGE_API_KEY
        relevance_threshold = 0.2
        sleep_interval = 5
        # 어제부터 3일 전까지 기사 수집 (AlphaVantage 무료 티어 기준)
        since = (datetime.now() - timedelta(days=1)).strftime("%Y%m%dT0000")

        results = []
        for ticker in all_tickers:
            logger.info(f"{ticker} 감성 분석 중...")
            resp = requests.get("https://www.alphavantage.co/query", params={
                "function": "NEWS_SENTIMENT",
                "tickers":  ticker,
                "time_from": since,
                "limit":    100,
                "apikey":   alpha_key,
            })
            if resp.status_code != 200:
                results.append({"ticker": ticker, "message": "API 호출 실패"})
                time.sleep(sleep_interval)
                continue

            feed = resp.json().get("feed", [])
            scores = [
                float(s["ticker_sentiment_score"])
                for article in feed
                for s in article.get("ticker_sentiment", [])
                if s["ticker"] == ticker and float(s.get("relevance_score", 0)) >= relevance_threshold
            ]

            if not scores:
                results.append({"ticker": ticker, "message": "관련 기사 없음"})
                time.sleep(sleep_interval)
                continue

            avg_score = sum(scores) / len(scores)

            # ticker_sentiment 테이블 upsert (unique: ticker)
            supabase.table("ticker_sentiment").upsert({
                "ticker":          ticker,
                "sentiment_score": avg_score,
                "article_count":   len(scores),
                "updated_at":      datetime.now().isoformat(),
            }, on_conflict="ticker").execute()

            results.append({
                "ticker":          ticker,
                "stock_name":      self.ticker_to_name.get(ticker, ticker),
                "sentiment_score": avg_score,
                "article_count":   len(scores),
                "is_recommended":  ticker in recommended_tickers,
                "is_holding":      ticker in holding_tickers,
            })
            time.sleep(sleep_interval)

        return {
            "message": f"{len(results)}개 티커 분석 완료 (추천: {len(recommended_tickers)}, 보유: {len(holding_tickers)})",
            "results": results,
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
            # 1. 기술적 지표 조회 (stock_signals)
            tech_response = supabase.table("stock_signals").select("*").order("date", desc=True).execute()
            if not tech_response.data:
                return {"message": "기술적 지표 데이터가 없습니다", "results": []}

            tech_df = pd.DataFrame(tech_response.data)
            tech_df["golden_cross"]    = tech_df["golden_cross"].astype(bool)
            tech_df["macd_buy_signal"] = tech_df["macd_buy_signal"].astype(bool)
            tech_df["rsi"]             = pd.to_numeric(tech_df["rsi"])
            tech_df = tech_df.sort_values("date", ascending=False).drop_duplicates(subset=["ticker"], keep="first")

            # 2. ML 예측 데이터 조회
            stock_recs = self.get_stock_recommendations()
            recommendations = stock_recs.get("recommendations", [])
            if not recommendations:
                return {"message": "추천 주식이 없습니다", "results": []}

            # 3. 감성분석 데이터 조회 (ticker_sentiment)
            sentiment_response = supabase.table("ticker_sentiment").select("*").execute()

            # 4. 데이터 매핑
            def _safe(v):
                try:
                    return None if pd.isna(v) else v
                except (ValueError, TypeError):
                    return v

            tech_map      = {row["ticker"]: {k: _safe(v) for k, v in row.to_dict().items()} for _, row in tech_df.iterrows()}
            sentiment_map = {item["ticker"]: item for item in sentiment_response.data} if sentiment_response.data else {}

            # 5. 결과 통합
            results = []
            for rec in recommendations:
                ticker    = rec["Stock"]
                tech_data = tech_map.get(ticker)
                if tech_data is None:
                    continue

                sentiment = sentiment_map.get(ticker)

                results.append({
                    "ticker":               ticker,
                    "stock_name":          self.ticker_to_name.get(ticker, ticker),
                    "accuracy":            rec.get("Accuracy (%)"),
                    "rise_probability":    rec.get("Rise Probability (%)"),
                    "last_price":          rec.get("Last Actual Price"),
                    "predicted_price":     rec.get("Predicted Future Price"),
                    "sentiment_score":     sentiment["sentiment_score"] if sentiment else None,
                    "article_count":       sentiment["article_count"] if sentiment else None,
                    "technical_date":      tech_data.get("date"),
                    "sma20":               tech_data.get("sma20"),
                    "sma50":               tech_data.get("sma50"),
                    "golden_cross":        bool(tech_data.get("golden_cross", False)),
                    "rsi":                 tech_data.get("rsi"),
                    "macd":                tech_data.get("macd"),
                    "signal":              tech_data.get("signal_line"),
                    "macd_buy_signal":     bool(tech_data.get("macd_buy_signal", False)),
                    "volume_ratio":        tech_data.get("volume_ratio"),
                    "adx":                 tech_data.get("adx"),
                    "daily_change_pct":    tech_data.get("daily_change_pct"),
                })

            # 5-1. VIX 조회 (economic_indicators)
            vix_value = None
            try:
                vix_response = supabase.table("economic_indicators").select("vix").order("date", desc=True).limit(1).execute()
                if vix_response.data and vix_response.data[0].get("vix") is not None:
                    vix_value = float(vix_response.data[0]["vix"])
                    logger.info(f"VIX 지수: {vix_value}")
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
            logger.exception(f"추천 주식 분석 중 오류: {e}")
            raise

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
            
            # 3. 기술적 지표 데이터 가져오기 (stock_signals)
            tech_response = supabase.table("stock_signals").select("*").order("date", desc=True).execute()
            tech_data = pd.DataFrame(tech_response.data) if tech_response.data else pd.DataFrame()

            if not tech_data.empty:
                tech_data["golden_cross"]    = tech_data["golden_cross"].astype(bool)
                tech_data["macd_buy_signal"] = tech_data["macd_buy_signal"].astype(bool)
                tech_data["rsi"]             = pd.to_numeric(tech_data["rsi"])
                tech_data = tech_data.sort_values("date", ascending=False).drop_duplicates(subset=["ticker"], keep="first")

            # 4. 감성 분석 데이터 가져오기 (ticker_sentiment)
            sentiment_response = supabase.table("ticker_sentiment").select("*").execute()
            sentiment_data = {item["ticker"]: item for item in sentiment_response.data} if sentiment_response.data else {}

            # 5. VIX 조회 (economic_indicators)
            vix_value = None
            try:
                vix_response = supabase.table("economic_indicators").select("vix").order("date", desc=True).limit(1).execute()
                if vix_response.data and vix_response.data[0].get("vix") is not None:
                    vix_value = float(vix_response.data[0]["vix"])
                    logger.info(f"매도 판단용 VIX: {vix_value}")
            except Exception as e:
                logger.warning(f"VIX 조회 실패: {e}")

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

            for item in holdings:
                ticker         = item.get("ovrs_pdno")
                stock_name     = item.get("ovrs_item_name", ticker)
                purchase_price = float(item.get("pchs_avg_pric", 0))
                current_price  = float(item.get("now_pric2", 0))
                quantity       = int(item.get("ovrs_cblc_qty", 0))
                exchange_code  = item.get("ovrs_excg_cd", "")

                price_change_percent = ((current_price - purchase_price) / purchase_price) * 100 if purchase_price > 0 else 0

                sell_reasons           = []
                technical_sell_signals = 0

                # 조건 1: ATR 기반 동적 익절/손절
                trade_record = trade_records_map.get(ticker)
                if trade_record and trade_record.get("take_profit_price") and trade_record.get("stop_loss_price"):
                    tp_price = float(trade_record["take_profit_price"])
                    sl_price = float(trade_record["stop_loss_price"])
                    if current_price >= tp_price:
                        sell_reasons.append(f"ATR 익절: 현재 ${current_price:.2f} >= 익절가 ${tp_price:.2f} ({price_change_percent:.2f}%)")
                    elif current_price <= sl_price:
                        sell_reasons.append(f"ATR 손절: 현재 ${current_price:.2f} <= 손절가 ${sl_price:.2f} ({price_change_percent:.2f}%)")
                else:
                    if price_change_percent >= 5:
                        sell_reasons.append(f"익절(고정): +{price_change_percent:.2f}%")
                    elif price_change_percent <= -7:
                        sell_reasons.append(f"손절(고정): {price_change_percent:.2f}%")

                # 기술적 지표 (stock_signals 테이블, ticker 기반 매칭)
                tech_record = None
                if not tech_data.empty:
                    tech_filtered = tech_data[tech_data["ticker"] == ticker]
                    if not tech_filtered.empty:
                        tech_record = tech_filtered.iloc[0].to_dict()
                
                # 조건 2: 기술적 매도 신호 (stock_signals 컬럼명 사용)
                tech_sell_signals_details = []
                if tech_record:
                    if not tech_record.get("golden_cross", True):
                        technical_sell_signals += 1
                        tech_sell_signals_details.append("데드 크로스")

                    rsi_val = tech_record.get("rsi", 50)
                    if rsi_val and float(rsi_val) > 70:
                        technical_sell_signals += 1
                        tech_sell_signals_details.append(f"RSI 과매수({float(rsi_val):.1f})")

                    if not tech_record.get("macd_buy_signal", True):
                        technical_sell_signals += 1
                        tech_sell_signals_details.append("MACD 매도 신호")

                    vr   = tech_record.get("volume_ratio")
                    dchg = tech_record.get("daily_change_pct")
                    if vr is not None and dchg is not None and float(vr) >= 2.0 and float(dchg) <= -3:
                        technical_sell_signals += 1
                        tech_sell_signals_details.append(f"패닉셀(거래량 {float(vr):.1f}배, {float(dchg):.1f}%)")

                adx_value     = float(tech_record["adx"]) if tech_record and tech_record.get("adx") is not None else None
                adx_adjustment = 1 if adx_value is not None and adx_value > 25 else 0

                sentiment_score = sentiment_data.get(ticker, {}).get("sentiment_score")

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
            logger.exception(f"매도 대상 종목 식별 오류: {e}")
            return {"message": f"매도 대상 종목 식별 오류: {e}", "sell_candidates": []}