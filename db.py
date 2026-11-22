import json
import secrets
import math
import time
from contextlib import asynccontextmanager

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
CREATE TABLE IF NOT EXISTS marketplace_accounts(
  user INTEGER NOT NULL, platform TEXT NOT NULL, secret TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL,
  PRIMARY KEY(user, platform));
CREATE TABLE IF NOT EXISTS user_settings(
  user INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY(user, key));
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
    import offer_jobs, seller_events, fulfillment
    await _db.executescript(SCHEMA + offer_jobs.SCHEMA + seller_events.SCHEMA + fulfillment.SCHEMA)
    await _db.execute("PRAGMA journal_mode=WAL")
    columns = {row["name"] for row in await q("PRAGMA table_info(payments)")}
    for column in ("invoice_id INTEGER DEFAULT 0", "receipt_file_id TEXT", "receipt_type TEXT",
                   "request_key TEXT", "stars_amount INTEGER", "charge_id TEXT", "receipt_unique_id TEXT"):
        if column.split()[0] not in columns:
            await _db.execute(f"ALTER TABLE payments ADD COLUMN {column}")
    await _db.execute("CREATE UNIQUE INDEX IF NOT EXISTS payments_request ON payments(user,request_key)")
    await _db.execute("CREATE UNIQUE INDEX IF NOT EXISTS payments_charge ON payments(charge_id)")
    await _db.commit()
    S.clear()
    for r in await q("SELECT key,value FROM settings"):
        S[r["key"]] = r["value"]
    interrupted = await q("SELECT id FROM payments WHERE status='crypto_creating'")
    for p in interrupted:
        await fail_payment(p["id"], "crypto_creating", "notify_error")


async def close():
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def q(sql, a=()):
    async with _db.execute(sql, a) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def one(sql, a=()):
    r = await q(sql, a)
    return r[0] if r else None


async def ex(sql, a=()):
    async with transaction() as conn:
        async with conn.execute(sql, a) as cur:
            return cur.lastrowid


@asynccontextmanager
async def transaction():
    # Each write transaction has its own connection, so overlapping updates
    # cannot commit each other's work on the aiosqlite command queue.
    async with aiosqlite.connect(DB_PATH, timeout=15) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        yield conn
        await conn.commit()


async def _row(conn, sql, args):
    async with conn.execute(sql, args) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


# ---------- настройки ----------
async def set_s(key, value):
    await ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    S[key] = value


async def del_s(key):
    await ex("DELETE FROM settings WHERE key=?", (key,))
    S.pop(key, None)


def gs(key, default=""):
    return S.get(key, default)


def tariffs():
    try:
        rows = json.loads(S["tariffs"])
        if rows and isinstance(rows, list) and all(
            isinstance(row["t"], str) and row["t"] and 1 <= row["d"] <= 3650
            and math.isfinite(row["p"]) and 0 < row["p"] <= 1_000_000
            for row in rows
        ):
            return rows
    except (KeyError, ValueError, TypeError):
        pass
    return [row.copy() for row in DEFAULT_TARIFFS]


def ref_percent():
    try:
        value = float(gs("ref_percent", "5"))
        return value if 0 <= value <= 100 else 5.0
    except ValueError:
        return 5.0


def trial_days():
    try:
        value = int(gs("trial_days", "7"))
        return value if 1 <= value <= 3650 else 7
    except ValueError:
        return 7


def stars_rate():
    try:
        value = float(gs("stars_rate", "1"))
        return value if 0.01 <= value <= 10 else 1.0
    except ValueError:
        return 1.0


# ---------- пользователи ----------
async def get_user(uid):
    return await one("SELECT * FROM users WHERE id=?", (uid,))


async def find_user(s: str):
    s = s.strip().lstrip("@")
    if s.isdigit():
        return await get_user(int(s)) if s.isascii() and len(s) <= 18 else None
    return await one("SELECT * FROM users WHERE lower(username)=lower(?)", (s,))


def active(u) -> bool:
    return bool(u) and u["sub_until"] > time.time()


async def add_days(uid, days):
    async with transaction() as conn:
        return await _add_days(conn, uid, days)


async def _add_days(conn, uid, days):
    u = await _row(conn, "SELECT sub_until FROM users WHERE id=?", (uid,))
    if not u:
        return 0
    now = int(time.time())
    base = max(now, u["sub_until"])
    new = base + days * 86400
    if days < 0 and new <= now:
        new = 0
    await conn.execute("UPDATE users SET sub_until=? WHERE id=?", (new, uid))
    return new


async def add_balance(uid, amount, kind, note=""):
    if not math.isfinite(amount):
        raise ValueError("Некорректная сумма")
    async with transaction() as conn:
        await _add_balance(conn, uid, amount, kind, note)


async def _add_balance(conn, uid, amount, kind, note):
    cur = await conn.execute("UPDATE users SET balance=round(balance+?,2) WHERE id=?", (amount, uid))
    if cur.rowcount != 1:
        raise ValueError("Пользователь не найден")
    await conn.execute("INSERT INTO tx(user,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                       (uid, amount, kind, note, int(time.time())))


async def transfer(uid, target, amount):
    if uid == target or not math.isfinite(amount) or amount <= 0:
        return "amount"
    async with transaction() as conn:
        sender = await _row(conn, "SELECT balance FROM users WHERE id=? AND banned=0", (uid,))
        if not await _row(conn, "SELECT id FROM users WHERE id=? AND banned=0", (target,)):
            return "target"
        if not sender or sender["balance"] < amount:
            return "amount"
        await _add_balance(conn, uid, -amount, "transfer_out", str(target))
        await _add_balance(conn, target, amount, "transfer_in", str(uid))
        return "ok"


async def claim_trial(uid, days):
    async with transaction() as conn:
        cur = await conn.execute("UPDATE users SET trial_used=1 WHERE id=? AND trial_used=0", (uid,))
        return await _add_days(conn, uid, days) if cur.rowcount else 0


async def redeem_promo(uid, code):
    async with transaction() as conn:
        promo = await _row(conn, "SELECT * FROM promos WHERE code=? AND active=1", (code,))
        used = await _row(conn, "SELECT 1 FROM promo_uses WHERE code=? AND user=?", (code, uid))
        if not promo or used or (promo["max_uses"] and promo["used"] >= promo["max_uses"]):
            return None
        if promo["kind"] == "days" and 1 <= promo["value"] <= 3650:
            await _add_days(conn, uid, promo["value"])
        elif promo["kind"] == "discount" and 1 <= promo["value"] <= 99:
            await conn.execute("UPDATE users SET discount=max(discount,?) WHERE id=?", (promo["value"], uid))
            promo["value"] = (await _row(conn, "SELECT discount FROM users WHERE id=?", (uid,)))["discount"]
        else:
            return None
        await conn.execute("INSERT INTO promo_uses VALUES(?,?,?)", (code, uid, int(time.time())))
        await conn.execute("UPDATE promos SET used=used+1 WHERE code=?", (code,))
        return promo


async def save_marketplace_account(uid, platform, encrypted_secret, label):
    async with transaction() as conn:
        rows = await conn.execute_fetchall("SELECT platform,label FROM marketplace_accounts WHERE user=?", (uid,))
        related = [row for row in rows if row["platform"].partition("/")[0] == platform]
        existing = next((row for row in related if row["label"] == label), None)
        if not existing and len(rows) >= 5:
            raise AccountLimitError
        key = existing["platform"] if existing else platform
        if not existing and any(row["platform"] == platform for row in related):
            key = platform + "/" + secrets.token_hex(6)
        await conn.execute(
            "INSERT INTO marketplace_accounts(user,platform,secret,label,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user,platform) DO UPDATE SET secret=excluded.secret,label=excluded.label,updated=excluded.updated",
            (uid, key, encrypted_secret, label, int(time.time())),
        )


class AccountLimitError(ValueError):
    pass


async def get_marketplace_accounts(uid):
    return await q("SELECT platform,label,updated FROM marketplace_accounts WHERE user=? ORDER BY platform", (uid,))


async def get_marketplace_secret(uid, platform):
    return await one("SELECT secret,label FROM marketplace_accounts WHERE user=? AND platform=?", (uid, platform))


async def delete_marketplace_account(uid, platform):
    async with transaction() as conn:
        await conn.execute("DELETE FROM plugin_orders WHERE link IN (SELECT id FROM plugin_links WHERE user=? AND slot=?)",(uid,platform))
        await conn.execute("DELETE FROM plugin_links WHERE user=? AND slot=?",(uid,platform))
        await conn.execute("DELETE FROM offer_jobs WHERE user=? AND slot=?", (uid, platform))
        await conn.execute("DELETE FROM marketplace_accounts WHERE user=? AND platform=?", (uid, platform))


async def get_user_setting(uid, key, default="1"):
    row = await one("SELECT value FROM user_settings WHERE user=? AND key=?", (uid, key))
    return row["value"] if row else default


async def set_user_setting(uid, key, value):
    await ex(
        "INSERT INTO user_settings(user,key,value) VALUES(?,?,?) "
        "ON CONFLICT(user,key) DO UPDATE SET value=excluded.value",
        (uid, key, str(value)),
    )


# ---------- платежи ----------
def price_for(tariff, discount):
    return max(0.01, round(tariff["p"] * (100 - min(99, max(0, discount))) / 100, 2))


async def reserve_payment(uid, tariff, discount, status, *, request_key=None,
                          stars_amount=None):
    async with transaction() as conn:
        user = await _row(conn, "SELECT balance,discount,banned FROM users WHERE id=?", (uid,))
        if not user or user["banned"] or user["discount"] != discount:
            return "changed", None
        if request_key and await _row(conn, "SELECT 1 FROM payments WHERE user=? AND request_key=?", (uid, request_key)):
            return "used", None
        price = price_for(tariff, discount)
        if status == "balance" and user["balance"] < price:
            return "balance", None
        if status == "manual":
            if await _row(conn, "SELECT 1 FROM payments WHERE user=? AND status IN ('pending','manual')", (uid,)):
                return "pending", None
        async with conn.execute(
            "INSERT INTO payments(user,days,price,disc,status,ts,request_key,stars_amount) VALUES(?,?,?,?,?,?,?,?)",
            (uid, tariff["d"], price, discount, status, int(time.time()), request_key, stars_amount),
        ) as cur:
            pid = cur.lastrowid
        if discount and status != "stars":
            await conn.execute("UPDATE users SET discount=0 WHERE id=?", (uid,))
        if status == "balance":
            await _add_balance(conn, uid, -price, "sub_pay", tariff["t"])
            payment = await _row(conn, "SELECT * FROM payments WHERE id=?", (pid,))
            return "ok", await _finish(conn, payment)
        return "ok", pid


async def finish_payment(pid, expected="pending", *, uid=None, charge_id=None, stars_amount=None):
    async with transaction() as conn:
        payment = await _row(conn, "SELECT * FROM payments WHERE id=? AND status=?", (pid, expected))
        if not payment:
            return None
        if expected == "stars_checkout":
            if payment["user"] != uid or payment["stars_amount"] != stars_amount or not charge_id:
                return None
            if await _row(conn, "SELECT 1 FROM payments WHERE charge_id=?", (charge_id,)):
                return None
        return await _finish(conn, payment, charge_id)


async def _finish(conn, p, charge_id=None):
    await conn.execute("UPDATE payments SET status='ok',charge_id=? WHERE id=?", (charge_id, p["id"]))
    await _add_days(conn, p["user"], p["days"])
    user = await _row(conn, "SELECT ref_by FROM users WHERE id=?", (p["user"],))
    ref = user["ref_by"] if user else 0
    bonus = 0
    if ref and ref != p["user"] and await _row(conn, "SELECT id FROM users WHERE id=?", (ref,)):
        bonus = round(p["price"] * ref_percent() / 100, 2)
        if bonus > 0:
            await _add_balance(conn, ref, bonus, "ref_bonus", str(p["user"]))
    return p, ref, bonus


async def fail_payment(pid, expected, status, *, uid=None):
    async with transaction() as conn:
        payment = await _row(conn, "SELECT * FROM payments WHERE id=? AND status=?", (pid, expected))
        if not payment or (uid is not None and payment["user"] != uid):
            return False
        await conn.execute("UPDATE payments SET status=? WHERE id=?", (status, pid))
        if payment["disc"] and expected != "stars":
            await conn.execute("UPDATE users SET discount=max(discount,?) WHERE id=?",
                               (payment["disc"], payment["user"]))
        return True


async def approve_stars_checkout(pid, uid, amount):
    async with transaction() as conn:
        p = await _row(conn, "SELECT * FROM payments WHERE id=? AND user=? AND stars_amount=?",
                       (pid, uid, amount))
        if not p:
            return False
        if p["status"] == "stars_checkout":
            return True
        if p["status"] != "stars" or p["ts"] < int(time.time()) - 3600:
            return False
        u = await _row(conn, "SELECT discount,banned FROM users WHERE id=?", (uid,))
        if not u or u["banned"] or u["discount"] != p["disc"]:
            return False
        if p["disc"]:
            await conn.execute("UPDATE users SET discount=0 WHERE id=?", (uid,))
        await conn.execute("UPDATE payments SET status='stars_checkout' WHERE id=?", (pid,))
        return True


async def attach_receipt(uid, pid, receipt):
    file_id, file_type, unique_id = receipt
    async with transaction() as conn:
        p = await _row(conn, "SELECT * FROM payments WHERE id=? AND user=? AND status='manual'", (pid, uid))
        if not p:
            return "changed", None
        if await _row(conn, "SELECT 1 FROM payments WHERE user=? AND status='pending'", (uid,)):
            return "pending", None
        if await _row(conn, "SELECT 1 FROM payments WHERE user=? AND receipt_unique_id=? AND status='ok'", (uid, unique_id)):
            return "used", None
        await conn.execute(
            "UPDATE payments SET status='pending',receipt_file_id=?,receipt_type=?,receipt_unique_id=? WHERE id=?",
            (file_id, file_type, unique_id, pid),
        )
        return "ok", p
