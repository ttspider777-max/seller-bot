"""Per-user marketplace connections with encrypted session storage."""
import os
import re
import time
from html import escape
from http.cookies import SimpleCookie
from pathlib import Path

import aiohttp
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as KB, Message
from cryptography.fernet import Fernet, InvalidToken

import db
from config import ADMIN_ID, DB_PATH
from util import show

r = Router()
_PLATFORMS = {"playerok": "Playerok", "starvell": "Starvell"}
_KEY_PATH = Path(DB_PATH).with_name(".marketplace.key")


class Connection(StatesGroup):
    waiting_for_cookie = State()


def _fernet():
    """Load or create this installation's private encryption key."""
    try:
        key = _KEY_PATH.read_bytes()
    except FileNotFoundError:
        new_key = Fernet.generate_key()
        try:
            fd = os.open(str(_KEY_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as key_file:
                key_file.write(new_key)
            key = new_key
        except FileExistsError:
            key = _KEY_PATH.read_bytes()
        try:
            os.chmod(_KEY_PATH, 0o600)
        except OSError:
            pass
    return Fernet(key)


def _encrypt(secret: str) -> str:
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def _decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


def _playerok_token(raw: str) -> str:
    raw = raw.strip()
    if "\r" in raw or "\n" in raw:
        return ""
    if not raw:
        return ""
    if "=" not in raw:
        return raw
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return ""
    token = cookie.get("token")
    return token.value.strip() if token else ""


def _starvell_cookie(raw: str) -> str:
    raw = raw.strip()
    if not raw or "\r" in raw or "\n" in raw:
        return ""
    if re.search(r"(?:^|;\s*)session\s*=", raw, re.IGNORECASE):
        parsed = SimpleCookie()
        try:
            parsed.load(raw)
        except Exception:
            return ""
        session = parsed.get("session")
        return f"session={session.value}" if session and session.value else ""
    return f"session={raw}"


async def _verify(platform: str, secret: str):
    """Return the seller label after a read-only authenticated profile request."""
    if platform == "playerok":
        token = _playerok_token(secret)
        if not token:
            raise ValueError("cookie Playerok должна содержать cookie token")
        from PlayerokAPI import Account

        async with Account(token=token) as account:
            me = await account.get_me()
        return str(getattr(me, "username", None) or getattr(me, "display_name", None) or getattr(me, "id", None) or "аккаунт")

    cookie = _starvell_cookie(secret)
    if not cookie:
        raise ValueError("не удалось прочитать cookie session Starvell")
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"Cookie": cookie, "Accept": "application/json", "User-Agent": "Mozilla/5.0 seller-panel-bot/1.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get("https://starvell.com/api/profiles/me") as response:
            if response.status != 200:
                raise ValueError(f"Starvell вернул HTTP {response.status}")
            payload = await response.json(content_type=None)
    profile = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(profile, dict) or not profile:
        raise ValueError("Starvell вернул неожиданный ответ")
    return str(profile.get("username") or profile.get("name") or profile.get("id") or "аккаунт")


def _menu(uid, accounts):
    by_platform = {row["platform"]: row["label"] for row in accounts}
    rows = []
    for key, name in _PLATFORMS.items():
        if key in by_platform:
            rows.append([IB(text=f"🔄 Проверить {name}", callback_data=f"mk:check:{key}"),
                         IB(text=f"🗑 Отключить {name}", callback_data=f"mk:delete:{key}")])
        else:
            rows.append([IB(text=f"➕ Подключить {name}", callback_data=f"mk:connect:{key}")])
    rows.append([IB(text="« В главное меню", callback_data="menu")])
    labels = []
    for key, name in _PLATFORMS.items():
        label = by_platform.get(key)
        labels.append(f"{name}: 🟢 {escape(label)}" if label else f"{name}: ⚪ не подключён")
    return "🔌 <b>Мои площадки</b>\n\n" + "\n".join(labels) + \
        "\n\nПодключение проверяет аккаунт чтением профиля. Cookie хранится отдельно для вашего Telegram ID в зашифрованном виде.", \
        KB(inline_keyboard=rows)


async def _has_access(uid: int) -> bool:
    subscribed = uid == ADMIN_ID or db.active(await db.get_user(uid))
    enabled = await db.get_user_setting(uid, "plugin_marketplaces", "1") == "1"
    return subscribed and enabled


@r.callback_query(F.data == "marketplaces")
async def show_marketplaces(c: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await _has_access(c.from_user.id):
        subscribed = c.from_user.id == ADMIN_ID or db.active(await db.get_user(c.from_user.id))
        reason = "Для подключения площадок нужна активная подписка." if not subscribed else "Плагин площадок отключён в настройках."
        await c.answer(reason, show_alert=True)
        return
    text, markup = _menu(c.from_user.id, await db.get_marketplace_accounts(c.from_user.id))
    await show(c, text, markup)
    await c.answer()


@r.callback_query(F.data.startswith("mk:connect:"))
async def begin_connect(c: CallbackQuery, state: FSMContext):
    platform = c.data.rsplit(":", 1)[-1]
    if platform not in _PLATFORMS:
        await c.answer("Неизвестная площадка.", show_alert=True)
        return
    if c.from_user.id != ADMIN_ID and not db.active(await db.get_user(c.from_user.id)):
        await c.answer("Для подключения площадок нужна активная подписка.", show_alert=True)
        return
    if await db.get_user_setting(c.from_user.id, "plugin_marketplaces", "1") != "1":
        await c.answer("Сначала включите плагин площадок в настройках.", show_alert=True)
        return
    await state.set_state(Connection.waiting_for_cookie)
    await state.update_data(platform=platform)
    if platform == "playerok":
        prompt = ("Пришлите cookie <code>token</code> из Playerok или всю строку Cookie из браузера.\n"
                  "Сообщение будет удалено после получения; бот проверит профиль и не покажет cookie в ответе.")
    else:
        prompt = ("Пришлите значение cookie <code>session</code> из Starvell или строку Cookie.\n"
                  "Сообщение будет удалено после получения; бот проверит профиль и не покажет cookie в ответе.")
    await show(c, prompt, KB(inline_keyboard=[[IB(text="Отмена", callback_data="mk:cancel")]]))
    await c.answer()


@r.callback_query(F.data == "mk:cancel")
async def cancel_connect(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = _menu(c.from_user.id, await db.get_marketplace_accounts(c.from_user.id))
    await show(c, text, markup)
    await c.answer("Отменено")


@r.message(Connection.waiting_for_cookie)
async def receive_cookie(m: Message, state: FSMContext):
    try:
        secret = (m.text or "").strip()
        data = await state.get_data()
        platform = data.get("platform")
        if not secret or platform not in _PLATFORMS:
            await state.clear()
            await m.answer("Не получил cookie текстом. Откройте «Мои площадки» и попробуйте снова.")
            return
        try:
            await m.delete()
        except Exception:
            pass
        try:
            label = await _verify(platform, secret)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "площадка не подтвердила авторизацию"
            await m.answer(f"❌ Не удалось подключить {_PLATFORMS[platform]}: {escape(reason)}.\nПовторите ввод или нажмите «Отмена».",
                           reply_markup=KB(inline_keyboard=[[IB(text="Отмена", callback_data="mk:cancel")]]))
            return
        await db.save_marketplace_account(m.from_user.id, platform, _encrypt(secret), label)
        await state.clear()
        text, markup = _menu(m.from_user.id, await db.get_marketplace_accounts(m.from_user.id))
        await m.answer(f"✅ {_PLATFORMS[platform]} подключён: <b>{escape(label)}</b>\n\n{text}", reply_markup=markup)
    finally:
        # Do not retain the submitted cookie in FSM memory after finishing.
        if await state.get_state() is None:
            await state.clear()


@r.callback_query(F.data.startswith("mk:check:"))
async def check_connection(c: CallbackQuery):
    platform = c.data.rsplit(":", 1)[-1]
    account = await db.get_marketplace_secret(c.from_user.id, platform)
    if not account or platform not in _PLATFORMS:
        await c.answer("Аккаунт не подключён.", show_alert=True)
        return
    try:
        label = await _verify(platform, _decrypt(account["secret"]))
        await db.save_marketplace_account(c.from_user.id, platform, account["secret"], label)
        await c.answer(f"Подключение работает: {label}", show_alert=True)
    except InvalidToken:
        await c.answer("Не удалось расшифровать сессию. Обратитесь к администратору бота.", show_alert=True)
    except Exception:
        await c.answer("Проверка не прошла. Cookie могла истечь; подключите аккаунт заново.", show_alert=True)


@r.callback_query(F.data.startswith("mk:delete:"))
async def disconnect_account(c: CallbackQuery):
    platform = c.data.rsplit(":", 1)[-1]
    if platform not in _PLATFORMS:
        await c.answer("Неизвестная площадка.", show_alert=True)
        return
    await db.delete_marketplace_account(c.from_user.id, platform)
    text, markup = _menu(c.from_user.id, await db.get_marketplace_accounts(c.from_user.id))
    await show(c, text, markup)
    await c.answer("Аккаунт отключён")
