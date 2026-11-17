"""Optional, read-only seller account checks for marketplace connections.

Kept separate from the subscription bot so marketplace failures cannot affect
existing payment, referral, or user flows. Credentials are read from local .env.
"""
from html import escape
from http.cookies import SimpleCookie

import aiohttp
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as KB

from config import ADMIN_ID, PLAYEROK_COOKIE, PLAYEROK_TOKEN, STARVELL_SESSION

r = Router()
r.callback_query.filter(F.from_user.id == ADMIN_ID)


def _playerok_token_from_cookie():
    """Accept either the token cookie value or a complete Cookie header."""
    raw = PLAYEROK_COOKIE.strip() or PLAYEROK_TOKEN.strip()
    if not raw:
        return ""
    if "=" not in raw:
        return raw
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return ""
    morsel = cookie.get("token")
    return morsel.value.strip() if morsel else ""


async def _playerok_status():
    token = _playerok_token_from_cookie()
    if not token:
        return "⚪ Не настроен (PLAYEROK_COOKIE отсутствует или в cookie нет поля token)"
    try:
        from PlayerokAPI import Account

        async with Account(token=token) as account:
            me = await account.get_me()
        username = getattr(me, "username", None) or getattr(me, "display_name", None) or "аккаунт подтверждён"
        return f"🟢 Подключён: <b>{escape(str(username))}</b>"
    except Exception as exc:
        # Never include exception text: HTTP errors may contain private response data.
        return f"🔴 Проверка не прошла ({type(exc).__name__}). Проверьте токен и доступ к Playerok."


async def _starvell_status():
    if not STARVELL_SESSION:
        return "⚪ Не настроен (STARVELL_SESSION отсутствует в .env)"
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={
            "Cookie": f"session={STARVELL_SESSION}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 seller-panel-bot/1.0",
        }) as session:
            async with session.get("https://starvell.com/api/profiles/me") as response:
                if response.status in (401, 403):
                    return "🔴 Сессия отклонена или истекла."
                if response.status != 200:
                    return f"🔴 Starvell вернул HTTP {response.status}."
                payload = await response.json(content_type=None)
        profile = payload.get("data", payload) if isinstance(payload, dict) else {}
        if not isinstance(profile, dict) or not profile:
            return "🔴 Starvell вернул неожиданный ответ."
        name = profile.get("username") or profile.get("name") or profile.get("id") or "аккаунт подтверждён"
        return f"🟢 Подключён: <b>{escape(str(name))}</b>"
    except Exception as exc:
        return f"🔴 Проверка не прошла ({type(exc).__name__}). Проверьте сессию и доступ к Starvell."


@r.callback_query(F.data == "a:marketplaces")
async def marketplaces_status(c: CallbackQuery):
    playerok, starvell = await _playerok_status(), await _starvell_status()
    text = (
        "🔌 <b>Подключение площадок</b>\n\n"
        f"<b>Playerok:</b> {playerok}\n"
        f"<b>Starvell:</b> {starvell}\n\n"
        "Проверка только читает профиль и не меняет аккаунты. "
        "Сессии задаются локально в .env; не отправляйте их в чат."
    )
    await c.message.edit_text(text, reply_markup=KB(inline_keyboard=[
        [IB(text="🔄 Проверить ещё раз", callback_data="a:marketplaces")],
        [IB(text="« В админ-панель", callback_data="a:menu")],
    ]))
    await c.answer()
