import asyncio
import logging
import time
from decimal import Decimal, InvalidOperation

import aiohttp

import db
from config import ADMIN_ID, CRYPTO_TOKEN
from texts import money, t
from util import fdate

API = "https://pay.crypt.bot/api/"


async def _call(method, params):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        async with session.post(API + method, json=params, allow_redirects=False,
                                headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN}) as response:
            response.raise_for_status()
            data = await response.json()
    if not data.get("ok"):
        raise RuntimeError("Crypto Pay rejected the request")
    return data["result"]


async def create_invoice(price_rub, description):
    return await _call("createInvoice", {
        "currency_type": "fiat", "fiat": "RUB", "amount": f"{price_rub:.2f}",
        "description": description[:1000], "expires_in": 3600,
    })


async def notify_referrer(bot, ref, bonus):
    user = await db.get_user(ref) if ref else None
    if user and user["notif"] and bonus:
        try:
            await bot.send_message(ref, f"💰 Реферал оплатил подписку. Начислено <b>{money(bonus)} ₽</b>.")
        except Exception:
            logging.warning("Unable to notify referrer %s", ref)


async def notify_payment(bot, result):
    p, ref, bonus = result
    user = await db.get_user(p["user"])
    try:
        await bot.send_message(p["user"], await t("pay_ok", days=p["days"], until=fdate(user["sub_until"])))
    except Exception:
        logging.warning("Unable to notify user about payment %s", p["id"])
    await notify_referrer(bot, ref, bonus)


async def check_and_confirm(bot, pid, uid=None):
    p = await db.one("SELECT * FROM payments WHERE id=? AND invoice_id>0", (pid,))
    if not p or (uid is not None and p["user"] != uid):
        return "none"
    if p["status"] == "ok":
        return "paid"
    if p["status"] != "crypto":
        return "expired"
    res = await _call("getInvoices", {"invoice_ids": str(p["invoice_id"])})
    invoice = next((item for item in res.get("items", []) if item.get("invoice_id") == p["invoice_id"]), None)
    if not invoice:
        return "unavailable"
    if invoice.get("status") == "expired":
        return "expired" if await db.fail_payment(pid, "crypto", "expired") else "unavailable"
    if invoice.get("status") != "paid":
        return "wait"
    try:
        valid_amount = Decimal(str(invoice["amount"])) == Decimal(str(p["price"]))
    except (KeyError, InvalidOperation):
        valid_amount = False
    if not valid_amount or invoice.get("fiat") != "RUB":
        logging.error("Crypto Pay amount or currency mismatch for payment %s", pid)
        return "unavailable"
    result = await db.finish_payment(pid, "crypto")
    if not result:
        current = await db.one("SELECT status FROM payments WHERE id=?", (pid,))
        return "paid" if current and current["status"] == "ok" else "unavailable"
    await notify_payment(bot, result)
    try:
        await bot.send_message(ADMIN_ID, f"🪙 Crypto Pay, заявка #{pid}. {money(p['price'])} ₽.")
    except Exception:
        logging.warning("Unable to notify admin about payment %s", pid)
    return "paid"


async def poller(bot):
    while True:
        for p in await db.q("SELECT id FROM payments WHERE status='crypto' AND invoice_id>0"):
            try:
                await check_and_confirm(bot, p["id"])
            except Exception:
                logging.warning("Crypto Pay check failed for payment %s", p["id"])
        await asyncio.sleep(30)
