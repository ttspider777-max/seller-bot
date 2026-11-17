import asyncio
import csv
import io
import logging
import secrets
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as KB, Message

import db
from config import ADMIN_ID
from texts import BUTTONS, TEXTS, B, btn_title, esc, money
from util import fdate, kb, show

r = Router()
r.message.filter(F.from_user.id == ADMIN_ID)
r.callback_query.filter(F.from_user.id == ADMIN_ID)


class A(StatesGroup):
    inp = State()


def home():
    return IB(text="« В админ-панель", callback_data="a:menu")


def G(text, cb):
    return IB(text=text, callback_data=cb, style="success")  # зелёная кнопка


def menu_kb():
    return kb(
        [G("📊 Статистика", "a:stats"), G("👥 Пользователи", "a:users:0")],
        [G("🔎 Найти / Бан", "a:ask:find")],
        [G("🎁 Выдать подписку", "a:ask:grant"), G("➖ Забрать подписку", "a:ask:revoke")],
        [G("🎟 Промокоды", "a:promos"), G("📢 Обяз. подписка", "a:chans")],
        [G("✏️ Тексты", "a:texts"), G("🔘 Кнопки / эмодзи", "a:btns")],
        [G("👋 Приветствие", "a:text:welcome"), G("🔗 Ссылки", "a:links")],
        [G("💳 Тарифы и оплата", "a:money")],
        [G("📨 Рассылка", "a:ask:bcast"), G("📁 Экспорт", "a:export")],
        [IB(text="« Главное меню", callback_data="menu")],
    )


@r.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Админ-панель</b>", reply_markup=menu_kb())


@r.callback_query(F.data == "a:menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "🛠 <b>Админ-панель</b>", menu_kb())
    await c.answer()


# ---------- статистика ----------
@r.callback_query(F.data == "a:stats")
async def cb_stats(c: CallbackQuery):
    now = int(time.time())
    day = now - 86400
    n = lambda sql, *a: db.one(sql, a)
    total = (await n("SELECT count(*) x FROM users"))["x"]
    new = (await n("SELECT count(*) x FROM users WHERE joined>?", day))["x"]
    act = (await n("SELECT count(*) x FROM users WHERE sub_until>?", now))["x"]
    ban = (await n("SELECT count(*) x FROM users WHERE banned=1"))["x"]
    trial = (await n("SELECT count(*) x FROM users WHERE trial_used=1"))["x"]
    pay = await n("SELECT count(*) x, coalesce(sum(price),0) s FROM payments WHERE status IN ('ok','balance')")
    pend = (await n("SELECT count(*) x FROM payments WHERE status='pending'"))["x"]
    text = (
        "📊 <b>Статистика</b>\n\n<blockquote>"
        f"👥 Пользователей: {total} (+{new} за 24ч)\n💎 С подпиской: {act}\n⛔ Забанено: {ban}\n"
        f"🔥 Брали пробный период: {trial}\n💰 Оплат: {pay['x']} на {money(pay['s'])} ₽\n⏳ Заявок на проверке: {pend}</blockquote>"
    )
    await show(c, text, kb(home()))
    await c.answer()


# ---------- пользователи ----------
@r.callback_query(F.data.startswith("a:users:"))
async def cb_users(c: CallbackQuery):
    page = int(c.data.split(":")[2])
    per = 10
    total = (await db.one("SELECT count(*) x FROM users"))["x"]
    rows = await db.q("SELECT * FROM users ORDER BY joined DESC LIMIT ? OFFSET ?", (per, page * per))
    btns = []
    for u in rows:
        mark = ("⛔" if u["banned"] else "💎" if db.active(u) else "▫️")
        uname = f"@{u['username']}" if u["username"] else "—"
        btns.append([IB(text=f"{mark} {u['id']} · {u['name'][:15]} · {uname}", callback_data=f"a:u:{u['id']}")])
    nav = []
    if page > 0:
        nav.append(IB(text="◀️", callback_data=f"a:users:{page - 1}"))
    if (page + 1) * per < total:
        nav.append(IB(text="▶️", callback_data=f"a:users:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([home()])
    await show(c, f"👥 <b>Пользователи</b> ({total})\n💎 есть подписка · ⛔ бан · ▫️ нет подписки", KB(inline_keyboard=btns))
    await c.answer()


async def user_card(uid):
    u = await db.get_user(uid)
    if not u:
        return "Пользователь не найден.", kb(home())
    uname = f"@{u['username']}" if u["username"] else "—"
    sub = f"до {fdate(u['sub_until'])}" if db.active(u) else "нет"
    refs = (await db.one("SELECT count(*) x FROM users WHERE ref_by=?", (uid,)))["x"]
    text = (
        f"👤 <b>{esc(u['name'])}</b>\n\n<blockquote>🆔 <code>{u['id']}</code>\n🔗 {uname}\n"
        f"📅 Запустил бота: {fdate(u['joined'], True)}\n💎 Подписка: {sub}\n💰 Баланс: {money(u['balance'])} ₽\n"
        f"👥 Рефералов: {refs}  (пригласил: {u['ref_by'] or '—'})\n🔥 Пробный период: {'взят' if u['trial_used'] else 'нет'}\n"
        f"⛔ Бан: {'ДА' if u['banned'] else 'нет'}</blockquote>"
    )
    ban_btn = IB(text="✅ Разбанить", callback_data=f"a:unban:{uid}") if u["banned"] else IB(text="⛔ Забанить", callback_data=f"a:ban:{uid}")
    return text, kb(
        [ban_btn],
        [IB(text="🎁 Выдать дни", callback_data=f"a:ask:grant_u:{uid}"), IB(text="➖ Забрать", callback_data=f"a:ask:revoke_u:{uid}")],
        [IB(text="💰 Изменить баланс", callback_data=f"a:ask:bal_u:{uid}")],
        [IB(text="« К списку", callback_data="a:users:0"), home()],
    )


@r.callback_query(F.data.startswith("a:u:"))
async def cb_user(c: CallbackQuery):
    text, k = await user_card(int(c.data.split(":")[2]))
    await show(c, text, k)
    await c.answer()


@r.callback_query(F.data.regexp(r"^a:(ban|unban):\d+$"))
async def cb_ban(c: CallbackQuery):
    _, act, uid = c.data.split(":")
    uid = int(uid)
    if uid == ADMIN_ID:
        await c.answer("Себя банить нельзя 🙂", show_alert=True)
        return
    await db.ex("UPDATE users SET banned=? WHERE id=?", (1 if act == "ban" else 0, uid))
    text, k = await user_card(uid)
    await show(c, text, k)
    await c.answer("Готово")


# ---------- универсальный ввод ----------
PROMPTS = {
    "find": "🔎 Отправьте ID или @username пользователя.",
    "grant": "🎁 Отправьте <code>ID дней</code>, например: <code>123456789 30</code>",
    "revoke": "➖ Отправьте <code>ID</code> (забрать всю подписку) или <code>ID дней</code> (уменьшить).",
    "grant_u": "🎁 Сколько дней выдать?",
    "revoke_u": "➖ Сколько дней забрать? (<code>0</code> — забрать всю подписку)",
    "bal_u": "💰 На сколько изменить баланс? (например <code>100</code> или <code>-50</code>)",
    "promo_new": (
        "🎟 Новый промокод. Отправьте одной строкой:\n<code>КОД ТИП ЗНАЧЕНИЕ ЛИМИТ</code>\n\n"
        "• ТИП: <code>days</code> (дни подписки) или <code>discount</code> (скидка %)\n"
        "• КОД <code>-</code> — сгенерировать случайный\n• ЛИМИТ <code>0</code> — без лимита\n\n"
        "Примеры:\n<code>START3 days 3 100</code>\n<code>SALE20 discount 20 0</code>"
    ),
    "chan_add": (
        "📢 Отправьте <code>@username</code> или ID (<code>-100…</code>) канала/чата.\n"
        "⚠️ Бот должен быть администратором в нём, иначе проверка не сработает."
    ),
    "bcast": "📨 Отправьте сообщение для рассылки (можно с фото, форматированием, премиум-эмодзи).",
}


def prompt_for(act):
    base = act.split(":")[0]
    if base == "text":
        key = act.split(":")[1]
        cur = db.gs("t:" + key) or TEXTS[key][1]
        ph = TEXTS[key][2]
        return (f"✏️ <b>{TEXTS[key][0]}</b>\nПлейсхолдеры: <code>{esc(ph) or 'нет'}</code>\n\n"
                f"Текущий текст (HTML):\n<pre>{esc(cur)}</pre>\nОтправьте новый текст. Форматирование и премиум-эмодзи сохранятся.")
    if base == "btn_name":
        key = act.split(":")[1]
        return f"🔘 Текущее название: <b>{esc(btn_title(key))}</b>\nОтправьте новое название кнопки."
    if base == "btn_emo":
        return "😀 Отправьте сообщение с <b>одним премиум-эмодзи</b> (или его числовой ID). <code>-</code> — убрать эмодзи."
    if base == "link":
        return "🔗 Отправьте ссылку (https://t.me/… ). <code>-</code> — удалить ссылку."
    if base == "set":
        return "Отправьте новое значение (число)."
    if base == "tariff":
        i = int(act.split(":")[1])
        x = db.tariffs()[i]
        return (f"💳 Тариф: <code>{esc(x['t'])} | {x['d']} | {x['p']} | {esc(x['b'])}</code>\n\n"
                "Отправьте в формате:\n<code>Название | дней | цена | пометка</code>\nПометка (например <code>-10%</code>) может быть пустой.")
    return PROMPTS[base]


@r.callback_query(F.data.startswith("a:ask:"))
async def cb_ask(c: CallbackQuery, state: FSMContext):
    act = c.data[len("a:ask:"):]
    await state.set_state(A.inp)
    await state.update_data(act=act)
    await show(c, prompt_for(act), kb([IB(text="✖️ Отмена", callback_data="a:menu")]))
    await c.answer()


def custom_emoji_id(m: Message):
    for e in (m.entities or []):
        if e.type == "custom_emoji":
            return e.custom_emoji_id
    t = (m.text or "").strip()
    return t if t.isdigit() else None


async def run_broadcast(bot, m: Message):
    users = await db.q("SELECT id FROM users WHERE banned=0")
    ok = bad = 0
    for u in users:
        try:
            await bot.copy_message(u["id"], m.chat.id, m.message_id)
            ok += 1
        except Exception:
            bad += 1
        await asyncio.sleep(0.05)
    await bot.send_message(ADMIN_ID, f"📨 Рассылка завершена: доставлено {ok}, ошибок {bad}.")


async def notify(bot, uid, text):
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass


@r.message(A.inp)
async def on_input(m: Message, state: FSMContext):
    act = (await state.get_data())["act"]
    base, _, arg = act.partition(":")
    txt = (m.text or "").strip()
    done = kb(home())

    async def finish(text, markup=done):
        await state.clear()
        await m.answer(text, reply_markup=markup)

    async def err(text):
        await m.answer(text + "\n\nПопробуйте ещё раз или нажмите «Отмена» в сообщении выше.")

    try:
        if base == "find":
            u = await db.find_user(txt)
            if not u:
                return await err("❌ Не найден (пользователь должен был запускать бота).")
            text, k = await user_card(u["id"])
            return await finish(text, k)

        if base in ("grant", "revoke", "grant_u", "revoke_u", "bal_u"):
            parts = txt.split()
            if base.endswith("_u"):
                uid, rest = int(arg), parts
            else:
                uid, rest = int(parts[0].lstrip("@")) if parts and parts[0].lstrip("@").isdigit() else None, parts[1:]
                if uid is None:
                    u = await db.find_user(parts[0]) if parts else None
                    uid = u["id"] if u else None
            if not uid or not await db.get_user(uid):
                return await err("❌ Пользователь не найден.")
            if base in ("grant", "grant_u"):
                days = int(rest[0])
                until = await db.add_days(uid, days)
                await notify(m.bot, uid, f"🎁 Вам выдана подписка на <b>{days}</b> дн. Действует до <b>{fdate(until)}</b>.")
                return await finish(f"✅ Выдано {days} дн. пользователю {uid}. Подписка до {fdate(until)}.")
            if base in ("revoke", "revoke_u"):
                days = int(rest[0]) if rest else 0
                if days <= 0:
                    await db.ex("UPDATE users SET sub_until=0 WHERE id=?", (uid,))
                    await notify(m.bot, uid, "ℹ️ Ваша подписка была отменена администратором.")
                    return await finish(f"✅ Подписка пользователя {uid} полностью забрана.")
                until = await db.add_days(uid, -days)
                return await finish(f"✅ Забрано {days} дн. у {uid}. Теперь: {fdate(until) if until else 'нет подписки'}.")
            amount = float(rest[0].replace(",", "."))
            await db.add_balance(uid, amount, "admin", "")
            return await finish(f"✅ Баланс {uid} изменён на {money(amount)} ₽.")

        if base == "promo_new":
            code, kind, value, limit = txt.split()
            code = ("P" + secrets.token_hex(3)).upper() if code == "-" else code.upper()
            kind = {"days": "days", "дни": "days", "discount": "discount", "скидка": "discount", "percent": "discount"}.get(kind.lower())
            if not kind or int(value) <= 0:
                raise ValueError
            await db.ex("INSERT OR REPLACE INTO promos(code,kind,value,max_uses,used,active) VALUES(?,?,?,?,0,1)",
                        (code, kind, int(value), int(limit)))
            return await finish(f"✅ Промокод <code>{code}</code> создан.", kb([IB(text="🎟 К промокодам", callback_data="a:promos")], home()))

        if base == "chan_add":
            ref = int(txt) if txt.lstrip("-").isdigit() else (txt if txt.startswith("@") else "@" + txt.split("/")[-1])
            chat = await m.bot.get_chat(ref)
            link = f"https://t.me/{chat.username}" if chat.username else None
            if not link:
                try:
                    link = (await m.bot.create_chat_invite_link(chat.id)).invite_link
                except Exception:
                    link = None
            await db.ex("INSERT INTO channels(chat_id,title,link) VALUES(?,?,?)", (str(chat.id), chat.title or str(chat.id), link))
            warn = "" if link else "\n⚠️ Ссылку получить не удалось — пользователям не покажется кнопка."
            return await finish(f"✅ Добавлено: {esc(chat.title)}{warn}", kb([IB(text="📢 К списку", callback_data="a:chans")], home()))

        if base == "bcast":
            asyncio.create_task(run_broadcast(m.bot, m))
            return await finish("📨 Рассылка запущена, по окончании пришлю отчёт.")

        if base == "text":
            if arg not in TEXTS:
                raise ValueError
            await db.set_s("t:" + arg, m.html_text)
            return await finish("✅ Текст сохранён.", kb([IB(text="✏️ К текстам", callback_data="a:texts")], home()))

        if base == "btn_name":
            await db.set_s("b:" + arg, txt)
            return await finish("✅ Название сохранено.", kb([IB(text="🔘 К кнопкам", callback_data="a:btns")], home()))

        if base == "btn_emo":
            if txt == "-":
                await db.del_s("e:" + arg)
                return await finish("✅ Эмодзи убрано.", kb([IB(text="🔘 К кнопкам", callback_data="a:btns")], home()))
            eid = custom_emoji_id(m)
            if not eid:
                return await err("❌ Не нашёл премиум-эмодзи в сообщении.")
            await db.set_s("e:" + arg, eid)
            return await finish(
                "✅ Премиум-эмодзи сохранено (ID <code>%s</code>).\n"
                "Подсказка: уберите обычный эмодзи из названия кнопки, иначе будет два." % eid,
                kb([IB(text="🔘 К кнопкам", callback_data="a:btns")], home()),
            )

        if base == "link":
            if txt == "-":
                await db.del_s(arg)
            else:
                if not txt.startswith(("http://", "https://", "tg://")):
                    return await err("❌ Ссылка должна начинаться с https://")
                await db.set_s(arg, txt)
            return await finish("✅ Ссылка сохранена.", kb([IB(text="🔗 К ссылкам", callback_data="a:links")], home()))

        if base == "set":
            float(txt.replace(",", "."))
            await db.set_s(arg, txt.replace(",", "."))
            return await finish("✅ Сохранено.", kb([IB(text="💳 К тарифам", callback_data="a:money")], home()))

        if base == "tariff":
            parts = [p.strip() for p in txt.split("|")]
            tars = db.tariffs()
            tars[int(arg)] = {"t": parts[0], "d": int(parts[1]), "p": float(parts[2].replace(",", ".")), "b": parts[3] if len(parts) > 3 else ""}
            import json
            await db.set_s("tariffs", json.dumps(tars, ensure_ascii=False))
            return await finish("✅ Тариф сохранён.", kb([IB(text="💳 К тарифам", callback_data="a:money")], home()))
    except (ValueError, IndexError):
        return await err("❌ Неверный формат.")
    except Exception as e:
        logging.exception("admin input")
        return await err(f"❌ Ошибка: {esc(e)}")


# ---------- промокоды ----------
@r.callback_query(F.data == "a:promos")
async def cb_promos(c: CallbackQuery):
    rows = await db.q("SELECT * FROM promos ORDER BY rowid DESC LIMIT 30")
    btns = [[IB(text="➕ Создать промокод", callback_data="a:ask:promo_new")]]
    for p in rows:
        kind = f"+{p['value']} дн." if p["kind"] == "days" else f"-{p['value']}%"
        lim = p["max_uses"] or "∞"
        btns.append([IB(text=f"{'🟢' if p['active'] else '⚪'} {p['code']} · {kind} · {p['used']}/{lim}", callback_data=f"a:ptog:{p['code']}"),
                     IB(text="🗑", callback_data=f"a:pdel:{p['code']}")])
    btns.append([home()])
    await show(c, "🎟 <b>Промокоды</b>\nНажмите на код — включить/выключить, 🗑 — удалить.", KB(inline_keyboard=btns))
    await c.answer()


@r.callback_query(F.data.startswith("a:ptog:"))
async def cb_ptog(c: CallbackQuery):
    await db.ex("UPDATE promos SET active=1-active WHERE code=?", (c.data.split(":", 2)[2],))
    await cb_promos(c)


@r.callback_query(F.data.startswith("a:pdel:"))
async def cb_pdel(c: CallbackQuery):
    await db.ex("DELETE FROM promos WHERE code=?", (c.data.split(":", 2)[2],))
    await cb_promos(c)


# ---------- обязательная подписка ----------
@r.callback_query(F.data == "a:chans")
async def cb_chans(c: CallbackQuery):
    rows = await db.q("SELECT * FROM channels")
    btns = [[IB(text="➕ Добавить канал/чат", callback_data="a:ask:chan_add")]]
    btns += [[IB(text=f"🗑 {x['title']}", callback_data=f"a:cdel:{x['id']}")] for x in rows]
    btns.append([home()])
    note = "" if rows else "\n\nСписок пуст — обязательная подписка выключена."
    await show(c, "📢 <b>Обязательная подписка</b>\nПользователи не смогут пользоваться ботом, пока не подпишутся на всё из списка." + note,
               KB(inline_keyboard=btns))
    await c.answer()


@r.callback_query(F.data.startswith("a:cdel:"))
async def cb_cdel(c: CallbackQuery):
    await db.ex("DELETE FROM channels WHERE id=?", (int(c.data.split(":")[2]),))
    await cb_chans(c)


# ---------- тексты ----------
@r.callback_query(F.data == "a:texts")
async def cb_texts(c: CallbackQuery):
    btns = [[IB(text=("✏️ " if db.gs("t:" + k) else "") + v[0], callback_data=f"a:text:{k}")] for k, v in TEXTS.items()]
    btns.append([home()])
    await show(c, "✏️ <b>Тексты бота</b>\nВыберите текст для редактирования (✏️ — уже изменён).", KB(inline_keyboard=btns))
    await c.answer()


@r.callback_query(F.data.startswith("a:text:"))
async def cb_text(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.set_state(A.inp)
    await state.update_data(act="text:" + key)
    await show(c, prompt_for("text:" + key), kb([IB(text="↩️ Сбросить на стандартный", callback_data=f"a:treset:{key}")], [IB(text="✖️ Отмена", callback_data="a:texts")]))
    await c.answer()


@r.callback_query(F.data.startswith("a:treset:"))
async def cb_treset(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await db.del_s("t:" + c.data.split(":")[2])
    await c.answer("Сброшено")
    await cb_texts(c)


# ---------- кнопки ----------
@r.callback_query(F.data == "a:btns")
async def cb_btns(c: CallbackQuery):
    btns = [[IB(text=("✏️ " if db.gs("b:" + k) or db.gs("e:" + k) else "") + v[0], callback_data=f"a:btn:{k}")] for k, v in BUTTONS.items()]
    btns.append([home()])
    await show(c, "🔘 <b>Кнопки</b>\nМожно менять название и ставить премиум-эмодзи на кнопку.", KB(inline_keyboard=btns))
    await c.answer()


@r.callback_query(F.data.startswith("a:btn:"))
async def cb_btn(c: CallbackQuery):
    key = c.data.split(":")[2]
    eid = db.gs("e:" + key)
    col = {"success": "🟩 зелёная", "primary": "🟦 синяя", "danger": "🟥 красная"}.get(db.gs("s:" + key), "⬜ обычная")
    await show(c, f"🔘 <b>{BUTTONS[key][0]}</b>\nНазвание: <b>{esc(btn_title(key))}</b>\nЦвет: {col}\nПремиум-эмодзи: {('<code>' + eid + '</code>') if eid else 'нет'}",
               kb([IB(text="📝 Название", callback_data=f"a:ask:btn_name:{key}"), IB(text="😀 Премиум-эмодзи", callback_data=f"a:ask:btn_emo:{key}")],
                  [IB(text="🟩 Зелёная", callback_data=f"a:bcol:{key}:success"), IB(text="🟦 Синяя", callback_data=f"a:bcol:{key}:primary"),
                   IB(text="🟥 Красная", callback_data=f"a:bcol:{key}:danger"), IB(text="⬜ Обычная", callback_data=f"a:bcol:{key}:none")],
                  [IB(text="↩️ Сбросить", callback_data=f"a:breset:{key}")], [IB(text="« К кнопкам", callback_data="a:btns")]))
    await c.answer()


@r.callback_query(F.data.startswith("a:bcol:"))
async def cb_bcol(c: CallbackQuery):
    _, _, key, val = c.data.split(":")
    if val == "none":
        await db.del_s("s:" + key)
    else:
        await db.set_s("s:" + key, val)
    await c.answer("Цвет сохранён")
    c.data = f"a:btn:{key}"
    await cb_btn(c)


@r.callback_query(F.data.startswith("a:breset:"))
async def cb_breset(c: CallbackQuery):
    key = c.data.split(":")[2]
    await db.del_s("b:" + key)
    await db.del_s("e:" + key)
    await db.del_s("s:" + key)
    await c.answer("Сброшено")
    await cb_btns(c)


# ---------- ссылки ----------
LINKS = [("link_news", "📰 Новости (канал)"), ("link_guide", "📖 Инструкция (канал с гайдом)"),
         ("link_features", "⚙️ Функционал (пост)"), ("link_support", "💬 Поддержка")]


@r.callback_query(F.data == "a:links")
async def cb_links(c: CallbackQuery):
    btns = [[IB(text=f"{name}: {'✅' if db.gs(k) else '—'}", callback_data=f"a:ask:link:{k}")] for k, name in LINKS]
    btns.append([home()])
    cur = "\n".join(f"{name}: {esc(db.gs(k)) or '—'}" for k, name in LINKS)
    await show(c, f"🔗 <b>Ссылки в меню</b>\n\n{cur}", KB(inline_keyboard=btns))
    await c.answer()


# ---------- тарифы и оплата ----------
@r.callback_query(F.data == "a:money")
async def cb_money(c: CallbackQuery):
    tars = db.tariffs()
    btns = [[IB(text=f"{x['t']} · {x['d']} дн. · {money(x['p'])} ₽ {x['b']}", callback_data=f"a:ask:tariff:{i}")] for i, x in enumerate(tars)]
    btns += [
        [IB(text=f"👥 Реф. процент: {money(db.ref_percent())}%", callback_data="a:ask:set:ref_percent")],
        [IB(text=f"🔥 Дней пробного периода: {db.trial_days()}", callback_data="a:ask:set:trial_days")],
        [IB(text="💳 Реквизиты для оплаты", callback_data="a:text:requisites")],
        [home()],
    ]
    await show(c, "💳 <b>Тарифы и оплата</b>\nНажмите на тариф, чтобы изменить название, срок, цену и пометку.", KB(inline_keyboard=btns))
    await c.answer()


# ---------- платежи ----------
@r.callback_query(F.data.regexp(r"^a:p(ok|no):\d+$"))
async def cb_pay(c: CallbackQuery):
    _, act, pid = c.data.split(":")
    pid = int(pid)
    p = await db.one("SELECT * FROM payments WHERE id=?", (pid,))
    if not p or p["status"] != "pending":
        await c.answer("Заявка уже обработана", show_alert=True)
        return
    if act == "ok":
        res = await db.finish_payment(pid)
        _, ref, bonus = res
        u = await db.get_user(p["user"])
        from texts import t
        await notify(c.bot, p["user"], await t("pay_ok", days=p["days"], until=fdate(u["sub_until"])))
        if ref and bonus:
            ru = await db.get_user(ref)
            if ru and ru["notif"]:
                await notify(c.bot, ref, f"💰 Реферал оплатил подписку: вам начислено <b>{money(bonus)} ₽</b>.")
        mark = "✅ Подтверждено"
    else:
        await db.ex("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
        if p["disc"]:
            await db.ex("UPDATE users SET discount=? WHERE id=?", (p["disc"], p["user"]))
        from texts import t
        await notify(c.bot, p["user"], await t("pay_no", id=pid))
        mark = "❌ Отклонено"
    try:
        if c.message.photo or c.message.document:
            await c.message.edit_caption((c.message.html_text or c.message.caption or "") + f"\n\n<b>{mark}</b>")
        else:
            await c.message.edit_text(c.message.html_text + f"\n\n<b>{mark}</b>")
    except Exception:
        pass
    await c.answer(mark)


# ---------- экспорт ----------
@r.callback_query(F.data == "a:export")
async def cb_export(c: CallbackQuery):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "name", "username", "joined", "subscription_until", "banned", "balance", "ref_by"])
    for u in await db.q("SELECT * FROM users ORDER BY joined"):
        w.writerow([u["id"], u["name"], u["username"], fdate(u["joined"], True), fdate(u["sub_until"]) if u["sub_until"] else "", u["banned"], u["balance"], u["ref_by"]])
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    await c.message.answer_document(BufferedInputFile(data, "users.csv"), caption="📁 Все пользователи бота")
    await c.answer()
