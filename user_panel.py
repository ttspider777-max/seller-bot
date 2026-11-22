"""Persistent bottom navigation, plugin catalog, and personal settings."""
from aiogram import F, Router
from aiogram.filters import Command
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
from marketplaces import PLATFORMS
from menu_widget import send_menu
from texts import B, esc, t
from util import show

r = Router()
BOTTOM_PANEL = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧩 Плагины"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="🏠 Меню", style="success")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Выберите раздел",
)


def _link_button(key, setting):
    url = db.gs(setting)
    return B(key, url=url) if url else B(key, "nolink")


def main_kb(uid):
    rows = [
        [_link_button("news", "link_news"), _link_button("guide", "link_guide")],
        [_link_button("features", "link_features")],
        [B("free")],
        [B("profile"), B("sub")],
    ]
    if uid == ADMIN_ID:
        rows.append([B("admin", "a:menu")])
    return IKB(inline_keyboard=rows)


async def menu_text(user):
    return await t("welcome", name=esc(user.full_name), id=user.id,
                   username=("@" + user.username) if user.username else "—")


def help_view():
    return IKB(inline_keyboard=[
        [_link_button("news", "link_news"), _link_button("guide", "link_guide")],
        [_link_button("features", "link_features"), _link_button("support", "link_support")],
        [B("main_menu", "menu")],
    ])


@r.callback_query(F.data == "help_menu")
async def show_help(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "💬 <b>Помощь</b>\nВыберите раздел.", help_view())
    await c.answer()


def catalog_view():
    icons = {"playerok": "🎮", "starvell": "⭐", "ggsel": "🛍"}
    rows = [
        [IB(text=f"{icons[key]} {name}", callback_data=f"mk:view:{key}", style="primary")
         for key, name in PLATFORMS.items()],
        [B("marketplaces")],
        [IB(text="🧩 Плагины продавца", callback_data="plugin_list")],
        [B("main_menu", "menu")],
    ]
    return "Выберите площадку:", IKB(inline_keyboard=rows)


async def _settings(uid):
    user = await db.get_user(uid)
    plugin = await db.get_user_setting(uid, "plugin_marketplaces", "1") == "1"
    ref_notifications = bool(user and user["notif"])
    rows = [
        [IB(text=f"Реферальные уведомления: {'вкл' if ref_notifications else 'выкл'}", callback_data="userpanel:ref")],
        [IB(text=f"Плагин площадок: {'вкл' if plugin else 'выкл'}", callback_data="userpanel:plugin")],
        [B("plugin_catalog", "userpanel:catalog"), B("main_menu", "menu")],
    ]
    return "⚙️ <b>Настройки</b>\n\nКоманды продавца, правила и уведомления — /seller. Расписание для лотов — /offer.", IKB(inline_keyboard=rows)


@r.message(F.text == "🧩 Плагины")
async def show_catalog_message(m: Message, state: FSMContext):
    await state.clear()
    await send_menu(m.bot, m.chat.id, *catalog_view())


@r.message(Command("plugins"))
async def show_catalog_command(m: Message, state: FSMContext):
    await state.clear()
    text, markup = catalog_view()
    await m.answer(text, reply_markup=markup)


@r.message(F.text == "⚙️ Настройки")
async def show_settings_message(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await _settings(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@r.message(Command("settings"))
async def show_settings_command(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await _settings(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@r.message(F.text == "🏠 Меню")
async def show_home_message(m: Message, state: FSMContext):
    await state.clear()
    await send_menu(m.bot, m.chat.id, await menu_text(m.from_user), main_kb(m.from_user.id))


@r.message(Command("menu"))
async def show_home_command(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await menu_text(m.from_user), reply_markup=main_kb(m.from_user.id))


@r.callback_query(F.data == "userpanel:catalog")
async def show_catalog_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = catalog_view()
    await show(c, text, markup)
    await c.answer()


@r.callback_query(F.data == "userpanel:settings")
async def show_settings_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await _settings(c.from_user.id)
    await show(c, text, markup)
    await c.answer()


@r.callback_query(F.data == "userpanel:plugin")
async def toggle_marketplace_plugin(c: CallbackQuery):
    current = await db.get_user_setting(c.from_user.id, "plugin_marketplaces", "1")
    await db.set_user_setting(c.from_user.id, "plugin_marketplaces", "0" if current == "1" else "1")
    text, markup = await _settings(c.from_user.id)
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
