import asyncio
import logging
import time

import aiohttp

import db
from config import ADMIN_ID, CRYPTO_TOKEN
from texts import money, t
from util import fdate

API = "https://pay.crypt.bot/api/"
_lock = asyncio.Lock()


async def _call(method, params):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(API + method, json=params, headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN}) as r:
            data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(str(data))
    return data["result"]


async def create_invoice(price_rub, description):
    return await _call("createInvoice", {
        "currency_type": "fiat", "fiat": "RUB", "amount": f"{price_rub:.2f}",
        "description": description[:1000], "expires_in": 3600,
    })


async def check_and_confirm(bot, pid):
    async with _lock:
        p = await db.one("SELECT * FROM payments WHERE id=?", (pid,))
        if not p:
            return "none"
        if p["status"] == "ok":
            return "paid"
        if p["status"] != "crypto":
            return "expired"
        res = await _call("getInvoices", {"invoice_ids": str(p["invoice_id"])})
        items = res.get("items", [])
        status = items[0]["status"] if items else "expired"
        if status == "expired":
            await db.ex("UPDATE payments SET status='expired' WHERE id=?", (pid,))
            return "expired"
        if status != "paid":
            return "wait"
        await db.ex("UPDATE payments SET status='pending' WHERE id=?", (pid,))
        out = await db.finish_payment(pid)
        if not out:
            return "none"
        _, ref, bonus = out
        if p["disc"]:
            await db.ex("UPDATE users SET discount=0 WHERE id=?", (p["user"],))
        u = await db.get_user(p["user"])
        try:
            await bot.send_message(p["user"], await t("pay_ok", days=p["days"], until=fdate(u["sub_until"])))
            await bot.send_message(ADMIN_ID, f"🪙 Крипто-оплата #{pid}: пользователь {p['user']}, {money(p['price'])} ₽, +{p['days']} дн.")
            ru = await db.get_user(ref) if ref else None
            if ru and ru["notif"] and bonus:
                await bot.send_message(ref, f"💰 Реферал оплатил подписку: вам начислено <b>{money(bonus)} ₽</b>.")
        except Exception:
            pass
        return "paid"


async def poller(bot):
    while True:
        await asyncio.sleep(20)
        try:
            for p in await db.q("SELECT id FROM payments WHERE status='crypto' AND ts>?", (int(time.time()) - 7200,)):
                await check_and_confirm(bot, p["id"])
        except Exception:
            logging.exception("crypto poller")
