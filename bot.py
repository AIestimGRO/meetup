import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TZ = ZoneInfo("Europe/Moscow")
TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))
ADMINS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
PRICE = int(os.environ.get("PRICE_RUB", "0"))
DB_PATH = os.environ.get("DATABASE_PATH", "/data/meetup.sqlite3")
FIELD_ADMIN_PHONE = "+79653743566"
if not TOKEN or TOKEN == "PUT_NEW_TOKEN_HERE" or not CHAT_ID or not ADMINS:
    raise RuntimeError("Set BOT_TOKEN, CHAT_ID, ADMIN_IDS in .env")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("football")
db = sqlite3.connect(DB_PATH, isolation_level=None)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
db.executescript("""
CREATE TABLE IF NOT EXISTS games(
 id INTEGER PRIMARY KEY, game_key TEXT UNIQUE NOT NULL,
 kind TEXT NOT NULL, game_date TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0,
 message_id INTEGER, revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots(
 id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL REFERENCES games(id),
 owner_id INTEGER NOT NULL, owner_name TEXT NOT NULL, friend_no INTEGER NOT NULL,
 payment TEXT NOT NULL DEFAULT 'unpaid',
 UNIQUE(game_id,owner_id,friend_no)
);
""")
db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
lock = asyncio.Lock()
view_mode = {}

bot = Bot(token=TOKEN)
router = Router()
dp = Dispatcher()
dp.include_router(router)


def now():
    return datetime.now(TZ)


def current():
    return db.execute("SELECT * FROM games WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()


def players(game_id):
    return db.execute("SELECT * FROM slots WHERE game_id=? ORDER BY id", (game_id,)).fetchall()


def game_for(kind, day):
    key = f"{day.isoformat()}-{kind}"
    row = db.execute("SELECT * FROM games WHERE game_key=?", (key,)).fetchone()
    if not row:
        db.execute(
            "INSERT INTO games(game_key,kind,game_date,created_at) VALUES(?,?,?,?)",
            (key, kind, day.isoformat(), now().isoformat()))
        row = db.execute("SELECT * FROM games WHERE game_key=?", (key,)).fetchone()
    return row


def setting(key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def put_setting(key, value):
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key,value))


def keyboard(mode="main"):
    if mode == "payment":
        rows = [[InlineKeyboardButton(text="💳 Перевёл за себя", callback_data="pay:0:transfer"),
                 InlineKeyboardButton(text="💵 Наличными за себя", callback_data="pay:0:cash")]]
        for n in range(1,4):
            rows.append([InlineKeyboardButton(text=f"💳 Друг {n}",callback_data=f"pay:{n}:transfer"),
                         InlineKeyboardButton(text=f"💵 Друг {n}",callback_data=f"pay:{n}:cash")])
        rows.append([InlineKeyboardButton(text="💳 За всех",callback_data="pay:all:transfer"),
                     InlineKeyboardButton(text="💵 За всех",callback_data="pay:all:cash")])
    elif mode == "contact":
        rows = []
    else:
        rows = [[InlineKeyboardButton(text="➕ Записаться",callback_data="join"),
                 InlineKeyboardButton(text="➖ Отменить",callback_data="leave")],
                [InlineKeyboardButton(text="➕ 1 друг",callback_data="friend_add"),
                 InlineKeyboardButton(text="➖ 1 друг",callback_data="friend_del")],
                [InlineKeyboardButton(text="💳 Оплата",callback_data="menu:payment")],
                [InlineKeyboardButton(text="📞 Администратор поля",callback_data="menu:contact")]]
    if mode != "main":
        rows.append([InlineKeyboardButton(text="⬅️ Назад к записи",callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def display(row):
    name = html.escape(row["owner_name"])
    if row["friend_no"]:
        name += f" — друг {row['friend_no']}"
    status = row["payment"]
    icon = {"unpaid":"⏳", "pending_transfer":"💳⏳", "pending_cash":"💵⏳",
            "confirmed_transfer":"✅💳", "confirmed_cash":"✅💵"}.get(status,"⏳")
    return name + " " + icon


def poll_text(game, mode="main"):
    day = "СРЕДУ" if game["kind"] == "wed" else "ВОСКРЕСЕНЬЕ"
    listed = players(game["id"])
    result = [f"⚽ <b>ФУТБОЛ — {day}</b>",
              f"📅 {html.escape(game['game_date'])}",
              f"👥 Записано: <b>{len(listed)}</b>" + (f" · {PRICE} ₽/чел." if PRICE else ""),
              "", "<b>Список игроков:</b>"]
    for index, row in enumerate(listed, 1):
        line = f"{index}. {display(row)}"
        if len("\n".join(result)) + len(line) > 3500:
            result.append("… Остальные участники не показаны")
            break
        result.append(line)
    if not listed:
        result.append("Пока никого нет.")
    result += ["", "💳 — заявил о переводе · 💵 — заявил о наличных",
               "⏳ — ждём подтверждения · ✅ — организатор подтвердил",
               "Добавить можно до 3 друзей. Оплатить — за каждого или за всех."]
    if mode == "payment":
        receiver, number = setting("receiver_name"), setting("receiver_phone")
        result += ["", "💳 <b>ОПЛАТА</b>"]
        if receiver and number:
            result += [f"Получатель: <b>{html.escape(receiver)}</b>",
                       f"Телефон для перевода: <code>{html.escape(number)}</code>"]
        else:
            result.append("Получатель перевода пока не назначен организатором.")
        result += ["Выберите способ оплаты за себя, друга или всю компанию.",
                   "После отметки организатор проверит и подтвердит оплату."]
    elif mode == "contact":
        result += ["", "📞 <b>АДМИНИСТРАТОР ПОЛЯ</b>",
                   f"Номер: <a href=\"tel:{FIELD_ADMIN_PHONE}\">{FIELD_ADMIN_PHONE}</a>",
                   "Нажмите на номер, чтобы позвонить (зависит от приложения Telegram)."]
    return "\n".join(result)


async def update_poll(game):
    if not game or not game["message_id"]:
        return
    try:
        await bot.edit_message_text(
            chat_id=CHAT_ID, message_id=game["message_id"],
            text=poll_text(game, view_mode.get(game["id"], "main")),
            reply_markup=keyboard(view_mode.get(game["id"], "main")), parse_mode="HTML")
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            log.error("Cannot edit poll: %s", exc)


async def delete_message(game):
    if not game or not game["message_id"]:
        return
    try:
        await bot.delete_message(CHAT_ID, game["message_id"])
    except Exception as exc:
        log.warning("Failed to delete message %s, manual cleanup may be needed: %s",
                    game["message_id"], exc)


async def close_game():
    async with lock:
        game = current()
        if not game:
            return
        await delete_message(game)
        view_mode.pop(game["id"], None)
        db.execute("UPDATE games SET active=0,message_id=NULL WHERE id=?", (game["id"],))


async def publish(kind, day, refresh=False):
    async with lock:
        old = current()
        target = game_for(kind, day)
        if old and old["id"] == target["id"] and not refresh and old["message_id"]:
            return
        if old:
            await delete_message(old)
            view_mode.pop(old["id"], None)
            db.execute("UPDATE games SET active=0,message_id=NULL WHERE id=?", (old["id"],))
        revision = 1 if refresh else target["revision"]
        target = db.execute("SELECT * FROM games WHERE id=?", (target["id"],)).fetchone()
        view_mode[target["id"]] = "main"
        sent = await bot.send_message(CHAT_ID, poll_text(target), reply_markup=keyboard(),
                                      parse_mode="HTML")
        db.execute(
            "UPDATE games SET active=1,message_id=?,revision=? WHERE id=?",
            (sent.message_id, revision, target["id"]))
        log.info("Published game=%s message=%s revision=%s", target["game_key"],
                 sent.message_id, revision)


def desired_window(instant):
    # Tuesday 10:00 -> Thursday 09:55
    # Friday 11:00 -> Sunday 15:00, with Sunday refresh after 10:55
    weekday = instant.weekday()
    date = instant.date()
    if weekday == 1 and instant.hour >= 10 or weekday == 2 or weekday == 3 and (instant.hour,instant.minute) < (9,55):
        tuesday = date - timedelta(days=(weekday-1))
        return "wed", tuesday+timedelta(days=1), False
    if weekday == 4 and instant.hour >= 11 or weekday == 5 or weekday == 6 and (instant.hour,instant.minute) < (15,0):
        friday = date - timedelta(days=(weekday-4))
        refreshed = weekday == 6 and (instant.hour,instant.minute) >= (10,55)
        return "sun", friday+timedelta(days=2), refreshed
    return None


async def reconcile():
    window = desired_window(now())
    if not window:
        await close_game()
        return
    kind, game_date, refreshed = window
    async with lock:
        active = current()
        same = active and active["game_key"] == f"{game_date.isoformat()}-{kind}"
        valid = same and active["message_id"] and (not refreshed or active["revision"] >= 1)
    if not valid:
        await publish(kind, game_date, refresh=refreshed)


@router.message(Command("start"))
async def start(message: Message):
    await message.answer("⚽ Записывайтесь кнопками под голосовалкой в группе. "
                         "Можно добавить до 3 друзей. Отмечайте переводы и наличные; "
                         "организатор подтверждает поступление. /status — текущая игра.")


@router.message(Command("status"))
async def status(message: Message):
    game = current()
    if not game:
        await message.answer("Сейчас открытой записи нет.")
    else:
        await message.answer(poll_text(game), parse_mode="HTML")


@router.message(Command("publish_wednesday", "publish_sunday", "close"))
async def admin_command(message: Message):
    if message.from_user.id not in ADMINS:
        return
    command = message.text.split()[0].split("@")[0]
    if command == "/close":
        await close_game()
        await message.answer("Голосовалка закрыта.")
        return
    kind = "wed" if command == "/publish_wednesday" else "sun"
    date = now().date()
    offset = ((2 if kind=="wed" else 6) - date.weekday()) % 7
    await publish(kind, date + timedelta(days=offset))
    await message.answer("Голосовалка опубликована.")


@router.message(Command("set_receiver", "clear_receiver", "receiver"))
async def set_receiver(message: Message):
    if not message.from_user or message.from_user.id not in ADMINS:
        return
    command = message.text.split()[0].split("@")[0]
    if command == "/receiver":
        name, phone = setting("receiver_name"), setting("receiver_phone")
        await message.answer(f"Получатель переводов: {name} — {phone}" if name else
                             "Получатель не назначен.")
        return
    if command == "/clear_receiver":
        async with lock:
            put_setting("receiver_name", "")
            put_setting("receiver_phone", "")
            await update_poll(current())
        await message.answer("Реквизиты переводов очищены.")
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Используй: /set_receiver +79991234567 Имя. "
                             "Либо ответь командой на сообщение участника группы.")
        return
    phone = re.sub(r"[\s()\-]", "", parts[1])
    if not re.fullmatch(r"\+7\d{10}", phone):
        await message.answer("Укажи номер в формате +79991234567.")
        return
    replied = message.reply_to_message.from_user if message.reply_to_message else None
    if replied and not replied.is_bot:
        name = " ".join(filter(None, [replied.first_name, replied.last_name]))
    elif len(parts) == 3 and parts[2].strip():
        name = parts[2].strip()[:80]
    else:
        await message.answer("Укажи имя после номера либо ответь на сообщение участника.")
        return
    async with lock:
        put_setting("receiver_name", name)
        put_setting("receiver_phone", phone)
        await update_poll(current())
    await message.answer(f"Получатель назначен: {name}, {phone}. "
                         "Номер виден в разделе оплаты.")


@router.callback_query(F.data.startswith("menu:"))
async def change_menu(call: CallbackQuery):
    if not call.message or not call.from_user:
        return
    mode = call.data.split(":",1)[1]
    if mode not in ("main","payment","contact"):
        await call.answer("Неизвестное меню.",show_alert=True)
        return
    async with lock:
        game = current()
        if not game or call.message.chat.id != CHAT_ID or call.message.message_id != game["message_id"]:
            await call.answer("Эта голосовалка закрыта.",show_alert=True)
            return
        view_mode[game["id"]] = mode
        await update_poll(game)
    await call.answer()


@router.callback_query(F.data.in_({"join","leave","friend_add","friend_del"}) | F.data.startswith("pay:"))
async def vote(call: CallbackQuery):
    user = call.from_user
    async with lock:
        game = current()
        if not game or call.message.chat.id != CHAT_ID or call.message.message_id != game["message_id"]:
            await call.answer("Эта голосовалка уже закрыта.", show_alert=True)
            return
        action = call.data
        my = db.execute(
            "SELECT * FROM slots WHERE game_id=? AND owner_id=? ORDER BY friend_no",
            (game["id"], user.id)).fetchall()
        nums = {r["friend_no"] for r in my}
        name = " ".join(filter(None, [user.first_name, user.last_name])) or str(user.id)
        notice = "Готово"
        if action == "join":
            if 0 in nums:
                notice = "Вы уже записаны."
            else:
                db.execute("INSERT INTO slots(game_id,owner_id,owner_name,friend_no) VALUES(?,?,?,0)",
                           (game["id"], user.id, name))
        elif action == "leave":
            if not my:
                notice = "Вас нет в списке."
            else:
                db.execute("DELETE FROM slots WHERE game_id=? AND owner_id=?", (game["id"],user.id))
                notice = "Вы и ваши друзья удалены из записи."
        elif action == "friend_add":
            if 0 not in nums:
                notice = "Сначала запишитесь сами."
            else:
                free = next((i for i in range(1,4) if i not in nums), None)
                if free is None:
                    notice = "Максимум 3 друга."
                else:
                    db.execute("INSERT INTO slots(game_id,owner_id,owner_name,friend_no) VALUES(?,?,?,?)",
                               (game["id"],user.id,name,free))
        elif action == "friend_del":
            friends = [i for i in nums if i > 0]
            if not friends:
                notice = "Добавленных друзей нет."
            else:
                db.execute("DELETE FROM slots WHERE game_id=? AND owner_id=? AND friend_no=?",
                           (game["id"],user.id,max(friends)))
        elif action.startswith("pay:"):
            _, which, kind = action.split(":")
            if kind not in ("transfer","cash"):
                await call.answer("Некорректный способ оплаты.",show_alert=True)
                return
            selected = my if which == "all" else [r for r in my if r["friend_no"] == int(which)]
            if not selected:
                notice = "Сначала добавьте себя или соответствующего друга."
            else:
                for row in selected:
                    # Do not allow player to overwrite confirmed payment.
                    if row["payment"].startswith("confirmed_"):
                        continue
                    db.execute("UPDATE slots SET payment=? WHERE id=?",
                               ("pending_"+kind,row["id"]))
                notice = "Отметка отправлена. Организатор должен подтвердить получение."
        game = current()
        await update_poll(game)
    await call.answer(notice, show_alert=notice != "Готово")


def payments_keyboard(items):
    rows = []
    for row in items[:80]:
        state = row["payment"]
        action = "✅" if state.startswith("pending_") else "✓"
        suffix = "друг "+str(row["friend_no"]) if row["friend_no"] else "сам"
        rows.append([InlineKeyboardButton(
            text=f"{action} {row['owner_name'][:20]} ({suffix}) {state.replace('pending_','').replace('unpaid','—')}",
            callback_data=f"confirm:{row['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("payments"))
async def payments(message: Message):
    if message.from_user.id not in ADMINS:
        return
    if message.chat.type != "private":
        await message.answer("Откройте /payments в личном чате с ботом — не публикуем финансы в группе.")
        return
    game = db.execute("SELECT * FROM games ORDER BY id DESC LIMIT 1").fetchone()
    if not game:
        await message.answer("Игр пока нет.")
        return
    pending = db.execute(
        "SELECT * FROM slots WHERE game_id=? AND payment NOT LIKE 'confirmed_%' ORDER BY id",
        (game["id"],)).fetchall()
    confirmed = db.execute(
        "SELECT count(*) n FROM slots WHERE game_id=? AND payment LIKE 'confirmed_%'",
        (game["id"],)).fetchone()["n"]
    if not pending:
        await message.answer(f"Игра {game['game_date']}: все {confirmed} оплат подтверждены (или участников нет).")
        return
    await message.answer(
        f"⚽ Игра {game['game_date']}\nПодтверждено: {confirmed}\n"
        "Нажмите на строку, чтобы подтвердить получение оплаты. "
        "Если человек не отмечал способ, выберите его после нажатия.\n"
        "Обозначения: transfer — перевод, cash — наличные, — ещё не заявил.",
        reply_markup=payments_keyboard(pending))


@router.callback_query(F.data.startswith("confirm:"))
async def admin_payment(call: CallbackQuery):
    if call.from_user.id not in ADMINS or call.message.chat.type != "private":
        await call.answer("Недостаточно прав.",show_alert=True)
        return
    slot_id = int(call.data.split(":")[1])
    async with lock:
        row = db.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not row:
            await call.answer("Запись уже удалена.", show_alert=True)
            return
        if row["payment"] == "unpaid":
            keyboard_ = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Перевод",callback_data=f"final:{slot_id}:transfer"),
                InlineKeyboardButton(text="✅ Наличные",callback_data=f"final:{slot_id}:cash")]])
            await call.message.answer("Как получена оплата?",reply_markup=keyboard_)
            await call.answer()
            return
        kind = row["payment"].split("_")[-1]
        db.execute("UPDATE slots SET payment=? WHERE id=?",("confirmed_"+kind,slot_id))
        await update_poll(current() if current() and current()["id"]==row["game_id"] else None)
    await call.answer("Оплата подтверждена.")
    await call.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("final:"))
async def manual_confirmation(call: CallbackQuery):
    if call.from_user.id not in ADMINS or call.message.chat.type != "private":
        await call.answer("Недостаточно прав.",show_alert=True)
        return
    _, id_string, kind = call.data.split(":")
    if kind not in ("cash","transfer"):
        await call.answer("Ошибка",show_alert=True)
        return
    async with lock:
        row = db.execute("SELECT * FROM slots WHERE id=?", (int(id_string),)).fetchone()
        if not row:
            await call.answer("Запись не найдена.",show_alert=True)
            return
        db.execute("UPDATE slots SET payment=? WHERE id=?",("confirmed_"+kind,row["id"]))
        active = current()
        await update_poll(active if active and active["id"] == row["game_id"] else None)
    await call.answer("Подтверждено")
    await call.message.edit_reply_markup(reply_markup=None)


async def run():
    scheduler = AsyncIOScheduler(timezone=TZ)
    # Every scheduled operation also repairs missed executions after a restart.
    scheduler.add_job(reconcile, "cron", day_of_week="tue", hour=10, minute=0, id="wed_open",
                      misfire_grace_time=120, coalesce=True)
    scheduler.add_job(reconcile, "cron", day_of_week="thu", hour=9, minute=55, id="wed_close")
    scheduler.add_job(reconcile, "cron", day_of_week="fri", hour=11, minute=0, id="sun_open")
    scheduler.add_job(reconcile, "cron", day_of_week="sun", hour=10, minute=55, id="sun_refresh")
    scheduler.add_job(reconcile, "cron", day_of_week="sun", hour=15, minute=0, id="sun_close")
    scheduler.start()
    try:
        await reconcile()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(run())
