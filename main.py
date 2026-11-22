import asyncio
import logging
import math
import secrets
import time
from datetime import datetime

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as KB
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery, WidgetQuery, WidgetQueryTypeData

import admin
import marketplaces
import fulfillment
import user_panel
import crypto
import db
from config import ADMIN_ID, BOT_TOKEN, CRYPTO_TOKEN
from menu_widget import send_menu
from texts import B, btn_title, esc, money, t
from user_panel import main_kb, menu_text
from util import TZ, fdate, form_input, kb, show

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
    return B("main_menu" if to == "menu" else "back", to)


# ---------------- middleware: бан, обязательная подписка, регистрация ----------------
async def unsubscribed(bot: Bot, uid):
    res = []
    for c in await db.q("SELECT * FROM channels"):
        try:
            cid = int(c["chat_id"]) if c["chat_id"].lstrip("-").isdigit() else c["chat_id"]
            mb = await bot.get_chat_member(cid, uid)
            if mb.status in ("left", "kicked") or (mb.status == "restricted" and not mb.is_member):
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
        if isinstance(event, Message):
            if event.successful_payment or (event.text or "").partition(" ")[0].split("@")[0] == "/paysupport":
                return await handler(event, data)
        bot: Bot = data["bot"]
        u = await db.get_user(user.id)
        name = user.full_name or ""
        uname = user.username or ""
        if not u:
            ref = 0
            if isinstance(event, Message) and event.text and event.text.startswith("/start ref_"):
                p = event.text.split("ref_", 1)[1].strip()
                if p.isascii() and p.isdigit() and len(p) <= 18 and int(p) != user.id and await db.get_user(int(p)):
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
@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await send_menu(m.bot, m.chat.id, await menu_text(m.from_user), main_kb(m.from_user.id),
                    bottom_panel=user_panel.BOTTOM_PANEL)


@r.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, await menu_text(c.from_user), main_kb(c.from_user.id))
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
    await send_menu(c.bot, c.from_user.id, await menu_text(c.from_user), main_kb(c.from_user.id),
                    bottom_panel=user_panel.BOTTOM_PANEL)


# ---------------- бесплатные дни ----------------
@r.callback_query(F.data == "free")
async def cb_free(c: CallbackQuery, state: FSMContext):
    await state.clear()
    days = db.trial_days()
    until = await db.claim_trial(c.from_user.id, days)
    if not until:
        await c.answer(await t("free_used"), show_alert=True)
        return
    await show(c, await t("free_ok", days=days, until=fdate(until)), kb(back()))
    await c.answer()


# ---------------- профиль ----------------
async def ref_numbers(uid):
    refs = (await db.one("SELECT count(*) n FROM users WHERE ref_by=?", (uid,)))["n"]
    profit = (await db.one("SELECT coalesce(sum(amount),0) s FROM tx WHERE user=? AND kind='ref_bonus'", (uid,)))["s"]
    return refs, profit


@r.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, *(await profile_view(c.from_user.id)))
    await c.answer()


async def profile_view(uid):
    u = await db.get_user(uid)
    refs, _ = await ref_numbers(u["id"])
    pay = await db.one("SELECT count(*) n, coalesce(sum(price),0) s FROM payments WHERE user=? AND status IN ('ok','balance')", (u["id"],))
    text = await t(
        "profile", id=u["id"], name=esc(u["name"]), username=("@" + u["username"]) if u["username"] else "—",
        joined=fdate(u["joined"]), status=sub_status(u), left=left_text(u), purchases=pay["n"],
        spent=money(pay["s"]), discount=u["discount"], refs=refs, balance=money(u["balance"]),
        percent=money(db.ref_percent()),
    )
    return text, kb([B("refsys"), B("promo")], [B("sub"), B("free")], back())


@r.widget_query()
async def widget_navigation(query: WidgetQuery, state: FSMContext | None = None):
    uid = query.from_user.id
    if not query.chat or query.chat.id != uid or query.chat.type != ChatType.PRIVATE:
        await query.answer(data="Откройте личный чат с ботом.")
        return
    if not isinstance(query.type, WidgetQueryTypeData) or not query.data.startswith("nav:"):
        await query.answer(data="Кнопка недоступна.")
        return
    user = await db.get_user(uid)
    if not user or user["banned"]:
        await query.answer(data="Доступ закрыт.")
        return
    if uid != ADMIN_ID and await unsubscribed(query.bot, uid):
        await query.answer(data="Проверьте обязательную подписку через /start.")
        return

    _, action = query.data.split(":", 1)
    platform = action.removeprefix("mk:view:")
    if state is not None:
        await state.clear()
    if action == "menu":
        content = await menu_text(query.from_user), main_kb(uid)
    elif action == "userpanel:catalog":
        content = user_panel.catalog_view()
    elif action == "nolink":
        await query.answer(data=await t("no_link"))
        return
    elif action == "sub":
        content = await subscription_view(uid)
    elif action == "free":
        days = db.trial_days()
        until = await db.claim_trial(uid, days)
        if not until:
            await query.answer(data=await t("free_used"))
            return
        content = await t("free_ok", days=days, until=fdate(until)), kb(back())
    elif action == "profile":
        content = await profile_view(uid)
    elif action == "help_menu":
        content = "💬 <b>Помощь</b>\nВыберите раздел.", user_panel.help_view()
    elif action == "plugin_list":
        content = await fulfillment.catalog(uid)
    elif action == "marketplaces":
        content = await marketplaces.accounts_view(uid)
    elif action.startswith("mk:view:") and platform in marketplaces.PLATFORMS:
        content = await marketplaces.platform_view(uid, platform)
    elif action == "a:menu" and uid == ADMIN_ID:
        content = "🛠 <b>Админ-панель</b>", admin.menu_kb()
    else:
        await query.answer(data="Раздел недоступен.")
        return

    text, markup = content
    if markup is None:
        await query.answer(data=text)
        return
    if action in ("menu", "userpanel:catalog"):
        await send_menu(query.bot, uid, text, markup)
    else:
        await query.bot.send_message(uid, text, reply_markup=markup, disable_web_page_preview=True)
    await query.answer()


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
    paying = (await db.one(
        "SELECT count(DISTINCT p.user) n FROM payments p JOIN users u ON u.id=p.user "
        "WHERE u.ref_by=? AND p.status IN ('ok','balance')", (uid,),
    ))["n"]
    active_refs = (await db.one("SELECT count(*) n FROM users WHERE ref_by=? AND sub_until>?", (uid, int(time.time()))))["n"]
    today = int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
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


@r.message(U.transfer, F.text, form_input)
async def in_transfer(m: Message, state: FSMContext):
    try:
        receiver, raw_amount = m.text.split()
        to = int(receiver)
        amount = round(float(raw_amount.replace(",", ".")), 2)
        if not 0 < to < 2**63 or not math.isfinite(amount):
            raise ValueError
    except ValueError:
        await m.answer("Формат: <code>ID сумма</code>", reply_markup=kb(back("refsys")))
        return
    u = await db.get_user(m.from_user.id)
    result = await db.transfer(u["id"], to, amount)
    if result != "ok":
        reason = {
            "target": "Получатель не найден или недоступен.",
            "amount": "Проверьте сумму, баланс и ID получателя.",
        }[result]
        await m.answer(reason, reply_markup=kb(back("refsys")))
        return
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


@r.message(U.promo, F.text, form_input)
async def in_promo(m: Message, state: FSMContext):
    code = m.text.strip().upper()
    p = await db.redeem_promo(m.from_user.id, code)
    if not p:
        await m.answer(await t("promo_bad"), reply_markup=kb(back("sub")))
        return
    await state.clear()
    key = "promo_days" if p["kind"] == "days" else "promo_disc"
    await m.answer(await t(key, value=p["value"]), reply_markup=kb(back("sub")))


# ---------------- подписка ----------------
def price_of(tar, disc):
    return db.price_for(tar, disc)


def tar_btn_text(tar, disc=0):
    b = f" ({tar['b']} 🔥)" if tar.get("b") else ""
    return f"💎 {tar['t']} | {money(price_of(tar, disc))} ₽{b}"


@r.callback_query(F.data == "sub")
async def cb_sub(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, *(await subscription_view(c.from_user.id)))
    await c.answer()


async def subscription_view(uid):
    u = await db.get_user(uid)
    tars = db.tariffs()
    lines = []
    for i, x in enumerate(tars):
        pre = "┌" if i == 0 else ("└" if i == len(tars) - 1 else "├")
        lines.append(f"{pre} {esc(x['t'])} · {money(price_of(x, u['discount']))} ₽" + (f" ({esc(x['b'])})" if x.get("b") else ""))
    disc = f"\n🎟 Ваша скидка: <b>{u['discount']}%</b>" if u["discount"] else ""
    text = await t("sub_main", status=sub_status(u), id=u["id"], accounts=len(await db.get_marketplace_accounts(u["id"])), tariffs="\n".join(lines), discount_line=disc)
    rows = [[B("promo")]]
    manual = await db.one("SELECT id FROM payments WHERE user=? AND status='manual'", (u["id"],))
    if manual:
        rows.append([IB(text="Чек или реквизиты СБП", callback_data=f"resume:{manual['id']}", style="primary")])
    if await db.one("SELECT 1 FROM payments WHERE user=? AND status='pending'", (u["id"],)):
        text += "\n\nЧек на проверке."
    rows += [[IB(text=tar_btn_text(x, u["discount"]), callback_data=f"buy:{i}", style="primary")] for i, x in enumerate(tars)]
    rows += [[B("free")], [B("refsys")], [back()]]
    return text, KB(inline_keyboard=rows)


@r.callback_query(F.data.regexp(r"^buy:\d{1,4}$"))
async def cb_buy(c: CallbackQuery, state: FSMContext):
    tariffs = db.tariffs()
    i = int(c.data.split(":")[1])
    if not 0 <= i < len(tariffs):
        await c.answer("Тариф изменился. Откройте подписку заново.", show_alert=True)
        return
    tariff = tariffs[i]
    user = await db.get_user(c.from_user.id)
    discount = user["discount"]
    price = price_of(tariff, discount)
    stars_amount = max(1, math.ceil(price * db.stars_rate()))
    nonce = secrets.token_urlsafe(8)
    await state.clear()
    await state.set_data({"quote": {"tariff": tariff, "discount": discount, "stars": stars_amount, "id": nonce}})
    discount_text = f" (скидка {discount}%)" if discount else ""
    text = await t("pay_choose", tariff=esc(tariff["t"]), price=money(price),
                   disc=discount_text, balance=money(user["balance"]))
    methods = [
        B("pay_stars", f"pay:stars:{nonce}", title=f"{btn_title('pay_stars')} · {stars_amount} XTR"),
        B("pay_balance", f"pay:balance:{nonce}"),
    ]
    if db.gs("t:requisites"):
        methods.append(B("pay_manual", f"pay:manual:{nonce}"))
    if CRYPTO_TOKEN:
        methods.append(B("pay_crypto", f"pay:crypto:{nonce}"))
    await show(c, text, kb(*methods, back("sub")))
    await c.answer()


@r.callback_query(F.data.regexp(r"^pay:(stars|balance|manual|crypto):[\w-]{11}$"))
async def cb_payment(c: CallbackQuery, state: FSMContext):
    _, method, nonce = c.data.split(":")
    quote = (await state.get_data()).get("quote")
    if not quote or quote["id"] != nonce:
        await c.answer("Откройте тариф заново.", show_alert=True)
        return
    if (method == "manual" and not db.gs("t:requisites")) or (method == "crypto" and not CRYPTO_TOKEN):
        await c.answer("Способ оплаты недоступен.", show_alert=True)
        return

    tariff, discount = quote["tariff"], quote["discount"]
    uid = c.from_user.id
    stars = quote["stars"] if method == "stars" else None
    status = "crypto_creating" if method == "crypto" else method
    result, order = await db.reserve_payment(uid, tariff, discount, status,
                                             request_key=nonce, stars_amount=stars)
    if result != "ok":
        error = {
            "balance": "Недостаточно средств на внутреннем балансе.",
            "pending": "У вас уже есть оплата СБП. Откройте её в разделе «Подписка».",
            "used": "Этот заказ уже оформлен.",
            "changed": "Условия оплаты изменились. Откройте тариф заново.",
        }[result]
        await c.answer(error, show_alert=True)
        return
    await state.clear()

    if method == "balance":
        _, ref, bonus = order
        until = (await db.get_user(uid))["sub_until"]
        await show(c, await t("pay_bal_ok", days=tariff["d"], until=fdate(until)), kb(back()))
        await c.answer()
        await crypto.notify_referrer(c.bot, ref, bonus)
        return

    await c.answer()
    price = price_of(tariff, discount)
    if method == "manual":
        text = await t("pay_manual", tariff=esc(tariff["t"]), price=money(price),
                       requisites=await t("requisites"))
        await show(c, text, manual_keyboard(order))
        return
    if method == "stars":
        try:
            await c.bot.send_invoice(
                chat_id=uid, title="Подписка Seller Panel",
                description=f"Доступ на {tariff['d']} дней",
                payload=f"sub:{order}", currency="XTR", provider_token="",
                prices=[LabeledPrice(label="Подписка", amount=stars)],
                start_parameter=f"subscription-{order}",
            )
        except Exception:
            await db.fail_payment(order, "stars", "notify_error")
            await show(c, "Не удалось отправить счёт Stars. Откройте тариф заново.", kb(back("sub")))
        return

    try:
        inv = await crypto.create_invoice(price, f"Подписка {tariff['t']} (ID {uid})")
        url = inv.get("bot_invoice_url") or inv.get("pay_url")
        if not url or not url.startswith("https://"):
            raise ValueError("Missing Crypto Pay invoice URL")
        await db.ex("UPDATE payments SET status='crypto',invoice_id=? WHERE id=? AND status='crypto_creating'",
                    (int(inv["invoice_id"]), order))
    except Exception:
        await db.fail_payment(order, "crypto_creating", "notify_error")
        await show(c, "Не удалось создать счёт Crypto Pay. Откройте тариф заново.", kb(back("sub")))
        return
    await show(c, await t("pay_crypto", tariff=esc(tariff["t"]), price=money(price)),
               kb(B("pay_link", url=url), B("pay_check", f"chkc:{order}"), back("sub")))


@r.callback_query(F.data.regexp(r"^(buy|payb|paym|pays|payc|paid):"))
async def stale_payment_button(c: CallbackQuery):
    await c.answer("Эта кнопка устарела. Откройте раздел «Подписка» заново.", show_alert=True)


def stars_payment_id(payload):
    prefix, _, raw_id = payload.partition(":")
    if prefix == "sub" and raw_id.isascii() and raw_id.isdigit() and len(raw_id) <= 18:
        return int(raw_id)
    return None


@r.pre_checkout_query()
async def stars_pre_checkout(query: PreCheckoutQuery):
    pid = stars_payment_id(query.invoice_payload)
    ok = bool(pid and query.currency == "XTR" and
              await db.approve_stars_checkout(pid, query.from_user.id, query.total_amount))
    await query.answer(ok=ok, error_message=None if ok else "Счёт устарел. Откройте тариф заново.")


@r.message(F.successful_payment)
async def stars_success(m: Message):
    payment = m.successful_payment
    pid = stars_payment_id(payment.invoice_payload)
    if not pid or payment.currency != "XTR":
        return
    result = await db.finish_payment(pid, "stars_checkout", uid=m.from_user.id,
                                     charge_id=payment.telegram_payment_charge_id, stars_amount=payment.total_amount)
    if result:
        await crypto.notify_payment(m.bot, result)
    elif not await db.one("SELECT 1 FROM payments WHERE charge_id=? AND user=?",
                          (payment.telegram_payment_charge_id, m.from_user.id)):
        logging.error("Unmatched Telegram Stars payment for order %s", pid)
        await m.answer("Платёж получен Telegram. Напишите /paysupport и укажите номер заказа.")


@r.message(Command("paysupport"))
async def pay_support(m: Message, state: FSMContext):
    await state.clear()
    url = db.gs("link_support")
    markup = kb(B("support", url=url), back("sub")) if url else kb(back("sub"))
    await m.answer("Для вопросов по оплате укажите номер заказа и дату платежа. "
                   + ("Напишите в поддержку." if url else f"Передайте данные администратору бота (ID {ADMIN_ID})."),
                   reply_markup=markup)


@r.callback_query(F.data.regexp(r"^chkc:\d{1,18}$"))
async def cb_chk_crypto(c: CallbackQuery):
    if not CRYPTO_TOKEN:
        await c.answer("Crypto Pay сейчас недоступен.", show_alert=True)
        return
    try:
        result = await crypto.check_and_confirm(c.bot, int(c.data.split(":")[1]), c.from_user.id)
    except Exception:
        result = "unavailable"
    text = {
        "paid": "Оплата подтверждена ✅",
        "wait": "Оплата пока не найдена.",
        "expired": "Срок оплаты истёк. Создайте новый счёт.",
        "unavailable": "Не удалось проверить счёт. Попробуйте позже.",
        "none": "Счёт не найден.",
    }[result]
    await c.answer(text, show_alert=True)


def manual_keyboard(pid):
    return kb(B("paid", f"receipt:{pid}"),
              IB(text="Отменить неоплаченный счёт", callback_data=f"ask_cancel:{pid}"), back("sub"))


@r.callback_query(F.data.regexp(r"^ask_cancel:\d{1,18}$"))
async def ask_cancel_manual(c: CallbackQuery, state: FSMContext):
    await state.clear()
    pid = int(c.data.split(":")[1])
    markup = kb(IB(text="Да, я не платил", callback_data=f"cancel_manual:{pid}", style="danger"),
                IB(text="Вернуться к счёту", callback_data=f"resume:{pid}"))
    await show(c, "Отменяйте счёт, только если ещё не переводили деньги.", markup)
    await c.answer()


@r.callback_query(F.data.regexp(r"^cancel_manual:\d{1,18}$"))
async def cancel_manual(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[1])
    if not await db.fail_payment(pid, "manual", "cancelled", uid=c.from_user.id):
        await c.answer("Счёт уже обработан.", show_alert=True)
        return
    await cb_sub(c, state)


@r.callback_query(F.data.regexp(r"^resume:\d{1,18}$"))
async def resume_manual(c: CallbackQuery, state: FSMContext):
    await state.clear()
    pid = int(c.data.split(":")[1])
    p = await db.one("SELECT * FROM payments WHERE id=? AND user=? AND status='manual'", (pid, c.from_user.id))
    if not p:
        await c.answer("Счёт уже обработан.", show_alert=True)
        return
    text = await t("pay_manual", tariff=f"{p['days']} дн.", price=money(p["price"]), requisites=await t("requisites"))
    await show(c, text, manual_keyboard(pid))
    await c.answer()


@r.callback_query(F.data.regexp(r"^receipt:\d{1,18}$"))
async def cb_paid_state(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[1])
    if not await db.one("SELECT 1 FROM payments WHERE id=? AND user=? AND status='manual'", (pid, c.from_user.id)):
        await c.answer("Счёт уже обработан.", show_alert=True)
        return
    await state.set_state(U.receipt)
    await state.set_data({"manual_id": pid})
    await show(c, "📎 <b>Пришлите чек</b>\nОтправьте фото или PDF одним сообщением.", kb(back("sub")))
    await c.answer()


@r.message(U.receipt, form_input)
async def in_receipt(m: Message, state: FSMContext):
    if m.photo:
        attachment, kind = m.photo[-1], "photo"
    elif m.document and (m.document.mime_type == "application/pdf" or (m.document.mime_type or "").startswith("image/")):
        attachment, kind = m.document, "document"
    else:
        await m.answer("Пришлите фото или PDF чека.")
        return
    pid = (await state.get_data()).get("manual_id")
    receipt = (attachment.file_id, kind, attachment.file_unique_id)
    status, p = await db.attach_receipt(m.from_user.id, pid, receipt)
    if status != "ok":
        await m.answer({
            "changed": "Счёт уже обработан.",
            "pending": "У вас уже есть чек на проверке.",
            "used": "Этот чек уже был подтверждён.",
        }[status], reply_markup=kb(back("sub")))
        return
    await state.clear()
    caption = (
        f"💳 <b>Заявка #{pid}</b>\n"
        f"Пользователь: {esc(m.from_user.full_name)} · <code>{m.from_user.id}</code>\n"
        f"Подписка: {p['days']} дн. · <b>{money(p['price'])} ₽</b>"
    )
    markup = kb([IB(text="Подтвердить", callback_data=f"a:pok:{pid}", style="success"),
                 IB(text="Отклонить", callback_data=f"a:pno:{pid}", style="danger")])
    try:
        send = m.bot.send_photo if kind == "photo" else m.bot.send_document
        await send(ADMIN_ID, attachment.file_id, caption=caption, reply_markup=markup)
    except Exception:
        logging.warning("Unable to deliver receipt %s to admin", pid)
        await m.answer(f"Чек #{pid} сохранён. Администратор сможет открыть его в очереди платежей.",
                       reply_markup=kb(back("sub")))
        return
    await m.answer(f"✅ Чек #{pid} отправлен на проверку.", reply_markup=kb(back("sub")))


@r.callback_query()
async def unavailable_button(c: CallbackQuery):
    await c.answer("Этот раздел недоступен. Откройте меню через /start.", show_alert=True)


# ---------------- запуск ----------------
def build_dispatcher():
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.message.outer_middleware(Gate())
    dp.callback_query.outer_middleware(Gate())
    dp.include_router(admin.r)
    dp.include_router(user_panel.r)
    dp.include_router(fulfillment.r)
    import offer_jobs, seller_events, seller_desk
    dp.include_router(offer_jobs.r)
    dp.include_router(seller_events.r)
    dp.include_router(seller_desk.r)
    dp.include_router(marketplaces.r)
    dp.include_router(r)
    return dp


async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в файле .env")
    dp = build_dispatcher()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await db.init()
    import seller_events
    delivery = asyncio.create_task(fulfillment.worker())
    watcher = asyncio.create_task(seller_events.monitor(bot))
    poller = None
    try:
        delay = 2
        while True:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                me = await bot.get_me()
                break
            except TelegramNetworkError:
                logging.warning("Telegram API недоступен. Следующая попытка через %s с.", delay)
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
        logging.info("Бот @%s запущен", me.username)
        if CRYPTO_TOKEN:
            poller = asyncio.create_task(crypto.poller(bot))
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        delivery.cancel()
        await asyncio.gather(delivery, return_exceptions=True)
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        if poller is not None:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
