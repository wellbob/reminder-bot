#!/usr/bin/env python3
"""
Telegram Reminder Bot
Стек: python-telegram-bot + APScheduler + dateparser (fallback) + SQLite
"""

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import dateparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ── Настройки ────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TIMEZONE   = "Europe/Moscow"
TZ         = ZoneInfo(TIMEZONE)
DB_PATH    = "/data/reminders.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=TIMEZONE)

DATEPARSER_SETTINGS = {
    "PREFER_DATES_FROM": "future",
    "TIMEZONE": TIMEZONE,
    "RETURN_AS_TIMEZONE_AWARE": True,
    "DATE_ORDER": "DMY",
}

# Состояния диалога
WAITING_TIME = 1

# ── База данных ──────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            fire_at TEXT    NOT NULL,
            text    TEXT    NOT NULL,
            sent    INTEGER DEFAULT 0
        )
    """)
    con.commit()
    con.close()


def save_reminder(chat_id: int, fire_at: datetime, text: str) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "INSERT INTO reminders (chat_id, fire_at, text) VALUES (?, ?, ?)",
        (chat_id, fire_at.isoformat(), text),
    )
    rid = cur.lastrowid
    con.commit()
    con.close()
    return rid


def mark_sent(reminder_id: int):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE reminders SET sent=1 WHERE id=?", (reminder_id,))
    con.commit()
    con.close()


def get_pending(chat_id: int):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, fire_at, text FROM reminders "
        "WHERE chat_id=? AND sent=0 ORDER BY fire_at",
        (chat_id,),
    ).fetchall()
    con.close()
    return rows


def delete_reminder(reminder_id: int, chat_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "DELETE FROM reminders WHERE id=? AND chat_id=? AND sent=0",
        (reminder_id, chat_id),
    )
    deleted = cur.rowcount > 0
    con.commit()
    con.close()
    return deleted


# ── Парсинг текста ───────────────────────────────────────────────────────────
def is_reminder(text: str) -> bool:
    return bool(re.search(r"\bнапомни(те|ть)?(\s+мне)?\b", text, re.IGNORECASE))


def extract_subject(text: str) -> str:
    """Извлекает тему напоминания."""
    m = re.search(
        r"\b(?:про|об|о том,?\s+что)\s+(.+?)(?:\s+через|\s+в\s+\d|\s+завтра|$)",
        text, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    cleaned = re.sub(
        r"(напомни(те|ть)?(\s+мне)?|через\s+[\w\s]+|завтра|послезавтра|"
        r"в\s+\d[\d:]*|несколько\s+раз|и\s+полтора\s+часа?)",
        "", text, flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.")
    return cleaned if cleaned else text


DAYS_RU = {
    "понедельник": 0, "вторник": 1, "среду": 2, "среда": 2,
    "четверг": 3, "пятницу": 4, "пятница": 4,
    "субботу": 5, "суббота": 5, "воскресенье": 6,
}


def _parse_single_offset(expr: str):
    """«2 минуты», «минуту», «час», «полтора часа», «полчаса», «26 дней» → timedelta."""
    expr = expr.strip().lower()

    if re.search(r"полтора\s*час", expr):
        return timedelta(hours=1, minutes=30)
    if re.search(r"пол\s*час", expr):
        return timedelta(minutes=30)

    # с числом + единица
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(недел|день|дн|сутк|час|мин)", expr)
    if m:
        val = float(m.group(1).replace(",", "."))
        unit = m.group(2)
        if unit.startswith("недел"):
            return timedelta(weeks=val)
        if unit in ("день", "дн", "сутк"):
            return timedelta(days=val)
        if unit.startswith("час"):
            return timedelta(hours=val)
        return timedelta(minutes=val)

    # без числа
    if re.search(r"\bчас", expr):      return timedelta(hours=1)
    if re.search(r"\bмин", expr):      return timedelta(minutes=1)
    if re.search(r"\bден|день|дн", expr): return timedelta(days=1)

    return None


def _parse_time_str(s: str):
    """«12:00», «в 9», «9:30» → (hour, minute) или None."""
    s = s.strip().lower()
    s = re.sub(r"^в\s+", "", s)
    m = re.match(r"(\d{1,2})[:\.](\d{2})$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"(\d{1,2})$", s)
    if m:
        return int(m.group(1)), 0
    return None


def parse_times(text: str):
    """
    Возвращает (список datetime, нужно_уточнить_время).
    Если нужно_уточнить_время=True — найдена только дата (дни), время не указано.
    """
    now = datetime.now(tz=TZ)
    times = []
    t = text.lower()

    # 1. «через X и (через) Y» — два напоминания (только часы/минуты)
    m = re.search(
        r"через\s+([\w\s,.]+?(?:час|мин)[а-я]*)"
        r"\s+и\s+"
        r"((?:через\s+)?[\w\s,.]+?(?:час|мин)[а-я]*)",
        t,
    )
    if m:
        for raw in [m.group(1), m.group(2)]:
            raw = re.sub(r"^через\s+", "", raw.strip())
            delta = _parse_single_offset(raw)
            if delta:
                times.append(now + delta)
        if times:
            return times, False

    # 2. «через X» — один offset (минуты/часы/дни/недели)
    m = re.search(r"через\s+(.+?)(?:\s+(?:про|чтобы|что|об|о)\b|$)", t)
    if m:
        raw = m.group(1).strip()
        delta = _parse_single_offset(raw)
        if delta:
            target = now + delta
            # Если дельта >= 1 дня и время не указано — спрашиваем время
            if delta.total_seconds() >= 86400:
                time_m = re.search(r"в\s+(\d{1,2})[:\.](\d{2})", t)
                if time_m:
                    target = target.replace(
                        hour=int(time_m.group(1)), minute=int(time_m.group(2)),
                        second=0, microsecond=0,
                    )
                    return [target], False
                # Нет времени — вернём дату и флаг "спроси время"
                return [target], True
            return [target], False

    # 3. «завтра/послезавтра»
    day_offset = 0
    if re.search(r"послезавтра", t): day_offset = 2
    elif re.search(r"завтра", t):    day_offset = 1

    if day_offset:
        base = now + timedelta(days=day_offset)
        m = re.search(r"в\s+(\d{1,2})[:\.](\d{2})", t)
        if m:
            dt = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return [dt], False
        return [base], True  # время не указано — спросим

    # 4. «в понедельник...»
    for day_name, day_num in DAYS_RU.items():
        if day_name in t:
            days_ahead = (day_num - now.weekday()) % 7 or 7
            base = now + timedelta(days=days_ahead)
            m = re.search(r"в\s+(\d{1,2})[:\.](\d{2})", t)
            if m:
                dt = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
                return [dt], False
            return [base], True

    # 5. «в HH:MM»
    m = re.search(r"\bв\s+(\d{1,2})[:\.](\d{2})", t)
    if m:
        dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return [dt], False

    # 6. Fallback
    dt = dateparser.parse(text, languages=["ru"], settings=DATEPARSER_SETTINGS)
    if dt and dt > now:
        return [dt], False

    return [], False


# ── Отправка напоминания ─────────────────────────────────────────────────────
async def send_reminder(bot, chat_id: int, text: str, reminder_id: int):
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"⏰ *Напоминание:* {text}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error(f"Не удалось отправить напоминание #{reminder_id}: {e}")
    finally:
        mark_sent(reminder_id)


# ── Восстановление задач после перезапуска ───────────────────────────────────
async def reload_pending_jobs(bot):
    now = datetime.now(tz=TZ)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, chat_id, fire_at, text FROM reminders WHERE sent=0"
    ).fetchall()
    con.close()

    overdue = restored = 0
    for rid, chat_id, fire_at, text in rows:
        dt = datetime.fromisoformat(fire_at).astimezone(TZ)
        if dt <= now:
            await send_reminder(bot, chat_id, text, rid)
            overdue += 1
        else:
            scheduler.add_job(
                send_reminder, trigger="date", run_date=dt,
                args=[bot, chat_id, text, rid],
                id=f"reminder_{rid}", replace_existing=True,
            )
            restored += 1

    log.info(f"Восстановлено: {restored} напоминаний, {overdue} просроченных отправлено.")


def _confirm_and_schedule(bot, chat_id, subject, times, ctx):
    confirmations = []
    for dt in times:
        rid = save_reminder(chat_id, dt, subject)
        scheduler.add_job(
            send_reminder, trigger="date", run_date=dt,
            args=[bot, chat_id, subject, rid],
            id=f"reminder_{rid}", replace_existing=True,
        )
        confirmations.append(f"  🕐 {dt.strftime('%d.%m.%Y в %H:%M')}")
    return f"✅ Напомню про *{subject}*:\n" + "\n".join(confirmations)


# ── Хэндлеры ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Пиши мне что и когда, я напомню.\n\n"
        "*Примеры:*\n"
        "• напомни завтра в 9:00 про отчёт\n"
        "• напомни через час проверить аккумулятор\n"
        "• напомни через 26 дней про оплату форнекс\n"
        "• напомни через час и полтора часа про звонок\n"
        "• напомни в пятницу в 18:00 про встречу\n\n"
        "*/list* — активные напоминания\n"
        "*/cancel <id>* — отменить напоминание",
        parse_mode="Markdown",
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_pending(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("📭 Нет активных напоминаний.")
        return
    lines = ["📋 *Активные напоминания:*\n"]
    for rid, fire_at, text in rows:
        dt = datetime.fromisoformat(fire_at).astimezone(TZ)
        lines.append(f"`#{rid}` {dt.strftime('%d.%m в %H:%M')} — {text}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Укажи ID напоминания: /cancel 5")
        return
    try:
        rid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    if delete_reminder(rid, update.effective_chat.id):
        job = scheduler.get_job(f"reminder_{rid}")
        if job:
            job.remove()
        await update.message.reply_text(f"✅ Напоминание #{rid} отменено.")
    else:
        await update.message.reply_text(f"❌ Напоминание #{rid} не найдено.")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if not is_reminder(text):
        await update.message.reply_text(
            "Напиши «напомни мне ...» и я поставлю напоминание 🙂\n"
            "Или /start для примеров."
        )
        return ConversationHandler.END

    times, need_time = parse_times(text)
    subject = extract_subject(text)

    if not times:
        await update.message.reply_text(
            "🤔 Не смог разобрать время. Попробуй чуть точнее, например:\n"
            "«напомни через 26 дней в 12:00 про оплату»"
        )
        return ConversationHandler.END

    if need_time:
        # Сохраняем дату и тему — ждём время от пользователя
        ctx.user_data["pending_date"] = times[0]
        ctx.user_data["pending_subject"] = subject
        date_str = times[0].strftime("%d.%m.%Y")
        await update.message.reply_text(
            f"📅 Дата: *{date_str}*\nВ какое время напомнить? Например: `12:00`",
            parse_mode="Markdown",
        )
        return WAITING_TIME

    reply = _confirm_and_schedule(ctx.bot, chat_id, subject, times, ctx)
    await update.message.reply_text(reply, parse_mode="Markdown")
    return ConversationHandler.END


async def handle_time_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Получаем время от пользователя после вопроса."""
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    parsed = _parse_time_str(text)
    if not parsed:
        await update.message.reply_text(
            "Не понял время. Напиши в формате `12:00` или просто `12`",
            parse_mode="Markdown",
        )
        return WAITING_TIME

    h, mn = parsed
    base: datetime = ctx.user_data.get("pending_date")
    subject: str = ctx.user_data.get("pending_subject", "—")

    if not base:
        await update.message.reply_text("Что-то пошло не так, попробуй заново.")
        return ConversationHandler.END

    dt = base.replace(hour=h, minute=mn, second=0, microsecond=0)
    now = datetime.now(tz=TZ)
    if dt <= now:
        await update.message.reply_text(
            "⚠️ Это время уже прошло. Напиши другое время."
        )
        return WAITING_TIME

    ctx.user_data.clear()
    reply = _confirm_and_schedule(ctx.bot, chat_id, subject, [dt], ctx)
    await update.message.reply_text(reply, parse_mode="Markdown")
    return ConversationHandler.END


async def cancel_dialog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ── Запуск ───────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    scheduler.start()
    await reload_pending_jobs(app.bot)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная BOT_TOKEN не задана!")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)],
        states={
            WAITING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time_reply)],
        },
        fallbacks=[CommandHandler("cancel", cancel_dialog)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(conv)

    log.info("Бот запущен.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
