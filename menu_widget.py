"""Pill-shaped Telegram Widget menus with an inline-keyboard fallback."""

from html import escape

from aiogram.exceptions import TelegramBadRequest, TelegramNotFound

from config import WIDGET_MENUS


_CSS = """
body { margin: 8px 8px 20px; }
.intro { margin-bottom: 8px; white-space: pre-line; }
.menu { display: grid; gap: 7px; }
.row { display: grid; gap: 7px; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); }
.menu button {
    display: block; width: 100%; min-height: 40px; margin: 0; padding: 5px 7px;
    border: 0; border-radius: 999px; font: inherit; font-size: 14px; font-weight: 650;
    line-height: 1.2; text-align: center; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
    color: var(--tg-theme-text-color);
    background: var(--tg-theme-secondary-bg-color);
}
.menu button.success { background: light-dark(#d9f0e2, #20452e); color: light-dark(#165e33, #87db9e); }
.menu button.primary { background: var(--tg-theme-button-color); color: var(--tg-theme-button-text-color); }
.menu button.danger { background: light-dark(#f5dddd, #50322f); color: light-dark(#9b2d2d, #f4aaaa); }
.menu button:active { opacity: 0.8; }
.menu button:disabled { opacity: 0.6; }
.feedback { margin-top: 7px; font-size: 13px; color: var(--tg-theme-hint-color); }
.feedback:empty { display: none; }
"""

_SCRIPT = """
const feedback = document.querySelector('.feedback');
for (const button of document.querySelectorAll('.menu button')) {
    button.addEventListener('click', async () => {
        feedback.textContent = '';
        button.disabled = true;
        try {
            if (button.dataset.url) {
                if (button.dataset.url.startsWith('https://t.me/')) {
                    TelegramWidget.openTelegramLink(button.dataset.url);
                } else {
                    TelegramWidget.openLink(button.dataset.url);
                }
                return;
            }
            const answer = await TelegramWidget.request(button.dataset.action);
            feedback.textContent = answer.result || '';
        } catch (error) {
            feedback.textContent = 'Не удалось открыть раздел. Попробуйте ещё раз или отправьте /menu.';
        } finally {
            button.disabled = false;
        }
    });
}
"""


def widget_html(text, markup):
    rows = []
    for row in markup.inline_keyboard:
        buttons = []
        for button in row:
            style = button.style if button.style in ("success", "primary", "danger") else ""
            target = (f'data-url="{escape(button.url, quote=True)}"' if button.url else
                      f'data-action="{escape("nav:" + button.callback_data, quote=True)}"')
            buttons.append(
                f'<button class="{style}" {target}>{escape(button.text)}</button>'
            )
        rows.append(f'<div class="row">{"".join(buttons)}</div>')
    return (
        f'<style>{_CSS}</style><div class="intro">{text}</div>'
        f'<div class="menu">{"".join(rows)}</div>'
        f'<div class="feedback" role="status"></div><script>{_SCRIPT}</script>'
    )


async def send_menu(bot, chat_id, text, markup, *, bottom_panel=None):
    if WIDGET_MENUS:
        try:
            await bot.send_widget(chat_id=chat_id, html=widget_html(text, markup),
                                  width=420, allow_queries=True, reply_markup=bottom_panel)
            return
        except (TelegramBadRequest, TelegramNotFound):
            # An older Bot API can reject the widget method.
            pass
    if bottom_panel is not None:
        await bot.send_message(chat_id, "⚡ Seller Panel", reply_markup=bottom_panel)
    await bot.send_message(chat_id, text, reply_markup=markup)
