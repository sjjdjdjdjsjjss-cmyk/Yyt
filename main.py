import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, ChatPermissions,
    InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated,
)

from db import (
    init_db, save_chat, add_punishment, get_warns, clear_warns,
)
from utils import (
    parse_duration, human_duration,
    parse_target, resolve_user, is_admin,
)


# ====================== НАСТРОЙКИ ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СВОЙ_ТОКЕН")
CHANNEL = "@karmaproj"
WARN_LIMIT = 3

logging.basicConfig(level=logging.INFO)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

MUTE_PERMS = ChatPermissions(can_send_messages=False)
UNMUTE_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)

_BOT_USERNAME = "DiplomatBot"


# ====================== КЛАВИАТУРЫ ======================
def sub_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться", url="https://t.me/karmaproj")],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
    ])


def menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧭 Как работает", callback_data="how")],
        [InlineKeyboardButton(text="📖 Команды админа", callback_data="cmds")],
        [InlineKeyboardButton(text="➕ Добавить в чат",
                              url=f"https://t.me/{_BOT_USERNAME}?startgroup=true")],
    ])


# ====================== ПОДПИСКА ======================
async def is_subscribed(user_id):
    try:
        m = await bot.get_chat_member(CHANNEL, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False


# ====================== /start ======================
WELCOME = (
    "👋 Здравствуйте!\n\n"
    "Вас приветствует <b>Diplomat</b> — бот-менеджер для Telegram-чатов.\n\n"
    "Помогу с модерацией, наказаниями и порядком."
)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "👋 Привет!\n\nПодпишись на канал, чтобы пользоваться ботом:",
            reply_markup=sub_kb(),
        )
        return
    await message.answer(WELCOME, reply_markup=menu_kb())


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery):
    if not await is_subscribed(call.from_user.id):
        await call.answer("❌ Ты ещё не подписан!", show_alert=True)
        return
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
        "3. Готово — бот следит за порядком!",
        reply_markup=menu_kb(),
    )


@dp.callback_query(F.data == "cmds")
async def cb_cmds(call: CallbackQuery):
    await call.message.edit_text(
        "📖 <b>Команды админа</b>\n\n"
        "<b>Наказания:</b>\n"
        "/ban @user 7 спам — бан на 7 дней\n"
        "/mute @user 30 флуд — мут на 30 минут\n"
        "/kick @user — кик\n"
        "/warn @user мат — варн (3 = кик)\n"
        "/unban /unmute /unwarn — снять\n\n"
        "Работает через reply, @username или ID.\n"
        "Срок 0 = навсегда.",
        reply_markup=menu_kb(),
    )


# ====================== BAN ======================
@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return
    args = message.text.split()[1:]
    uid, uname, rest = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        await message.reply("❌ Укажи юзера: reply, @username или ID.")
        return

    days_arg = rest[0] if rest else "0"
    reason = " ".join(rest[1:]) if len(rest) > 1 else "не указана"
    until = parse_duration(days_arg, "days")

    try:
        await bot.ban_chat_member(message.chat.id, target.id, until_date=until)
    except Exception as e:
        await message.reply(f"❌ Не смог забанить: {e}")
        return

    await add_punishment(message.chat.id, target.id,
                         message.from_user.id, "ban", until, reason)
    await message.answer(
        f"🔨 <b>{target.full_name}</b> забанен\n"
        f"⏱ Срок: {human_duration(until, 'days')}\n"
        f"📝 Причина: {reason}",
    )


# ====================== MUTE ======================
@dp.message(Command("mute"))
async def cmd_mute(message: Message):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return
    args = message.text.split()[1:]
    uid, uname, rest = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        await message.reply("❌ Укажи юзера: reply, @username или ID.")
        return

    mins_arg = rest[0] if rest else "0"
    reason = " ".join(rest[1:]) if len(rest) > 1 else "не указана"
    until = parse_duration(mins_arg, "minutes")

    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=MUTE_PERMS, until_date=until,
        )
    except Exception as e:
        await message.reply(f"❌ Не смог замутить: {e}")
        return

    await add_punishment(message.chat.id, target.id,
                         message.from_user.id, "mute", until, reason)
    await message.answer(
        f"🔇 <b>{target.full_name}</b> замучен\n"
        f"⏱ Срок: {human_duration(until, 'minutes')}\n"
        f"📝 Причина: {reason}",
    )


# ====================== KICK ======================
@dp.message(Command("kick"))
async def cmd_kick(message: Message):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return
    args = message.text.split()[1:]
    uid, uname, rest = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        await message.reply("❌ Укажи юзера.")
        return

    reason = " ".join(rest) if rest else "не указана"
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await bot.unban_chat_member(message.chat.id, target.id)
    except Exception as e:
        await message.reply(f"❌ Не смог кикнуть: {e}")
        return

    await add_punishment(message.chat.id, target.id,
                         message.from_user.id, "kick", None, reason)
    await message.answer(
        f"👢 <b>{target.full_name}</b> кикнут\n"
        f"📝 Причина: {reason}",
    )


# ====================== WARN ======================
@dp.message(Command("warn"))
async def cmd_warn(message: Message):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return
    args = message.text.split()[1:]
    uid, uname, rest = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        await message.reply("❌ Укажи юзера.")
        return

    reason = " ".join(rest) if rest else "не указана"
    await add_punishment(message.chat.id, target.id,
                         message.from_user.id, "warn", None, reason)
    count = await get_warns(message.chat.id, target.id)

    if count >= WARN_LIMIT:
        try:
            await bot.ban_chat_member(message.chat.id, target.id)
            await bot.unban_chat_member(message.chat.id, target.id)
            await clear_warns(message.chat.id, target.id)
        except Exception:
            pass
        await message.answer(
            f"⚠️ <b>{target.full_name}</b> получил {count}-й варн\n"
            f"👢 Авто-кик сработал!",
        )
    else:
        await message.answer(
            f"⚠️ <b>{target.full_name}</b> получил варн ({count}/{WARN_LIMIT})\n"
            f"📝 Причина: {reason}",
        )


# ====================== UNBAN / UNMUTE / UNWARN ======================
@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        await message.reply("❌ Укажи юзера.")
        return
    try:
        await bot.unban_chat_member(message.chat.id, target.id, only_if_banned=True)
        await message.answer(f"✅ <b>{target.full_name}</b> разбанен")
    except Exception as e:
        await message.reply(f"❌ {e}")


@dp.message(Command("unmute"))
async def cmd_unmute(message: Message):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        await message.reply("❌ Укажи юзера.")
        return
    try:
        await bot.restrict_chat_member(message.chat.id, target.id,
                                       permissions=UNMUTE_PERMS)
        await message.answer(f"🔊 <b>{target.full_name}</b> размучен")
    except Exception as e:
        await message.reply(f"❌ {e}")


@dp.message(Command("unwarn"))
async def cmd_unwarn(message: Message):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return
    args = message.text.split()[1:]
    uid, uname, _ = await parse_target(message, args)
    target = await resolve_user(message, bot, uid, uname)
    if not target:
        await message.reply("❌ Укажи юзера.")
        return
    await clear_warns(message.chat.id, target.id)
    await message.answer(f"✅ Варны <b>{target.full_name}</b> сброшены")


# ====================== ДОБАВЛЕНИЕ В ЧАТ ======================
@dp.my_chat_member()
async def on_bot_added(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    await save_chat(chat.id, chat.title or "")
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
                    "• Закрепление сообщений",
                )
        except Exception:
            pass


# ====================== ЗАПУСК ======================
async def main():
    global _BOT_USERNAME
    await init_db()
    me = await bot.get_me()
    _BOT_USERNAME = me.username
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())