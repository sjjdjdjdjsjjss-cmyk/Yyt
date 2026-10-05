import aiosqlite
from datetime import datetime

DB_PATH = "/data/diplomat.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT
        );
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            title TEXT
        );
        CREATE TABLE IF NOT EXISTS punishments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            admin_id INTEGER,
            type TEXT,
            until TEXT,
            reason TEXT,
            created_at TEXT
        );
        """)
        await db.commit()


async def save_chat(chat_id, title):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chats (chat_id, title) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
            (chat_id, title),
        )
        await db.commit()


async def add_punishment(chat_id, user_id, admin_id, ptype, until, reason):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO punishments (chat_id, user_id, admin_id, type, until, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, admin_id, ptype,
             until.isoformat() if until else None,
             reason, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_warns(chat_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM punishments WHERE chat_id=? AND user_id=? AND type='warn'",
            (chat_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def clear_warns(chat_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM punishments WHERE chat_id=? AND user_id=? AND type='warn'",
            (chat_id, user_id),
        )
        await db.commit()