#!/usr/bin/env python3
"""
Telegram Reminder Bot
Фичи: разовые и повторяющиеся напоминания, снуз-кнопки, голосовые сообщения
Стек: python-telegram-bot + APScheduler + Whisper + SQLite
"""

import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import dateparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ── Настройки ────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TIMEZONE  = "Europe/Moscow"
TZ        = ZoneInfo(TIMEZONE)
DB_PATH   = "/data/reminders.db"

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

# Whisper модель (загружается лениво при первом голосовом)
_whisper_model = None


def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        try:
            import whisper
            log.info("Загружаю Whisper tiny...")
            _whisper_model = whisper.load_model("tiny")
            log.info("Whisper готов.")
        except Exception as e:
            log.warning(f"Whisper недоступен: {e}")
    return _whisper_model


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
    con.execute("""
        CREATE TABLE IF NOT EXISTS recurring (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id  INTEGER NOT NULL,
            schedule TEXT    NOT NULL,
            text     TEXT    NOT NULL,
            active   INTEGER DEFAULT 1
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


def get_recurring(chat_id: int):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, schedule, text FROM recurring WHERE chat_id=? AND active=1",
        (chat_id,),
    ).fetchall()
    con.close()
    return rows


def save_recurring(chat_id: int, schedule: dict, text: str) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "INSERT INTO recurring (chat_id, schedule, text) VALUES (?, ?, ?)",
        (chat_id, json.dumps(schedule, ensure_ascii=False), text),
    )
    rid = cur.lastrowid
    con.commit()
    con.close()
    return rid


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


def delete_recurring(rid: int, chat_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "UPDATE recurring SET active=0 WHERE id=? AND chat_id=?",
        (rid, chat_id),
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
    # Попытка 1: берём всё после «про/об» — самый надёжный способ
    m = re.search(r'\b(?:про|об)\s+(.+)', text, re.IGNORECASE | re.DOTALL)
    if m:
        subject = m.group(1).strip()
        # Убираем хвосты если время/дата попала в конец
        subject = re.sub(
            r'\s+(?:через\s+[\w\s]+|в\s+\d{1,2}:\d{2})\s*$', '',
            subject, flags=re.IGNORECASE
        ).strip()
        return subject

    # Попытка 2: убираем временны́е конструкции и ключевые слова
    cleaned = re.sub(
        r'напомни(те|ть)?(\s+мне)?'
        r'|\bв\s+\d{1,2}[./]\d{1,2}[./]\d{2,4}\s+в\s+\d{1,2}[:\.]?\d*'
        r'|\bв\s+\d{1,2}[./]\d{1,2}[./]\d{2,4}'
        r'|\bв\s+\d{1,2}:\d{2}'
        r'|\bчерез\s+(?:полтора\s+)?(?:\d+\s+)?(?:час|минут|мин|день|дн|недел|секунд)\w*'
        r'|\bзавтра|\bпослезавтра'
        r'|\bнесколько\s+раз'
        r'|\bкаждый\s+\w+',
        '', text, flags=re.IGNORECASE
    )
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip(' ,.')
    return cleaned if cleaned else text


DAYS_RU = {
    "понедельник": 0, "вторник": 1, "среду": 2, "среда": 2,
    "четверг": 3, "пятницу": 4, "пятница": 4,
    "субботу": 5, "суббота": 5, "воскресенье": 6,
}

DAYS_APSched = {
    0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun",
}


def _parse_single_offset(expr: str):
    expr = expr.strip().lower()
    if re.search(r"полтора\s*час", expr): return timedelta(hours=1, minutes=30)
    if re.search(r"пол\s*час", expr):     return timedelta(minutes=30)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(недел|день|дн|сутк|час|мин)", expr)
    if m:
        val = float(m.group(1).replace(",", "."))
        unit = m.group(2)
        if unit.startswith("недел"): return timedelta(weeks=val)
        if unit in ("день", "дн", "сутк"): return timedelta(days=val)
        if unit.startswith("час"): return timedelta(hours=val)
        return timedelta(minutes=val)
    if re.search(r"\bчас", expr): return timedelta(hours=1)
    if re.search(r"\bмин", expr): return timedelta(minutes=1)
    return None


def _parse_time_str(s: str):
    s = re.sub(r"^в\s+", "", s.strip().lower())
    m = re.match(r"(\d{1,2})[:\.](\d{2})$", s)
    if m: return int(m.group(1)), int(m.group(2))
    m = re.match(r"(\d{1,2})$", s)
    if m: return int(m.group(1)), 0
    return None


def try_parse_recurring(text: str) -> dict | None:
    """
    Распознаёт повторяющиеся шаблоны.
    Возвращает dict с ключами APScheduler cron trigger или None.
    """
    t = text.lower()
    if not re.search(r"каждый|каждую|каждое|ежедневно|еженедельно", t):
        return None

    hour, minute = 9, 0
    m = re.search(r"в\s+(\d{1,2})[:\.](\d{2})", t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"в\s+(\d{1,2})\b", t)
        if m:
            hour = int(m.group(1))

    # ежедневно
    if re.search(r"каждый\s+день|ежедневно|каждое\s+утро", t):
        return {"day_of_week": "*", "hour": hour, "minute": minute}

    # будние дни
    if re.search(r"каждый\s+будни|по\s+будням|рабочий\s+день", t):
        return {"day_of_week": "mon-fri", "hour": hour, "minute": minute}

    # конкретный день недели
    for day_name, day_num in DAYS_RU.items():
        if re.search(rf"каждый\s+{day_name}|каждую\s+{day_name}", t):
            return {"day_of_week": DAYS_APSched[day_num], "hour": hour, "minute": minute}

    return None


def parse_times(text: str):
    now = datetime.now(tz=TZ)
    times = []
    t = text.lower()

    # 0. DD.MM.YYYY в HH:MM  или  DD.MM.YY в HH:MM — точная дата со временем
    m = re.search(
        r'(\d{1,2})[./](\d{1,2})[./](\d{2,4})\s+в\s+(\d{1,2})[:\.](\d{2})', t
    )
    if m:
        day, mon, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mn = int(m.group(4)), int(m.group(5))
        if yr < 100: yr += 2000
        try:
            dt = datetime(yr, mon, day, h, mn, 0, tzinfo=TZ)
            if dt > now:
                return [dt], False
        except ValueError:
            pass

    # 0b. DD.MM.YYYY без времени — точная дата, время уточним
    m = re.search(r'(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?!\d)', t)
    if m:
        day, mon, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100: yr += 2000
        try:
            base = now.replace(
                year=yr, month=mon, day=day,
                hour=9, minute=0, second=0, microsecond=0
            )
            # Если в тексте есть время (HH:MM) — используем его
            time_m = re.search(r'\bв\s+(\d{1,2}):(\d{2})', t)
            if time_m:
                base = base.replace(
                    hour=int(time_m.group(1)), minute=int(time_m.group(2))
                )
                if base > now:
                    return [base], False
            elif base > now:
                return [base], True   # спросим время
        except ValueError:
            pass

    # 1. через X и Y
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

    # 2. через X
    m = re.search(r"через\s+(.+?)(?:\s+(?:про|чтобы|что|об|о)\b|$)", t)
    if m:
        delta = _parse_single_offset(m.group(1).strip())
        if delta:
            target = now + delta
            if delta.total_seconds() >= 86400:
                time_m = re.search(r"в\s+(\d{1,2}):(\d{2})", t)
                if time_m:
                    target = target.replace(
                        hour=int(time_m.group(1)), minute=int(time_m.group(2)),
                        second=0, microsecond=0,
                    )
                    return [target], False
                return [target], True
            return [target], False

    # 3. завтра/послезавтра
    day_offset = 0
    if re.search(r"послезавтра", t): day_offset = 2
    elif re.search(r"завтра", t):    day_offset = 1
    if day_offset:
        base = now + timedelta(days=day_offset)
        m = re.search(r"в\s+(\d{1,2}):(\d{2})", t)
        if m:
            dt = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return [dt], False
        return [base], True

    # 4. день недели
    for day_name, day_num in DAYS_RU.items():
        if day_name in t:
            days_ahead = (day_num - now.weekday()) % 7 or 7
            base = now + timedelta(days=days_ahead)
            m = re.search(r"в\s+(\d{1,2}):(\d{2})", t)
            if m:
                dt = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
                return [dt], False
            return [base], True

    # 5. в HH:MM (только через двоеточие, чтобы не путать с датой DD.MM)
    m = re.search(r"\bв\s+(\d{1,2}):(\d{2})", t)
    if m:
        dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return [dt], False

    # 6. fallback
    dt = dateparser.parse(text, languages=["ru"], settings=DATEPARSER_SETTINGS)
    if dt and dt > now:
        return [dt], False

    return [], False


# ── Снуз-кнопки ─────────────────────────────────────────────────────────────
def snooze_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Готово",    callback_data=f"done:{reminder_id}"),
        InlineKeyboardButton("💤 +30 мин",  callback_data=f"snooze:30:{reminder_id}"),
        InlineKeyboardButton("💤 +1 час",   callback_data=f"snooze:60:{reminder_id}"),
        InlineKeyboardButton("💤 +3 часа",  callback_data=f"snooze:180:{reminder_id}"),
    ]])


async def send_reminder(bot, chat_id: int, text: str, reminder_id: int):
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"⏰ *Напоминание:* {text}",
            parse_mode="Markdown",
            reply_markup=snooze_keyboard(reminder_id),
        )
    except Exception as e:
        log.error(f"Не удалось отправить напоминание #{reminder_id}: {e}")
    finally:
        mark_sent(reminder_id)


async def handle_snooze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("done:"):
        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(
            query.message.text + "\n\n✅ _Выполнено_", parse_mode="Markdown"
        )
        return

    if data.startswith("snooze:"):
        _, minutes_str, rid_str = data.split(":")
        minutes = int(minutes_str)
        chat_id = query.message.chat_id
        subject = re.sub(r"^⏰ \*Напоминание:\* ", "", query.message.text)

        fire_at = datetime.now(tz=TZ) + timedelta(minutes=minutes)
        new_rid = save_reminder(chat_id, fire_at, subject)
        scheduler.add_job(
            send_reminder, trigger="date", run_date=fire_at,
            args=[ctx.bot, chat_id, subject, new_rid],
            id=f"reminder_{new_rid}", replace_existing=True,
        )
        label = f"{minutes} мин" if minutes < 60 else f"{minutes // 60} ч"
        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(
            query.message.text + f"\n\n💤 _Отложено на {label}_",
            parse_mode="Markdown",
        )


async def send_recurring(bot, chat_id: int, text: str):
    """Отправка повторяющегося напоминания (без кнопки отмены, с кнопкой снуза)."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"🔁 *Напоминание:* {text}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error(f"Ошибка повторяющегося напоминания: {e}")


# ── Восстановление задач ─────────────────────────────────────────────────────
async def reload_pending_jobs(bot):
    now = datetime.now(tz=TZ)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, chat_id, fire_at, text FROM reminders WHERE sent=0"
    ).fetchall()
    recurring = con.execute(
        "SELECT id, chat_id, schedule, text FROM recurring WHERE active=1"
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

    for rid, chat_id, schedule_json, text in recurring:
        schedule = json.loads(schedule_json)
        scheduler.add_job(
            send_recurring, trigger="cron",
            args=[bot, chat_id, text],
            id=f"recurring_{rid}", replace_existing=True,
            **schedule,
        )

    log.info(
        f"Восстановлено: {restored} разовых, {overdue} просроченных, "
        f"{len(recurring)} повторяющихся."
    )


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


# ── Голосовые сообщения ──────────────────────────────────────────────────────
async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    model = get_whisper()
    if model is None:
        await update.message.reply_text(
            "⚠️ Голосовые сообщения недоступны — Whisper не установлен."
        )
        return

    await update.message.reply_text("🎙 Распознаю голос...")

    voice = update.message.voice
    tg_file = await ctx.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await tg_file.download_to_drive(tmp_path)

    try:
        result = model.transcribe(tmp_path, language="ru")
        recognized = result["text"].strip()
        log.info(f"Whisper распознал: {recognized!r}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка распознавания: {e}")
        return
    finally:
        os.unlink(tmp_path)

    await update.message.reply_text(f"🗣 Распознано: _{recognized}_", parse_mode="Markdown")

    # Обрабатываем как обычное текстовое сообщение
    if not is_reminder(recognized):
        await update.message.reply_text(
            "Не похоже на напоминание. Скажи что-то вроде «напомни через час про звонок»."
        )
        return

    recurring = try_parse_recurring(recognized)
    if recurring:
        subject = extract_subject(recognized)
        rid = save_recurring(update.effective_chat.id, recurring, subject)
        scheduler.add_job(
            send_recurring, trigger="cron",
            args=[ctx.bot, update.effective_chat.id, subject],
            id=f"recurring_{rid}", replace_existing=True,
            **recurring,
        )
        dow = recurring.get("day_of_week", "*")
        h, mn = recurring["hour"], recurring["minute"]
        await update.message.reply_text(
            f"🔁 Повторяющееся напоминание создано!\n"
            f"*{subject}* — {dow} в {h:02d}:{mn:02d}",
            parse_mode="Markdown",
        )
        return

    times, need_time = parse_times(recognized)
    subject = extract_subject(recognized)

    if not times:
        await update.message.reply_text("🤔 Не смог разобрать время. Попробуй ещё раз.")
        return

    if need_time:
        ctx.user_data["pending_date"] = times[0]
        ctx.user_data["pending_subject"] = subject
        await update.message.reply_text(
            f"📅 Дата: *{times[0].strftime('%d.%m.%Y')}*\nВ какое время? Например: `12:00`",
            parse_mode="Markdown",
        )
        return

    reply = _confirm_and_schedule(ctx.bot, update.effective_chat.id, subject, times, ctx)
    await update.message.reply_text(reply, parse_mode="Markdown")


# ── Команды ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Пиши или говори — я напомню.\n\n"
        "*Разовые напоминания:*\n"
        "• напомни завтра в 9:00 про отчёт\n"
        "• напомни через 26 дней про оплату форнекс\n"
        "• напомни через час и полтора часа про звонок\n"
        "• напомни в пятницу в 18:00 про встречу\n\n"
        "*Повторяющиеся:*\n"
        "• напомни каждый день в 9:00 про зарядку\n"
        "• напомни каждый понедельник в 10:00 про отчёт\n\n"
        "🎙 Можно отправить голосовое сообщение!\n\n"
        "*/list* — активные напоминания\n"
        "*/cancel <id>* — отменить\n"
        "*/cancel r<id>* — отменить повторяющееся",
        parse_mode="Markdown",
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    one_time = get_pending(chat_id)
    recur    = get_recurring(chat_id)

    if not one_time and not recur:
        await update.message.reply_text("📭 Нет активных напоминаний.")
        return

    lines = []
    if one_time:
        lines.append("📋 *Разовые:*")
        for rid, fire_at, text in one_time:
            dt = datetime.fromisoformat(fire_at).astimezone(TZ)
            lines.append(f"`#{rid}` {dt.strftime('%d.%m в %H:%M')} — {text}")

    if recur:
        lines.append("\n🔁 *Повторяющиеся:*")
        dow_ru = {"mon":"пн","tue":"вт","wed":"ср","thu":"чт","fri":"пт","sat":"сб","sun":"вс","*":"каждый день","mon-fri":"пн-пт"}
        for rid, schedule_json, text in recur:
            s = json.loads(schedule_json)
            dow = dow_ru.get(s.get("day_of_week","*"), s.get("day_of_week","*"))
            lines.append(f"`#r{rid}` {dow} в {s['hour']:02d}:{s['minute']:02d} — {text}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /cancel 5  или  /cancel r3")
        return

    arg = ctx.args[0]

    # повторяющееся
    if arg.startswith("r"):
        try:
            rid = int(arg[1:])
        except ValueError:
            await update.message.reply_text("Неверный ID.")
            return
        if delete_recurring(rid, update.effective_chat.id):
            job = scheduler.get_job(f"recurring_{rid}")
            if job:
                job.remove()
            await update.message.reply_text(f"✅ Повторяющееся напоминание #r{rid} отменено.")
        else:
            await update.message.reply_text(f"❌ Напоминание #r{rid} не найдено.")
        return

    # разовое
    try:
        rid = int(arg)
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


# ── Основной хэндлер текста ──────────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if not is_reminder(text):
        await update.message.reply_text(
            "Напиши «напомни мне ...» и я поставлю напоминание 🙂\n"
            "Или /start для примеров."
        )
        return ConversationHandler.END

    # Повторяющееся?
    recurring = try_parse_recurring(text)
    if recurring:
        subject = extract_subject(text)
        rid = save_recurring(chat_id, recurring, subject)
        scheduler.add_job(
            send_recurring, trigger="cron",
            args=[ctx.bot, chat_id, subject],
            id=f"recurring_{rid}", replace_existing=True,
            **recurring,
        )
        dow = recurring.get("day_of_week", "*")
        h, mn = recurring["hour"], recurring["minute"]
        dow_label = {"*": "каждый день", "mon-fri": "пн–пт"}.get(dow, f"каждый {dow}")
        await update.message.reply_text(
            f"🔁 Повторяющееся напоминание создано!\n"
            f"*{subject}* — {dow_label} в {h:02d}:{mn:02d}",
            parse_mode="Markdown",
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
        ctx.user_data["pending_date"] = times[0]
        ctx.user_data["pending_subject"] = subject
        await update.message.reply_text(
            f"📅 Дата: *{times[0].strftime('%d.%m.%Y')}*\nВ какое время? Например: `12:00`",
            parse_mode="Markdown",
        )
        return WAITING_TIME

    reply = _confirm_and_schedule(ctx.bot, chat_id, subject, times, ctx)
    await update.message.reply_text(reply, parse_mode="Markdown")
    return ConversationHandler.END


async def handle_time_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    parsed = _parse_time_str(text)
    if not parsed:
        await update.message.reply_text("Напиши в формате `12:00` или просто `12`", parse_mode="Markdown")
        return WAITING_TIME

    h, mn = parsed
    base: datetime = ctx.user_data.get("pending_date")
    subject: str = ctx.user_data.get("pending_subject", "—")

    if not base:
        await update.message.reply_text("Что-то пошло не так, попробуй заново.")
        return ConversationHandler.END

    dt = base.replace(hour=h, minute=mn, second=0, microsecond=0)
    if dt <= datetime.now(tz=TZ):
        await update.message.reply_text("⚠️ Это время уже прошло. Напиши другое время.")
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
    app.add_handler(CallbackQueryHandler(handle_snooze))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(conv)

    log.info("Бот запущен.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
