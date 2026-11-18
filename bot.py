"""
Telegram Status Tracker BOT
============================
Telethon (user account) відстежує статус цілі.
aiogram (bot) надсилає красиві сповіщення власнику.

Python: 3.8+  |  Telethon + aiogram 3.x
"""

import os as _os
_os.environ["TZ"] = "Europe/Kyiv"
try:
    import time as _time
    _time.tzset()
except AttributeError:
    pass

import asyncio
import html
import logging
import random
import sys
from collections import deque
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV_TZ = timezone(timedelta(hours=2))

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.exceptions import TelegramBadRequest
from telethon import TelegramClient
from telethon.errors import FloodWaitError, UserPrivacyRestrictedError
from telethon.tl.types import (
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

# ─────────────────────────────────────────────────────────────────────────────
# КОНФІГУРАЦІЯ — читається зі змінних середовища (або fallback на значення)
# ─────────────────────────────────────────────────────────────────────────────
import os as _os

API_ID: int   = int(_os.environ.get("API_ID", "36703360"))
API_HASH: str = _os.environ.get("API_HASH", "84595afef048f7f717fddc5c2d2fbb99")
BOT_TOKEN: str = _os.environ.get("BOT_TOKEN", "9278687261:AAGxm039dcpa3xL2F9cBs3t9orw_muQzf5s")
TARGET_USER: str | int = _os.environ.get("TARGET_USER", "yana_aaay")
SESSION_NAME: str = "tracker_session"
# StringSession — компактний рядок сесії для хмарного деплою
SESSION_STRING: str = _os.environ.get("SESSION_STRING", "")

CHECK_INTERVAL_BASE: int = 37
MAX_HISTORY: int = 100
LOG_FILE: str | None = None   # На Railway логи йдуть у stdout
# ─────────────────────────────────────────────────────────────────────────────


# ── Логування ─────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    def kyiv_time_converter(sec=None):
        if sec is None:
            return datetime.now(KYIV_TZ).timetuple()
        return datetime.fromtimestamp(sec, tz=timezone.utc).astimezone(KYIV_TZ).timetuple()

    class KyivFormatter(logging.Formatter):
        converter = staticmethod(kyiv_time_converter)

    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(KyivFormatter(fmt=fmt, datefmt=datefmt))
    
    root_log = logging.getLogger()
    root_log.setLevel(logging.INFO)
    root_log.handlers = [handler]
    
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    return logging.getLogger("tracker_bot")

log = setup_logging()


# ── Глобальний стан ───────────────────────────────────────────────────────────
class State:
    owner_chat_id: int = 0           # встановлюється при /start
    history: deque = deque(maxlen=MAX_HISTORY)   # список dict подій
    is_online: bool | None = None    # None = невідомо (перший запуск)
    last_online_at: datetime | None = None       # коли востаннє зайшов онлайн
    last_seen_at: datetime | None = None         # коли виходив
    online_count_today: int = 0
    online_seconds_today: int = 0
    day_reset: datetime = datetime.now(KYIV_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    target_name: str = TARGET_USER if isinstance(TARGET_USER, str) else str(TARGET_USER)

state = State()


# ── Утиліти ───────────────────────────────────────────────────────────────────
def now_local() -> datetime:
    return datetime.now(KYIV_TZ)

def fmt_dt(dt: datetime) -> str:
    if dt is None:
        return "невідомо"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KYIV_TZ).strftime("%d.%m.%Y %H:%M:%S")

def fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} хв {seconds % 60} сек"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h} год {m} хв"

def status_label(status) -> tuple[str, str]:
    """Повертає (emoji, текст)."""
    if isinstance(status, UserStatusOnline):
        return "🟢", "онлайн"
    if isinstance(status, UserStatusOffline):
        was = status.was_online
        if was.tzinfo is None:
            was = was.replace(tzinfo=timezone.utc)
        return "⚫", f"офлайн (остання активність: {fmt_dt(was)})"
    if isinstance(status, UserStatusRecently):
        return "🕐", "нещодавно в мережі"
    if isinstance(status, UserStatusLastWeek):
        return "📅", "був(-ла) протягом тижня"
    if isinstance(status, UserStatusLastMonth):
        return "📆", "був(-ла) протягом місяця"
    if isinstance(status, UserStatusEmpty):
        return "❓", "статус прихований"
    return "❓", f"невідомий статус"

def reset_daily_stats_if_needed():
    """Скидає щоденну статистику о 00:00 за київським часом."""
    today_start = datetime.now(KYIV_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if today_start > state.day_reset:
        state.online_count_today = 0
        state.online_seconds_today = 0
        state.day_reset = today_start


# ── Клавіатури ────────────────────────────────────────────────────────────────
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📍 Статус зараз", callback_data="status"),
            InlineKeyboardButton(text="📋 Історія", callback_data="history"),
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
            InlineKeyboardButton(text="🔄 Оновити", callback_data="status"),
        ],
    ])

def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Головне меню", callback_data="menu")]
    ])


# ── Формування повідомлень (HTML) ─────────────────────────────────────────────
def get_safe_target() -> str:
    target_str = str(state.target_name)
    if not target_str.lstrip("-").isdigit():
        return html.escape(f"@{target_str}")
    return html.escape(f"ID {target_str}")

def build_status_message() -> str:
    target = get_safe_target()
    now = fmt_dt(now_local())

    if state.is_online is None:
        status_line = "❓ Статус ще не отримано..."
    elif state.is_online:
        since = ""
        if state.last_online_at:
            delta = int((datetime.now(timezone.utc) - state.last_online_at).total_seconds())
            since = f"\n⏱ В мережі вже: <b>{fmt_duration(delta)}</b>"
        status_line = f"🟢 <b>ОНЛАЙН</b>{since}"
    else:
        last = f"\n👁 Остання активність: <b>{fmt_dt(state.last_seen_at)}</b>" if state.last_seen_at else ""
        status_line = f"⚫ <b>ОФЛАЙН</b>{last}"

    return (
        f"👤 Ціль: <b>{target}</b>\n"
        f"{'─' * 28}\n"
        f"{status_line}\n\n"
        f"🕐 Оновлено: {now}"
    )

def build_history_message() -> str:
    if not state.history:
        return "📋 Історія порожня — чекаємо перших подій..."
    lines = [f"📋 <b>Останні {len(state.history)} подій:</b>\n{'─' * 28}"]
    for i, event in enumerate(reversed(state.history), 1):
        emoji = "🟢" if event["online"] else "⚫"
        label = html.escape(event['label'])
        lines.append(f"{emoji} {event['time']}  —  {label}")
        if i >= 20:  # показуємо максимум 20 останніх
            lines.append(f"<i>...і ще {len(state.history) - 20} подій</i>")
            break
    return "\n".join(lines)

def build_stats_message() -> str:
    reset_daily_stats_if_needed()
    target = get_safe_target()
    total_events = len(state.history)

    return (
        f"📊 <b>Статистика моніторингу</b>\n{'─' * 28}\n"
        f"👤 Ціль: <b>{target}</b>\n\n"
        f"📅 <b>Сьогодні:</b>\n"
        f"  • Заходів онлайн: <b>{state.online_count_today}</b>\n"
        f"  • Час онлайн: <b>{fmt_duration(state.online_seconds_today)}</b>\n\n"
        f"📦 <b>Всього в пам'яті:</b>\n"
        f"  • Подій записано: <b>{total_events}</b>\n"
        f"  • Трекер активний: <b>{fmt_duration(int((datetime.now(KYIV_TZ) - state.day_reset).total_seconds()))}</b>\n\n"
        f"⚙️ Інтервал перевірки: <b>~{CHECK_INTERVAL_BASE} сек</b>"
    )


# ── Telegram Bot (aiogram) ────────────────────────────────────────────────────
router = Router()

@router.message(Command("start"))
async def cmd_start(msg: Message):
    state.owner_chat_id = msg.chat.id
    target = get_safe_target()
    first_name = html.escape(msg.from_user.first_name if msg.from_user else "користувач")
    await msg.answer(
        f"👋 <b>Привіт, {first_name}!</b>\n\n"
        f"🔍 Трекер статусу Telegram запущено.\n"
        f"👤 Відстежую: <b>{target}</b>\n\n"
        f"Я надсилатиму сповіщення коли ціль:\n"
        f"🟢 заходить онлайн\n"
        f"⚫ виходить офлайн\n\n"
        f"Використовуй кнопки нижче або команди:\n"
        f"/status — поточний статус\n"
        f"/history — історія подій\n"
        f"/stats — статистика",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )
    log.info("Owner registered: chat_id=%d", msg.chat.id)

@router.message(Command("status"))
async def cmd_status(msg: Message):
    await msg.answer(build_status_message(), parse_mode=ParseMode.HTML, reply_markup=main_keyboard())

@router.message(Command("history"))
async def cmd_history(msg: Message):
    await msg.answer(build_history_message(), parse_mode=ParseMode.HTML, reply_markup=back_keyboard())

@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    await msg.answer(build_stats_message(), parse_mode=ParseMode.HTML, reply_markup=back_keyboard())

@router.callback_query(F.data == "status")
async def cb_status(cb: CallbackQuery):
    try:
        await cb.message.edit_text(build_status_message(), parse_mode=ParseMode.HTML, reply_markup=main_keyboard())
    except TelegramBadRequest:
        pass
    await cb.answer()

@router.callback_query(F.data == "history")
async def cb_history(cb: CallbackQuery):
    try:
        await cb.message.edit_text(build_history_message(), parse_mode=ParseMode.HTML, reply_markup=back_keyboard())
    except TelegramBadRequest:
        pass
    await cb.answer()

@router.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery):
    try:
        await cb.message.edit_text(build_stats_message(), parse_mode=ParseMode.HTML, reply_markup=back_keyboard())
    except TelegramBadRequest:
        pass
    await cb.answer()

@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    target = get_safe_target()
    try:
        await cb.message.edit_text(
            f"🏠 <b>Головне меню</b>\n👤 Відстежую: <b>{target}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
    except TelegramBadRequest:
        pass
    await cb.answer()


# ── Надсилання сповіщень ──────────────────────────────────────────────────────
async def notify(bot: Bot, text: str):
    if not state.owner_chat_id:
        log.warning("owner_chat_id не встановлено — надішліть /start боту!")
        return
    try:
        await bot.send_message(state.owner_chat_id, text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard())
    except Exception as e:
        log.warning("Не вдалось надіслати сповіщення: %s", e)


# ── Telethon tracker ──────────────────────────────────────────────────────────
async def run_tracker(tg_client: TelegramClient, bot: Bot):
    log.info("Tracker started → target: %s", TARGET_USER)

    # Намагаємось отримати реальне ім'я цілі
    try:
        entity = await tg_client.get_entity(TARGET_USER)
        name = entity.first_name or ""
        if entity.last_name:
            name += f" {entity.last_name}"
        state.target_name = entity.username or name or str(entity.id)
    except Exception:
        pass

    while True:
        sleep_sec = CHECK_INTERVAL_BASE + random.randint(-5, 5)
        reset_daily_stats_if_needed()

        try:
            entity = await tg_client.get_entity(TARGET_USER)
            emoji, label = status_label(entity.status)
            is_now_online = isinstance(entity.status, UserStatusOnline)

            # Перший запуск
            if state.is_online is None:
                state.is_online = is_now_online
                if isinstance(entity.status, UserStatusOffline):
                    was = entity.status.was_online
                    if was.tzinfo is None:
                        was = was.replace(tzinfo=timezone.utc)
                    state.last_seen_at = was
                log.info("Initial status: %s %s", emoji, label)

            # Зайшов ОНЛАЙН
            elif is_now_online and not state.is_online:
                state.is_online = True
                state.last_online_at = datetime.now(timezone.utc)
                state.online_count_today += 1

                event_time = now_local().strftime("%H:%M:%S")
                state.history.append({"online": True, "label": "зайшов(-ла) онлайн", "time": event_time})

                log.info("▶ CAME ONLINE")
                target_display = get_safe_target()
                await notify(bot,
                    f"🟢 <b>{target_display} — ОНЛАЙН</b>\n"
                    f"🕐 {now_local().strftime('%d.%m.%Y %H:%M:%S')}\n"
                    f"📅 Заходів сьогодні: <b>{state.online_count_today}</b>"
                )

            # Вийшов ОФЛАЙН
            elif not is_now_online and state.is_online:
                state.is_online = False
                now_utc = datetime.now(timezone.utc)

                # Тривалість сесії
                duration = 0
                if state.last_online_at:
                    duration = int((now_utc - state.last_online_at).total_seconds())
                    state.online_seconds_today += duration

                if isinstance(entity.status, UserStatusOffline):
                    was = entity.status.was_online
                    if was.tzinfo is None:
                        was = was.replace(tzinfo=timezone.utc)
                    state.last_seen_at = was

                event_time = now_local().strftime("%H:%M:%S")
                state.history.append({"online": False, "label": f"офлайн (сесія: {fmt_duration(duration)})", "time": event_time})

                log.info("◀ WENT OFFLINE — session: %s", fmt_duration(duration))
                target_display = get_safe_target()
                await notify(bot,
                    f"⚫ <b>{target_display} — ОФЛАЙН</b>\n"
                    f"🕐 {now_local().strftime('%d.%m.%Y %H:%M:%S')}\n"
                    f"⏱ Час сесії: <b>{fmt_duration(duration)}</b>\n"
                    f"📊 Онлайн сьогодні: <b>{fmt_duration(state.online_seconds_today)}</b>"
                )

        except FloodWaitError as e:
            wait = e.seconds + 10
            log.warning("FloodWaitError — sleep %d s", wait)
            await asyncio.sleep(wait)
            continue

        except UserPrivacyRestrictedError:
            log.error("Privacy restricted — cannot read status of %s", TARGET_USER)
            await asyncio.sleep(sleep_sec * 3)
            continue

        except (OSError, ConnectionError) as e:
            log.warning("Network error: %s", e)
            await asyncio.sleep(sleep_sec * 2)
            continue

        except Exception as e:
            log.exception("Unexpected error: %s", e)
            await asyncio.sleep(sleep_sec * 2)
            continue

        await asyncio.sleep(sleep_sec)


# ── Точка входу ───────────────────────────────────────────────────────────────
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # Якщо є SESSION_STRING — використовуємо StringSession (для хмарного деплою)
    from telethon.sessions import StringSession
    session = StringSession(SESSION_STRING) if SESSION_STRING else SESSION_NAME

    async with TelegramClient(session, API_ID, API_HASH) as tg_client:
        await tg_client.start()
        me = await tg_client.get_me()
        log.info("Telethon connected as: %s", me.first_name)

        # Автоматично призначаємо свій акаунт як власника для сповіщень
        if not state.owner_chat_id and me and me.id:
            state.owner_chat_id = me.id
            log.info("Auto-registered owner chat_id from Telegram account: %d", me.id)

        # Запускаємо трекер та бота паралельно
        await asyncio.gather(
            dp.start_polling(bot, handle_signals=False),
            run_tracker(tg_client, bot),
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
