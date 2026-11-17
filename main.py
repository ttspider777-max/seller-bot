import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as KB, Message

import admin
import marketplaces
import user_panel
import crypto
import db
from config import ADMIN_ID, BOT_TOKEN, CRYPTO_TOKEN
from texts import B, esc, money, t
from util import TZ, fdate, kb, show

r = Router()
_me = {}


class U(StatesGroup):
    promo = State()
    transfer = State()
    receipt = State()


def sub_status(u):
    if db.active(u):
        return f"✅ Статус: <b>активна (до {fdate(u['sub_until'])})</b>"
    return "❌ Статус: <b>не активна</b>" if not u["sub_until"] else f"❌ Статус: <b>истекла ({fdate(u['sub_until'])})</b>"


def left_text(u):
    if not db.active(u):
        return "—"
    secs = int(u["sub_until"] - time.time())
    d, h = secs // 86400, secs % 86400 // 3600
    return f"{d} дн. {h} ч."


def back(to="menu"):
    return B("back", to)


# ---------------- middleware: бан, обязательная подписка, регистрация ----------------
async def unsubscribed(bot: Bot, uid):
    res = []
    for c in await db.q("SELECT * FROM channels"):
        try:
            cid = int(c["chat_id"]) if c["chat_id"].lstrip("-").isdigit() else c["chat_id"]
            mb = await bot.get_chat_member(cid, uid)
            if mb.status in ("left", "kicked"):
                res.append(c)
        except Exception:
            pass  # не можем проверить (бот не админ) — не блокируем
    return res


class Gate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        if not user or (chat and chat.type != ChatType.PRIVATE):
            return
        bot: Bot = data["bot"]
        u = await db.get_user(user.id)
        name = user.full_name or ""
        uname = user.username or ""
        if not u:
            ref = 0
            if isinstance(event, Message) and event.text and event.text.startswith("/start ref_"):
                p = event.text.split("ref_", 1)[1].strip()
                if p.isdigit() and int(p) != user.id and await db.get_user(int(p)):
                    ref = int(p)
            await db.ex("INSERT INTO users(id,name,username,joined,ref_by) VALUES(?,?,?,?,?)",
                        (user.id, name, uname, int(time.time()), ref))
            u = await db.get_user(user.id)
            if ref:
                ru = await db.get_user(ref)
                if ru and ru["notif"]:
                    try:
                        await bot.send_message(ref, f"👥 По вашей ссылке зарегистрировался новый реферал: <b>{esc(name)}</b>")
                    except Exception:
                        pass
        elif u["name"] != name or (u["username"] or "") != uname:
            await db.ex("UPDATE users SET name=?, username=? WHERE id=?", (name, uname, user.id))

        if user.id == ADMIN_ID:
            return await handler(event, data)
        if u["banned"]:
            if isinstance(event, CallbackQuery):
                await event.answer(await t("banned"), show_alert=True)
            else:
                await event.answer(await t("banned"))
            return
        if not (isinstance(event, CallbackQuery) and event.data == "chk_sub"):
            miss = await unsubscribed(bot, user.id)
            if miss:
                rows = [[IB(text=f"📢 {c['title']}", url=c["link"])] for c in miss if c["link"]]
                rows.append([B("check_sub", "chk_sub")])
                if isinstance(event, CallbackQuery):
                    await event.answer()
                await bot.send_message(user.id, await t("need_sub"), reply_markup=KB(inline_keyboard=rows))
                return
        return await handler(event, data)


# ---------------- главное меню ----------------
def link_btn(key, setting):
    url = db.gs(setting)
    if url:
        return B(key, url=url)
    return B(key, "nolink")


def main_kb(uid):
    rows = [
        [link_btn("news", "link_news"), link_btn("guide", "link_guide")],
        [link_btn("features", "link_features")],
        [B("free")],
        [IB(text="🧩 Плагины", callback_data="userpanel:catalog"), IB(text="⚙️ Настройки", callback_data="userpanel:settings")],
        [B("profile"), B("sub")],
    ]
    if uid == ADMIN_ID:
        rows.append([B("admin", "a:menu")])
    return KB(inline_keyboard=rows)


async def menu_text(user):
    return await t("welcome", name=esc(user.full_name), id=user.id, username=("@" + user.username) if user.username else "—")


@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await menu_text(m.from_user), reply_markup=main_kb(m.from_user.id))
    await m.answer("Быстрый доступ — на нижней панели 👇", reply_markup=user_panel.BOTTOM_PANEL)


@r.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, await menu_text(c.from_user), main_kb(c.from_user.id))
    await c.message.answer("Быстрый доступ — на нижней панели 👇", reply_markup=user_panel.BOTTOM_PANEL)
    await c.answer()


@r.callback_query(F.data == "nolink")
async def cb_nolink(c: CallbackQuery):
    await c.answer(await t("no_link"), show_alert=True)


@r.callback_query(F.data == "chk_sub")
async def cb_chk(c: CallbackQuery):
    if await unsubscribed(c.bot, c.from_user.id):
        await c.answer("Вы подписались не на всё 😉", show_alert=True)
        return
    await c.answer("Готово ✅")
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.message.answer(await menu_text(c.from_user), reply_markup=main_kb(c.from_user.id))
    await c.message.answer("Быстрый доступ — на нижней панели 👇", reply_markup=user_panel.BOTTOM_PANEL)


# ---------------- бесплатные дни ----------------
@r.callback_query(F.data == "free")
async def cb_free(c: CallbackQuery):
    u = await db.get_user(c.from_user.id)
    if u["trial_used"]:
        await c.answer(await t("free_used"), show_alert=True)
        return
    days = db.trial_days()
    await db.ex("UPDATE users SET trial_used=1 WHERE id=?", (u["id"],))
    until = await db.add_days(u["id"], days)
    await show(c, await t("free_ok", days=days, until=fdate(until)), kb(back()))
    await c.answer()


# ---------------- профиль ----------------
async def ref_numbers(uid):
    refs = (await db.one("SELECT count(*) n FROM users WHERE ref_by=?", (uid,)))["n"]
    profit = (await db.one("SELECT coalesce(sum(amount),0) s FROM tx WHERE user=? AND kind='ref_bonus'", (uid,)))["s"]
    return refs, profit


@r.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    u = await db.get_user(c.from_user.id)
    refs, _ = await ref_numbers(u["id"])
    pay = await db.one("SELECT count(*) n, coalesce(sum(price),0) s FROM payments WHERE user=? AND status='ok'", (u["id"],))
    text = await t(
        "profile", id=u["id"], name=esc(u["name"]), username=("@" + u["username"]) if u["username"] else "—",
        joined=fdate(u["joined"]), status=sub_status(u), left=left_text(u), purchases=pay["n"],
        spent=money(pay["s"]), discount=u["discount"], refs=refs, balance=money(u["balance"]),
        percent=money(db.ref_percent()),
    )
    await show(c, text, kb([B("refsys"), B("promo")], B("sub"), back()))
    await c.answer()


# ---------------- реферальная система ----------------
async def bot_username(bot: Bot):
    if "u" not in _me:
        _me["u"] = (await bot.get_me()).username
    return _me["u"]


@r.callback_query(F.data == "refsys")
async def cb_ref(c: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await db.get_user(c.from_user.id)
    refs, profit = await ref_numbers(u["id"])
    text = await t("ref_main", refs=refs, profit=money(profit), balance=money(u["balance"]), percent=money(db.ref_percent()))
    sup = db.gs("link_support")
    rows = [
        [B("ref_links")],
        [B("ref_stats"), B("ref_list")],
        [B("ref_tx"), B("ref_notif")],
        [B("ref_transfer")],
        [B("ref_more"), B("support", url=sup) if sup else B("support", "nolink")],
        [back("profile")],
    ]
    await show(c, text, KB(inline_keyboard=rows))
    await c.answer()


@r.callback_query(F.data == "ref_links")
async def cb_ref_links(c: CallbackQuery):
    link = f"https://t.me/{await bot_username(c.bot)}?start=ref_{c.from_user.id}"
    await show(c, f"🔗 <b>Ваша реферальная ссылка</b>\n\n<code>{link}</code>\n\nНажмите на ссылку, чтобы скопировать.", kb(back("refsys")))
    await c.answer()


@r.callback_query(F.data == "ref_stats")
async def cb_ref_stats(c: CallbackQuery):
    uid = c.from_user.id
    refs, profit = await ref_numbers(uid)
    paying = (await db.one("SELECT count(DISTINCT note) n FROM tx WHERE user=? AND kind='ref_bonus'", (uid,)))["n"]
    active_refs = (await db.one("SELECT count(*) n FROM users WHERE ref_by=? AND sub_until>?", (uid, int(time.time()))))["n"]
    today = int(datetime.now(TZ).replace(hour=0, minute=0, second=0).timestamp())
    td = (await db.one("SELECT coalesce(sum(amount),0) s FROM tx WHERE user=? AND kind='ref_bonus' AND ts>=?", (uid, today)))["s"]
    text = (
        "📊 <b>Статистика рефералов</b>\n\n<blockquote>"
        f"👥 Всего рефералов: {refs}\n💎 С активной подпиской: {active_refs}\n"
        f"🛒 Оплативших: {paying}\n💰 Заработано всего: {money(profit)} ₽\n📅 Сегодня: {money(td)} ₽</blockquote>"
    )
    await show(c, text, kb(back("refsys")))
    await c.answer()


@r.callback_query(F.data == "ref_list")
async def cb_ref_list(c: CallbackQuery):
    rows = await db.q("SELECT * FROM users WHERE ref_by=? ORDER BY joined DESC LIMIT 25", (c.from_user.id,))
    if rows:
        body = "\n".join(f"{'💎' if db.active(x) else '▫️'} {esc(x['name'])} · {fdate(x['joined'])}" for x in rows)
    else:
        body = "Пока никого нет."
    await show(c, f"👥 <b>Ваши рефералы</b>\n\n<blockquote>{body}</blockquote>", kb(back("refsys")))
    await c.answer()


KIND = {"ref_bonus": "Реф. бонус", "transfer_in": "Перевод получен", "transfer_out": "Перевод отправлен",
        "sub_pay": "Оплата подписки", "admin": "Начисление админом"}


@r.callback_query(F.data == "ref_tx")
async def cb_ref_tx(c: CallbackQuery):
    rows = await db.q("SELECT * FROM tx WHERE user=? ORDER BY id DESC LIMIT 15", (c.from_user.id,))
    if rows:
        body = "\n".join(f"{fdate(x['ts'], True)} · {'+' if x['amount'] > 0 else ''}{money(x['amount'])} ₽ · {KIND.get(x['kind'], x['kind'])}" for x in rows)
    else:
        body = "Транзакций пока нет."
    await show(c, f"🧾 <b>Транзакции</b>\n\n<blockquote>{body}</blockquote>", kb(back("refsys")))
    await c.answer()


@r.callback_query(F.data == "ref_notif")
async def cb_ref_notif(c: CallbackQuery):
    u = await db.get_user(c.from_user.id)
    new = 0 if u["notif"] else 1
    await db.ex("UPDATE users SET notif=? WHERE id=?", (new, u["id"]))
    await c.answer("Уведомления " + ("включены 🔔" if new else "выключены 🔕"), show_alert=True)


@r.callback_query(F.data == "ref_more")
async def cb_ref_more(c: CallbackQuery):
    await show(c, await t("ref_more", percent=money(db.ref_percent())), kb(back("refsys")))
    await c.answer()


@r.callback_query(F.data == "ref_transfer")
async def cb_transfer(c: CallbackQuery, state: FSMContext):
    u = await db.get_user(c.from_user.id)
    await state.set_state(U.transfer)
    await show(c, await t("transfer_ask", balance=money(u["balance"])), kb(back("refsys")))
    await c.answer()


@r.message(U.transfer, F.text)
async def in_transfer(m: Message, state: FSMContext):
    parts = m.text.split()
    try:
        to, amount = int(parts[0]), round(float(parts[1].replace(",", ".")), 2)
    except Exception:
        await m.answer("Формат: <code>ID сумма</code>", reply_markup=kb(back("refsys")))
        return
    u = await db.get_user(m.from_user.id)
    target = await db.get_user(to)
    if not target or to == u["id"]:
        await m.answer("❌ Получатель не найден (он должен запускать бота).", reply_markup=kb(back("refsys")))
        return
    if amount <= 0 or amount > u["balance"]:
        await m.answer(f"❌ Некорректная сумма. Ваш баланс: {money(u['balance'])} ₽", reply_markup=kb(back("refsys")))
        return
    await db.add_balance(u["id"], -amount, "transfer_out", str(to))
    await db.add_balance(to, amount, "transfer_in", str(u["id"]))
    await state.clear()
    await m.answer(f"✅ Переведено {money(amount)} ₽ пользователю {to}.", reply_markup=kb(back("refsys")))
    try:
        await m.bot.send_message(to, f"💸 Вам перевели <b>{money(amount)} ₽</b> от {esc(u['name'])}.")
    except Exception:
        pass


# ---------------- промокод ----------------
@r.callback_query(F.data == "promo")
async def cb_promo(c: CallbackQuery, state: FSMContext):
    await state.set_state(U.promo)
    await show(c, await t("promo_ask"), kb(back("sub")))
    await c.answer()


@r.message(U.promo, F.text)
async def in_promo(m: Message, state: FSMContext):
    code = m.text.strip().upper()
    p = await db.one("SELECT * FROM promos WHERE code=? AND active=1", (code,))
    used = await db.one("SELECT 1 FROM promo_uses WHERE code=? AND user=?", (code, m.from_user.id))
    if not p or used or (p["max_uses"] and p["used"] >= p["max_uses"]):
        await m.answer(await t("promo_bad"), reply_markup=kb(back("sub")))
        return
    await db.ex("INSERT INTO promo_uses VALUES(?,?,?)", (code, m.from_user.id, int(time.time())))
    await db.ex("UPDATE promos SET used=used+1 WHERE code=?", (code,))
    await state.clear()
    if p["kind"] == "days":
        await db.add_days(m.from_user.id, p["value"])
        await m.answer(await t("promo_days", value=p["value"]), reply_markup=kb(back("sub")))
    else:
        await db.ex("UPDATE users SET discount=? WHERE id=?", (p["value"], m.from_user.id))
        await m.answer(await t("promo_disc", value=p["value"]), reply_markup=kb(back("sub")))


# ---------------- подписка ----------------
def price_of(tar, disc):
    return round(tar["p"] * (100 - disc) / 100, 2)


def tar_btn_text(tar, disc=0):
    b = f" ({tar['b']} 🔥)" if tar.get("b") else ""
    return f"💎 {tar['t']} | {money(price_of(tar, disc))} ₽{b}"


@r.callback_query(F.data == "sub")
async def cb_sub(c: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await db.get_user(c.from_user.id)
    tars = db.tariffs()
    lines = []
    for i, x in enumerate(tars):
        pre = "┌" if i == 0 else ("└" if i == len(tars) - 1 else "├")
        lines.append(f"{pre} 🔴 {x['t']} — {money(x['p'])} ₽" + (f" ({x['b']})" if x.get("b") else ""))
    disc = f"\n🎟 Ваша скидка: <b>{u['discount']}%</b>" if u["discount"] else ""
    text = await t("sub_main", status=sub_status(u), id=u["id"], accounts=0, tariffs="\n".join(lines), discount_line=disc)
    rows = [[B("promo")]]
    rows += [[IB(text=tar_btn_text(x, u["discount"]), callback_data=f"buy:{i}")] for i, x in enumerate(tars)]
    rows += [[B("free")], [B("refsys")], [back()]]
    await show(c, text, KB(inline_keyboard=rows))
    await c.answer()


@r.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery):
    i = int(c.data.split(":")[1])
    tar = db.tariffs()[i]
    u = await db.get_user(c.from_user.id)
    price = price_of(tar, u["discount"])
    d = f" (скидка {u['discount']}%)" if u["discount"] else ""
    text = await t("pay_choose", tariff=tar["t"], price=money(price), disc=d, balance=money(u["balance"]))
    methods = [B("pay_balance", f"payb:{i}"), B("pay_manual", f"paym:{i}")]
    if CRYPTO_TOKEN:
        methods.append(B("pay_crypto", f"payc:{i}"))
    methods.append(back("sub"))
    await show(c, text, kb(*methods))
    await c.answer()


@r.callback_query(F.data.startswith("payb:"))
async def cb_pay_bal(c: CallbackQuery):
    i = int(c.data.split(":")[1])
    tar = db.tariffs()[i]
    u = await db.get_user(c.from_user.id)
    price = price_of(tar, u["discount"])
    if u["balance"] < price:
        await c.answer(await t("pay_bal_no", price=money(price), balance=money(u["balance"])), show_alert=True)
        return
    discount_used = u["discount"]
    await db.add_balance(u["id"], -price, "sub_pay", tar["t"])
    if u["discount"]:
        await db.ex("UPDATE users SET discount=0 WHERE id=?", (u["id"],))
    until = await db.add_days(u["id"], tar["d"])
    await db.ex("INSERT INTO payments(user,days,price,disc,status,ts) VALUES(?,?,?,?,?,?)",
                (u["id"], tar["d"], price, discount_used, "balance", int(time.time())))
    await show(c, await t("pay_bal_ok", days=tar["d"], until=fdate(until)), kb(back()))
    await c.answer()


@r.callback_query(F.data.startswith("paym:"))
async def cb_pay_manual(c: CallbackQuery):
    i = int(c.data.split(":")[1])
    tar = db.tariffs()[i]
    u = await db.get_user(c.from_user.id)
    price = price_of(tar, u["discount"])
    req = await t("requisites")
    text = await t("pay_manual", tariff=tar["t"], price=money(price), requisites=req)
    await show(c, text, kb(B("paid", f"paid:{i}"), back(f"buy:{i}")))
    await c.answer()


@r.callback_query(F.data.startswith("pays:"))
async def cb_pay_stars(c: CallbackQuery):
    await c.answer("Оплата Telegram Stars пока не настроена. Выберите СБП или баланс.", show_alert=True)


@r.callback_query(F.data.startswith("payc:"))
async def cb_pay_crypto(c: CallbackQuery):
    i = int(c.data.split(":")[1])
    tar = db.tariffs()[i]
    u = await db.get_user(c.from_user.id)
    price = price_of(tar, u["discount"])
    try:
        inv = await crypto.create_invoice(price, f"Подписка {tar['t']} (ID {u['id']})")
    except Exception:
        logging.exception("crypto")
        await c.answer("Не удалось создать счёт, попробуйте позже", show_alert=True)
        return
    pid = await db.ex("INSERT INTO payments(user,days,price,disc,status,ts,invoice_id) VALUES(?,?,?,?,?,?,?)",
                      (u["id"], tar["d"], price, u["discount"], "crypto", int(time.time()), inv["invoice_id"]))
    url = inv.get("bot_invoice_url") or inv.get("pay_url")
    await show(c, await t("pay_crypto", tariff=tar["t"], price=money(price)),
               kb(B("pay_link", url=url), B("pay_check", f"chkc:{pid}"), back(f"buy:{i}")))
    await c.answer()


@r.callback_query(F.data.startswith("chkc:"))
async def cb_chk_crypto(c: CallbackQuery):
    res = await crypto.check_and_confirm(c.bot, int(c.data.split(":")[1]))
    msg = {"paid": "✅ Оплачено!", "wait": "Оплата пока не найдена ⏳", "expired": "Счёт истёк, создайте новый"}.get(res, "Готово")
    await c.answer(msg, show_alert=True)


@r.callback_query(F.data.startswith("paid:"))
async def cb_paid_state(c: CallbackQuery, state: FSMContext):
    i = int(c.data.split(":")[1])
    tars = db.tariffs()
    if i < 0 or i >= len(tars):
        await c.answer("Тариф не найден. Откройте список тарифов заново.", show_alert=True)
        return
    u = await db.get_user(c.from_user.id)
    pend = await db.one("SELECT id FROM payments WHERE user=? AND status='pending'", (u["id"],))
    if pend:
        await c.answer("У вас уже есть заявка на проверке ⏳", show_alert=True)
        return
    await state.set_state(U.receipt)
    await state.update_data(tariff_index=i)
    await c.message.edit_text(
        "📎 <b>Пришлите чек об оплате</b>\n\nОтправьте фото или PDF чека одним сообщением. "
        "После получения бот передаст чек администратору на проверку.",
        reply_markup=kb(back("sub")),
    )
    await c.answer()


@r.message(U.receipt)
async def in_receipt(m: Message, state: FSMContext):
    if m.photo:
        receipt_file_id, receipt_type = m.photo[-1].file_id, "photo"
    elif m.document and (m.document.mime_type == "application/pdf" or (m.document.mime_type or "").startswith("image/")):
        receipt_file_id, receipt_type = m.document.file_id, "document"
    else:
        await m.answer("Пришлите чек фотографией или файлом PDF/изображением.")
        return

    data = await state.get_data()
    i = data.get("tariff_index")
    tars = db.tariffs()
    if not isinstance(i, int) or i < 0 or i >= len(tars):
        await state.clear()
        await m.answer("Срок оформления истёк. Начните оплату заново через меню подписки.")
        return
    u = await db.get_user(m.from_user.id)
    pend = await db.one("SELECT id FROM payments WHERE user=? AND status='pending'", (u["id"],))
    if pend:
        await state.clear()
        await m.answer("У вас уже есть заявка на проверке ⏳", reply_markup=kb(back()))
        return

    tar = tars[i]
    price = price_of(tar, u["discount"])
    pid = await db.ex(
        "INSERT INTO payments(user,days,price,disc,status,ts,receipt_file_id,receipt_type) VALUES(?,?,?,?,?,?,?,?)",
        (u["id"], tar["d"], price, u["discount"], "pending", int(time.time()), receipt_file_id, receipt_type),
    )
    if u["discount"]:
        await db.ex("UPDATE users SET discount=0 WHERE id=?", (u["id"],))
    await state.clear()
    uname = f"@{u['username']}" if u["username"] else "—"
    try:
        caption = (
            f"💳 <b>Заявка на оплату #{pid}</b>\n\nПользователь: {esc(u['name'])} ({uname}) <code>{u['id']}</code>\n"
            f"Тариф: {esc(tar['t'])} ({tar['d']} дн.)\nСумма: <b>{money(price)} ₽</b>\n\nЧек приложен."
        )
        markup = kb([IB(text="✅ Подтвердить", callback_data=f"a:pok:{pid}"), IB(text="❌ Отклонить", callback_data=f"a:pno:{pid}")])
        if receipt_type == "photo":
            await m.bot.send_photo(ADMIN_ID, receipt_file_id, caption=caption, reply_markup=markup)
        else:
            await m.bot.send_document(ADMIN_ID, receipt_file_id, caption=caption, reply_markup=markup)
    except Exception:
        logging.exception("notify admin")
        await db.ex("UPDATE payments SET status='notify_error' WHERE id=?", (pid,))
        if u["discount"]:
            await db.ex("UPDATE users SET discount=? WHERE id=?", (u["discount"], u["id"]))
        await m.answer("Не удалось передать чек администратору. Заявка отменена, попробуйте отправить чек ещё раз.", reply_markup=kb(back("sub")))
        return
    await m.answer(f"✅ Чек отправлен на проверку. Номер заявки: #{pid}.", reply_markup=kb(back()))


# ---------------- запуск ----------------
async def main():
    logging.basicConfig(level=logging.INFO)
    await db.init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Gate())
    dp.callback_query.outer_middleware(Gate())
    dp.include_router(admin.r)
    dp.include_router(user_panel.r)
    dp.include_router(marketplaces.r)
    dp.include_router(r)
    asyncio.create_task(crypto.poller(bot))
    await bot.delete_webhook(drop_pending_updates=False)
    me = await bot.get_me()
    logging.info("Бот @%s запущен", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
