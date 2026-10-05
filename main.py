"""
Diplomat — бот-менеджер для Telegram.
Улучшенная версия: много команд, гибкие сроки, автомодерация.
Всё в одном файле. Без БД (память).
"""
import asyncio
import logging
import os
import re
import random
import time
from datetime import datetime, timedelta
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart, BaseFilter
from aiogram.types import (
    Message, CallbackQuery, ChatPermissions,
    InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated,
)


# ====================== НАСТРОЙКИ ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СВОЙ_ТОКЕН")
CHANNEL = "@karmaproj"
CHANNEL_URL = "https://t.me/karmaproj"
WARN_LIMIT = 3

FLOOD_LIMIT = 5
FLOOD_WINDOW = 5
FLOOD_MUTE_MINUTES = 5

CAPS_MIN_LEN = 10
CAPS_MIN_PERCENT = 70

AUTODELETE_SECONDS = 15
ADMIN_CACHE_TTL = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("diplomat")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

MUTE_PERMS = ChatPermissions(can_send_messages=False)
READONLY_PERMS = ChatPermissions(can_send_messages=False)
UNMUTE_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_send_polls=True,
    can_invite_users=True,
)

_BOT_USERNAME = "DiplomatBot"
START_TIME = time.time()


# ====================== ПАМЯТЬ ======================
warns_store = {}
notes_store = {}
rules_store = {}
filters_store = {}
whitelist_store = {}
bad_words_store = {}
settings_store = {}
activity_store = {}
known_users = {}
subs_checked = {}
flood_log = defaultdict(list)
_admin_cache = {}


def get_settings(chat_id):
    if chat_id not in settings_store:
        settings_store[chat_id] = {
            "antimat": 0, "antiflood": 0, "antilink": 0, "anticaps": 0,
            "welcome": 1, "goodbye": 1,
            "welcome_text": None, "goodbye_text": None,
        }
    return settings_store[chat_id]


def update_setting(chat_id, key, value):
    get_settings(chat_id)[key] = value


# ====================== ХЕЛПЕРЫ ======================
def uptime_str():
    sec = int(time.time() - START_TIME)
    h, m = divmod(sec // 60, 60)
    d, h = divmod(h, 24)
    return f"{d}д {h}ч {m}м"


def parse_duration_str(text):
    """Парсит '7d', '30m', '2h', '1w', '45s'. Возвращает timedelta или None."""
    m = re.search(r"(\d+)\s*([smhdw])", text.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
    }[unit]


def fmt_duration(td):
    if td is None:
        return "навсегда"
    s = int(td.total_seconds())
    if s >= 86400: return f"{s // 86400} д."
    if s >= 3600:  return f"{s // 3600} ч."
    if s >= 60:    return f"{s // 60} мин."
    return f"{s} сек."


def fmt_until(until_ts):
    if not until_ts:
        return "навсегда"
    td = datetime.fromtimestamp(until_ts) - datetime.now()
    return fmt_duration(td) if td.total_seconds() > 0 else "истёк"


async def parse_target(message, args):
    """reply → @username → numeric ID. Возвращает (uid, uname, remaining_args)."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return (message.reply_to_message.from_user.id,
                message.reply_to_message.from_user.username, args)
    if not args:
        return None, None, args
    first = args[0]
    if first.startswith("@"):
        return None, first[1:], args[1:]
    if first.lstrip("-").isdigit() and len(first) >= 5:
        return int(first), None, args[1:]
    return None, None, args


async def resolve_user(message, bot, user_id, username):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    if username:
        try:
            m = await bot.get_chat_member(message.chat.id, f"@{username}")
            return m.user
        except Exception:
            uid = known_users.get((message.chat.id, username.lower()))
            if uid:
                try:
                    m = await bot.get_chat_member(message.chat.id, uid)
                    return m.user
                except Exception:
                    return None
            return None
    if user_id:
        try:
            m = await bot.get_chat_member(message.chat.id, user_id)
            return m.user
        except Exception:
            return None
    return None


async def is_admin(bot, chat_id, user_id):
    key = (chat_id, user_id)
    now = time.time()
    if key in _admin_cache:
        ts, val = _admin_cache[key]
        if now - ts < ADMIN_CACHE_TTL:
            return val
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        ok = m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        ok = False
    _admin_cache[key] = (now, ok)
    return ok


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        return await is_admin(message.bot, message.chat.id, message.from_user.id)


async def ensure_rights(bot, chat_id):
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
        return bool(getattr(me, "can_restrict_members", False))
    except Exception:
        return False


async def _autodelete(chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def _warn_msg(message, text):
    try:
        msg = await message.answer(text)
        asyncio.create_task(
            _autodelete(message.chat.id, msg.message_id, AUTODELETE_SECONDS)
        )
    except Exception:
        log.exception("warn msg failed")


NO_TARGET = (
    "❌ Не нашёл юзера.\n\n"
    "• Ответь <b>реплаем</b> на сообщение\n"
    "• Или укажи числовой ID\n"
    "• @username работает, если юзер уже писал в чате"
)
NOT_ENOUGH_RIGHTS = (
    "⚠️ У меня нет прав.\nВыдай: <b>Ограничивать участников</b>, "
    "<b>Блокировать участников</b>, <b>Удалять сообщения</b>."
)
NOT_ADMIN = "⛔ Только для администраторов."


# ====================== УТИЛИТЫ АНТИМАТА ======================
BAD_ROOTS = [
    "бля", "хуй", "хуе", "хуя", "хую", "пизд", "еба", "ебал",
    "ебан", "ебет", "ёб", "сука", "суки", "муда", "муди",
    "нах", "пидор", "пидар", "долбо", "гандон", "шлюх", "мраз",
    "fuck", "shit", "bitch", "cunt", "dick", "pussy", "asshole",
]

LEET_MAP = {
    "a": "а", "b": "б", "e": "е", "k": "к", "m": "м", "h": "н",
    "o": "о", "p": "р", "c": "с", "t": "т", "x": "х", "y": "у",
    "0": "о", "1": "и", "3": "з", "4": "ч", "5": "с", "6": "б",
    "8": "в", "@": "а", "$": "с", "!": "и",
}


def normalize(text):
    t = text.lower()
    for k, v in LEET_MAP.items():
        t = t.replace(k, v)
    t = re.sub(r"[^а-яёa-z]", "", t)
    return re.sub(r"(.)\1{2,}", r"\1\1", t)


def contains_mat(text, custom=None):
    if not text:
        return None
    n = normalize(text)
    for root in BAD_ROOTS:
        if root in n:
            return root
    if custom:
        for w in custom:
            if w.lower() in n:
                return w
    return None


LINK_RE = re.compile(r"(?:https?://|t\.me/|telegram\.me/|www\.)\S+", re.I)


def contains_link(text):
    return bool(LINK_RE.search(text or ""))


def is_caps(text):
    letters = [c for c in text if c.isalpha()]
    if len(letters) < CAPS_MIN_LEN:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return (upper / len(letters)) * 100 >= CAPS_MIN_PERCENT


def extract_text(message):
    parts = []
    if message.text:
        parts.append(message.text)
    if message.caption:
        parts.append(message.caption)
    return " ".join(parts)


# ====================== КЛАВИАТУРЫ ======================
def sub_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
    ])


def menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧭 Как работает", callback_data="how")],
        [InlineKeyboardButton(text="📖 Команды", callback_data="cmds")],
        [InlineKeyboardButton(text="🛠 Модерация", callback_data="mod")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton(text="ℹ️ Инфо", callback_data="info")],
        [InlineKeyboardButton(text="➕ Добавить в чат",
                              url=f"https://t.me/{_BOT_USERNAME}?startgroup=true")],
    ])


# ====================== ПОДПИСКА MIDDLEWARE ======================
async def is_subscribed(user_id):
    try:
        m = await bot.get_chat_member(CHANNEL, user_id)
        return m.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception:
        return False


class SubMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        chat = getattr(event, "chat", None)
        if chat is None and isinstance(event, CallbackQuery):
            chat = event.message.chat if event.message else None
        if chat is None or chat.type != "private":
            return await handler(event, data)

        user = event.from_user
        if not user:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        checked = subs_checked.get(user.id)
        now = int(time.time())
        if checked and (now - checked) < 3600:
            return await handler(event, data)

        if await is_subscribed(user.id):
            subs_checked[user.id] = now
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer(
                f"🔒 Подпишись на {CHANNEL}, чтобы пользоваться ботом.",
                reply_markup=sub_kb(),
            )
        else:
            await event.answer("Подпишись сначала!", show_alert=True)
        return None


dp.message.middleware(SubMiddleware())
dp.callback_query.middleware(SubMiddleware())


# ====================== /start ======================
WELCOME = (
    "👋 Здравствуйте!\n\n"
    "Вас приветствует <b>Diplomat</b> — бот-менеджер для Telegram-чатов.\n\n"
    "Помогу с модерацией, наказаниями и порядком.\n\n"
    "📖 Все команды: /help"
)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "👋 Привет!\n\nПодпишись на канал, чтобы пользоваться ботом:",
            reply_markup=sub_kb(),
        )
        return
    if message.chat.type == "private":
        await message.answer(WELCOME, reply_markup=menu_kb())
    else:
        await message.reply("👋 Diplomat на связи. 📖 /help")


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery):
    if not await is_subscribed(call.from_user.id):
        await call.answer("❌ Ты ещё не подписан!", show_alert=True)
        return
    subs_checked[call.from_user.id] = int(time.time())
    await call.message.edit_text(WELCOME, reply_markup=menu_kb())


@dp.callback_query(F.data == "how")
async def cb_how(call: CallbackQuery):
    await call.message.edit_text(
        "🧭 <b>Как работает Diplomat</b>\n\n"
        "1. Добавь бота в чат\n"
        "2. Выдай права админа:\n"
        "   • Удаление сообщений\n"
        "   • Блокировка участников\n"
        "   • Закрепление сообщений\n"
        "3. Готово — бот следит за порядком!\n\n"
        "📖 Все команды: /help",
        reply_markup=menu_kb(),
    )


@dp.callback_query(F.data == "cmds")
async def cb_cmds(call: CallbackQuery):
    await call.message.edit_text(HELP_TEXT, reply_markup=menu_kb())


@dp.callback_query(F.data == "mod")
async def cb_mod(call: CallbackQuery):
    await call.message.edit_text(
        "🛠 <b>Модерация</b>\n\n"
        "/purge N — удалить N сообщений\n"
        "/del — удалить (reply)\n"
        "/pin /unpin — закрепить/открепить\n"
        "/kick /ban /mute /warn\n"
        "/report — жалоба админам\n"
        "/lock /unlock — закрыть/открыть чат",
        reply_markup=menu_kb(),
    )


@dp.callback_query(F.data == "settings")
async def cb_settings(call: CallbackQuery):
    await call.message.edit_text(
        "⚙️ <b>Настройки</b>\n\n"
        "/antimat on|off\n"
        "/antilink on|off\n"
        "/anticaps on|off\n"
        "/antiflood on|off\n"
        "/welcome on|off | /setwelcome текст\n"
        "/goodbye on|off | /setgoodbye текст\n"
        "/setrules текст | /rules\n"
        "/whitelist /unwhitelist\n"
        "/settings — всё сразу\n"
        "/reset — сброс",
        reply_markup=menu_kb(),
    )


@dp.callback_query(F.data == "info")
async def cb_info(call: CallbackQuery):
    await call.message.edit_text(
        "ℹ️ <b>Инфо</b>\n\n"
        "/help — все команды\n"
        "/id — твой ID\n"
        "/chat — инфо о чате\n"
        "/user — инфо о юзере\n"
        "/admins — список админов\n"
        "/ping /uptime — состояние\n"
        "/stats — статистика\n"
        "/top — топ активных",
        reply_markup=menu_kb(),
    )


# ====================== /help ======================
HELP_TEXT = (
    "🛡 <b>Diplomat — команды</b>\n\n"

    "🔨 <b>Наказания:</b>\n"
    "<code>/ban [срок] [причина]</code> — 1d, 7d, 1w\n"
    "/unban — разбан\n"
    "/kick — кик\n"
    "/softban — softban (удалить+кик)\n"
    "<code>/mute [срок]</code> — 5m, 1h, 1d\n"
    "/unmute — размут\n"
    "<code>/ro [срок]</code> — только чтение\n"
    "/warn — варн (3 = мут)\n"
    "/unwarn /warns /warns_top /resetwarns\n\n"

    "👑 <b>Роли:</b>\n"
    "/promote — повысить\n"
    "/demote — понизить\n\n"

    "📝 <b>Заметки:</b>\n"
    "<code>/note текст</code> — reply\n"
    "/delnote /history\n\n"

    "⚙️ <b>Настройки:</b>\n"
    "/setrules | /rules\n"
    "/antimat on|off | /antimat_add | /antimat_del | /antimat_list\n"
    "/antilink on|off\n"
    "/anticaps on|off\n"
    "/antiflood on|off\n"
    "/welcome on|off | /setwelcome\n"
    "/goodbye on|off | /setgoodbye\n"
    "/whitelist /unwhitelist\n"
    "/settings /reset\n\n"

    "🧹 <b>Модерация:</b>\n"
    "/purge N | /del | /pin | /unpin | /report\n"
    "/lock /unlock\n\n"

    "ℹ️ <b>Инфо:</b>\n"
    "/help /id /chat /user /admins\n"
    "/ping /uptime /stats /top\n\n"

    "🎲 <b>Развлечения:</b>\n"
    "/dice /coin /roll /8ball /slap /hug\n\n"

    "💡 Срок: <code>s/m/h/d/w</code> (сек/мин/час/день/нед)\n"
    "💡 Цель: reply, ID или @username"
)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


# ====================== BAN ======================
@dp.message(Command("ban"), IsAdmin())
async def cmd_ban(message: Message):
    if message.chat.type == "private":
        return
    if not await ensure_rights(bot, message.chat.id):
        return await message.reply(NOT_ENOUGH_RIGHTS)
    args = message.text.split()[1:]
    uid, uname, rest = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)

    dur = parse_duration_str(message.text) or timedelta(days=1)
    until = datetime.now() + dur
    reason = "не указана"
    if rest:
        non_dur = [a for a in rest if not re.match(r"^\d+[smhdw]$", a.lower())]
        if non_dur:
            reason = " ".join(non_dur)

    try:
        await bot.ban_chat_member(message.chat.id, target.id, until_date=until)
    except Exception as e:
        return await message.reply(f"❌ Не смог забанить: {e}")

    await message.answer(
        f"🔨 <b>{target.full_name}</b> забанен\n"
        f"⏱ Срок: {fmt_duration(dur)}\n"
        f"📝 Причина: {reason}"
    )


@dp.message(Command("tempban"), IsAdmin())
async def cmd_tempban(message: Message):
    await cmd_ban(message)


@dp.message(Command("softban"), IsAdmin())
async def cmd_softban(message: Message):
    if message.chat.type == "private":
        return
    if not await ensure_rights(bot, message.chat.id):
        return await message.reply(NOT_ENOUGH_RIGHTS)
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.answer(f"🧹 Softban: <b>{target.full_name}</b>")
    except Exception as e:
        await message.reply(f"❌ {e}")


@dp.message(Command("unban"), IsAdmin())
async def cmd_unban(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    try:
        await bot.unban_chat_member(message.chat.id, target.id, only_if_banned=True)
        await message.answer(f"✅ <b>{target.full_name}</b> разбанен")
    except Exception as e:
        await message.reply(f"❌ {e}")


# ====================== MUTE ======================
@dp.message(Command("mute"), IsAdmin())
async def cmd_mute(message: Message):
    if message.chat.type == "private":
        return
    if not await ensure_rights(bot, message.chat.id):
        return await message.reply(NOT_ENOUGH_RIGHTS)
    args = message.text.split()[1:]
    uid, uname, rest = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)

    dur = parse_duration_str(message.text) or timedelta(hours=1)
    until = datetime.now() + dur
    reason = "не указана"
    if rest:
        non_dur = [a for a in rest if not re.match(r"^\d+[smhdw]$", a.lower())]
        if non_dur:
            reason = " ".join(non_dur)

    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=MUTE_PERMS, until_date=until,
        )
    except Exception as e:
        return await message.reply(f"❌ Не смог замутить: {e}")

    await message.answer(
        f"🔇 <b>{target.full_name}</b> замучен\n"
        f"⏱ Срок: {fmt_duration(dur)}\n"
        f"📝 Причина: {reason}"
    )


@dp.message(Command("ro"), IsAdmin())
async def cmd_ro(message: Message):
    if message.chat.type == "private":
        return
    if not await ensure_rights(bot, message.chat.id):
        return await message.reply(NOT_ENOUGH_RIGHTS)
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    dur = parse_duration_str(message.text) or timedelta(hours=1)
    until = datetime.now() + dur
    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=READONLY_PERMS, until_date=until,
        )
        await message.answer(
            f"📖 <b>{target.full_name}</b> только-чтение на {fmt_duration(dur)}"
        )
    except Exception as e:
        await message.reply(f"❌ {e}")


@dp.message(Command("unmute"), IsAdmin())
async def cmd_unmute(message: Message):
    if message.chat.type == "private":
        return
    if not await ensure_rights(bot, message.chat.id):
        return await message.reply(NOT_ENOUGH_RIGHTS)
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id, permissions=UNMUTE_PERMS
        )
        await message.answer(f"🔊 <b>{target.full_name}</b> размучен")
    except Exception as e:
        await message.reply(f"❌ {e}")


# ====================== KICK ======================
@dp.message(Command("kick"), IsAdmin())
async def cmd_kick(message: Message):
    if message.chat.type == "private":
        return
    if not await ensure_rights(bot, message.chat.id):
        return await message.reply(NOT_ENOUGH_RIGHTS)
    args = message.text.split()[1:]
    uid, uname, rest = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    reason = " ".join(rest) if rest else "не указана"
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.answer(
            f"👢 <b>{target.full_name}</b> кикнут\n"
            f"📝 Причина: {reason}"
        )
    except Exception as e:
        await message.reply(f"❌ {e}")


# ====================== WARN ======================
@dp.message(Command("warn"), IsAdmin())
async def cmd_warn(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, rest = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)

    reason = " ".join(rest) if rest else "не указана"
    key = (message.chat.id, target.id)
    warns_store[key] = warns_store.get(key, 0) + 1
    count = warns_store[key]

    if count >= WARN_LIMIT:
        dur = timedelta(hours=1)
        until = datetime.now() + dur
        try:
            await bot.restrict_chat_member(
                message.chat.id, target.id,
                permissions=MUTE_PERMS, until_date=until,
            )
            warns_store.pop(key, None)
            await message.answer(
                f"⚠️ <b>{target.full_name}</b> получил {count}-й варн\n"
                f"🔇 Авто-мут на 1 час"
            )
        except Exception:
            await message.answer(f"⚠️ {count}-й варн (не смог замутить)")
    else:
        await message.answer(
            f"⚠️ <b>{target.full_name}</b> получил варн ({count}/{WARN_LIMIT})\n"
            f"📝 Причина: {reason}"
        )


@dp.message(Command("unwarn"), IsAdmin())
async def cmd_unwarn(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    key = (message.chat.id, target.id)
    n = max(0, warns_store.get(key, 0) - 1)
    if n == 0:
        warns_store.pop(key, None)
    else:
        warns_store[key] = n
    await message.answer(f"✅ Снят варн. Осталось: {n}")


@dp.message(Command("warns"), IsAdmin())
async def cmd_warns(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    count = warns_store.get((message.chat.id, target.id), 0)
    await message.answer(f"⚠️ <b>{target.full_name}</b>: {count}/{WARN_LIMIT}")


@dp.message(Command("warns_top"), IsAdmin())
async def cmd_warns_top(message: Message):
    if message.chat.type == "private":
        return
    rows = [(uid, n) for (c, uid), n in warns_store.items() if c == message.chat.id]
    rows.sort(key=lambda x: -x[1])
    if not rows:
        return await message.reply("Варнов ни у кого нет ✨")
    lines = ["🏆 <b>Топ по варнам:</b>\n"]
    for i, (uid, n) in enumerate(rows[:10], 1):
        lines.append(f"{i}. <code>{uid}</code> — {n}")
    await message.answer("\n".join(lines))


@dp.message(Command("resetwarns"), IsAdmin())
async def cmd_resetwarns(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    warns_store.pop((message.chat.id, target.id), None)
    await message.answer(f"♻️ Варны <b>{target.full_name}</b> сброшены")


# ====================== РОЛИ ======================
@dp.message(Command("promote"), IsAdmin())
async def cmd_promote(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    try:
        await bot.promote_chat_member(
            message.chat.id, target.id,
            can_manage_chat=True, can_delete_messages=True,
            can_restrict_members=True, can_invite_users=True,
            can_pin_messages=True,
        )
        await message.answer(f"👑 <b>{target.full_name}</b> повышен")
    except Exception as e:
        await message.reply(f"❌ {e}")


@dp.message(Command("demote"), IsAdmin())
async def cmd_demote(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    try:
        await bot.promote_chat_member(
            message.chat.id, target.id,
            can_manage_chat=False, can_delete_messages=False,
            can_restrict_members=False, can_invite_users=False,
            can_pin_messages=False, can_change_info=False,
        )
        await message.answer(f"⬇️ <b>{target.full_name}</b> понижен")
    except Exception as e:
        await message.reply(f"❌ {e}")


# ====================== ЗАМЕТКИ ======================
@dp.message(Command("note"), IsAdmin())
async def cmd_note(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, rest = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    text = " ".join(rest) if rest else ""
    if not text:
        return await message.reply("Напиши текст заметки.")
    notes_store[(message.chat.id, target.id)] = text
    await message.answer(f"📝 Заметка на <b>{target.full_name}</b> сохранена")


@dp.message(Command("delnote"), IsAdmin())
async def cmd_delnote(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    notes_store.pop((message.chat.id, target.id), None)
    await message.answer("🗑 Заметка удалена")


@dp.message(Command("history"), IsAdmin())
async def cmd_history(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    warns = warns_store.get((message.chat.id, target.id), 0)
    note = notes_store.get((message.chat.id, target.id), "—")
    await message.answer(
        f"📋 <b>История</b> {target.full_name}\n"
        f"• Варнов: {warns}/{WARN_LIMIT}\n"
        f"• Заметка: {note}"
    )


# ====================== ПРАВИЛА ======================
@dp.message(Command("setrules"), IsAdmin())
async def cmd_setrules(message: Message):
    if message.chat.type == "private":
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        return await message.reply("Напиши текст правил.")
    rules_store[message.chat.id] = text
    await message.answer("✅ Правила обновлены")


@dp.message(Command("rules"))
async def cmd_rules(message: Message):
    text = rules_store.get(message.chat.id)
    if not text:
        return await message.reply("📜 Правила не установлены. /setrules")
    await message.answer(f"📜 <b>Правила чата</b>\n\n{text}")


# ====================== НАСТРОЙКИ (вкл/выкл) ======================
def _toggle_cmd(name, key):
    async def handler(message: Message):
        if message.chat.type == "private":
            return
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return
        arg = message.text.partition(" ")[2].strip().lower()
        s = get_settings(message.chat.id)
        if arg == "on":
            update_setting(message.chat.id, key, 1)
            await message.reply(f"✅ {name} ВКЛ")
        elif arg == "off":
            update_setting(message.chat.id, key, 0)
            await message.reply(f"❌ {name} ВЫКЛ")
        else:
            await message.reply(f"{name}: {'вкл' if s[key] else 'выкл'}")
    return handler


for _name, _key, _aliases in [
    ("Антимат", "antimat", ["antimat"]),
    ("Антилинк", "antilink", ["antilink"]),
    ("Антикапс", "anticaps", ["anticaps"]),
    ("Антифлуд", "antiflood", ["antiflood"]),
    ("Приветствие", "welcome", ["welcome"]),
    ("Прощание", "goodbye", ["goodbye"]),
]:
    h = _toggle_cmd(_name, _key)
    for a in _aliases:
        dp.message.register(h, Command(a))


# ====================== СТОП-СЛОВА ======================
@dp.message(Command("antimat_add"), IsAdmin())
async def cmd_antimat_add(message: Message):
    if message.chat.type == "private":
        return
    word = message.text.partition(" ")[2].strip().lower()
    if not word:
        return await message.reply("Напиши слово.")
    bad_words_store.setdefault(message.chat.id, set()).add(word)
    await message.answer(f"✅ Добавлено: <code>{word}</code>")


@dp.message(Command("antimat_del"), IsAdmin())
async def cmd_antimat_del(message: Message):
    if message.chat.type == "private":
        return
    word = message.text.partition(" ")[2].strip().lower()
    if not word:
        return await message.reply("Напиши слово.")
    bad_words_store.setdefault(message.chat.id, set()).discard(word)
    await message.answer(f"🗑 Удалено: <code>{word}</code>")


@dp.message(Command("antimat_list"), IsAdmin())
async def cmd_antimat_list(message: Message):
    if message.chat.type == "private":
        return
    words = list(bad_words_store.get(message.chat.id, set()))
    if not words:
        return await message.reply("Своих стоп-слов нет.")
    await message.answer(
        "🚫 Свои стоп-слова:\n" + ", ".join(f"<code>{w}</code>" for w in words)
    )


# ====================== WELCOME/GOODBYE TEXT ======================
@dp.message(Command("setwelcome"), IsAdmin())
async def cmd_setwelcome(message: Message):
    if message.chat.type == "private":
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        return await message.reply("Доступно: {mention}, {name}, {chat}")
    update_setting(message.chat.id, "welcome_text", text)
    await message.answer("✅ Приветствие обновлено")


@dp.message(Command("setgoodbye"), IsAdmin())
async def cmd_setgoodbye(message: Message):
    if message.chat.type == "private":
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        return await message.reply("Доступно: {name}, {chat}")
    update_setting(message.chat.id, "goodbye_text", text)
    await message.answer("✅ Прощание обновлено")


# ====================== WHITELIST ======================
@dp.message(Command("whitelist"), IsAdmin())
async def cmd_whitelist(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    whitelist_store.setdefault(message.chat.id, set()).add(target.id)
    await message.answer(f"✅ <b>{target.full_name}</b> в белом списке")


@dp.message(Command("unwhitelist"), IsAdmin())
async def cmd_unwhitelist(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        return await message.reply(NO_TARGET)
    whitelist_store.setdefault(message.chat.id, set()).discard(target.id)
    await message.answer(f"🗑 <b>{target.full_name}</b> убран из белого списка")


# ====================== SETTINGS / RESET ======================
@dp.message(Command("settings"), IsAdmin())
async def cmd_settings(message: Message):
    if message.chat.type == "private":
        return
    s = get_settings(message.chat.id)
    f = lambda v: "✅" if v else "❌"
    await message.reply(
        f"⚙️ <b>Настройки чата</b>\n\n"
        f"Антимат: {f(s['antimat'])}\n"
        f"Антифлуд: {f(s['antiflood'])}\n"
        f"Антилинк: {f(s['antilink'])}\n"
        f"Антикапс: {f(s['anticaps'])}\n"
        f"Приветствие: {f(s['welcome'])}\n"
        f"Прощание: {f(s['goodbye'])}\n"
        f"Правила: {'✅' if rules_store.get(message.chat.id) else '❌'}\n"
        f"Стоп-слов: {len(bad_words_store.get(message.chat.id, set()))}\n"
        f"Фильтров: {len(filters_store.get(message.chat.id, set()))}"
    )


@dp.message(Command("reset"), IsAdmin())
async def cmd_reset(message: Message):
    if message.chat.type == "private":
        return
    settings_store.pop(message.chat.id, None)
    bad_words_store.pop(message.chat.id, None)
    filters_store.pop(message.chat.id, None)
    rules_store.pop(message.chat.id, None)
    await message.answer("♻️ Настройки сброшены")


# ====================== PING / UPTIME / STATS ======================
@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    t = time.time()
    m = await message.reply("🏓 Пингую...")
    ms = int((time.time() - t) * 1000)
    await m.edit_text(f"🏓 Понг! {ms} мс")


@dp.message(Command("uptime"))
async def cmd_uptime(message: Message):
    await message.reply(f"⏱ Аптайм: <b>{uptime_str()}</b>")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    chat_users_warns = 0
    chat_total_warns = 0
    for (c, _), n in warns_store.items():
        if c == message.chat.id:
            chat_users_warns += 1
            chat_total_warns += n
    await message.reply(
        f"📊 <b>Статистика чата</b>\n\n"
        f"⏱ Аптайм бота: {uptime_str()}\n"
        f"⚠️ Юзеров с варнами: {chat_users_warns}\n"
        f"📈 Всего варнов: {chat_total_warns}\n"
        f"📝 Заметок: {sum(1 for (c, _) in notes_store if c == message.chat.id)}\n"
        f"🚫 Стоп-слов: {len(bad_words_store.get(message.chat.id, set()))}"
    )


@dp.message(Command("top"))
async def cmd_top(message: Message):
    rows = [(uid, n) for (c, uid), n in activity_store.items() if c == message.chat.id]
    rows.sort(key=lambda x: -x[1])
    if not rows:
        return await message.reply("Активности нет.")
    lines = ["🏆 <b>Топ активных:</b>\n"]
    for i, (uid, n) in enumerate(rows[:10], 1):
        lines.append(f"{i}. <code>{uid}</code> — {n} сообщ.")
    await message.answer("\n".join(lines))


# ====================== ИНФО ======================
@dp.message(Command("id"))
async def cmd_id(message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return await message.reply(f"🆔 <code>{u.id}</code>")
    await message.reply(
        f"🆔 Твой ID: <code>{message.from_user.id}</code>\n"
        f"💬 ID чата: <code>{message.chat.id}</code>"
    )


@dp.message(Command("chat"))
async def cmd_chat(message: Message):
    if message.chat.type == "private":
        return
    try:
        count = await bot.get_chat_member_count(message.chat.id)
    except Exception:
        count = "—"
    await message.reply(
        f"💬 <b>{message.chat.title}</b>\n"
        f"🆔 <code>{message.chat.id}</code>\n"
        f"👥 {count}"
    )


@dp.message(Command("user"))
async def cmd_user(message: Message):
    u = message.from_user
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
    warns = warns_store.get((message.chat.id, u.id), 0)
    note = notes_store.get((message.chat.id, u.id), "—")
    await message.reply(
        f"👤 <b>{u.full_name}</b>\n"
        f"🆔 <code>{u.id}</code>\n"
        f"🔗 @{u.username or '—'}\n"
        f"⚠️ Варнов: {warns}\n"
        f"📝 Заметка: {note}"
    )


@dp.message(Command("admins"))
async def cmd_admins(message: Message):
    if message.chat.type == "private":
        return
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
    except Exception:
        return await message.reply("Не удалось получить список.")
    lines = ["👑 <b>Админы:</b>\n"]
    for a in admins:
        if a.user.is_bot:
            continue
        tag = "👑" if a.status == ChatMemberStatus.CREATOR else "🛡"
        lines.append(f"{tag} {a.user.full_name}")
    await message.reply("\n".join(lines))


# ====================== МОДЕРАЦИЯ ======================
@dp.message(Command("purge"), IsAdmin())
async def cmd_purge(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    if not args or not args[0].isdigit():
        return await message.reply("❌ /purge N — удалить N сообщений")
    n = min(int(args[0]), 100)
    deleted = 0
    async for msg in bot.get_chat_history(message.chat.id, limit=n + 1):
        try:
            await bot.delete_message(message.chat.id, msg.message_id)
            deleted += 1
        except Exception:
            pass
    notice = await bot.send_message(message.chat.id, f"🧹 Удалено {deleted}")
    asyncio.create_task(_autodelete(message.chat.id, notice.message_id, 3))


@dp.message(Command("del"), IsAdmin())
async def cmd_del(message: Message):
    if not message.reply_to_message:
        return await message.reply("❌ Ответь на сообщение.")
    try:
        await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
        await message.delete()
    except Exception as e:
        await message.reply(f"❌ {e}")


@dp.message(Command("pin"), IsAdmin())
async def cmd_pin(message: Message):
    if not message.reply_to_message:
        return await message.reply("❌ Ответь на сообщение.")
    try:
        await bot.pin_chat_message(
            message.chat.id, message.reply_to_message.message_id,
            disable_notification=True,
        )
        await message.delete()
    except Exception as e:
        await message.reply(f"❌ {e}")


@dp.message(Command("unpin"), IsAdmin())
async def cmd_unpin(message: Message):
    try:
        await bot.unpin_chat_message(message.chat.id)
        await message.answer("📌 Откреплено")
    except Exception as e:
        await message.reply(f"❌ {e}")


@dp.message(Command("lock"), IsAdmin())
async def cmd_lock(message: Message):
    if message.chat.type == "private":
        return
    try:
        await bot.set_chat_permissions(
            message.chat.id, ChatPermissions(can_send_messages=False)
        )
        await message.answer("🔒 Чат закрыт")
    except Exception as e:
        await message.reply(f"❌ {e}")


@dp.message(Command("unlock"), IsAdmin())
async def cmd_unlock(message: Message):
    if message.chat.type == "private":
        return
    try:
        await bot.set_chat_permissions(message.chat.id, UNMUTE_PERMS)
        await message.answer("🔓 Чат открыт")
    except Exception as e:
        await message.reply(f"❌ {e}")


@dp.message(Command("report"))
async def cmd_report(message: Message):
    if message.chat.type == "private":
        return
    if not message.reply_to_message:
        return await message.reply("❌ Ответь на сообщение нарушителя.")
    reason = message.text.partition(" ")[2].strip() or "без причины"
    reported = message.reply_to_message.from_user
    admins = []
    try:
        for a in await bot.get_chat_administrators(message.chat.id):
            if not a.user.is_bot:
                admins.append(a.user.mention_html())
    except Exception:
        pass
    text = (
        f"🚨 Жалоба от {message.from_user.mention_html()}\n"
        f"👤 На: {reported.mention_html()}\n"
        f"📝 {reason}"
    )
    if admins:
        text += "\n📣 " + " ".join(admins[:5])
    await message.reply(text)


# ====================== РАЗВЛЕЧЕНИЯ ======================
@dp.message(Command("roll"))
async def cmd_roll(message: Message):
    m = re.search(r"(\d+)", message.text)
    n = int(m.group(1)) if m else 100
    if n < 2 or n > 1_000_000:
        return await message.reply("Число от 2 до 1000000.")
    await message.reply(f"🎯 {random.randint(1, n)}")


@dp.message(Command("coin"))
async def cmd_coin(message: Message):
    await message.reply(random.choice(["🪙 Орёл", "🪙 Решка"]))


@dp.message(Command("dice"))
async def cmd_dice(message: Message):
    await message.answer_dice(emoji="🎲")


@dp.message(Command("8ball"))
async def cmd_8ball(message: Message):
    q = message.text.partition(" ")[2].strip()
    if not q:
        return await message.reply("Задай вопрос.")
    answers = [
        "Да ✅", "Нет ❌", "Возможно 🤔", "Определённо да!",
        "Спроси позже...", "Не могу сказать", "Скорее да", "Скорее нет",
        "100% да", "Даже не думай",
    ]
    await message.reply(f"🎱 {random.choice(answers)}")


@dp.message(Command("slap"))
async def cmd_slap(message: Message):
    if not message.reply_to_message:
        return await message.reply("Ответь реплаем.")
    a = message.from_user.first_name
    b = message.reply_to_message.from_user.first_name
    await message.reply(random.choice([
        f"{a} отвешивает оплеуху {b} 👋",
        f"{a} отправляет {b} в нокаут 🥊",
        f"{a} кидает тапок в {b} 🥿",
    ]))


@dp.message(Command("hug"))
async def cmd_hug(message: Message):
    if not message.reply_to_message:
        return await message.reply("Ответь реплаем.")
    a = message.from_user.first_name
    b = message.reply_to_message.from_user.first_name
    await message.reply(random.choice([
        f"{a} крепко обнимает {b} 🤗",
        f"{a} дарит тёплые объятия {b} 🫂",
    ]))


# ====================== СТАРЫЕ ФИЛЬТРЫ (совместимость) ======================
@dp.message(Command("addfilter"), IsAdmin())
async def cmd_addfilter(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    if not args:
        return await message.reply("❌ /addfilter слово")
    word = args[0].lower()
    filters_store.setdefault(message.chat.id, set()).add(word)
    await message.answer(f"🚫 Слово <b>{word}</b> добавлено в фильтр")


@dp.message(Command("delfilter"), IsAdmin())
async def cmd_delfilter(message: Message):
    if message.chat.type == "private":
        return
    args = message.text.split()[1:]
    if not args:
        return await message.reply("❌ /delfilter слово")
    word = args[0].lower()
    filters_store.setdefault(message.chat.id, set()).discard(word)
    await message.answer(f"✅ Слово <b>{word}</b> удалено")


@dp.message(Command("filters"))
async def cmd_filters(message: Message):
    if message.chat.type == "private":
        return
    words = filters_store.get(message.chat.id, set())
    if not words:
        return await message.reply("🚫 Фильтров нет")
    await message.answer("🚫 <b>Фильтры:</b>\n" + "\n".join(f"• {w}" for w in words))


# ====================== СОБЫТИЯ ======================
DEFAULT_WELCOME = (
    "👋 Добро пожаловать, {mention}!\n\n"
    "📖 /help — команды\n"
    "📜 /rules — правила"
)
DEFAULT_GOODBYE = "👋 {name} покинул(а) чат."


@dp.message(F.new_chat_members)
async def welcome_evt(message: Message):
    s = get_settings(message.chat.id)
    if not s["welcome"]:
        return
    template = s["welcome_text"] or DEFAULT_WELCOME
    for user in message.new_chat_members:
        if user.id == bot.id:
            continue
        try:
            await message.answer(template.format(
                mention=user.mention_html(),
                name=user.full_name,
                chat=message.chat.title or "чат",
            ))
        except Exception:
            log.exception("welcome failed")


@dp.message(F.left_chat_member)
async def goodbye_evt(message: Message):
    s = get_settings(message.chat.id)
    if not s["goodbye"]:
        return
    u = message.left_chat_member
    if not u or u.id == bot.id:
        return
    template = s["goodbye_text"] or DEFAULT_GOODBYE
    try:
        await message.answer(template.format(
            name=u.full_name,
            chat=message.chat.title or "чат",
        ))
    except Exception:
        log.exception("goodbye failed")


@dp.my_chat_member()
async def on_bot_added(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    if event.new_chat_member.status == "member":
        try:
            me = await bot.get_chat_member(chat.id, bot.id)
            if me.status != "administrator":
                await bot.send_message(
                    chat.id,
                    "👋 Спасибо, что добавили!\n\n"
                    "⚠️ Выдайте мне права администратора:\n"
                    "• Удаление сообщений\n"
                    "• Блокировка участников\n"
                    "• Закрепление сообщений\n\n"
                    "📖 Все команды: /help",
                )
        except Exception:
            pass


# ====================== АВТОМОДЕРАЦИЯ ======================
@dp.message(F.text | F.caption, ~F.text.startswith("/"))
async def automod(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    if not message.from_user or message.from_user.is_bot:
        return

    # запоминаем username
    if message.from_user.username:
        known_users[(message.chat.id, message.from_user.username.lower())] = message.from_user.id

    # активность
    activity_store[(message.chat.id, message.from_user.id)] = \
        activity_store.get((message.chat.id, message.from_user.id), 0) + 1

    # админы и whitelist не трогаем
    if await is_admin(bot, message.chat.id, message.from_user.id):
        return
    if message.from_user.id in whitelist_store.get(message.chat.id, set()):
        return

    s = get_settings(message.chat.id)
    text = extract_text(message)

    # кастомные фильтры (старые)
    filters = filters_store.get(message.chat.id, set())
    if text and filters:
        low = text.lower()
        for w in filters:
            if w in low:
                try:
                    await message.delete()
                    await _warn_msg(
                        message,
                        f"🚫 {message.from_user.mention_html()}, это слово запрещено!",
                    )
                except Exception:
                    pass
                return

    # антимат
    if s["antimat"] and text:
        custom = list(bad_words_store.get(message.chat.id, set()))
        root = contains_mat(text, custom)
        if root:
            try:
                await message.delete()
            except Exception:
                pass
            await _warn_msg(message, f"🚫 {message.from_user.mention_html()}, мат!")
            return

    # антилинк
    if s["antilink"] and text and contains_link(text):
        try:
            await message.delete()
        except Exception:
            pass
        await _warn_msg(message, f"🔗 {message.from_user.mention_html()}, ссылки запрещены!")
        return

    # антикапс
    if s["anticaps"] and text and is_caps(text):
        try:
            await message.delete()
        except Exception:
            pass
        await _warn_msg(message, f"🔠 {message.from_user.mention_html()}, не капси!")
        return

    # антифлуд
    if s["antiflood"]:
        key = (message.chat.id, message.from_user.id)
        now = asyncio.get_event_loop().time()
        lst = flood_log[key]
        lst.append(now)
        lst[:] = [t for t in lst if now - t < FLOOD_WINDOW]
        if len(lst) >= FLOOD_LIMIT:
            try:
                await message.delete()
            except Exception:
                pass
            until = datetime.now() + timedelta(minutes=FLOOD_MUTE_MINUTES)
            try:
                await bot.restrict_chat_member(
                    message.chat.id, message.from_user.id,
                    permissions=MUTE_PERMS, until_date=until,
                )
                await _warn_msg(
                    message,
                    f"🚫 {message.from_user.mention_html()} мут {FLOOD_MUTE_MINUTES} мин.",
                )
            except Exception:
                log.exception("flood mute failed")
            finally:
                flood_log[key] = []


# ====================== ЗАПУСК ======================
async def main():
    global _BOT_USERNAME
    me = await bot.get_me()
    _BOT_USERNAME = me.username
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Diplomat запущен ✅")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
