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
from util import form_input, show

r = Router()
PLATFORMS = {"playerok": "Playerok", "starvell": "Starvell", "ggsel": "GGsel"}
_GGSEL_OFFERS = "https://seller.ggsel.com/api_sellers/v2/offers"
_KEY_PATH = Path(DB_PATH).with_name(".marketplace.key")


class Connection(StatesGroup):
    waiting_for_cookie = State()


def _fernet(*, create=False):
    """Load or create this installation's private encryption key."""
    try:
        key = _KEY_PATH.read_bytes()
    except FileNotFoundError:
        if not create:
            raise InvalidToken
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
    return _fernet(create=True).encrypt(secret.encode("utf-8")).decode("ascii")


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
    if platform == "ggsel":
        if not secret or len(secret) > 1024 or not secret.isascii() or any(ch.isspace() for ch in secret):
            raise ValueError("Нужен ключ Seller API v2 из кабинета продавца GGsel")
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {"Authorization": secret, "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_GGSEL_OFFERS, headers=headers, allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise ValueError("GGsel отклонил ключ или у него нет доступа к Seller API v2")
                if response.status != 200:
                    raise ValueError(f"GGsel вернул HTTP {response.status}")
                payload = await response.json()
        if not isinstance(payload, (dict, list)) or (isinstance(payload, dict) and payload.get("error")):
            raise ValueError("GGsel вернул неожиданный ответ")
        return "Seller API v2"

    if platform == "playerok":
        token = _playerok_token(secret)
        if not token:
            raise ValueError("cookie Playerok должна содержать cookie token")
        from PlayerokAPI import Account

        async with Account(token=token) as account:
            me = await account.get_me()
        label = getattr(me, "username", None) or getattr(me, "display_name", None) or getattr(me, "id", None)
        if not label:
            raise ValueError("Playerok не вернул профиль продавца")
        return str(label)[:80]

    cookie = _starvell_cookie(secret)
    if not cookie:
        raise ValueError("не удалось прочитать cookie session Starvell")
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"Cookie": cookie, "Accept": "application/json", "User-Agent": "Mozilla/5.0 seller-panel-bot/1.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get("https://starvell.com/api/profiles/me", allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f"Starvell вернул HTTP {response.status}")
            payload = await response.json(content_type=None)
    profile = payload.get("data", payload) if isinstance(payload, dict) else {}
    label = (profile.get("username") or profile.get("name") or profile.get("id")) if isinstance(profile, dict) else None
    if not label:
        raise ValueError("Starvell вернул неожиданный ответ")
    return str(label)[:80]


def _menu(accounts):
    by_platform = {row["platform"]: row["label"] for row in accounts}
    rows = []
    for key, name in PLATFORMS.items():
        if key in by_platform:
            rows.append([IB(text=f"🔄 {name}", callback_data=f"mk:check:{key}", style="primary"),
                         IB(text="Отключить", callback_data=f"mk:delete:{key}", style="danger")])
        else:
            rows.append([IB(text=f"➕ {name}", callback_data=f"mk:connect:{key}", style="primary")])
    rows.append([IB(text="🧩 Плагины", callback_data="userpanel:catalog"),
                 IB(text="Главное меню", callback_data="menu", style="success")])
    labels = []
    for key, name in PLATFORMS.items():
        label = by_platform.get(key)
        labels.append(f"{name}: 🟢 {escape(label)}" if label else f"{name}: ⚪ не подключён")
    return "🔌 <b>Мои площадки</b>\n\n" + "\n".join(labels) + \
        "\n\nДанные подключения хранятся в зашифрованном виде.", \
        KB(inline_keyboard=rows)


async def _has_access(uid: int) -> bool:
    subscribed = uid == ADMIN_ID or db.active(await db.get_user(uid))
    enabled = await db.get_user_setting(uid, "plugin_marketplaces", "1") == "1"
    return subscribed and enabled


@r.callback_query(F.data == "marketplaces")
async def show_marketplaces(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await accounts_view(c.from_user.id)
    if markup is None:
        await c.answer(text, show_alert=True)
        return
    await show(c, text, markup)
    await c.answer()


async def accounts_view(uid):
    if not await _has_access(uid):
        subscribed = uid == ADMIN_ID or db.active(await db.get_user(uid))
        reason = "Для подключения площадок нужна активная подписка." if not subscribed else "Плагин площадок отключён в настройках."
        return reason, None
    return _menu(await db.get_marketplace_accounts(uid))


@r.callback_query(F.data.startswith("mk:view:"))
async def view_platform(c: CallbackQuery, state: FSMContext):
    await state.clear()
    key = c.data.rsplit(":", 1)[-1]
    if key not in PLATFORMS:
        await c.answer("Площадка не найдена.", show_alert=True)
        return
    await show(c, *(await platform_view(c.from_user.id, key)))
    await c.answer()


async def platform_view(uid, key):
    name = PLATFORMS[key]
    accounts = await db.get_marketplace_accounts(uid)
    account = next((row for row in accounts if row["platform"] == key), None)
    buttons = []
    if await _has_access(uid):
        if account:
            buttons.append([
                IB(text="Проверить", callback_data=f"mk:check:{key}", style="primary"),
                IB(text="Отключить", callback_data=f"mk:delete:{key}", style="danger"),
            ])
        else:
            buttons.append([IB(text="Подключить", callback_data=f"mk:connect:{key}", style="success")])
    else:
        buttons.append([IB(text="Подписка", callback_data="sub"), IB(text="Настройки", callback_data="userpanel:settings")])
        if account:
            buttons.append([IB(text="Отключить", callback_data=f"mk:delete:{key}", style="danger")])
    buttons.append([IB(text="К площадкам", callback_data="userpanel:catalog", style="success")])
    status = f"🟢 Подключено · {escape(account['label'])}" if account else "⚪ Не подключено"
    method = "Ключ Seller API v2" if key == "ggsel" else "Cookie своего аккаунта"
    return f"<b>{name}</b>\n{status}\nПодключение: {method}.", KB(inline_keyboard=buttons)


@r.callback_query(F.data.startswith("mk:connect:"))
async def begin_connect(c: CallbackQuery, state: FSMContext):
    platform = c.data.rsplit(":", 1)[-1]
    if platform not in PLATFORMS:
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
    elif platform == "ggsel":
        prompt = ("Пришлите ключ <b>Seller API v2</b> из кабинета GGsel. "
                  "Достаточно разрешения <code>api_sellers/v2</code>. После получения сообщение будет удалено.")
    else:
        prompt = ("Пришлите значение cookie <code>session</code> из Starvell или строку Cookie.\n"
                  "Сообщение будет удалено после получения; бот проверит профиль и не покажет cookie в ответе.")
    await show(c, prompt, KB(inline_keyboard=[[IB(text="Отмена", callback_data="mk:cancel")]]))
    await c.answer()


@r.callback_query(F.data == "mk:cancel")
async def cancel_connect(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = _menu(await db.get_marketplace_accounts(c.from_user.id))
    await show(c, text, markup)
    await c.answer("Отменено")


@r.message(Connection.waiting_for_cookie, form_input)
async def receive_cookie(m: Message, state: FSMContext):
    secret = (m.text or "").strip()
    platform = (await state.get_data()).get("platform")
    if secret:
        try:
            await m.delete()
        except Exception:
            pass
    if not await _has_access(m.from_user.id):
        await state.clear()
        await m.answer("Для подключения нужна активная подписка и включённый плагин.")
        return
    cancel = KB(inline_keyboard=[[IB(text="Отмена", callback_data="mk:cancel")]])
    if not secret or len(secret) > 4096 or platform not in PLATFORMS:
        await m.answer("Пришлите данные подключения одним текстовым сообщением.", reply_markup=cancel)
        return
    try:
        label = await _verify(platform, secret)
        if not await _has_access(m.from_user.id):
            await state.clear()
            await m.answer("Подключение недоступно. Проверьте подписку и настройки.")
            return
        await db.save_marketplace_account(m.from_user.id, platform, _encrypt(secret), label)
    except Exception:
        await m.answer(
            f"Не удалось подключить {PLATFORMS[platform]}. Проверьте данные подключения и попробуйте снова.",
            reply_markup=cancel,
        )
        return
    await state.clear()
    text, markup = _menu(await db.get_marketplace_accounts(m.from_user.id))
    await m.answer(f"✅ {PLATFORMS[platform]} подключён.\n\n{text}", reply_markup=markup)


@r.callback_query(F.data.startswith("mk:check:"))
async def check_connection(c: CallbackQuery):
    if not await _has_access(c.from_user.id):
        await c.answer("Проверьте подписку и настройки интеграции.", show_alert=True)
        return
    platform = c.data.rsplit(":", 1)[-1]
    account = await db.get_marketplace_secret(c.from_user.id, platform)
    if not account or platform not in PLATFORMS:
        await c.answer("Аккаунт не подключён.", show_alert=True)
        return
    try:
        label = await _verify(platform, _decrypt(account["secret"]))
        await db.ex(
            "UPDATE marketplace_accounts SET label=?,updated=? WHERE user=? AND platform=? AND secret=?",
            (label, int(time.time()), c.from_user.id, platform, account["secret"]),
        )
        await c.answer(f"Подключение работает: {label}", show_alert=True)
    except InvalidToken:
        await c.answer("Не удалось расшифровать сессию. Обратитесь к администратору бота.", show_alert=True)
    except Exception:
        await c.answer("Проверка не прошла. Проверьте данные или подключите аккаунт заново.", show_alert=True)


@r.callback_query(F.data.startswith("mk:delete:"))
async def disconnect_account(c: CallbackQuery):
    platform = c.data.rsplit(":", 1)[-1]
    if platform not in PLATFORMS:
        await c.answer("Неизвестная площадка.", show_alert=True)
        return
    await db.delete_marketplace_account(c.from_user.id, platform)
    text, markup = _menu(await db.get_marketplace_accounts(c.from_user.id))
    await show(c, text, markup)
    await c.answer("Аккаунт отключён")
