import json
import time

import aiosqlite

from config import DB_PATH

_db = None
S: dict = {}  # кеш настроек

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT, username TEXT, joined INTEGER,
  banned INTEGER DEFAULT 0, sub_until INTEGER DEFAULT 0, ref_by INTEGER DEFAULT 0,
  balance REAL DEFAULT 0, trial_used INTEGER DEFAULT 0, notif INTEGER DEFAULT 1,
  discount INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS promos(
  code TEXT PRIMARY KEY, kind TEXT, value INTEGER, max_uses INTEGER DEFAULT 0,
  used INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, user INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS tx(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER, amount REAL, kind TEXT,
  note TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER, days INTEGER, price REAL,
  disc INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', ts INTEGER);
CREATE TABLE IF NOT EXISTS channels(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, title TEXT, link TEXT);
"""

DEFAULT_TARIFFS = [
    {"t": "14 дней", "d": 14, "p": 399, "b": ""},
    {"t": "1 месяц", "d": 30, "p": 779, "b": "-10%"},
    {"t": "3 месяца", "d": 90, "p": 2290, "b": "-15%"},
    {"t": "6 месяцев", "d": 180, "p": 4299, "b": "-20%"},
]


async def init():
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    try:
        await _db.execute("ALTER TABLE payments ADD COLUMN invoice_id INTEGER DEFAULT 0")
    except Exception:
        pass
    await _db.commit()
    S.clear()
    for r in await q("SELECT key,value FROM settings"):
        S[r["key"]] = r["value"]


async def q(sql, a=()):
    cur = await _db.execute(sql, a)
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def one(sql, a=()):
    r = await q(sql, a)
    return r[0] if r else None


async def ex(sql, a=()):
    cur = await _db.execute(sql, a)
    await _db.commit()
    return cur.lastrowid


# ---------- настройки ----------
async def set_s(key, value):
    S[key] = value
    await ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


async def del_s(key):
    S.pop(key, None)
    await ex("DELETE FROM settings WHERE key=?", (key,))


def gs(key, default=""):
    return S.get(key, default)


def tariffs():
    try:
        return json.loads(S["tariffs"])
    except Exception:
        return DEFAULT_TARIFFS


def ref_percent():
    try:
        return float(gs("ref_percent", "5"))
    except ValueError:
        return 5.0


def trial_days():
    try:
        return int(gs("trial_days", "7"))
    except ValueError:
        return 7


# ---------- пользователи ----------
async def get_user(uid):
    return await one("SELECT * FROM users WHERE id=?", (uid,))


async def find_user(s: str):
    s = s.strip().lstrip("@")
    if s.isdigit():
        return await get_user(int(s))
    return await one("SELECT * FROM users WHERE lower(username)=lower(?)", (s,))


def active(u) -> bool:
    return bool(u) and u["sub_until"] > time.time()


async def add_days(uid, days):
    u = await get_user(uid)
    if not u:
        return 0
    now = int(time.time())
    base = max(now, u["sub_until"])
    new = base + days * 86400
    if days < 0 and new <= now:
        new = 0
    await ex("UPDATE users SET sub_until=? WHERE id=?", (new, uid))
    return new


async def add_balance(uid, amount, kind, note=""):
    await ex("UPDATE users SET balance=round(balance+?,2) WHERE id=?", (amount, uid))
    await ex("INSERT INTO tx(user,amount,kind,note,ts) VALUES(?,?,?,?,?)", (uid, amount, kind, note, int(time.time())))


# ---------- платежи ----------
async def finish_payment(pid):
    """Подтверждение ручного платежа. Возвращает (payment, ref_id, bonus) или None."""
    p = await one("SELECT * FROM payments WHERE id=?", (pid,))
    if not p or p["status"] != "pending":
        return None
    await ex("UPDATE payments SET status='ok' WHERE id=?", (pid,))
    await add_days(p["user"], p["days"])
    u = await get_user(p["user"])
    ref = u["ref_by"] if u else 0
    bonus = 0
    if ref and await get_user(ref):
        bonus = round(p["price"] * ref_percent() / 100, 2)
        if bonus > 0:
            await add_balance(ref, bonus, "ref_bonus", str(p["user"]))
    return p, ref, bonus
