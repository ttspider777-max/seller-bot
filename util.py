from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup as KB, Message

TZ = timezone(timedelta(hours=3))


def form_input(message):
    return not message.successful_payment and not (message.text or "").startswith("/")


def fdate(ts, with_time=False):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def kb(*rows):
    return KB(inline_keyboard=[list(x) if isinstance(x, (list, tuple)) else [x] for x in rows])


async def show(ev, text, markup=None):
    msg = ev.message if isinstance(ev, CallbackQuery) else ev
    if not isinstance(msg, Message):
        if isinstance(ev, CallbackQuery):
            await ev.bot.send_message(ev.from_user.id, text, reply_markup=markup, disable_web_page_preview=True)
        return
    if isinstance(ev, CallbackQuery):
        try:
            await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            if not any(reason in str(e).lower() for reason in
                       ("can't be edited", "message to edit not found", "no text in the message")):
                raise
    await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)
