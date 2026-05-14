"""
Telegram Bot - 명령어 핸들러 및 봇 실행

실행 방식: FastAPI lifespan에서 별도 스레드로 run_polling() 실행
"""
import asyncio
import logging
import threading
from datetime import datetime

import pytz
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

from app.core.config import settings
from app.db.supabase import supabase

logger = logging.getLogger(__name__)

_bot_thread: threading.Thread | None = None
_bot_app: Application | None = None


# ============================================================
# 접근 제한 (등록된 CHAT_ID만 허용)
# ============================================================
def _authorized(update: Update) -> bool:
    return str(update.effective_chat.id) == str(settings.TELEGRAM_CHAT_ID)


async def _deny(update: Update):
    await update.message.reply_text("⛔ 권한이 없습니다.")


# ============================================================
# /start, /help
# ============================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    text = (
        "📈 <b>주식 자동매매 봇</b>\n\n"
        "<b>📊 조회</b>\n"
        "/status — 스케줄러 상태\n"
        "/portfolio — 보유 종목\n"
        "/today — 오늘 매수 후보\n"
        "/history — 최근 거래 내역\n"
        "/config — 시스템 설정\n\n"
        "<b>⚙️ 제어</b>\n"
        "/start_buy — 매수 스케줄러 시작\n"
        "/stop_buy — 매수 스케줄러 중지\n"
        "/start_sell — 매도 스케줄러 시작\n"
        "/stop_sell — 매도 스케줄러 중지\n"
        "/buy_now — 즉시 매수 실행\n"
        "/sell_now — 즉시 매도 실행\n\n"
        "<b>📋 종목 관리</b>\n"
        "/tickers — 활성 종목 목록\n"
        "/add TICKER — 종목 추가\n"
        "/remove TICKER — 종목 비활성화"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ============================================================
# /status
# ============================================================
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)

    from app.utils.scheduler import get_scheduler_status
    status = get_scheduler_status()

    buy_icon  = "🟢" if status["buy_running"]  else "🔴"
    sell_icon = "🟢" if status["sell_running"] else "🔴"

    # 오늘 거래 요약
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        trades = supabase.table("trade_records").select("status, ticker, profit_loss_pct").gte(
            "created_at", today
        ).execute().data or []
    except Exception:
        trades = []

    bought = [t for t in trades if t["status"] in ("holding", "buy_ordered")]
    sold   = [t for t in trades if t["status"] == "sold"]
    avg_pnl = (
        sum(t["profit_loss_pct"] for t in sold if t["profit_loss_pct"]) / len(sold)
        if sold else None
    )

    pnl_str = f"{avg_pnl:+.2f}%" if avg_pnl is not None else "N/A"
    now_kst = datetime.now(pytz.timezone("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")

    text = (
        f"📊 <b>시스템 상태</b> ({now_kst})\n\n"
        f"  매수 스케줄러: {buy_icon}\n"
        f"  매도 스케줄러: {sell_icon}\n\n"
        f"  오늘 매수: {len(bought)}건\n"
        f"  오늘 매도: {len(sold)}건\n"
        f"  오늘 평균 손익: {pnl_str}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ============================================================
# /portfolio
# ============================================================
async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)

    try:
        holdings = supabase.table("trade_records").select(
            "ticker, stock_name, buy_price, holding_quantity, take_profit_price, stop_loss_price"
        ).in_("status", ["holding", "buy_ordered"]).execute().data or []
    except Exception as e:
        return await update.message.reply_text(f"❌ DB 조회 오류: {e}")

    if not holdings:
        return await update.message.reply_text("📭 보유 종목이 없습니다.")

    lines = ["📦 <b>보유 종목</b>\n"]
    for h in holdings:
        tp = f"  익절: ${h['take_profit_price']:.2f}" if h.get("take_profit_price") else ""
        sl = f" | 손절: ${h['stop_loss_price']:.2f}" if h.get("stop_loss_price") else ""
        lines.append(
            f"<b>{h['stock_name']} ({h['ticker']})</b>\n"
            f"  매수가: ${h['buy_price']:.2f} × {h['holding_quantity']}주\n"
            f"{tp}{sl}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ============================================================
# /today
# ============================================================
async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)

    today = datetime.now().strftime("%Y-%m-%d")
    try:
        rows = supabase.table("llm_decision_logs").select(
            "ticker, stock_name, decision, reason, rise_probability, composite_score"
        ).eq("decision_date", today).order("composite_score", desc=True).execute().data or []
    except Exception as e:
        return await update.message.reply_text(f"❌ DB 조회 오류: {e}")

    if not rows:
        return await update.message.reply_text(f"📭 {today} 매수 판단 기록이 없습니다.")

    lines = [f"📋 <b>오늘의 LLM 판단</b> ({today})\n"]
    for r in rows:
        icon = "✅" if r["decision"] == "BUY" else "⏸"
        prob = r.get("rise_probability") or 0
        score = r.get("composite_score") or 0
        lines.append(
            f"{icon} <b>{r['stock_name']} ({r['ticker']})</b>\n"
            f"  결정: {r['decision']} | 상승확률: {prob:.1f}% | 점수: {score:.4f}\n"
            f"  이유: {r.get('reason', '')[:80]}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ============================================================
# /history
# ============================================================
async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)

    try:
        rows = supabase.table("trade_records").select(
            "ticker, stock_name, buy_price, sell_price, quantity, status, profit_loss_pct, sell_reason, created_at"
        ).in_("status", ["sold", "buy_failed"]).order("created_at", desc=True).limit(10).execute().data or []
    except Exception as e:
        return await update.message.reply_text(f"❌ DB 조회 오류: {e}")

    if not rows:
        return await update.message.reply_text("📭 거래 내역이 없습니다.")

    lines = ["📜 <b>최근 거래 내역</b>\n"]
    for r in rows:
        if r["status"] == "sold" and r.get("profit_loss_pct") is not None:
            pct = r["profit_loss_pct"]
            icon = "🟢" if pct >= 0 else "🔴"
            pnl  = f"{pct:+.2f}%"
        else:
            icon = "❌"
            pnl  = "미체결"
        date = r["created_at"][:10] if r.get("created_at") else ""
        sell_price = r.get("sell_price")
        sell_price_str = f"${float(sell_price):.2f}" if sell_price is not None else "N/A"
        lines.append(
            f"{icon} <b>{r['stock_name']} ({r['ticker']})</b> {date}\n"
            f"  매수: ${r['buy_price']:.2f} → 매도: {sell_price_str} | 손익: {pnl}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ============================================================
# /config
# ============================================================
async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)

    try:
        rows = supabase.table("system_config").select("key, value, description").order("key").execute().data or []
    except Exception as e:
        return await update.message.reply_text(f"❌ DB 조회 오류: {e}")

    lines = ["⚙️ <b>시스템 설정 (system_config)</b>\n"]
    for r in rows:
        lines.append(f"  <code>{r['key']}</code> = <b>{r['value']}</b>\n  {r.get('description', '')}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ============================================================
# /start_buy, /stop_buy, /start_sell, /stop_sell
# ============================================================
async def cmd_start_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    from app.utils.scheduler import start_scheduler
    ok = start_scheduler()
    await update.message.reply_text("✅ 매수 스케줄러 시작됨" if ok else "⚠️ 이미 실행 중입니다.")


async def cmd_stop_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    from app.utils.scheduler import stop_scheduler
    ok = stop_scheduler()
    await update.message.reply_text("✅ 매수 스케줄러 중지됨" if ok else "⚠️ 이미 중지되어 있습니다.")


async def cmd_start_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    from app.utils.scheduler import start_sell_scheduler
    ok = start_sell_scheduler()
    await update.message.reply_text("✅ 매도 스케줄러 시작됨" if ok else "⚠️ 이미 실행 중입니다.")


async def cmd_stop_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    from app.utils.scheduler import stop_sell_scheduler
    ok = stop_sell_scheduler()
    await update.message.reply_text("✅ 매도 스케줄러 중지됨" if ok else "⚠️ 이미 중지되어 있습니다.")


async def cmd_buy_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    await update.message.reply_text("⏳ 즉시 매수 실행 중...")
    from app.utils.scheduler import run_auto_buy_now
    run_auto_buy_now()
    await update.message.reply_text("✅ 즉시 매수 실행 완료")


async def cmd_sell_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    await update.message.reply_text("⏳ 즉시 매도 실행 중...")
    from app.utils.scheduler import run_auto_sell_now
    run_auto_sell_now()
    await update.message.reply_text("✅ 즉시 매도 실행 완료")


# ============================================================
# /tickers, /add, /remove
# ============================================================
async def cmd_tickers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)

    try:
        rows = supabase.table("stock_universe").select(
            "ticker, name_ko, sector, is_etf"
        ).eq("is_active", True).order("ticker").execute().data or []
    except Exception as e:
        return await update.message.reply_text(f"❌ DB 조회 오류: {e}")

    stocks = [r for r in rows if not r.get("is_etf")]
    etfs   = [r for r in rows if r.get("is_etf")]

    lines = [f"📋 <b>활성 종목 ({len(stocks)}개)</b>\n"]
    for r in stocks:
        lines.append(f"  <b>{r['ticker']}</b> {r['name_ko']} — {r.get('sector', '')}")
    if etfs:
        lines.append(f"\n📊 <b>ETF ({len(etfs)}개)</b>")
        for r in etfs:
            lines.append(f"  <b>{r['ticker']}</b> {r['name_ko']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)

    args = ctx.args
    if not args:
        return await update.message.reply_text("사용법: /add TICKER [거래소]\n예) /add NVDA NASD")

    ticker   = args[0].upper()
    exchange = args[1].upper() if len(args) > 1 else "NASD"

    try:
        existing = supabase.table("stock_universe").select("ticker, is_active").eq("ticker", ticker).execute().data
        if existing:
            if existing[0]["is_active"]:
                await update.message.reply_text(f"⚠️ {ticker} 은 이미 활성 종목입니다. 백필 재예약을 진행합니다.")
            else:
                supabase.table("stock_universe").update({"is_active": True}).eq("ticker", ticker).execute()
                await update.message.reply_text(f"✅ {ticker} 재활성화 — 백필을 예약합니다.")
        else:
            supabase.table("stock_universe").insert({
                "ticker": ticker,
                "name_ko": ticker,
                "exchange": exchange,
                "is_active": True,
                "is_etf": False,
            }).execute()

        # 백필 job 예약
        from app.services.backfill_service import enqueue_backfill_job
        result = enqueue_backfill_job(ticker, exchange)
        if result.get("success"):
            await update.message.reply_text(
                f"✅ <b>{ticker} ({exchange}) 추가 완료</b>\n"
                f"백필 작업을 예약했습니다.\n\n"
                f"진행:\n"
                f"- 가격 데이터: pending\n"
                f"- 기술지표: pending\n"
                f"- 감성분석: pending\n"
                f"- ML 예측: 다음 예측 스케줄에 포함\n\n"
                f"<i>이름은 Supabase에서 직접 수정하세요.</i>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(f"⚠️ 종목은 추가됐지만 백필 예약 실패: {result.get('message')}")
    except Exception as e:
        await update.message.reply_text(f"❌ 추가 실패: {e}")


# ============================================================
# /intraday_status, /start_intraday, /stop_intraday, /intraday_today
# ============================================================
async def cmd_intraday_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    try:
        from app.utils.scheduler import get_intraday_status
        sched = get_intraday_status()
        cfg = {r["key"]: r["value"] for r in (supabase.table("system_config").select("key, value").like("key", "intraday_%").execute().data or [])}
        enabled = cfg.get("intraday_enabled", "false").lower() in {"1", "true", "yes", "on"}
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            sig_rows = supabase.table("intraday_signals").select("ticker").gte("timestamp", today).execute().data or []
        except Exception:
            sig_rows = []
        try:
            orders = supabase.table("trade_records").select("id").eq("strategy", "intraday").gte("buy_date", today).execute().data or []
        except Exception:
            orders = []

        text = (
            f"⚡️ <b>인트라데이 엔진</b>\n\n"
            f"  엔진: {'🟢 ON' if enabled else '🔴 OFF'}\n"
            f"  스케줄러: {'🟢' if sched['scheduler_running'] else '🔴'}\n"
            f"  평가 주기: {cfg.get('intraday_interval_minutes', '5')}분\n"
            f"  ET 윈도우: {cfg.get('intraday_start_et', '09:45')} ~ {cfg.get('intraday_end_et', '15:30')}\n"
            f"  최소 점수: {cfg.get('intraday_min_score', '0.7')}\n"
            f"  오늘 후보 평가: {len(sig_rows)}\n"
            f"  오늘 주문: {len(orders)}\n"
            f"  LLM 검토: {cfg.get('intraday_use_llm_review', 'false')}\n"
        )
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ 조회 실패: {e}")


async def cmd_start_intraday(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    try:
        supabase.table("system_config").upsert({
            "key": "intraday_enabled",
            "value": "true",
            "description": "인트라데이 매수 엔진 활성화 여부",
        }, on_conflict="key").execute()
        await update.message.reply_text("✅ 인트라데이 엔진 ON (intraday_enabled=true)")
    except Exception as e:
        await update.message.reply_text(f"❌ 설정 실패: {e}")


async def cmd_stop_intraday(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    try:
        supabase.table("system_config").upsert({
            "key": "intraday_enabled",
            "value": "false",
            "description": "인트라데이 매수 엔진 활성화 여부",
        }, on_conflict="key").execute()
        await update.message.reply_text("⏸ 인트라데이 엔진 OFF (intraday_enabled=false)")
    except Exception as e:
        await update.message.reply_text(f"❌ 설정 실패: {e}")


async def cmd_intraday_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        rows = (
            supabase.table("intraday_signals")
            .select("ticker, timestamp, price, signal_score, reason")
            .gte("timestamp", today)
            .order("signal_score", desc=True)
            .limit(20)
            .execute()
            .data
            or []
        )
        if not rows:
            return await update.message.reply_text(f"📭 {today} 인트라데이 평가 없음")
        lines = [f"⚡️ <b>오늘 인트라데이 평가</b> ({today})\n"]
        for r in rows:
            score = float(r.get("signal_score") or 0)
            ts = (r.get("timestamp") or "")[11:16]
            lines.append(
                f"  <b>{r['ticker']}</b> [{ts}] ${float(r.get('price') or 0):.2f} "
                f"score={score:.2f}\n  {('이유: ' + (r.get('reason') or ''))[:120]}\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ 조회 실패: {e}")


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)

    args = ctx.args
    if not args:
        return await update.message.reply_text("사용법: /remove TICKER\n예) /remove INTC")

    ticker = args[0].upper()
    try:
        supabase.table("stock_universe").update({"is_active": False}).eq("ticker", ticker).execute()
        await update.message.reply_text(f"✅ {ticker} 비활성화 완료 (데이터는 유지)")
    except Exception as e:
        await update.message.reply_text(f"❌ 비활성화 실패: {e}")


# ============================================================
# 봇 빌드 & 실행
# ============================================================
def _build_app() -> Application:
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_start))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("portfolio",  cmd_portfolio))
    app.add_handler(CommandHandler("today",      cmd_today))
    app.add_handler(CommandHandler("history",    cmd_history))
    app.add_handler(CommandHandler("config",     cmd_config))
    app.add_handler(CommandHandler("start_buy",  cmd_start_buy))
    app.add_handler(CommandHandler("stop_buy",   cmd_stop_buy))
    app.add_handler(CommandHandler("start_sell", cmd_start_sell))
    app.add_handler(CommandHandler("stop_sell",  cmd_stop_sell))
    app.add_handler(CommandHandler("buy_now",    cmd_buy_now))
    app.add_handler(CommandHandler("sell_now",   cmd_sell_now))
    app.add_handler(CommandHandler("tickers",    cmd_tickers))
    app.add_handler(CommandHandler("add",        cmd_add))
    app.add_handler(CommandHandler("remove",     cmd_remove))
    app.add_handler(CommandHandler("intraday_status", cmd_intraday_status))
    app.add_handler(CommandHandler("start_intraday", cmd_start_intraday))
    app.add_handler(CommandHandler("stop_intraday",  cmd_stop_intraday))
    app.add_handler(CommandHandler("intraday_today", cmd_intraday_today))

    return app


def _run_bot_loop(app: Application):
    """별도 스레드에서 봇 이벤트 루프 실행"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_set_commands(app))
        app.run_polling(stop_signals=None)
    finally:
        loop.close()


async def _set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("status",     "스케줄러 상태"),
        BotCommand("portfolio",  "보유 종목"),
        BotCommand("today",      "오늘 매수 판단"),
        BotCommand("history",    "최근 거래 내역"),
        BotCommand("config",     "시스템 설정"),
        BotCommand("start_buy",  "매수 시작"),
        BotCommand("stop_buy",   "매수 중지"),
        BotCommand("start_sell", "매도 시작"),
        BotCommand("stop_sell",  "매도 중지"),
        BotCommand("buy_now",    "즉시 매수"),
        BotCommand("sell_now",   "즉시 매도"),
        BotCommand("tickers",    "활성 종목 목록"),
        BotCommand("add",        "종목 추가 + 백필 예약"),
        BotCommand("remove",     "종목 비활성화"),
        BotCommand("intraday_status", "인트라데이 엔진 상태"),
        BotCommand("start_intraday", "인트라데이 엔진 ON"),
        BotCommand("stop_intraday",  "인트라데이 엔진 OFF"),
        BotCommand("intraday_today", "오늘 인트라데이 평가"),
    ])


def start_bot():
    """FastAPI lifespan에서 호출 — 봇을 백그라운드 스레드로 시작"""
    global _bot_thread, _bot_app

    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN 미설정 — Telegram Bot 비활성화")
        return

    _bot_app = _build_app()
    _bot_thread = threading.Thread(target=_run_bot_loop, args=(_bot_app,), daemon=True)
    _bot_thread.start()
    logger.info("Telegram Bot 시작됨")


def stop_bot():
    """FastAPI lifespan 종료 시 호출"""
    global _bot_app
    if _bot_app:
        try:
            asyncio.run(_bot_app.updater.stop())
        except Exception:
            pass
        logger.info("Telegram Bot 종료됨")
