import asyncio
import schedule
import time
import pytz
from datetime import datetime, timedelta
import threading
from app.services.stock_recommendation_service import StockRecommendationService, EXCHANGE_TO_API
from app.services.llm_review_service import review_buy_candidates
from app.services.balance_service import get_current_price, order_overseas_stock, get_all_overseas_balances, inquire_psamount, get_overseas_nccs
from app.services.volume_service import get_overseas_daily_price
from app.db.supabase import supabase
from app.core.config import settings
import logging
from app.services.economic_service import update_economic_data_in_background
from app.services.mlops_service import (
    should_run_monthly_retrain,
    run_monthly_retrain_job,
    evaluate_promotion_gate,
    evaluate_rollback_trigger,
)
from app.services.risk_service import (
    check_daily_loss_limit, check_max_positions,
    check_sector_concentration, calculate_position_size, check_vix_halt_gate,
    check_event_blackout, check_liquidity_filter,
    check_correlation_limit, get_mdd_risk_state,
)
from app.telegram_bot.notifier import (
    notify_buy_order, notify_sell_order, notify_vix_alert, notify_error,
    notify_daily_report, notify, notify_pipeline_failure, notify_pipeline_success,
)

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
        self.market_tz = pytz.timezone(settings.MARKET_TIMEZONE)
        self.running = False
        self.sell_running = False  # 매도 스케줄러 실행 상태
        self.scheduler_thread = None
        self._last_buy_date = None  # 당일 매수 중복 방지

    @staticmethod
    def _parse_hhmm(value: str, default_h: int, default_m: int) -> tuple[int, int]:
        try:
            hh, mm = value.split(":")
            return int(hh), int(mm)
        except Exception:
            return default_h, default_m

    def _get_buy_window(self) -> tuple[tuple[int, int], tuple[int, int]]:
        start_cfg = "10:30"
        end_cfg = "10:35"
        try:
            start_row = supabase.table("system_config").select("value").eq("key", "buy_window_start_et").limit(1).execute().data
            end_row = supabase.table("system_config").select("value").eq("key", "buy_window_end_et").limit(1).execute().data
            if start_row:
                start_cfg = start_row[0].get("value", start_cfg)
            if end_row:
                end_cfg = end_row[0].get("value", end_cfg)
        except Exception:
            pass
        return (
            self._parse_hhmm(start_cfg, 10, 30),
            self._parse_hhmm(end_cfg, 10, 35),
        )

    def _get_bool_config(self, key: str, default: bool) -> bool:
        try:
            row = supabase.table("system_config").select("value").eq("key", key).limit(1).execute().data
            if not row:
                return default
            value = str(row[0].get("value", "")).strip().lower()
            return value in {"1", "true", "yes", "y", "on"}
        except Exception as e:
            logger.warning(f"{key} 설정 조회 실패. 기본값({default}) 사용: {e}")
            return default

    def _is_market_open_time(self, now_market: datetime) -> bool:
        hour = now_market.hour
        minute = now_market.minute
        return (
            (hour == 9 and minute >= 30) or
            (10 <= hour < 16) or
            (hour == 16 and minute == 0)
        )

    def _is_buy_window(self, now_market: datetime) -> bool:
        (sh, sm), (eh, em) = self._get_buy_window()
        current_minutes = now_market.hour * 60 + now_market.minute
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        return start_minutes <= current_minutes < end_minutes
    
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
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(self._execute_auto_buy())
            else:
                loop.create_task(self._execute_auto_buy())
            return True
        except Exception as e:
            logger.error(f"자동 매수 작업 중 오류 발생: {str(e)}", exc_info=True)
            return False
    
    def _run_auto_sell(self):
        """자동 매도 실행 함수 - 1분마다 실행됨"""
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(self._execute_auto_sell())
            else:
                loop.create_task(self._execute_auto_sell())
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
            now_ny = datetime.now(self.market_tz)
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
        now_in_ny = datetime.now(self.market_tz)
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
        is_market_open_time = self._is_market_open_time(now_in_ny)

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
                            "sell_date": datetime.now(self.market_tz).isoformat(),
                            "sell_reason": sell_reason,
                            "profit_loss": round(profit_loss, 2) if profit_loss else None,
                            "profit_loss_pct": round(profit_loss_pct, 2) if profit_loss_pct else None,
                        }).eq("ticker", ticker).eq("status", "holding").execute()
                        logger.info(f"  {stock_name}({ticker}) trade_records 매도 주문 접수 (사유: {sell_reason}, 예상손익: {profit_loss_pct:.2f}%)" if profit_loss_pct else f"  {stock_name}({ticker}) trade_records 매도 주문 접수 (사유: {sell_reason})")
                        notify_sell_order(ticker, stock_name, purchase_price, current_price,
                                          quantity, sell_reason, profit_loss, profit_loss_pct)
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
        now_in_ny = datetime.now(self.market_tz)
        ny_weekday = now_in_ny.weekday()
        ny_date = now_in_ny.date()

        # 평일 10:30~10:35 ET 사이에만 실행 (장 시작 후 1시간)
        is_weekday = 0 <= ny_weekday <= 4
        is_buy_time = self._is_buy_window(now_in_ny)

        if not (is_weekday and is_buy_time):
            return

        buy_once_per_day = self._get_bool_config("buy_once_per_day", default=True)

        # 기본은 하루 1회만 실행. 테스트 모드에서는 buy_once_per_day=false로 장중 반복 실행 가능.
        if buy_once_per_day and self._last_buy_date == ny_date:
            return

        logger.info(f"자동 매수 작업 시작 (뉴욕: {now_in_ny.strftime('%Y-%m-%d %H:%M:%S')})")
        if buy_once_per_day:
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

        # ── 리스크 체크 1: 일일 최대 손실 한도 ──────────────────
        safe, reason = check_daily_loss_limit()
        if not safe:
            logger.warning(f"일일 손실 한도 초과 — 매수 중단: {reason}")
            notify(f"⛔ <b>일일 손실 한도 초과 — 매수 중단</b>\n{reason}")
            return

        # ── 리스크 체크 2: 최대 보유 종목 수 ─────────────────────
        can_buy, reason = check_max_positions(holding_tickers)
        if not can_buy:
            logger.info(f"최대 보유 종목 수 도달 — 매수 없음: {reason}")
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

        vix_value = buy_candidates[0].get("vix_value") if buy_candidates else None
        can_trade_by_vix, vix_reason = check_vix_halt_gate(vix_value)
        if not can_trade_by_vix:
            try:
                notify_vix_alert(vix_value, settings.VIX_HALT_THRESHOLD)
            except Exception:
                pass
            logger.warning(f"변동성 게이트로 매수 중단: {vix_reason}")
            return

        max_new_entries = settings.MAX_NEW_ENTRIES_PER_DAY
        buy_candidates = buy_candidates[:max_new_entries]
        logger.info(f"정량 필터 통과: {len(buy_candidates)}개 종목 매수 진행 (일일 최대 {max_new_entries}개)")

        llm_review = review_buy_candidates(buy_candidates, vix_value)
        buy_candidates = llm_review.get("reviewed_candidates", [])
        held_count = len(llm_review.get("held_candidates", []))
        if not buy_candidates:
            logger.info(f"LLM 최종 검토 결과 BUY 없음 (HOLD/FAIL {held_count}개). 매수를 중단합니다.")
            notify(f"⏸ <b>LLM 최종 검토로 매수 없음</b>\n{llm_review.get('llm_reasoning', '')[:300]}")
            return
        logger.info(f"LLM 최종 검토 통과: {len(buy_candidates)} BUY / {held_count} HOLD")

        can_trade_mdd, position_multiplier, mdd_reason = get_mdd_risk_state()
        if not can_trade_mdd:
            logger.warning(f"MDD 리스크 게이트로 신규 매수 중단: {mdd_reason}")
            notify(f"⛔ <b>MDD 리스크 게이트 중단</b>\n{mdd_reason}")
            return
        logger.info(f"MDD 리스크 상태: {mdd_reason}")
        gate_stats = {
            "total_candidates": len(buy_candidates),
            "skip_already_holding": 0,
            "skip_sector": 0,
            "skip_event": 0,
            "skip_liquidity": 0,
            "skip_correlation": 0,
            "placed_orders": 0,
        }
        
        # 각 종목에 대해 API 호출하여 현재 체결가 조회 및 매수 주문
        placed_orders = 0
        for candidate in buy_candidates:
            try:
                ticker = candidate["ticker"]
                stock_name = candidate["stock_name"]
                
                # 거래소 코드 결정 (매핑 테이블 기반)
                pure_ticker = ticker.split(".")[0] if "." in ticker else ticker
                exchange_code = self.recommendation_service.ticker_to_exchange.get(pure_ticker, "NASD")
                
                # 이미 보유 중이거나 이번 회차에서 주문한 종목인지 확인
                if pure_ticker in holding_tickers:
                    logger.info(f"{stock_name}({ticker}) - 이미 보유 중인 종목이므로 매수하지 않습니다.")
                    gate_stats["skip_already_holding"] += 1
                    continue

                # ── 리스크 체크 3: 섹터 집중도 ───────────────────
                can_buy_sector, sector_reason = check_sector_concentration(pure_ticker, holding_tickers)
                if not can_buy_sector:
                    logger.info(f"{stock_name}({ticker}) 섹터 집중도 제한: {sector_reason}")
                    gate_stats["skip_sector"] += 1
                    continue

                can_buy_event, event_reason = check_event_blackout(pure_ticker, now_et=now_in_ny)
                if not can_buy_event:
                    logger.info(f"{stock_name}({ticker}) 이벤트 블랙아웃 제외: {event_reason}")
                    gate_stats["skip_event"] += 1
                    continue

                can_buy_liquidity, liquidity_reason = check_liquidity_filter(pure_ticker)
                if not can_buy_liquidity:
                    logger.info(f"{stock_name}({ticker}) 유동성 필터 제외: {liquidity_reason}")
                    gate_stats["skip_liquidity"] += 1
                    continue

                can_buy_corr, corr_reason = check_correlation_limit(pure_ticker, holding_tickers)
                if not can_buy_corr:
                    logger.info(f"{stock_name}({ticker}) 상관계수 필터 제외: {corr_reason}")
                    gate_stats["skip_correlation"] += 1
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

                    # 포지션 사이징 (system_config.position_size_pct 기준)
                    quantity = calculate_position_size(available_amount, current_price)
                    quantity = int(quantity * position_multiplier)

                    if quantity < 1:
                        logger.info(
                            f"{stock_name}({ticker}) 매수수량이 1주 미만입니다. "
                            f"(가용금액=${available_amount:.2f}, 현재가=${current_price}, MDD배수={position_multiplier:.2f})"
                        )
                        continue

                    invest_amount = quantity * current_price
                    logger.info(f"{stock_name}({ticker}) 매수가능금액: ${available_amount:.2f}, 투자금: ${invest_amount:.2f}, 수량: {quantity}주")
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
                                take_profit_price = round(current_price + atr_value * settings.ATR_TAKE_PROFIT_MULTIPLIER, 2)
                                stop_loss_price = round(current_price - atr_value * settings.ATR_STOP_LOSS_MULTIPLIER, 2)
                                logger.info(f"  ATR={atr_value}, 익절가=${take_profit_price}, 손절가=${stop_loss_price}")

                        supabase.table("trade_records").insert({
                            "ticker": pure_ticker,
                            "stock_name": stock_name,
                            "buy_price": current_price,
                            "buy_date": datetime.now(self.market_tz).strftime("%Y-%m-%d %H:%M:%S"),
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
                        notify_buy_order(pure_ticker, stock_name, current_price, quantity,
                                         exchange_code, candidate.get("composite_score", 0),
                                         take_profit_price, stop_loss_price)
                    except Exception as tr_e:
                        logger.error(f"  {stock_name}({pure_ticker}) trade_records 저장 실패: {tr_e}")
                    placed_orders += 1
                    gate_stats["placed_orders"] += 1
                else:
                    logger.error(f"{stock_name}({ticker}) 매수 주문 실패: {order_result.get('msg1', '알 수 없는 오류')}")

                # 요청 간 지연 (API 요청 제한 방지)
                await asyncio.sleep(1)

                if placed_orders >= max_new_entries:
                    logger.info(f"일일 최대 신규 진입({max_new_entries})에 도달하여 매수를 종료합니다.")
                    break

            except Exception as e:
                logger.error(f"{candidate['stock_name']}({candidate['ticker']}) 매수 처리 중 오류: {str(e)}", exc_info=True)

        logger.info("자동 매수 처리가 완료되었습니다.")
        logger.info(
            "매수 게이트 요약: candidates=%s, placed=%s, already_holding=%s, sector=%s, event=%s, liquidity=%s, correlation=%s",
            gate_stats["total_candidates"],
            gate_stats["placed_orders"],
            gate_stats["skip_already_holding"],
            gate_stats["skip_sector"],
            gate_stats["skip_event"],
            gate_stats["skip_liquidity"],
            gate_stats["skip_correlation"],
        )

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


# ============================================================
# 일일 리포트 스케줄러 (17:00 ET = 장 마감 1시간 후)
# ============================================================
def _send_daily_report():
    try:
        today = datetime.now(pytz.timezone(settings.MARKET_TIMEZONE)).strftime("%Y-%m-%d")
        trades = supabase.table("trade_records").select(
            "status, profit_loss, profit_loss_pct"
        ).gte("created_at", today).execute().data or []

        holdings   = sum(1 for t in trades if t["status"] in ("holding", "buy_ordered"))
        sold_today = [t for t in trades if t["status"] == "sold" and t.get("profit_loss_pct") is not None]
        daily_pnl  = sum(t["profit_loss_pct"] for t in sold_today) / len(sold_today) if sold_today else None

        all_sold = supabase.table("trade_records").select("profit_loss").eq(
            "status", "sold"
        ).execute().data or []
        total_pnl = sum(t["profit_loss"] for t in all_sold if t.get("profit_loss")) or None

        notify_daily_report(today, holdings, daily_pnl, total_pnl)
    except Exception as e:
        logger.error(f"일일 리포트 발송 실패: {e}")


_daily_report_running = False


def start_daily_report_scheduler():
    global _daily_report_running
    if _daily_report_running:
        return False
    schedule.every().day.at("17:00").do(_send_daily_report)
    _daily_report_running = True
    logger.info("일일 리포트 스케줄러 시작됨 (매일 17:00 ET)")
    return True


# ============================================================
# MLOps 스케줄러 (월간 재학습 + 승격/롤백 게이트 모니터링)
# ============================================================
_mlops_running = False
_last_retrain_run_date = None
_last_rollback_alert_date = None


def _run_mlops_cycle():
    global _last_retrain_run_date, _last_rollback_alert_date
    now_et = datetime.now(pytz.timezone(settings.MARKET_TIMEZONE))

    # 1) 월간 재학습: 첫째 일요일 13:00 ET
    if should_run_monthly_retrain(now_et, _last_retrain_run_date):
        ok, msg = run_monthly_retrain_job()
        _last_retrain_run_date = now_et.date().isoformat()
        if ok:
            logger.info(f"[MLOps] {msg}")
            notify(f"🤖 <b>월간 재학습 완료</b>\n{msg}")
            promote_ok, promote_msg = evaluate_promotion_gate()
            if promote_ok:
                logger.info(f"[MLOps] {promote_msg}")
                notify(f"✅ <b>모델 승격 통과</b>\n{promote_msg}")
            else:
                logger.warning(f"[MLOps] {promote_msg}")
                notify(f"⛔ <b>모델 승격 차단</b>\n{promote_msg}")
        else:
            logger.error(f"[MLOps] {msg}")
            notify_error("mlops.monthly_retrain", msg)

    # 2) 롤백 트리거 모니터링: 매일 17:10 ET
    if now_et.hour == 17 and now_et.minute >= 10:
        rollback, reason = evaluate_rollback_trigger()
        today = now_et.date().isoformat()
        if rollback and _last_rollback_alert_date != today:
            _last_rollback_alert_date = today
            logger.warning(f"[MLOps] {reason}")
            notify(f"🚨 <b>롤백 트리거 감지</b>\n{reason}")


def start_mlops_scheduler():
    global _mlops_running
    if _mlops_running:
        return False
    schedule.every(30).minutes.do(_run_mlops_cycle)
    _mlops_running = True
    logger.info("MLOps 스케줄러 시작됨 (30분 주기 모니터링)")
    return True


def stop_mlops_scheduler():
    global _mlops_running
    if not _mlops_running:
        return False
    mlops_jobs = [job for job in schedule.jobs if job.job_func.__name__ == '_run_mlops_cycle']
    for job in mlops_jobs:
        schedule.cancel_job(job)
    _mlops_running = False
    logger.info("MLOps 스케줄러 중지됨")
    return True


# ============================================================
# 데이터 파이프라인 자동화 (Phase 1)
# ============================================================
_pipeline_logger = logging.getLogger('data_pipeline')
_pipeline_locks: dict[str, bool] = {}


def _read_cfg(key: str, default: str) -> str:
    try:
        row = supabase.table("system_config").select("value").eq("key", key).limit(1).execute().data
        if row:
            return str(row[0].get("value", default))
    except Exception:
        pass
    return default


def _pipeline_enabled() -> bool:
    return _read_cfg("data_pipeline_enabled", "true").strip().lower() in {"1", "true", "yes", "on"}


def _log_pipeline_run(job_name: str, status: str, message: str = "", duration_ms: int = 0, metadata: dict | None = None) -> None:
    try:
        record = {
            "job_name": job_name,
            "status": status,
            "message": (message or "")[:1000],
            "duration_ms": duration_ms,
            "finished_at": datetime.now().isoformat(),
        }
        if metadata:
            record["metadata"] = metadata
        supabase.table("pipeline_runs").insert(record).execute()
    except Exception as e:
        _pipeline_logger.warning(f"pipeline_runs 기록 실패 ({job_name}): {e}")


def _run_pipeline_job(job_name: str, fn, *args, **kwargs) -> None:
    """공통 wrapper: 중복실행 잠금 + 로그 + Telegram 알림."""
    if not _pipeline_enabled():
        _pipeline_logger.info(f"[{job_name}] data_pipeline_enabled=false — 스킵")
        _log_pipeline_run(job_name, "skipped", "pipeline disabled")
        return

    if _pipeline_locks.get(job_name):
        _pipeline_logger.warning(f"[{job_name}] 이미 실행 중 — 중복실행 방지")
        return

    _pipeline_locks[job_name] = True
    started = time.time()
    try:
        _pipeline_logger.info(f"[{job_name}] 시작")
        result = fn(*args, **kwargs)
        dur_ms = int((time.time() - started) * 1000)
        # 결과 dict가 success=False면 실패 취급
        is_success = True
        msg = ""
        if isinstance(result, dict):
            is_success = bool(result.get("success", True))
            msg = str(result.get("message", ""))
        if is_success:
            _pipeline_logger.info(f"[{job_name}] 성공 ({dur_ms}ms) {msg}")
            _log_pipeline_run(job_name, "success", msg, dur_ms)
        else:
            _pipeline_logger.error(f"[{job_name}] 실패 ({dur_ms}ms) {msg}")
            _log_pipeline_run(job_name, "failed", msg, dur_ms)
            notify_pipeline_failure(job_name, msg or "unknown")
    except Exception as e:
        dur_ms = int((time.time() - started) * 1000)
        _pipeline_logger.exception(f"[{job_name}] 예외: {e}")
        _log_pipeline_run(job_name, "failed", str(e), dur_ms)
        try:
            notify_pipeline_failure(job_name, str(e))
        except Exception:
            pass
    finally:
        _pipeline_locks[job_name] = False


# ── 개별 job 함수 ──────────────────────────────────────────
def _job_economic_update():
    _run_pipeline_job("economic_update", lambda: asyncio.run(update_economic_data_in_background(force=False)))


def _job_technical_signals():
    def _do():
        result = StockRecommendationService().generate_technical_recommendations()
        # 결과가 빈 data면 실패가 아닌 경고로 취급
        return {"success": True, "message": result.get("message", "")}
    _run_pipeline_job("technical_signals", _do)


def _job_ml_prediction():
    def _do():
        from app.services.ml_prediction_service import run_ml_prediction
        return run_ml_prediction()
    _run_pipeline_job("ml_prediction", _do)


def _job_sentiment_update():
    def _do():
        result = StockRecommendationService().fetch_and_store_sentiment_for_recommendations()
        return {"success": True, "message": result.get("message", "")}
    _run_pipeline_job("sentiment_update", _do)


# ── 등록/해제 ──────────────────────────────────────────────
_data_pipeline_running = False
_data_pipeline_job_names = {
    "_job_economic_update",
    "_job_technical_signals",
    "_job_ml_prediction",
    "_job_sentiment_update",
}


def _parse_hhmm_str(value: str, default: str) -> str:
    """'06:05' 형식 검증 후 그대로 반환, 잘못된 경우 default."""
    try:
        hh, mm = value.split(":")
        int(hh); int(mm)
        return f"{int(hh):02d}:{int(mm):02d}"
    except Exception:
        return default


def start_data_pipeline_scheduler():
    """system_config 시간값을 읽어 daily job 4개 등록."""
    global _data_pipeline_running
    if _data_pipeline_running:
        _pipeline_logger.warning("데이터 파이프라인 스케줄러가 이미 실행 중입니다.")
        return False

    # 기존 동일 이름 job 제거 (재시작 안전)
    for job in [j for j in schedule.jobs if j.job_func.__name__ in _data_pipeline_job_names]:
        schedule.cancel_job(job)

    econ_t = _parse_hhmm_str(_read_cfg("economic_update_time_kst", "06:05"), "06:05")
    sig_t = _parse_hhmm_str(_read_cfg("technical_signal_time_kst", "06:20"), "06:20")
    ml_t = _parse_hhmm_str(_read_cfg("ml_prediction_time_kst", "06:35"), "06:35")
    sent_t = _parse_hhmm_str(_read_cfg("sentiment_update_time_kst", "07:10"), "07:10")

    schedule.every().day.at(econ_t).do(_job_economic_update)
    schedule.every().day.at(sig_t).do(_job_technical_signals)
    schedule.every().day.at(ml_t).do(_job_ml_prediction)
    schedule.every().day.at(sent_t).do(_job_sentiment_update)

    _data_pipeline_running = True
    _pipeline_logger.info(
        f"데이터 파이프라인 스케줄러 시작 KST: "
        f"economic={econ_t}, signals={sig_t}, ml={ml_t}, sentiment={sent_t}"
    )
    return True


def stop_data_pipeline_scheduler():
    global _data_pipeline_running
    if not _data_pipeline_running:
        return False
    for job in [j for j in schedule.jobs if j.job_func.__name__ in _data_pipeline_job_names]:
        schedule.cancel_job(job)
    _data_pipeline_running = False
    _pipeline_logger.info("데이터 파이프라인 스케줄러 중지됨")
    return True


# 수동 트리거(테스트용)
def run_data_pipeline_now(job: str = "all"):
    if job in ("all", "economic"):
        _job_economic_update()
    if job in ("all", "signals"):
        _job_technical_signals()
    if job in ("all", "ml"):
        _job_ml_prediction()
    if job in ("all", "sentiment"):
        _job_sentiment_update()


# ============================================================
# Ticker backfill worker (Phase 2)
# ============================================================
_backfill_worker_running = False
_backfill_lock = False


def _run_backfill_worker():
    global _backfill_lock
    if _backfill_lock:
        return
    _backfill_lock = True
    try:
        from app.services.backfill_service import process_pending_backfill_jobs
        result = process_pending_backfill_jobs(max_jobs_per_run=3)
        if result.get("processed", 0) > 0:
            logger.info(f"[backfill] {result.get('message')}")
    except Exception as e:
        logger.exception(f"backfill worker 오류: {e}")
        try:
            notify_error("backfill_worker", str(e))
        except Exception:
            pass
    finally:
        _backfill_lock = False


def start_backfill_worker_scheduler():
    global _backfill_worker_running
    if _backfill_worker_running:
        return False
    # 기존 job 제거
    for job in [j for j in schedule.jobs if j.job_func.__name__ == '_run_backfill_worker']:
        schedule.cancel_job(job)
    interval = 2
    try:
        interval = int(float(_read_cfg("ticker_backfill_interval_min", "2")))
        interval = max(1, min(interval, 30))
    except Exception:
        pass
    schedule.every(interval).minutes.do(_run_backfill_worker)
    _backfill_worker_running = True
    logger.info(f"백필 워커 시작됨 ({interval}분 주기)")
    return True


def stop_backfill_worker_scheduler():
    global _backfill_worker_running
    if not _backfill_worker_running:
        return False
    for job in [j for j in schedule.jobs if j.job_func.__name__ == '_run_backfill_worker']:
        schedule.cancel_job(job)
    _backfill_worker_running = False
    logger.info("백필 워커 중지됨")
    return True


# ============================================================
# 인트라데이 스케줄러 (Phase 3)
# ============================================================
_intraday_running = False
_intraday_lock = False


def _run_intraday_cycle():
    """인트라데이 평가 1회 실행 (지정 주기마다)."""
    global _intraday_lock
    if _intraday_lock:
        logger.debug("[intraday] 이미 실행 중 — 스킵")
        return
    _intraday_lock = True
    try:
        from app.services.intraday_service import run_intraday_cycle
        run_intraday_cycle()
    except Exception as e:
        logger.exception(f"intraday cycle 오류: {e}")
        try:
            notify_error("intraday_cycle", str(e))
        except Exception:
            pass
    finally:
        _intraday_lock = False


def start_intraday_scheduler():
    global _intraday_running
    if _intraday_running:
        return False
    # 기존 job 제거
    for job in [j for j in schedule.jobs if j.job_func.__name__ == '_run_intraday_cycle']:
        schedule.cancel_job(job)
    interval = 5
    try:
        interval = int(float(_read_cfg("intraday_interval_minutes", "5")))
        interval = max(1, min(interval, 30))
    except Exception:
        pass
    schedule.every(interval).minutes.do(_run_intraday_cycle)
    _intraday_running = True
    logger.info(f"인트라데이 스케줄러 시작됨 ({interval}분 주기, 게이트는 intraday_enabled config로 제어)")
    return True


def stop_intraday_scheduler():
    global _intraday_running
    if not _intraday_running:
        return False
    for job in [j for j in schedule.jobs if j.job_func.__name__ == '_run_intraday_cycle']:
        schedule.cancel_job(job)
    _intraday_running = False
    logger.info("인트라데이 스케줄러 중지됨")
    return True


def get_intraday_status() -> dict:
    return {
        "scheduler_running": _intraday_running,
        "lock_held": _intraday_lock,
        "exit_scheduler_running": _intraday_exit_running,
    }


# ============================================================
# 인트라데이 청산 스케줄러 (1분 주기, intraday_enabled 게이트는 service에서)
# ============================================================
_intraday_exit_running = False
_intraday_exit_lock = False


def _run_intraday_exit():
    global _intraday_exit_lock
    if _intraday_exit_lock:
        logger.debug("[intraday_exit] 이미 실행 중 — 스킵")
        return
    _intraday_exit_lock = True
    try:
        from app.services.intraday_service import run_intraday_exit_cycle
        run_intraday_exit_cycle()
    except Exception as e:
        logger.exception(f"intraday_exit cycle 오류: {e}")
        try:
            notify_error("intraday_exit_cycle", str(e))
        except Exception:
            pass
    finally:
        _intraday_exit_lock = False


def start_intraday_exit_scheduler():
    global _intraday_exit_running
    if _intraday_exit_running:
        return False
    for job in [j for j in schedule.jobs if j.job_func.__name__ == '_run_intraday_exit']:
        schedule.cancel_job(job)
    schedule.every(1).minutes.do(_run_intraday_exit)
    _intraday_exit_running = True
    logger.info("인트라데이 청산 스케줄러 시작됨 (1분 주기)")
    return True


def stop_intraday_exit_scheduler():
    global _intraday_exit_running
    if not _intraday_exit_running:
        return False
    for job in [j for j in schedule.jobs if j.job_func.__name__ == '_run_intraday_exit']:
        schedule.cancel_job(job)
    _intraday_exit_running = False
    logger.info("인트라데이 청산 스케줄러 중지됨")
    return True
