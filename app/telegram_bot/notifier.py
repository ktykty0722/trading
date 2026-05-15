"""
Telegram 알림 발송 유틸리티.
scheduler.py 등 시스템 내부에서 호출하여 알림을 보냅니다.
"""
import logging
import asyncio
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


async def _send(text: str, parse_mode: str = "HTML") -> bool:
    """실제 메시지 발송 (내부용)"""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.debug("Telegram 설정 없음 - 알림 스킵")
        return False
    try:
        from telegram import Bot
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=parse_mode,
        )
        return True
    except Exception as e:
        logger.error(f"Telegram 알림 발송 실패: {e}")
        return False


def notify(text: str, parse_mode: str = "HTML") -> bool:
    """동기 컨텍스트에서 호출 가능한 알림 발송 래퍼"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 이미 이벤트 루프 안 (async 컨텍스트) → task로 예약
            asyncio.ensure_future(_send(text, parse_mode))
            return True
        else:
            return loop.run_until_complete(_send(text, parse_mode))
    except RuntimeError:
        # 이벤트 루프 없음 → 새 루프 생성
        return asyncio.run(_send(text, parse_mode))


async def notify_async(text: str, parse_mode: str = "HTML") -> bool:
    """비동기 컨텍스트에서 직접 호출하는 알림 발송"""
    return await _send(text, parse_mode)


# ============================================================
# 알림 템플릿
# ============================================================

def notify_buy_order(ticker: str, name: str, price: float, quantity: int,
                     exchange: str, composite_score: float,
                     take_profit: Optional[float] = None, stop_loss: Optional[float] = None):
    tp_str = f"  익절가: <b>${take_profit:.2f}</b>\n" if take_profit else ""
    sl_str = f"  손절가: <b>${stop_loss:.2f}</b>\n" if stop_loss else ""
    text = (
        f"🟢 <b>매수 주문 접수</b>\n\n"
        f"  종목: <b>{name} ({ticker})</b>\n"
        f"  거래소: {exchange}\n"
        f"  가격: <b>${price:.2f}</b>\n"
        f"  수량: <b>{quantity}주</b>\n"
        f"  투자금: <b>${price * quantity:,.2f}</b>\n"
        f"{tp_str}{sl_str}"
        f"  종합점수: {composite_score:.4f}"
    )
    notify(text)


def notify_sell_order(ticker: str, name: str, buy_price: float, sell_price: float,
                      quantity: int, reason: str,
                      profit_loss: Optional[float] = None, profit_loss_pct: Optional[float] = None):
    if profit_loss_pct is not None:
        emoji = "🔴" if profit_loss_pct < 0 else "🟢"
        pnl_str = f"  손익: <b>{'+' if profit_loss_pct >= 0 else ''}{profit_loss_pct:.2f}%</b> (${profit_loss:+,.2f})\n"
    else:
        emoji = "🔴"
        pnl_str = ""
    text = (
        f"{emoji} <b>매도 주문 접수</b>\n\n"
        f"  종목: <b>{name} ({ticker})</b>\n"
        f"  매수가: ${buy_price:.2f} → 매도가: <b>${sell_price:.2f}</b>\n"
        f"  수량: {quantity}주\n"
        f"{pnl_str}"
        f"  사유: {reason}"
    )
    notify(text)


def notify_vix_alert(vix_value: float, threshold: float):
    text = (
        f"⚠️ <b>VIX 급등 경고</b>\n\n"
        f"  현재 VIX: <b>{vix_value:.2f}</b>\n"
        f"  임계값: {threshold}\n\n"
        f"  매수가 전면 중단됩니다."
    )
    notify(text)


def notify_error(context: str, error: str):
    text = (
        f"🔴 <b>시스템 오류</b>\n\n"
        f"  위치: {context}\n"
        f"  오류: <code>{error[:300]}</code>"
    )
    notify(text)


def notify_pipeline_failure(job_name: str, error: str):
    """데이터 파이프라인 job 실패 알림"""
    text = (
        f"🔴 <b>데이터 파이프라인 실패</b>\n\n"
        f"  Job: <code>{job_name}</code>\n"
        f"  오류: <code>{error[:300]}</code>"
    )
    notify(text)


def notify_pipeline_success(job_name: str, summary: str):
    """데이터 파이프라인 job 성공 알림 (간략)"""
    text = (
        f"✅ <b>파이프라인 완료</b>\n"
        f"  Job: <code>{job_name}</code>\n"
        f"  결과: {summary[:300]}"
    )
    notify(text)


def notify_backfill_queued(ticker: str, exchange: str):
    """/add 직후 백필 예약 안내"""
    text = (
        f"✅ <b>{ticker} ({exchange}) 추가 완료</b>\n"
        f"백필 작업을 예약했습니다.\n\n"
        f"진행:\n"
        f"- 가격 데이터: pending\n"
        f"- 기술지표: pending\n"
        f"- 감성분석: pending\n"
        f"- ML 예측: 다음 예측 스케줄에 포함"
    )
    notify(text)


def notify_backfill_done(ticker: str, results: dict):
    """백필 완료 결과 알림"""
    prices_n = results.get("prices", 0)
    signals_ok = results.get("signals", False)
    sentiment = results.get("sentiment") or {}
    sent_msg = sentiment.get("message", "")
    sent_score = sentiment.get("sentiment_score")
    art_count = sentiment.get("article_count")

    if sent_score is not None and art_count:
        sentiment_line = f"기사 {art_count}건, score {float(sent_score):.2f}"
    elif art_count == 0:
        sentiment_line = "관련 기사 없음"
    else:
        sentiment_line = sent_msg or "n/a"

    ml_line = "즉시 학습 실행" if results.get("ml_after_add") else "다음 ML job에서 생성 예정"

    text = (
        f"✅ <b>{ticker} 백필 완료</b>\n"
        f"- 가격 데이터: {prices_n}건\n"
        f"- 기술지표: {'생성 완료' if signals_ok else '실패/부족'}\n"
        f"- 감성분석: {sentiment_line}\n"
        f"- ML 예측: {ml_line}"
    )
    notify(text)


def notify_backfill_failed(ticker: str, step: str, reason: str):
    """백필 실패 알림"""
    text = (
        f"❌ <b>{ticker} 백필 실패</b>\n"
        f"단계: <code>{step}</code>\n"
        f"이유: <code>{reason[:300]}</code>"
    )
    notify(text)


def notify_intraday_exit(ticker: str, name: str, buy_price: float, sell_price: float,
                         quantity: int, exit_strategy: str, reason: str,
                         profit_loss: Optional[float] = None,
                         profit_loss_pct: Optional[float] = None):
    """인트라데이 청산 주문 접수 알림"""
    if profit_loss_pct is not None:
        emoji = "🟢" if profit_loss_pct >= 0 else "🔴"
        pnl_line = f"  손익: <b>{profit_loss_pct:+.2f}%</b>"
        if profit_loss is not None:
            pnl_line += f" (${profit_loss:+,.2f})"
        pnl_line += "\n"
    else:
        emoji = "🔴"
        pnl_line = ""
    text = (
        f"⚡️📤 <b>인트라데이 청산</b> {emoji}\n\n"
        f"  종목: <b>{name} ({ticker})</b>\n"
        f"  매수가: ${buy_price:.2f} → 매도가: <b>${sell_price:.2f}</b>\n"
        f"  수량: {quantity}주\n"
        f"{pnl_line}"
        f"  전략: <code>{exit_strategy}</code>\n"
        f"  사유: {reason[:200]}"
    )
    notify(text)


def notify_intraday_order(ticker: str, name: str, price: float, quantity: int,
                          exchange: str, score: float, reason: str):
    """인트라데이 매수 주문 접수 알림"""
    text = (
        f"⚡️ <b>인트라데이 매수 접수</b>\n\n"
        f"  종목: <b>{name} ({ticker})</b>\n"
        f"  거래소: {exchange}\n"
        f"  가격: <b>${price:.2f}</b> × {quantity}주\n"
        f"  점수: {score:.2f}\n"
        f"  사유: {reason[:200]}"
    )
    notify(text)


def notify_daily_report(date: str, total_holdings: int, daily_pnl: Optional[float],
                        total_pnl: Optional[float]):
    daily_str = f"{'+' if daily_pnl >= 0 else ''}{daily_pnl:.2f}%" if daily_pnl is not None else "N/A"
    total_str = f"${total_pnl:+,.2f}" if total_pnl is not None else "N/A"
    text = (
        f"📊 <b>일일 리포트</b> ({date})\n\n"
        f"  보유 종목: {total_holdings}개\n"
        f"  당일 손익: <b>{daily_str}</b>\n"
        f"  누적 손익: <b>{total_str}</b>"
    )
    notify(text)
