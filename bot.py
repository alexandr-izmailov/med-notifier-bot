# Бот-напоминание о приёме лекарств.
#
# Установка (один раз):
#   pip install "aiogram>=3.13,<4" "apscheduler>=3.10.4,<4" tzdata
#
# Запуск:
#   python bot.py
#
# tzdata нужен, чтобы ZoneInfo("Europe/Moscow") работал на Windows.

import asyncio
import json
import logging
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

# На хостинге токен задаётся переменной BOT_TOKEN, в файл его не пишем.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("Задайте переменную окружения BOT_TOKEN")

TZ = ZoneInfo("Europe/Moscow")
DATA_FILE = Path(__file__).with_name("schedule.json")
DOSE_COUNT = 5
DAY_START = time(10, 0)
DAY_END = time(23, 0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("med-bot")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(
    timezone=TZ,
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        # Если компьютер спал, пропущенное напоминание ещё можно прислать в тот же день
        "misfire_grace_time": 13 * 60 * 60,
    },
)

# chat_id (строка) -> состояние дня
DB: dict = {}


def now_msk() -> datetime:
    return datetime.now(TZ)


def end_of(day) -> datetime:
    """23:00 конкретного дня. Позже этого времени напоминания не ставим."""
    return datetime.combine(day, DAY_END, tzinfo=TZ)


def fmt(dt: datetime) -> str:
    dt = dt.astimezone(TZ)
    if dt.microsecond:
        return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    if dt.second:
        return dt.strftime("%H:%M:%S")
    return dt.strftime("%H:%M")


def load_state() -> None:
    global DB
    if not DATA_FILE.exists():
        DB = {}
        return
    try:
        DB = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("Файл расписания повреждён, начинаю с пустого")
        DB = {}


def save_state() -> None:
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(DB, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(DATA_FILE)


def blank_doses() -> list[dict]:
    return [{"status": "idle", "at": None, "message_id": None} for _ in range(DOSE_COUNT)]


def first_slot(moment: datetime) -> datetime:
    """Первый приём: 10:00, либо сразу, если /start нажали позже 10:00 и раньше 23:00."""
    start = datetime.combine(moment.date(), DAY_START, tzinfo=TZ)
    end = end_of(moment.date())
    if moment <= start:
        return start
    if moment < end:
        return moment + timedelta(seconds=1)
    tomorrow = moment.date() + timedelta(days=1)
    return datetime.combine(tomorrow, DAY_START, tzinfo=TZ)


def remaining_times(start: datetime) -> list[datetime]:
    """Четыре оставшихся приёма: отрезок от факта первого «Принято» до 23:00 делится на 4 равные части.

    Приём №5 всегда ровно 23:00. Если первое подтверждение уже в 23:00 или позже,
    оставшиеся приёмы на сегодня не создаются.
    """
    end = end_of(start.date())
    if start >= end:
        return []
    step = (end - start) / 4
    return [start + step, start + step * 2, start + step * 3, end]


def job_id(chat_id: int, dose_no: int) -> str:
    return f"dose:{chat_id}:{dose_no}"


def cancel_user_jobs(chat_id: int) -> None:
    prefix = f"dose:{chat_id}:"
    for job in scheduler.get_jobs():
        if job.id.startswith(prefix):
            job.remove()


def put_user(chat_id: int, first_at: datetime) -> dict:
    """Новый день: в очереди только приём №1, сетка №2–№5 ещё не посчитана."""
    doses = blank_doses()
    doses[0] = {"status": "waiting", "at": first_at.isoformat(), "message_id": None}
    state = {
        "day": first_at.date().isoformat(),
        "grid_fixed": False,
        "doses": doses,
    }
    DB[str(chat_id)] = state
    save_state()
    return state


def cancel_user_jobs_one(chat_id: int, dose_no: int) -> None:
    job = scheduler.get_job(job_id(chat_id, dose_no))
    if job:
        job.remove()


def schedule_dose(chat_id: int, dose_no: int, when: datetime) -> None:
    """Ставит таймер и пишет в JSON ту же метку времени, с которой таймер потом сверится."""
    when = when.astimezone(TZ)
    if when > end_of(when.date()):
        log.info("Чат %s: приём %s на %s не ставлю, это позже 23:00", chat_id, dose_no, fmt(when))
        return
    state = DB.get(str(chat_id))
    if state is None:
        return
    stamp = when.isoformat()
    state["doses"][dose_no - 1]["at"] = stamp
    save_state()
    scheduler.add_job(
        send_dose,
        trigger=DateTrigger(run_date=when),
        args=[chat_id, dose_no, stamp],
        id=job_id(chat_id, dose_no),
        replace_existing=True,
    )
    log.info("Чат %s: приём %s запланирован на %s", chat_id, dose_no, fmt(when))


def keyboard(day: str, dose_no: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+10 мин", callback_data=f"snooze:10:{day}:{dose_no}"),
                InlineKeyboardButton(text="+20 мин", callback_data=f"snooze:20:{day}:{dose_no}"),
                InlineKeyboardButton(text="+30 мин", callback_data=f"snooze:30:{day}:{dose_no}"),
            ],
            [InlineKeyboardButton(text="Принято", callback_data=f"taken:{day}:{dose_no}")],
        ]
    )


def reminder_text(dose_no: int, when: datetime) -> str:
    return (
        "Пора принять лекарство.\n"
        f"Приём {dose_no} из {DOSE_COUNT}.\n"
        f"Время: {fmt(when)} (МСК)"
    )


async def send_dose(chat_id: int, dose_no: int, expected_at: str) -> None:
    state = DB.get(str(chat_id))
    if not state:
        return
    dose = state["doses"][dose_no - 1]
    # Старая задача после переноса или после «Принято» уже не должна писать в чат
    if dose["status"] != "waiting" or dose["at"] != expected_at:
        log.info(
            "Пропуск приёма %s в чате %s: status=%s",
            dose_no,
            chat_id,
            dose["status"],
        )
        return
    when = datetime.fromisoformat(expected_at)
    if now_msk() > end_of(when.date()) + timedelta(minutes=1):
        log.info("Чат %s: приём %s не отправлен, время позже 23:00", chat_id, dose_no)
        return
    try:
        message = await bot.send_message(
            chat_id,
            reminder_text(dose_no, when),
            reply_markup=keyboard(state["day"], dose_no),
        )
    except TelegramForbiddenError:
        log.warning("Чат %s недоступен, пользователь заблокировал бота", chat_id)
        return
    except Exception:
        log.exception("Не удалось отправить приём %s в чат %s", dose_no, chat_id)
        return
    dose["status"] = "sent"
    dose["message_id"] = message.message_id
    save_state()


def restore_jobs() -> None:
    """После перезапуска поднимаем таймеры из JSON, ничего не пересчитывая."""
    moment = now_msk()
    for key, state in list(DB.items()):
        chat_id = int(key)
        day = datetime.fromisoformat(state["day"]).date()
        if day < moment.date():
            begin_day(chat_id, first_slot(moment))
            continue
        for dose_no, dose in enumerate(state["doses"], start=1):
            if dose["status"] == "waiting" and dose["at"]:
                schedule_dose(chat_id, dose_no, datetime.fromisoformat(dose["at"]))


def begin_day(chat_id: int, first_at: datetime) -> None:
    cancel_user_jobs(chat_id)
    put_user(chat_id, first_at)
    schedule_dose(chat_id, 1, first_at)


async def midnight_reset() -> None:
    """В 00:00 счётчик обнуляется, первый приём нового дня снова стоит на 10:00."""
    moment = now_msk()
    opening = datetime.combine(moment.date(), DAY_START, tzinfo=TZ)
    log.info("Полночь МСК: сбрасываю расписание")
    for key in list(DB.keys()):
        begin_day(int(key), opening)


def start_text(first_at: datetime, moment: datetime) -> str:
    when = f"{first_at.strftime('%d.%m')} в {fmt(first_at)} (МСК)"
    if first_at.date() > moment.date():
        plan = f"Сейчас уже позже 23:00, сегодня напоминаний не будет.\nПервый приём: {when}."
    elif first_at - moment <= timedelta(seconds=2):
        plan = "Первое напоминание пришлю сейчас."
    else:
        plan = f"Первый приём: {when}."
    return (
        "Здравствуйте. Я напомню о приёме лекарств 5 раз за день.\n\n"
        f"{plan}\n"
        "Когда на первом напоминании нажмёте «Принято», посчитаю время приёмов 2–5. "
        "Последний будет ровно в 23:00."
    )


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    moment = now_msk()
    first_at = first_slot(moment)
    begin_day(message.chat.id, first_at)
    await message.answer(start_text(first_at, moment))


def dose_of(state: dict, dose_no: int) -> dict | None:
    if not 1 <= dose_no <= DOSE_COUNT:
        return None
    return state["doses"][dose_no - 1]


def empty_keyboard() -> InlineKeyboardMarkup:
    # Пустая клавиатура убирает кнопки. None в aiogram оставляет старые.
    return InlineKeyboardMarkup(inline_keyboard=[])


async def close_message(message: Message, text: str) -> None:
    try:
        await message.edit_text(text, reply_markup=empty_keyboard())
    except TelegramBadRequest:
        await message.answer(text)


@dp.callback_query(F.data.startswith("taken:"))
async def on_taken(callback: CallbackQuery) -> None:
    _, day, dose_raw = callback.data.split(":")
    dose_no = int(dose_raw)
    state = DB.get(str(callback.message.chat.id))
    if not state or state["day"] != day:
        await callback.answer("Это напоминание уже неактуально", show_alert=True)
        return
    dose = dose_of(state, dose_no)
    if dose is None or dose["status"] == "done":
        await callback.answer("Этот приём уже отмечен")
        return

    moment = now_msk().replace(microsecond=0)
    dose["status"] = "done"
    dose["at"] = moment.isoformat()
    cancel_user_jobs_one(callback.message.chat.id, dose_no)

    extra = ""
    # Сетка дня считается один раз — только по первому «Принято»
    if dose_no == 1 and not state["grid_fixed"]:
        times = remaining_times(moment)
        state["grid_fixed"] = True
        lines = []
        for offset, when in enumerate(times, start=2):
            state["doses"][offset - 1] = {
                "status": "waiting",
                "at": when.isoformat(),
                "message_id": None,
            }
            schedule_dose(callback.message.chat.id, offset, when)
            lines.append(f"{offset} — {fmt(when)}")
        if lines:
            extra = "\n\nДальше сегодня:\n" + "\n".join(lines)
        else:
            extra = "\n\nДо 23:00 новых приёмов сегодня уже не планирую."

    save_state()
    await callback.answer("Принято")
    await close_message(callback.message, f"Приём {dose_no} отмечен в {fmt(moment)}.{extra}")


@dp.callback_query(F.data.startswith("snooze:"))
async def on_snooze(callback: CallbackQuery) -> None:
    _, minutes_raw, day, dose_raw = callback.data.split(":")
    minutes = int(minutes_raw)
    dose_no = int(dose_raw)
    state = DB.get(str(callback.message.chat.id))
    if not state or state["day"] != day:
        await callback.answer("Это напоминание уже неактуально", show_alert=True)
        return
    dose = dose_of(state, dose_no)
    if dose is None or dose["status"] == "done":
        await callback.answer("Этот приём уже отмечен")
        return

    moment = now_msk().replace(microsecond=0)
    limit = end_of(moment.date())
    if moment >= limit:
        await callback.answer("После 23:00 напоминания не отправляются", show_alert=True)
        return

    shifted = moment + timedelta(minutes=minutes)
    capped = shifted > limit
    when = limit if capped else shifted
    # Переносится только текущий приём. Приёмы №2–№4 не сдвигают следующие.
    dose["status"] = "waiting"
    dose["at"] = when.isoformat()
    dose["message_id"] = None
    save_state()
    schedule_dose(callback.message.chat.id, dose_no, when)

    await callback.answer("Перенесено")
    text = f"Приём {dose_no} перенесён на {fmt(when)} (МСК)."
    if capped:
        text += "\nПозже 23:00 напоминания не отправляются."
    await close_message(callback.message, text)


async def serve_health() -> None:
    """Бесплатный веб-хостинг ждёт HTTP-порт. Локально PORT не задан, сервер не поднимается."""
    port_raw = os.getenv("PORT", "").strip()
    if not port_raw:
        return
    port = int(port_raw)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)
            body = b"ok"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    log.info("HTTP-порт %s открыт", port)
    async with server:
        await server.serve_forever()


async def main() -> None:
    health_task = asyncio.create_task(serve_health())
    load_state()
    scheduler.start()
    restore_jobs()
    scheduler.add_job(
        midnight_reset,
        trigger=CronTrigger(hour=0, minute=0, second=0, timezone=TZ),
        id="midnight-reset",
        replace_existing=True,
    )
    try:
        me = await bot.get_me()
        log.info("Бот @%s запущен, часовой пояс Europe/Moscow", me.username)
    except Exception:
        log.exception("Профиль бота сейчас не прочитался, запускаю цикл опроса")
    try:
        await dp.start_polling(
            bot,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
    finally:
        health_task.cancel()
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
