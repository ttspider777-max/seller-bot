"""Persistent bottom navigation, plugin catalog, and personal settings."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton as IB,
    InlineKeyboardMarkup as IKB,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import db
from config import ADMIN_ID
from texts import B, esc, t
from util import show

r = Router()
BOTTOM_PANEL = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧩 Плагины"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="🏠 Меню")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Выберите раздел",
)


def _link_button(key, setting):
    url = db.gs(setting)
    return B(key, url=url) if url else B(key, "nolink")


def _main_kb(uid):
    rows = [
        [_link_button("news", "link_news"), _link_button("guide", "link_guide")],
        [_link_button("features", "link_features")],
        [B("free")],
        [B("profile"), B("sub")],
    ]
    if uid == ADMIN_ID:
        rows.append([B("admin", "a:menu")])
    return IKB(inline_keyboard=rows)


async def _catalog(uid):
    enabled = await db.get_user_setting(uid, "plugin_marketplaces", "1") == "1"
    accounts = await db.get_marketplace_accounts(uid)
    connected = {x["platform"] for x in accounts}
    playerok = "подключён" if "playerok" in connected else "не подключён"
    starvell = "подключён" if "starvell" in connected else "не подключён"
    status = "🟢 включён" if enabled else "⚪ выключен"
    rows = []
    if enabled:
        rows.append([IB(text="Открыть подключение площадок", callback_data="marketplaces")])
        rows.append([IB(text="Отключить плагин", callback_data="userpanel:plugin")])
    else:
        rows.append([IB(text="Включить плагин", callback_data="userpanel:plugin")])
    rows.append([IB(text="Настройки", callback_data="userpanel:settings")])
    text = (
        "🧩 <b>Каталог плагинов</b>\n\n"
        f"<b>Интеграции Playerok и Starvell</b> — {status}\n"
        f"Ваши аккаунты: Playerok — {playerok}; Starvell — {starvell}.\n"
        "Плагин подключает аккаунты и проверяет профиль. Для подключения нужна активная подписка.\n\n"
        "🛠 <b>Автовыдача, автоподнятие, заказы и чаты</b> — пока в разработке; эти функции не включаются и не выполняются ботом."
    )
    return text, IKB(inline_keyboard=rows)


async def _settings(uid):
    user = await db.get_user(uid)
    plugin = await db.get_user_setting(uid, "plugin_marketplaces", "1") == "1"
    ref_notifications = bool(user and user["notif"])
    rows = [
        [IB(text=f"Реферальные уведомления: {'вкл' if ref_notifications else 'выкл'}", callback_data="userpanel:ref")],
        [IB(text=f"Плагин площадок: {'вкл' if plugin else 'выкл'}", callback_data="userpanel:plugin")],
        [IB(text="Каталог плагинов", callback_data="userpanel:catalog")],
    ]
    return "⚙️ <b>Настройки</b>\n\nЗдесь можно включать только доступные функции. Настройки применяются к вашему аккаунту.", IKB(inline_keyboard=rows)


@r.message(F.text == "🧩 Плагины")
async def show_catalog_message(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await _catalog(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@r.message(F.text == "⚙️ Настройки")
async def show_settings_message(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await _settings(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@r.message(F.text == "🏠 Меню")
async def show_home_message(m: Message, state: FSMContext):
    await state.clear()
    text = await t("welcome", name=esc(m.from_user.full_name), id=m.from_user.id,
                   username=("@" + m.from_user.username) if m.from_user.username else "—")
    await m.answer(text, reply_markup=_main_kb(m.from_user.id))


@r.callback_query(F.data == "userpanel:catalog")
async def show_catalog_callback(c: CallbackQuery):
    text, markup = await _catalog(c.from_user.id)
    await show(c, text, markup)
    await c.answer()


@r.callback_query(F.data == "userpanel:settings")
async def show_settings_callback(c: CallbackQuery):
    text, markup = await _settings(c.from_user.id)
    await show(c, text, markup)
    await c.answer()


@r.callback_query(F.data == "userpanel:plugin")
async def toggle_marketplace_plugin(c: CallbackQuery):
    current = await db.get_user_setting(c.from_user.id, "plugin_marketplaces", "1")
    await db.set_user_setting(c.from_user.id, "plugin_marketplaces", "0" if current == "1" else "1")
    text, markup = await _catalog(c.from_user.id)
    await show(c, text, markup)
    await c.answer("Плагин отключён" if current == "1" else "Плагин включён")


@r.callback_query(F.data == "userpanel:ref")
async def toggle_ref_notifications(c: CallbackQuery):
    user = await db.get_user(c.from_user.id)
    value = 0 if user and user["notif"] else 1
    await db.ex("UPDATE users SET notif=? WHERE id=?", (value, c.from_user.id))
    text, markup = await _settings(c.from_user.id)
    await show(c, text, markup)
    await c.answer("Уведомления включены" if value else "Уведомления отключены")
