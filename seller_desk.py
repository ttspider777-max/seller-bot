import secrets
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard, Message

import db
from marketplaces import _decrypt, _has_access
from seller_api import request

r = Router()


class Compose(StatesGroup):
    reply = State()


async def operate(uid, key, op, chat=None, text=None, *, seller=None):
    if not await _has_access(uid):
        raise PermissionError
    row = await db.get_marketplace_secret(uid, key)
    if not row or (seller is not None and row["label"] != seller):
        raise PermissionError
    return await request(key.partition("/")[0], _decrypt(row["secret"]), op, chat, text)


@r.callback_query(F.data.startswith("desk:"))
async def desk(c: CallbackQuery, state: FSMContext):
    _, op, key = c.data.split(":", 2)
    uid = c.from_user.id
    if op not in ("orders", "chats", "read") or not await _has_access(uid):
        return await c.answer("Раздел недоступен.", show_alert=True)
    if op == "read":
        setting = "read:" + key
        enabled = await db.get_user_setting(uid, setting, "0") != "1"
        await db.set_user_setting(uid, setting, str(int(enabled)))
        return await c.answer("Отметка при открытии включена." if enabled else "Отметка при открытии отключена.", show_alert=True)
    await c.answer()
    try:
        link = await db.get_marketplace_secret(uid,key)
        if not link:
            raise PermissionError
        rows = await operate(uid, key, op, seller=link["label"])
    except Exception:
        return await c.message.answer("Не удалось получить данные. Проверьте подключение аккаунта и повторите запрос.")
    await state.clear()
    if op == "orders":
        lines = [escape(str(x["title"])[:90]) + " · " + escape(str(x["status"])[:40]) for x in rows]
        return await c.message.answer("\n".join(lines) or "Заказов пока нет.")
    if not rows:
        return await c.message.answer("Чатов пока нет.")
    page = secrets.token_hex(4)
    await state.update_data(key=key, seller=link["label"], chats=[x["id"] for x in rows], page=page)
    buttons = [[Button(text=x["title"][:60], callback_data=f"dialog:{page}:{i}")] for i, x in enumerate(rows)]
    await c.message.answer("Выберите чат", reply_markup=Keyboard(inline_keyboard=buttons))


@r.callback_query(F.data.startswith("dialog:"))
async def dialog(c: CallbackQuery, state: FSMContext):
    _, page, index = c.data.split(":", 2)
    data = await state.get_data()
    if page != data.get("page") or not index.isdecimal() or int(index) >= len(data.get("chats", [])):
        return await c.answer("Откройте список чатов заново.")
    uid, key, chat = c.from_user.id, data["key"], data["chats"][int(index)]
    await c.answer()
    try:
        messages = await operate(uid, key, "messages", chat, seller=data["seller"])
        if await db.get_user_setting(uid, "read:" + key, "0") == "1":
            await operate(uid, key, "read", chat, seller=data["seller"])
    except Exception:
        return await c.message.answer("Не удалось открыть чат.")
    await state.set_state(Compose.reply)
    await state.update_data(chat=chat)
    history = "\n".join(str(t)[:210] for t in messages) or "Сообщений пока нет."
    await c.message.answer(history + "\n\nВведите ответ или /menu для выхода. Для отправки шаблона введите /use имя.", parse_mode=None)


@r.message(Command("template"))
async def template(m: Message):
    args = (m.text or "").split(maxsplit=2)
    if len(args) != 3 or len(args[1]) > 40 or not await _has_access(m.from_user.id):
        return await m.answer("Формат: /template имя текст (с активной подпиской).")
    await db.set_user_setting(m.from_user.id, "template:" + args[1].casefold(), args[2])
    await m.answer("Шаблон сохранён. В открытом чате отправьте /use " + escape(args[1]))


@r.message(Compose.reply, F.text)
async def reply(m: Message, state: FSMContext):
    text = m.text
    if text.startswith("/use "):
        text = await db.get_user_setting(m.from_user.id, "template:" + text[5:].strip().casefold(), "")
        if not text:
            return await m.answer("Такого шаблона нет.")
    elif text.startswith("/"):
        await state.clear()
        return await m.answer("Диалог закрыт. Откройте меню командой /menu.")
    data = await state.get_data()
    try:
        await operate(m.from_user.id, data["key"], "send", data["chat"], text, seller=data["seller"])
    except Exception:
        return await m.answer("Доставка не подтверждена. Проверьте историю перед повторной отправкой.")
    await m.answer("Ответ отправлен.")
