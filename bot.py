import asyncio
import contextlib
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
               "До 3 друзей, даже без своей записи. Оплата — за каждого или за всех."]
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
                   f"Номер для звонка: {FIELD_ADMIN_PHONE}",
                   "Нажмите на номер, если ваш Telegram предлагает звонок, либо скопируйте его."]
    return "\n".join(result)


async def update_poll(game):
    if not game or not game["message_id"]:
        return False
    try:
        await bot.edit_message_text(
            chat_id=CHAT_ID, message_id=game["message_id"],
            text=poll_text(game, view_mode.get(game["id"], "main")),
            reply_markup=keyboard(view_mode.get(game["id"], "main")), parse_mode="HTML")
        return True
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return True
        log.error("Cannot edit poll message_id=%s: %s", game["message_id"], exc)
        return False


async def delete_message(game):
    if not game or not game["message_id"]:
        return True
    try:
        await bot.delete_message(CHAT_ID, game["message_id"])
        return True
    except Exception as exc:
        reason = str(exc).lower()
        if "message to delete not found" in reason:
            return True
        log.error("Failed to delete message %s (retained in database): %s",
                  game["message_id"], exc)
        return False


async def close_game():
    async with lock:
        game = current()
        if not game:
            return
        if not await delete_message(game):
            # Keep ID for a later retry; don't silently lose the cleanup target.
            return False
        view_mode.pop(game["id"], None)
        db.execute("UPDATE games SET active=0,message_id=NULL WHERE id=?", (game["id"],))
        return True


async def publish(kind, day, refresh=False):
    async with lock:
        old = current()
        target = game_for(kind, day)
        if old and old["id"] == target["id"] and not refresh and old["message_id"]:
            return
        if setting("send_uncertain"):
            log.error("Publish blocked: unresolved send_uncertain=%s", setting("send_uncertain"))
            return False
        if old and not await delete_message(old):
            # Fail closed: never publish a second message while an old one might exist.
            log.error("Publish postponed: unable to remove previous message %s", old["message_id"])
            return False
        revision = 1 if refresh else target["revision"]
        target = db.execute("SELECT * FROM games WHERE id=?", (target["id"],)).fetchone()
        view_mode[target["id"]] = "main"
        # If sending times out, Telegram might still have posted the message.
        # Record uncertainty and block automatic duplicates until inspected.
        try:
            sent = await bot.send_message(CHAT_ID, poll_text(target), reply_markup=keyboard(),
                                          parse_mode="HTML")
        except Exception as exc:
            put_setting("send_uncertain", target["game_key"])
            log.error("Send result unknown for game %s: %s. Check Telegram chat and "
                      "use /resolve_send in private after manual verification.", target["game_key"], exc)
            return False
        if old:
            view_mode.pop(old["id"], None)
            db.execute("UPDATE games SET active=0,message_id=NULL WHERE id=?", (old["id"],))
        db.execute(
            "UPDATE games SET active=1,message_id=?,revision=? WHERE id=?",
            (sent.message_id, revision, target["id"]))
        log.info("Published game=%s message=%s revision=%s", target["game_key"],
                 sent.message_id, revision)
        return True


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
    if valid:
        # Refresh the same message after restart. Retry transient Telegram timeouts,
        # but never send a replacement poll or discard existing registrations.
        for attempt in range(1, 4):
            async with lock:
                latest = current()
                ok = await update_poll(latest)
            if ok:
                log.info("Existing poll successfully updated: message_id=%s", latest["message_id"])
                break
            if attempt < 3:
                log.warning("Retrying edit of existing poll (%d/3)", attempt + 1)
                await asyncio.sleep(3 * attempt)
        else:
            log.error("Existing poll could not be updated after 3 attempts; "
                      "preserving message_id=%s for later retry", active["message_id"])
    else:
        await publish(kind, game_date, refresh=refreshed)


@router.message(Command("resolve_send"))
async def resolve_send(message: Message):
    # Administrator must inspect the Telegram chat first. This command only
    # releases the duplicate-prevention lock, it never deletes chat messages.
    if not message.from_user or message.from_user.id not in ADMINS or message.chat.type != "private":
        return
    pending = setting("send_uncertain")
    if not pending:
        await message.answer("Нет неопределённых отправок.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or parts[1] != "CONFIRM":
        await message.answer(f"Неопределённая отправка: {pending}. Проверь группу. "
                             "Если сообщение уже появилось, НЕ снимай блокировку — "
                             "сначала сообщи об этом для восстановления ID. "
                             "Если сообщения точно нет, введи /resolve_send CONFIRM.")
        return
    async with lock:
        put_setting("send_uncertain", "")
    await message.answer("Блокировка снята. Следующая проверка расписания повторит публикацию.")


def backup_database():
    dest = Path(DB_PATH).parent / "backups"
    dest.mkdir(parents=True, exist_ok=True)
    filename = dest / ("meetup-" + now().strftime("%Y%m%d-%H%M%S") + ".sqlite3")
    try:
        with sqlite3.connect(str(filename)) as copy:
            db.backup(copy)
        for file in sorted(dest.glob("meetup-*.sqlite3"))[:-14]:
            file.unlink()
        log.info("Database backup created: %s", filename)
    except Exception:
        log.exception("SQLite backup failed")


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
            if 0 not in nums:
                notice = "Вы сами не записаны. Друзья остаются в списке."
            else:
                db.execute("DELETE FROM slots WHERE game_id=? AND owner_id=? AND friend_no=0",
                           (game["id"],user.id))
                notice = "Ваша запись отменена. Добавленные друзья остаются в игре."
        elif action == "friend_add":
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


def can_collect(user_id):
    return user_id in ADMINS or str(user_id) == setting("collector_id")


def latest_game():
    return db.execute("SELECT * FROM games ORDER BY id DESC LIMIT 1").fetchone()


def payment_label(payment):
    return {
        "unpaid": "⏳ Не заявлено",
        "pending_transfer": "💳 Ожидает перевод",
        "pending_cash": "💵 Ожидает наличные",
        "confirmed_transfer": "✅ Перевод подтверждён",
        "confirmed_cash": "✅ Наличные подтверждены",
    }.get(payment, payment)


PANEL_PAGE_SIZE = 6


def panel_data(page=0):
    game = latest_game()
    if game is None:
        return "⚽ Игр пока нет.", None
    rows = players(game["id"])
    total = len(rows)
    confirmed = sum(row["payment"].startswith("confirmed_") for row in rows)
    pending = sum(row["payment"].startswith("pending_") for row in rows)
    page_count = max(1, (total + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE)
    page = max(0, min(page, page_count-1))
    lines = [
        f"💰 <b>ОПЛАТЫ — {html.escape(game['game_date'])}</b>",
        f"👥 Всего: {total} · ✅ Подтверждено: {confirmed}",
        f"💳/💵 Заявили: {pending} · ⏳ Не заявили: {total-confirmed-pending}",
    ]
    if PRICE:
        lines.append(f"Собрано: {confirmed*PRICE:,} ₽ из {total*PRICE:,} ₽".replace(",", " "))
    lines += ["", "Нажми на человека, чтобы подтвердить получение денег.",
              f"Страница {page+1}/{page_count}"]
    buttons = []
    for item in rows[page*PANEL_PAGE_SIZE:(page+1)*PANEL_PAGE_SIZE]:
        suffix = f" (друг {item['friend_no']})" if item["friend_no"] else ""
        label = f"{item['owner_name'][:22]}{suffix}"
        lines.append(f"• {html.escape(label)} — {payment_label(item['payment'])}")
        if not item["payment"].startswith("confirmed_"):
            buttons.append([InlineKeyboardButton(
                text=f"✅ {label[:31]}",
                callback_data=f"pc:{item['id']}:{page}")])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️",callback_data=f"panel:{page-1}"))
    navigation.append(InlineKeyboardButton(text="🔄 Обновить",callback_data=f"panel:{page}"))
    if page+1 < page_count:
        navigation.append(InlineKeyboardButton(text="➡️",callback_data=f"panel:{page+1}"))
    buttons.append(navigation)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def redraw_panel(call, page=0):
    text, markup = panel_data(page)
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            log.error("Cannot update payment panel: %s", exc)
            await call.message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("set_collector", "collector", "clear_collector"))
async def collector_config(message: Message):
    if not message.from_user or message.from_user.id not in ADMINS:
        return
    command = message.text.split()[0].split("@")[0]
    if command == "/collector":
        selected = setting("collector_id")
        name = setting("collector_name")
        await message.answer(f"Сборщик: {name} · Telegram ID: {selected}" if selected
                             else "Сборщик не назначен. Используй /set_collector в ответ на сообщение человека в группе или /set_collector 123456789 Имя.")
        return
    if command == "/clear_collector":
        put_setting("collector_id", "")
        put_setting("collector_name", "")
        await message.answer("Доступ сборщика отозван. Администраторы сохраняют доступ.")
        return
    parts = (message.text or "").split(maxsplit=2)
    replied = message.reply_to_message.from_user if message.reply_to_message else None
    if replied and not replied.is_bot:
        user_id = replied.id
        name = " ".join(filter(None, [replied.first_name, replied.last_name]))
    elif len(parts) >= 3 and parts[1].isdigit() and int(parts[1]) > 0:
        user_id = int(parts[1])
        name = parts[2].strip()[:80]
    else:
        await message.answer("Ответь /set_collector на сообщение сборщика в группе, либо напиши /set_collector 123456789 Имя в личке с ботом.")
        return
    put_setting("collector_id", str(user_id))
    put_setting("collector_name", name)
    await message.answer(f"✅ Сборщик назначен: {name} (ID {user_id}). "
                         "Теперь он открывает бота в личке, нажимает Start и отправляет /payments. "
                         "Доступ только к оплатам, без административных команд.")


@router.message(Command("payments"))
async def payments(message: Message):
    if not message.from_user:
        return
    if message.chat.type != "private":
        await message.answer("Для приватности открой /payments в личном чате с ботом.")
        return
    if not can_collect(message.from_user.id):
        await message.answer("Финансовая панель доступна только назначенному сборщику и администраторам.")
        return
    text, markup = panel_data()
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data.startswith("panel:"))
async def panel_navigation(call: CallbackQuery):
    if call.message.chat.type != "private" or not can_collect(call.from_user.id):
        await call.answer("Нет доступа.",show_alert=True)
        return
    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректная страница.",show_alert=True)
        return
    await redraw_panel(call,page)
    await call.answer()


@router.callback_query(F.data.startswith("pc:"))
async def payment_confirm_choice(call: CallbackQuery):
    if call.message.chat.type != "private" or not can_collect(call.from_user.id):
        await call.answer("Нет доступа.",show_alert=True)
        return
    try:
        _, slot_id, page = call.data.split(":")
        slot_id, page = int(slot_id), int(page)
    except (ValueError, IndexError):
        await call.answer("Ошибка выбора.",show_alert=True)
        return
    row = db.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
    game = latest_game()
    if not row or not game or row["game_id"] != game["id"] or row["payment"].startswith("confirmed_"):
        await call.answer("Запись уже обработана или недоступна.",show_alert=True)
        await redraw_panel(call,page)
        return
    method = row["payment"].split("_")[-1] if row["payment"].startswith("pending_") else ""
    if method:
        buttons = [[InlineKeyboardButton(text=f"✅ Подтвердить {('перевод' if method=='transfer' else 'наличные')}",
                                         callback_data=f"pm:{slot_id}:{method}:{page}")]]
    else:
        buttons = [[InlineKeyboardButton(text="✅ Получил перевод",callback_data=f"pm:{slot_id}:transfer:{page}"),
                    InlineKeyboardButton(text="✅ Получил наличные",callback_data=f"pm:{slot_id}:cash:{page}")]]
    buttons.append([InlineKeyboardButton(text="⬅️ К списку оплат",callback_data=f"panel:{page}")])
    name = html.escape(row["owner_name"])
    if row["friend_no"]:
        name += f" (друг {row['friend_no']})"
    await call.message.edit_text(
        f"💰 <b>Подтвердить оплату?</b>\n{name}\n{payment_label(row['payment'])}\n"
        "Нажимай подтверждение только после фактического получения денег.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data.startswith("pm:"))
async def confirm_payment(call: CallbackQuery):
    if call.message.chat.type != "private" or not can_collect(call.from_user.id):
        await call.answer("Нет доступа.",show_alert=True)
        return
    try:
        _, slot_id, method, page = call.data.split(":")
        slot_id, page = int(slot_id), int(page)
    except (ValueError, IndexError):
        await call.answer("Ошибка данных.",show_alert=True)
        return
    if method not in ("transfer","cash"):
        await call.answer("Некорректный способ оплаты.",show_alert=True)
        return
    async with lock:
        row = db.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        game = latest_game()
        if not row or not game or row["game_id"] != game["id"]:
            await call.answer("Запись недоступна.",show_alert=True)
            return
        if row["payment"].startswith("confirmed_"):
            await call.answer("Уже подтверждено.")
        else:
            db.execute("UPDATE slots SET payment=? WHERE id=?",
                       ("confirmed_"+method,slot_id))
            log.info("Payment confirmed: slot=%s actor=%s method=%s",slot_id,call.from_user.id,method)
            active = current()
            if active and active["id"] == row["game_id"]:
                await update_poll(active)
            await call.answer("✅ Получение денег подтверждено.")
    await redraw_panel(call,page)


async def run():
    scheduler = AsyncIOScheduler(timezone=TZ)
    # Every scheduled operation also repairs missed executions after a restart.
    scheduler.add_job(reconcile, "cron", day_of_week="tue", hour=10, minute=0, id="wed_open",
                      misfire_grace_time=120, coalesce=True)
    scheduler.add_job(reconcile, "cron", day_of_week="thu", hour=9, minute=55, id="wed_close")
    scheduler.add_job(reconcile, "cron", day_of_week="fri", hour=11, minute=0, id="sun_open")
    scheduler.add_job(reconcile, "cron", day_of_week="sun", hour=10, minute=55, id="sun_refresh")
    scheduler.add_job(reconcile, "cron", day_of_week="sun", hour=15, minute=0, id="sun_close")
    # Periodically repair an edit that failed when Telegram was unavailable.
    scheduler.add_job(reconcile, "interval", minutes=15, id="network_recovery",
                      max_instances=1, coalesce=True)
    scheduler.add_job(backup_database, "cron", hour=3, minute=15, id="sqlite_backup")
    scheduler.start()
    # Do not stall polling during a long startup Telegram timeout.
    recovery = asyncio.create_task(reconcile(), name="initial_reconcile")
    backup_database()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        if not recovery.done():
            recovery.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recovery
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(run())
