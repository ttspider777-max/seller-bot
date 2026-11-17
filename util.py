from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup as KB

TZ = timezone(timedelta(hours=3))


def fdate(ts, with_time=False):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def kb(*rows):
    return KB(inline_keyboard=[list(x) if isinstance(x, (list, tuple)) else [x] for x in rows])


async def show(ev, text, markup=None):
    msg = ev.message if isinstance(ev, CallbackQuery) else ev
    if isinstance(ev, CallbackQuery):
        try:
            await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)
