from datetime import datetime, timedelta


def parse_duration(value, unit):
    """unit = 'days' | 'minutes'. '0' → None (навсегда)."""
    if not value or not value.isdigit():
        return None
    n = int(value)
    if n == 0:
        return None
    delta = timedelta(days=n) if unit == "days" else timedelta(minutes=n)
    return datetime.utcnow() + delta


def human_duration(until, unit):
    if until is None:
        return "навсегда"
    total = int((until - datetime.utcnow()).total_seconds())
    if unit == "days":
        return f"{max(1, total // 86400)} дн."
    return f"{max(1, total // 60)} мин."


async def parse_target(message, args):
    """Возвращает (user_id, username, remaining_args)."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return (message.reply_to_message.from_user.id,
                message.reply_to_message.from_user.username, args)
    if not args:
        return None, None, args
    first = args[0]
    if first.startswith("@"):
        return None, first[1:], args[1:]
    if first.isdigit() and len(first) >= 5:
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
            return None
    if user_id:
        try:
            m = await bot.get_chat_member(message.chat.id, user_id)
            return m.user
        except Exception:
            return None
    return None


async def is_admin(bot, chat_id, user_id):
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False