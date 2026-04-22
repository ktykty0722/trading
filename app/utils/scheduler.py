import asyncio
import schedule
import time
import pytz
from datetime import datetime, timedelta
import threading
from app.services.stock_recommendation_service import StockRecommendationService, TICKER_TO_EXCHANGE, EXCHANGE_TO_API
from app.services.balance_service import get_current_price, order_overseas_stock, get_all_overseas_balances, inquire_psamount, get_overseas_nccs
from app.services.volume_service import get_overseas_daily_price
from app.db.supabase import supabase
from app.core.config import settings
import logging
from app.services.economic_service import update_economic_data_in_background
from app.services.llm_review_service import review_buy_candidates

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('stock_scheduler.log')
    ]
)
logger = logging.getLogger('stock_scheduler')

class StockScheduler:
    """주식 자동매매 스케줄러 클래스"""
    
    def __init__(self):
        self.recommendation_service = StockRecommendationService()
        self.running = False
        self.sell_running = False  # 매도 스케줄러 실행 상태
        self.scheduler_thread = None
        self._last_buy_date = None  # 당일 매수 중복 방지
    
    def start(self):
        """매수 스케줄러 시작"""
        if self.running:
            logger.warning("매수 스케줄러가 이미 실행 중입니다.")
            return False

        # 기존 매수 job 정리 후 등록
        for job in [j for j in schedule.jobs if j.job_func.__name__ == '_run_auto_buy']:
            schedule.cancel_job(job)
        schedule.every(5).minutes.do(self._run_auto_buy)

        # 별도 스레드에서 스케줄러 실행
        self.running = True
        self.scheduler_thread = threading.Thread(target=self._run_scheduler)
        self.scheduler_thread.daemon = True
        self.scheduler_thread.start()

        logger.info("주식 자동매매 스케줄러가 시작되었습니다. 뉴욕 시간 10:30 ET에 매수 작업이 실행됩니다.")
        return True
    
    def stop(self):
        """매수 스케줄러 중지"""
        if not self.running:
            logger.warning("매수 스케줄러가 실행 중이 아닙니다.")
            return False
        
        self.running = False
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=5)
        
        # 매수 관련 작업 취소 (sell 스케줄러는 유지)
        buy_jobs = [job for job in schedule.jobs if job.job_func.__name__ == '_run_auto_buy']
        for job in buy_jobs:
            schedule.cancel_job(job)
        
        logger.info("매수 스케줄러가 중지되었습니다.")
        return True
    
    def start_sell_scheduler(self):
        """매도 스케줄러 시작"""
        if self.sell_running:
            logger.warning("매도 스케줄러가 이미 실행 중입니다.")
            return False

        # 기존 매도 job 정리 후 등록
        for job in [j for j in schedule.jobs if j.job_func.__name__ == '_run_auto_sell']:
            schedule.cancel_job(job)
        schedule.every(1).minutes.do(self._run_auto_sell)
        
        # 스케줄러 스레드가 없으면 시작
        if not self.running and not self.scheduler_thread:
            self.scheduler_thread = threading.Thread(target=self._run_scheduler)
            self.scheduler_thread.daemon = True
            self.scheduler_thread.start()
        
        self.sell_running = True
        logger.info("매도 스케줄러가 시작되었습니다. 1분마다 매도 대상을 확인합니다.")
        return True
    
    def stop_sell_scheduler(self):
        """매도 스케줄러 중지"""
        if not self.sell_running:
            logger.warning("매도 스케줄러가 실행 중이 아닙니다.")
            return False
        
        # 매도 관련 작업만 취소
        sell_jobs = [job for job in schedule.jobs if job.job_func.__name__ == '_run_auto_sell']
        for job in sell_jobs:
            schedule.cancel_job(job)
        
        self.sell_running = False
        
        # 매수, 매도 모두 중지된 경우 스레드 종료
        if not self.running and self.scheduler_thread:
            self.scheduler_thread.join(timeout=5)
            self.scheduler_thread = None
            
        logger.info("매도 스케줄러가 중지되었습니다.")
        return True
    
    def _run_scheduler(self):
        """스케줄러 백그라운드 실행 함수"""
        while self.running or self.sell_running:
            schedule.run_pending()
            time.sleep(1)
    
    def _run_auto_buy(self):
        """자동 매수 실행 함수 - 스케줄링된 시간에 실행됨"""
        try:
            asyncio.run(self._execute_auto_buy())
            return True
        except Exception as e:
            logger.error(f"자동 매수 작업 중 오류 발생: {str(e)}", exc_info=True)
            return False
    
    def _run_auto_sell(self):
        """자동 매도 실행 함수 - 1분마다 실행됨"""
        try:
            asyncio.run(self._execute_auto_sell())
            return True
        except Exception as e:
            logger.error(f"자동 매도 작업 중 오류 발생: {str(e)}", exc_info=True)
            return False
    
    def _reconcile_orders(self, balance_result=None):
        """
        KIS 원장 기준 주문 정합성 확인 (1분마다 평일 실행)

        1. buy_ordered/sell_ordered/holding 레코드의 holding_quantity를 KIS 원장과 동기화
        2. 장 중: 체결 확인 (buy_ordered→holding, sell_ordered→sold)
        3. 장 마감 후(16:15 ET~): 미체결 정리 (buy_ordered→buy_failed, sell_ordered→holding 복원)
        4. 고아 감지: KIS에 보유 중인데 trade_records에 없는 종목 → 레코드 자동 생성

        미국 주식 지정가 주문은 Day Order로, 당일 장 마감 시 자동 취소됨.

        Args:
            balance_result: 이미 조회한 KIS 잔고 결과 (None이면 새로 조회)
        """
        try:
            # 활성 레코드 조회 (buy_ordered, sell_ordered, holding)
            active_response = supabase.table("trade_records").select("*").in_(
                "status", ["buy_ordered", "sell_ordered", "holding"]
            ).execute()
            active_records = active_response.data if active_response.data else []

            # KIS 원장에서 실제 보유 종목 조회 (외부에서 전달받지 않았으면 새로 조회)
            if balance_result is None:
                balance_result = get_all_overseas_balances()
            if balance_result.get("rt_cd") != "0":
                logger.error(f"정합성 확인용 잔고 조회 실패: {balance_result.get('msg1', '')}")
                return

            # KIS 원장 보유 현황: {ticker: {qty, item_data}}
            kis_holdings = {}
            for item in balance_result.get("output1", []):
                ticker = item.get("ovrs_pdno")
                qty = int(item.get("ovrs_cblc_qty", 0))
                if ticker and qty > 0:
                    kis_holdings[ticker] = {"qty": qty, "item": item}

            # 장 마감 여부 확인 (16:15 ET 이후)
            ny_tz = pytz.timezone('America/New_York')
            now_ny = datetime.now(ny_tz)
            is_after_market_close = (now_ny.hour > 16) or (now_ny.hour == 16 and now_ny.minute >= 15)

            # 활성 레코드가 없어도 고아 감지는 실행
            tracked_tickers = set()

            for record in active_records:
                ticker = record["ticker"]
                status = record["status"]
                record_id = record["id"]
                kis_qty = kis_holdings.get(ticker, {}).get("qty", 0)
                tracked_tickers.add(ticker)

                if status == "buy_ordered":
                    if kis_qty > 0:
                        # 체결 확인 (부분 체결 포함) → holding 전환 + 보유수량 동기화
                        supabase.table("trade_records").update({
                            "status": "holding",
                            "holding_quantity": kis_qty,
                        }).eq("id", record_id).execute()
                        if kis_qty < record.get("quantity", 0):
                            logger.info(f"  {ticker} 부분 체결 → holding (주문: {record.get('quantity')}주, 체결: {kis_qty}주)")
                        else:
                            logger.info(f"  {ticker} 매수 체결 확인 → holding ({kis_qty}주)")
                    elif is_after_market_close:
                        # 장 마감 후 미보유 → 미체결 (Day Order 자동 취소)
                        supabase.table("trade_records").update({"status": "buy_failed"}).eq("id", record_id).execute()
                        logger.warning(f"  {ticker} 매수 미체결 (장 마감) → buy_failed")

                elif status == "holding":
                    # 보유 수량 동기화 (부분 체결 추가분 반영)
                    prev_qty = record.get("holding_quantity") or 0
                    if kis_qty > 0 and kis_qty != prev_qty:
                        supabase.table("trade_records").update({
                            "holding_quantity": kis_qty,
                        }).eq("id", record_id).execute()
                        logger.info(f"  {ticker} 보유수량 동기화: {prev_qty}주 → {kis_qty}주")

                elif status == "sell_ordered":
                    if kis_qty == 0:
                        # 전량 매도 체결 확정
                        supabase.table("trade_records").update({
                            "status": "sold",
                            "holding_quantity": 0,
                        }).eq("id", record_id).execute()
                        logger.info(f"  {ticker} 매도 체결 확인 → sold")
                    elif is_after_market_close:
                        prev_holding = record.get("holding_quantity") or record.get("quantity", 0)
                        if kis_qty < prev_holding:
                            # 부분 매도 체결 → holding 복원 (남은 수량)
                            supabase.table("trade_records").update({
                                "status": "holding",
                                "holding_quantity": kis_qty,
                                "sell_price": None,
                                "sell_date": None,
                                "sell_reason": None,
                                "profit_loss": None,
                                "profit_loss_pct": None,
                            }).eq("id", record_id).execute()
                            logger.warning(f"  {ticker} 부분 매도 (보유: {prev_holding}주 → {kis_qty}주) → holding 복원")
                        else:
                            # 매도 미체결 → holding 복원
                            supabase.table("trade_records").update({
                                "status": "holding",
                                "holding_quantity": kis_qty,
                                "sell_price": None,
                                "sell_date": None,
                                "sell_reason": None,
                                "profit_loss": None,
                                "profit_loss_pct": None,
                            }).eq("id", record_id).execute()
                            logger.warning(f"  {ticker} 매도 미체결 (장 마감) → holding 복원")

            # 고아 감지: KIS에 보유 중인데 trade_records에 없는 종목 (네트워크 에러 등)
            for ticker, info in kis_holdings.items():
                if ticker not in tracked_tickers:
                    item = info["item"]
                    qty = info["qty"]
                    supabase.table("trade_records").insert({
                        "ticker": ticker,
                        "stock_name": item.get("ovrs_item_name", ticker),
                        "buy_price": float(item.get("pchs_avg_pric", 0)),
                        "buy_date": now_ny.strftime("%Y-%m-%d %H:%M:%S"),
                        "quantity": qty,
                        "holding_quantity": qty,
                        "exchange_code": item.get("ovrs_excg_cd", ""),
                        "status": "holding",
                    }).execute()
                    logger.warning(f"  {ticker} 고아 감지: KIS 보유({qty}주) but trade_records 없음 → 레코드 자동 생성")

        except Exception as e:
            logger.error(f"주문 정합성 확인 실패: {e}", exc_info=True)

    async def _execute_auto_sell(self):
        """자동 매도 실행 로직"""
        # 현재 시간이 미국 장 시간인지 확인 (서머타임 고려)
        now_in_korea = datetime.now(pytz.timezone('Asia/Seoul'))

        # 미국 뉴욕 시간 (서머타임 자동 고려)
        now_in_ny = datetime.now(pytz.timezone('America/New_York'))
        ny_hour = now_in_ny.hour
        ny_minute = now_in_ny.minute
        ny_weekday = now_in_ny.weekday()  # 0=월요일, 6=일요일

        # 평일에만 실행
        is_weekday = 0 <= ny_weekday <= 4
        if not is_weekday:
            return

        # KIS 잔고를 한 번만 조회하여 reconcile + 매도 판단에 재사용
        balance_result = get_all_overseas_balances()

        # 주문 정합성 확인 (장 중 체결 확인 + 장 마감 후 미체결 정리)
        # 장 시간 체크 전에 실행해야 16:15 ET 이후에도 미체결 정리 가능
        self._reconcile_orders(balance_result=balance_result)

        # 미국 주식 시장은 평일(월-금) 9:30 AM - 4:00 PM ET
        is_market_open_time = (
            (ny_hour == 9 and ny_minute >= 30) or
            (10 <= ny_hour < 16) or
            (ny_hour == 16 and ny_minute == 0)
        )

        if not is_market_open_time:
            return

        logger.info(f"미국 장 시간 확인: {now_in_korea.strftime('%Y-%m-%d %H:%M:%S')} (뉴욕: {now_in_ny.strftime('%Y-%m-%d %H:%M:%S')})")

        # 매도 대상 종목 조회 (이미 조회한 잔고 재사용)
        sell_candidates_result = self.recommendation_service.get_stocks_to_sell(balance_result=balance_result)
        
        if not sell_candidates_result or not sell_candidates_result.get("sell_candidates"):
            logger.info("매도 대상 종목이 없습니다.")
            return
        
        sell_candidates = sell_candidates_result.get("sell_candidates", [])

        # sell_ordered 상태인 종목은 중복 매도 방지
        try:
            sell_ordered_response = supabase.table("trade_records").select("ticker").eq("status", "sell_ordered").execute()
            sell_ordered_tickers = {rec["ticker"] for rec in (sell_ordered_response.data or [])}
            if sell_ordered_tickers:
                before_count = len(sell_candidates)
                sell_candidates = [c for c in sell_candidates if c["ticker"] not in sell_ordered_tickers]
                if before_count != len(sell_candidates):
                    logger.info(f"매도 주문 접수 중인 {before_count - len(sell_candidates)}개 종목 제외")
        except Exception:
            pass

        if not sell_candidates:
            logger.info("매도 대상 종목이 없습니다.")
            return

        logger.info(f"매도 대상 종목 {len(sell_candidates)}개를 찾았습니다.")

        # 각 종목에 대해 매도 주문 실행
        for candidate in sell_candidates:
            try:
                ticker = candidate["ticker"]
                stock_name = candidate["stock_name"]
                exchange_code = candidate["exchange_code"]
                quantity = candidate["quantity"]
                
                # 매도 근거 로그 출력
                sell_reasons = candidate.get("sell_reasons", [])
                reasons_str = "; ".join(sell_reasons)
                logger.info(f"{stock_name}({ticker}) 매도 근거: {reasons_str}")
                
                # 거래소 코드 변환 (API 요청에 맞게 변환)
                api_exchange_code = EXCHANGE_TO_API.get(exchange_code, exchange_code)
                
                # 현재가 조회
                price_params = {
                    "AUTH": "",
                    "EXCD": api_exchange_code,  # 변환된 거래소 코드 사용
                    "SYMB": ticker
                }
                
                logger.info(f"{stock_name}({ticker}) 현재가 조회 요청. 거래소: {api_exchange_code}, 심볼: {ticker}")
                price_result = get_current_price(price_params)
                
                if price_result.get("rt_cd") != "0":
                    logger.error(f"{stock_name}({ticker}) 현재가 조회 실패: {price_result.get('msg1', '알 수 없는 오류')}")
                    # API 속도 제한에 도달했을 때 더 오래 대기
                    if "초당" in price_result.get('msg1', ''):
                        await asyncio.sleep(3)  # 속도 제한 오류 시 3초 대기
                    continue
                
                # 현재가 추출 (안전하게 처리)
                last_price = price_result.get("output", {}).get("last", "")
                try:
                    # 빈 문자열이나 None 체크
                    if not last_price or last_price == "":
                        logger.error(f"{stock_name}({ticker}) 현재가가 비어있습니다. 다음 API 호출에서 다시 시도합니다.")
                        await asyncio.sleep(2)  # 잠시 기다렸다가 넘어감
                        continue
                    
                    current_price = float(last_price)
                    
                    if current_price <= 0:
                        logger.error(f"{stock_name}({ticker}) 현재가가 유효하지 않습니다: {current_price}")
                        continue
                except ValueError as ve:
                    logger.error(f"{stock_name}({ticker}) 현재가 변환 오류: {str(ve)}, 값: '{last_price}'")
                    continue

                await asyncio.sleep(1.5)  # KIS API 초당 제한 방지
                # 매도 주문 실행
                order_data = {
                    "CANO": settings.KIS_CANO,
                    "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
                    "OVRS_EXCG_CD": exchange_code,  # API 문서에 따라 원래대로 exchange_code 사용
                    "PDNO": ticker,
                    "ORD_DVSN": "00",  # 지정가
                    "ORD_QTY": str(quantity),
                    "OVRS_ORD_UNPR": str(current_price),
                    "is_buy": False  # 매도
                }
                
                logger.info(f"{stock_name}({ticker}) 매도 주문 실행: 수량 {quantity}주, 가격 ${current_price}")
                order_result = order_overseas_stock(order_data)
                
                if order_result.get("rt_cd") == "0":
                    logger.info(f"{stock_name}({ticker}) 매도 주문 성공: {order_result.get('msg1', '주문이 접수되었습니다.')}")

                    # trade_records 업데이트 (status → sell_ordered)
                    try:
                        # 매도 사유 결정
                        sell_reasons = candidate.get("sell_reasons", [])
                        sell_reason = "signal"
                        for reason in sell_reasons:
                            if "익절" in reason:
                                sell_reason = "take_profit"
                                break
                            elif "손절" in reason:
                                sell_reason = "stop_loss"
                                break

                        purchase_price = candidate.get("purchase_price", 0)
                        profit_loss = (current_price - purchase_price) * quantity if purchase_price > 0 else None
                        profit_loss_pct = ((current_price - purchase_price) / purchase_price) * 100 if purchase_price > 0 else None

                        supabase.table("trade_records").update({
                            "status": "sell_ordered",
                            "sell_price": current_price,
                            "sell_date": datetime.now(pytz.timezone('America/New_York')).isoformat(),
                            "sell_reason": sell_reason,
                            "profit_loss": round(profit_loss, 2) if profit_loss else None,
                            "profit_loss_pct": round(profit_loss_pct, 2) if profit_loss_pct else None,
                        }).eq("ticker", ticker).eq("status", "holding").execute()
                        logger.info(f"  {stock_name}({ticker}) trade_records 매도 주문 접수 (사유: {sell_reason}, 예상손익: {profit_loss_pct:.2f}%)" if profit_loss_pct else f"  {stock_name}({ticker}) trade_records 매도 주문 접수 (사유: {sell_reason})")
                    except Exception as tr_e:
                        logger.error(f"  {stock_name}({ticker}) trade_records 업데이트 실패: {tr_e}")
                else:
                    logger.error(f"{stock_name}({ticker}) 매도 주문 실패: {order_result.get('msg1', '알 수 없는 오류')}")

                # 요청 간 지연 (API 요청 제한 방지)
                await asyncio.sleep(2)

            except Exception as e:
                logger.error(f"{candidate['stock_name']}({candidate['ticker']}) 매도 처리 중 오류: {str(e)}", exc_info=True)
                await asyncio.sleep(1)

        logger.info("자동 매도 처리가 완료되었습니다.")
    
    async def _execute_auto_buy(self):
        """자동 매수 실행 로직 - 뉴욕 시간 10:30 ET에 실행"""
        # 뉴욕 시간 확인 (서머타임 자동 고려)
        now_in_ny = datetime.now(pytz.timezone('America/New_York'))
        ny_hour = now_in_ny.hour
        ny_minute = now_in_ny.minute
        ny_weekday = now_in_ny.weekday()
        ny_date = now_in_ny.date()

        # 평일 10:30~10:35 ET 사이에만 실행 (장 시작 후 1시간)
        is_weekday = 0 <= ny_weekday <= 4
        is_buy_time = (ny_hour == 10 and 30 <= ny_minute < 35)

        if not (is_weekday and is_buy_time):
            return

        # 당일 이미 매수 실행했으면 스킵
        if self._last_buy_date == ny_date:
            return

        logger.info(f"자동 매수 작업 시작 (뉴욕: {now_in_ny.strftime('%Y-%m-%d %H:%M:%S')})")
        self._last_buy_date = ny_date

        now_in_korea = datetime.now(pytz.timezone('Asia/Seoul'))
        logger.info(f"매수 시간 확인: {now_in_korea.strftime('%Y-%m-%d %H:%M:%S')} (뉴욕: {now_in_ny.strftime('%Y-%m-%d %H:%M:%S')})")

        # 보유 종목 조회
        try:
            balance_result = get_all_overseas_balances()
            if balance_result.get("rt_cd") != "0":
                logger.error(f"보유 종목 조회 실패: {balance_result.get('msg1', '알 수 없는 오류')}")
                return
            
            # 보유 종목 티커 추출
            holdings = balance_result.get("output1", [])
            holding_tickers = set()
            
            for item in holdings:
                ticker = item.get("ovrs_pdno")
                if ticker:
                    holding_tickers.add(ticker)
            
            # buy_ordered/holding 상태인 종목도 중복 매수 방지 (DB 이중 체크)
            try:
                ordered_response = supabase.table("trade_records").select("ticker").in_(
                    "status", ["buy_ordered", "holding", "sell_ordered"]
                ).execute()
                if ordered_response.data:
                    for rec in ordered_response.data:
                        holding_tickers.add(rec["ticker"])
            except Exception:
                pass

            logger.info(f"현재 보유/주문 중인 종목 수: {len(holding_tickers)}")
        except Exception as e:
            logger.error(f"보유 종목 조회 중 오류 발생: {str(e)}", exc_info=True)
            return
            
        # StockRecommendationService에서 이미 필터링된 매수 대상 종목 가져오기
        recommendations = self.recommendation_service.get_combined_recommendations_with_technical_and_sentiment()
        
        if not recommendations or not recommendations.get("results"):
            logger.info("매수 대상 종목이 없습니다.")
            return
        
        buy_candidates = recommendations.get("results", [])

        if not buy_candidates:
            logger.info("매수 조건을 만족하는 종목이 없습니다.")
            return

        logger.info(f"매수 후보 {len(buy_candidates)}개 → LLM 최종 검토 시작")

        # LLM 최종 검토 (거부권만 행사)
        vix_value = buy_candidates[0].get("vix_value") if buy_candidates else None
        review_result = review_buy_candidates(buy_candidates, vix_value)
        buy_candidates = review_result["reviewed_candidates"]

        if not buy_candidates:
            logger.info("LLM 검토 결과 매수 대상이 없습니다.")
            return

        logger.info(f"LLM 검토 통과: {len(buy_candidates)}개 종목 매수 진행")
        
        # 각 종목에 대해 API 호출하여 현재 체결가 조회 및 매수 주문
        for candidate in buy_candidates:
            try:
                ticker = candidate["ticker"]
                stock_name = candidate["stock_name"]
                
                # 거래소 코드 결정 (매핑 테이블 기반)
                pure_ticker = ticker.split(".")[0] if "." in ticker else ticker
                exchange_code = TICKER_TO_EXCHANGE.get(pure_ticker, "NASD")
                
                # 이미 보유 중이거나 이번 회차에서 주문한 종목인지 확인
                if pure_ticker in holding_tickers:
                    logger.info(f"{stock_name}({ticker}) - 이미 보유 중인 종목이므로 매수하지 않습니다.")
                    continue
                
                # 거래소 코드 변환 (API 요청에 맞게 변환)
                api_exchange_code = EXCHANGE_TO_API.get(exchange_code, "NAS")

                # 현재가 조회
                price_params = {
                    "AUTH": "",
                    "EXCD": api_exchange_code,
                    "SYMB": pure_ticker
                }

                logger.info(f"{stock_name}({ticker}) 현재가 조회 요청. 거래소: {api_exchange_code}, 심볼: {pure_ticker}")
                price_result = get_current_price(price_params)

                if price_result.get("rt_cd") != "0":
                    logger.error(f"{stock_name}({ticker}) 현재가 조회 실패: {price_result.get('msg1', '알 수 없는 오류')}")
                    await asyncio.sleep(1.5)
                    continue

                # 현재가 추출
                current_price = float(price_result.get("output", {}).get("last", 0))

                if current_price <= 0:
                    logger.error(f"{stock_name}({ticker}) 현재가가 유효하지 않습니다: {current_price}")
                    continue

                await asyncio.sleep(1.5)  # KIS API 초당 제한 방지
                # 매수가능금액 조회 → 종목당 10% 투자
                try:
                    ps_params = {
                        "CANO": settings.KIS_CANO,
                        "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
                        "OVRS_EXCG_CD": exchange_code,
                        "OVRS_ORD_UNPR": str(current_price),
                        "ITEM_CD": pure_ticker,
                    }
                    ps_result = inquire_psamount(ps_params)

                    if ps_result.get("rt_cd") != "0":
                        logger.error(f"{stock_name}({ticker}) 매수가능금액 조회 실패: {ps_result.get('msg1', '')}")
                        continue

                    # 외화주문가능금액 추출 (원화통합계좌: 원화 자동환전 포함 금액)
                    ps_output = ps_result.get("output", {})
                    available_amount = float(ps_output.get("frcr_ord_psbl_amt1", 0) or ps_output.get("ovrs_ord_psbl_amt", 0))
                    if available_amount <= 0:
                        logger.info(f"{stock_name}({ticker}) 매수가능금액이 없습니다.")
                        continue

                    # 종목당 투자금 = 가용 금액의 10%
                    invest_amount = available_amount * 0.10
                    quantity = int(invest_amount / current_price)

                    if quantity < 1:
                        logger.info(f"{stock_name}({ticker}) 투자금(${invest_amount:.2f})으로 1주도 살 수 없습니다. (현재가 ${current_price})")
                        continue

                    logger.info(f"{stock_name}({ticker}) 매수가능금액: ${available_amount:.2f}, 투자금(10%): ${invest_amount:.2f}, 수량: {quantity}주")
                except Exception as ps_e:
                    logger.error(f"{stock_name}({ticker}) 매수가능금액 조회 오류: {ps_e}")
                    continue

                await asyncio.sleep(1.5)  # KIS API 초당 제한 방지
                # 매수 주문 실행
                order_data = {
                    "CANO": settings.KIS_CANO,
                    "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
                    "OVRS_EXCG_CD": exchange_code,  # API 문서에 따라 원래대로 exchange_code 사용
                    "PDNO": pure_ticker,
                    "ORD_DVSN": "00",  # 지정가
                    "ORD_QTY": str(quantity),
                    "OVRS_ORD_UNPR": str(current_price),
                    "is_buy": True
                }
                
                logger.info(f"{stock_name}({ticker}) 매수 주문 실행: 수량 {quantity}주, 가격 ${current_price}")
                order_result = order_overseas_stock(order_data)
                
                if order_result.get("rt_cd") == "0":
                    logger.info(f"{stock_name}({ticker}) 매수 주문 성공: {order_result.get('msg1', '주문이 접수되었습니다.')}")
                    holding_tickers.add(pure_ticker)  # 중복 매수 방지

                    # trade_records에 ATR 기반 익절/손절 기준 저장
                    try:
                        atr_value = None
                        take_profit_price = None
                        stop_loss_price = None

                        vol_result = get_overseas_daily_price(api_exchange_code, pure_ticker, gubn="0")
                        if vol_result and vol_result.get("rt_cd") == "0":
                            daily_data = vol_result.get("output2", [])
                            atr_value = self.recommendation_service.calculate_atr(daily_data)
                            if atr_value:
                                take_profit_price = round(current_price + atr_value * 2.5, 2)
                                stop_loss_price = round(current_price - atr_value * 1.5, 2)
                                logger.info(f"  ATR={atr_value}, 익절가=${take_profit_price}, 손절가=${stop_loss_price}")

                        supabase.table("trade_records").insert({
                            "ticker": pure_ticker,
                            "stock_name": stock_name,
                            "buy_price": current_price,
                            "buy_date": datetime.now(pytz.timezone('America/New_York')).strftime("%Y-%m-%d %H:%M:%S"),
                            "quantity": quantity,
                            "holding_quantity": 0,
                            "exchange_code": exchange_code,
                            "atr": atr_value,
                            "take_profit_price": take_profit_price,
                            "stop_loss_price": stop_loss_price,
                            "status": "buy_ordered",
                            "composite_score": candidate.get("composite_score"),
                        }).execute()
                        logger.info(f"  {stock_name}({pure_ticker}) trade_records 저장 완료 (status: buy_ordered)")
                    except Exception as tr_e:
                        logger.error(f"  {stock_name}({pure_ticker}) trade_records 저장 실패: {tr_e}")
                else:
                    logger.error(f"{stock_name}({ticker}) 매수 주문 실패: {order_result.get('msg1', '알 수 없는 오류')}")

                # 요청 간 지연 (API 요청 제한 방지)
                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"{candidate['stock_name']}({candidate['ticker']}) 매수 처리 중 오류: {str(e)}", exc_info=True)

        logger.info("자동 매수 처리가 완료되었습니다.")

# 싱글톤 인스턴스 생성
stock_scheduler = StockScheduler()

def start_scheduler():
    """매수 스케줄러 시작 함수"""
    return stock_scheduler.start()

def stop_scheduler():
    """매수 스케줄러 중지 함수"""
    return stock_scheduler.stop()

def start_sell_scheduler():
    """매도 스케줄러 시작 함수"""
    return stock_scheduler.start_sell_scheduler()

def stop_sell_scheduler():
    """매도 스케줄러 중지 함수"""
    return stock_scheduler.stop_sell_scheduler()

def get_scheduler_status():
    """스케줄러 상태 확인"""
    return {
        "buy_running": stock_scheduler.running,
        "sell_running": stock_scheduler.sell_running
    }

def run_auto_buy_now():
    """즉시 매수 실행 함수 (테스트용)"""
    stock_scheduler._run_auto_buy()
    
def run_auto_sell_now():
    """즉시 매도 실행 함수 (테스트용)"""
    stock_scheduler._run_auto_sell()

# 경제 데이터 스케줄러 관련 변수 및 함수
economic_data_scheduler_running = False
economic_data_scheduler_thread = None

def _run_economic_data_update(force: bool = False):
    """경제 데이터 업데이트 실행 함수"""
    try:
        logger = logging.getLogger('economic_scheduler')
        logger.info("경제 데이터 업데이트 작업 시작")
        asyncio.run(update_economic_data_in_background(force=force))
        logger.info("경제 데이터 업데이트 작업 완료")
        return True
    except Exception as e:
        logger = logging.getLogger('economic_scheduler')
        logger.error(f"경제 데이터 업데이트 작업 중 오류 발생: {str(e)}", exc_info=True)
        return False

def start_economic_data_scheduler():
    """경제 데이터 업데이트 스케줄러 시작 함수 (별도 스레드 없이 글로벌 schedule에 job만 등록)"""
    global economic_data_scheduler_running

    if economic_data_scheduler_running:
        logger = logging.getLogger('economic_scheduler')
        logger.warning("경제 데이터 스케줄러가 이미 실행 중입니다.")
        return False

    # 기존 job 정리 후 등록
    for job in [j for j in schedule.jobs if j.job_func.__name__ == '_run_economic_data_update']:
        schedule.cancel_job(job)
    schedule.every().day.at("06:05").do(_run_economic_data_update)

    economic_data_scheduler_running = True
    # 별도 스레드 불필요: stock_scheduler의 _run_scheduler 스레드가 schedule.run_pending()을 실행

    logger = logging.getLogger('economic_scheduler')
    logger.info("경제 데이터 업데이트 스케줄러가 시작되었습니다. 한국 시간 새벽 6시 5분에 실행됩니다.")
    return True

def stop_economic_data_scheduler():
    """경제 데이터 업데이트 스케줄러 중지 함수"""
    global economic_data_scheduler_running

    if not economic_data_scheduler_running:
        logger = logging.getLogger('economic_scheduler')
        logger.warning("경제 데이터 스케줄러가 실행 중이 아닙니다.")
        return False

    # 경제 데이터 관련 작업 취소
    economic_jobs = [job for job in schedule.jobs if job.job_func.__name__ == '_run_economic_data_update']
    for job in economic_jobs:
        schedule.cancel_job(job)

    economic_data_scheduler_running = False
    
    logger = logging.getLogger('economic_scheduler')
    logger.info("경제 데이터 업데이트 스케줄러가 중지되었습니다.")
    return True

def run_economic_data_update_now(force: bool = False):
    """즉시 경제 데이터 업데이트 실행 함수 (force=True: 장 중에도 강제 수집)"""
    return _run_economic_data_update(force=force) 