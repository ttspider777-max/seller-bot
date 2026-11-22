"""Observe seller orders, notify sellers and apply their explicit reply rules."""
import asyncio
import io
from PIL import Image, ImageDraw
from aiogram.types import BufferedInputFile
import logging
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent

import db
import offer_jobs
from config import ADMIN_ID
from marketplaces import _has_access
from seller_desk import operate

r = Router()
SCHEMA = """
CREATE TABLE IF NOT EXISTS observed_orders(
 user INTEGER, slot TEXT, seller TEXT, oid TEXT, status TEXT, first_seen INTEGER,
 PRIMARY KEY(user,slot,seller,oid));
CREATE TABLE IF NOT EXISTS seen_activity(user INTEGER,slot TEXT,seller TEXT,kind TEXT,remote TEXT,
 PRIMARY KEY(user,slot,seller,kind,remote));
CREATE TABLE IF NOT EXISTS seller_rules(
 id INTEGER PRIMARY KEY, user INTEGER, event TEXT, pattern TEXT, reply TEXT);
"""


@r.message(Command("seller"))
async def help_seller(m):
    await m.answer("""Заказы и переписка доступны в «Плагины» → площадка.
 /alerts on или off — мониторинг заказов
 /forum ID — направить уведомления в свою форум-группу, /forum off — вернуть в личку
 /rule событие | часть названия или * | ответ
 События: new, confirmed, refund, welcome (новый чат). Для заказа с чатом срабатывает первое подходящее правило.
 /rules — список, /delrule ID — удалить
 /block ID_чата или /unblock ID_чата — исключить чат из автоответов
 /template имя текст — сохранить шаблон
 /offer — расписание для лотов, /accounts — коды аккаунтов
 /stats — заказы, замеченные во время мониторинга
 /ranking on или off — участие в публичном рейтинге по замеченным заказам""")


@r.message(Command("alerts", "ranking", "forum", "block", "unblock", "rule", "rules", "delrule", "stats"))
async def settings(m):
    uid = m.from_user.id
    if not await _has_access(uid):
        return await m.answer("Для инструментов продавца нужна активная подписка и включённый плагин площадок.")
    cmd, _, value = m.text.strip().partition(" ")
    cmd = cmd.split("@")[0][1:]
    value = value.strip()
    if cmd in ("ranking", "alerts"):
        if value not in ("on", "off"):
            return await m.answer(f"/{cmd} on или /{cmd} off")
        await db.set_user_setting(uid, cmd, value)
    elif cmd == "forum":
        if value != "off":
            try:
                group = await m.bot.get_chat(int(value))
                member = await m.bot.get_chat_member(group.id, uid)
                if not group.is_forum or member.status not in ("administrator", "creator"):
                    raise ValueError
                await m.bot.send_message(group.id, "Уведомления Seller Panel подключены.")
            except Exception:
                return await m.answer("Проверьте ID форума, свои права администратора и возможность бота писать в группу.")
        await db.set_user_setting(uid, "forum", value)
    elif cmd in ("block", "unblock"):
        if not value:
            return await m.answer("Укажите ID чата площадки.")
        await db.set_user_setting(uid, "blocked:" + value, str(int(cmd == "block")))
    elif cmd == "rule":
        parts = [part.strip() for part in value.split("|", 2)]
        if len(parts) != 3 or not all(parts) or parts[0] not in ("new", "confirmed", "refund", "welcome"):
            return await m.answer("Пример: /rule confirmed | * | Спасибо за покупку!")
        await db.ex("INSERT INTO seller_rules(user,event,pattern,reply) VALUES(?,?,?,?)", (uid, *parts))
    elif cmd == "delrule":
        if not value.isdecimal():
            return await m.answer("Укажите номер правила из /rules.")
        await db.ex("DELETE FROM seller_rules WHERE id=? AND user=?", (value, uid))
    elif cmd == "rules":
        rows = await db.q("SELECT * FROM seller_rules WHERE user=? ORDER BY id", (uid,))
        messages = [f"{x['id']} · {x['event']} · {x['pattern'][:100]} · {x['reply'][:140]}" for x in rows]
        for i in range(0, len(messages), 12):
            await m.answer("\n".join(messages[i:i+12]), parse_mode=None)
        if not rows:
            await m.answer("Правил пока нет.")
        return
    elif cmd == "stats":
        rows = await db.q("SELECT slot,seller,status,count(*) n FROM observed_orders WHERE user=? GROUP BY slot,seller,status", (uid,))
        lines = [f"{x['slot']} · {x['seller']} · {x['status']}  {'▰' * min(x['n'],12)} {x['n']}" for x in rows]
        for i in range(0,len(lines),15):
            await m.answer("\n".join(lines[i:i+15]), parse_mode=None)
        if not rows:
            await m.answer("Пока нет данных. Включите /alerts on.")
        else:
            accounts=await db.q("SELECT DISTINCT slot,seller FROM observed_orders WHERE user=?",(uid,))
            for account in accounts:
                days=await db.q("""SELECT date(first_seen,'unixepoch') day,count(*) n FROM observed_orders
                    WHERE user=? AND slot=? AND seller=? GROUP BY day ORDER BY day DESC LIMIT 14""",(uid,account["slot"],account["seller"]))
                days.reverse()
                img=Image.new("RGB",(720,300),"#172538")
                draw=ImageDraw.Draw(img)
                width=650//len(days)
                peak=max(d["n"] for d in days)
                for i,d in enumerate(days):
                    x=35+i*width
                    y=245-round(185*d["n"]/peak)
                    draw.rectangle((x,y,x+min(42,width-4),245),fill="#55c8ae")
                    draw.text((x,y-14),str(d["n"]),fill="white")
                    draw.text((x,250),d["day"][5:],fill="white")
                buf=io.BytesIO()
                img.save(buf,"PNG")
                await m.answer_photo(BufferedInputFile(buf.getvalue(),filename="orders.png"),caption=f"{account['seller']} · Заказы по дате первого наблюдения (UTC)",parse_mode=None)
        return
    await m.answer("Настройка сохранена.")


@r.inline_query()
async def top(query):
    results = []
    if query.query.strip().lower() == "top":
        rows = await db.q("""SELECT u.name, count(*) AS n FROM observed_orders o
            JOIN user_settings s ON s.user=o.user AND s.key='ranking' AND s.value='on'
            JOIN users u ON u.id=o.user WHERE u.banned=0 GROUP BY u.id ORDER BY n DESC,u.id LIMIT 10""")
        lines = [f"{i}. {v['name']} · {v['n']}" for i,v in enumerate(rows,1)]
        content = "Рейтинг по замеченным заказам\n" + ("\n".join(lines) or "Пока нет участников.")
        results.append(InlineQueryResultArticle(id="top", title="Рейтинг продавцов", input_message_content=InputTextMessageContent(message_text=content, parse_mode=None)))
    await query.answer(results, cache_time=30)


async def observe(bot, account):
    uid, slot, seller = account["user"], account["platform"], account["label"]
    orders = await operate(uid, slot, "orders", seller=seller)
    marker = "monitor:" + slot + ":" + seller
    initialized = await db.get_user_setting(uid, marker, "0") == "1"
    for order in reversed(orders):
        key = (uid, slot, seller, order["id"])
        status = str(order["status"])
        old = await db.one("SELECT status FROM observed_orders WHERE user=? AND slot=? AND seller=? AND oid=?", key)
        if old and old["status"] == status:
            continue
        await db.ex("""INSERT INTO observed_orders VALUES(?,?,?,?,?,?) ON CONFLICT(user,slot,seller,oid)
            DO UPDATE SET status=excluded.status""", (*key, status, int(time.time())))
        if not initialized:
            continue
        target = await db.get_user_setting(uid, "forum", "off")
        event = "new" if not old else {"COMPLETED":"confirmed","CONFIRMED":"confirmed","REFUND":"refund","ROLLED_BACK":"refund"}.get(status.upper(), "changed")
        notice = f"{seller} · {order['title']}\nЗаказ {order['id']} · {status}"
        try:
            await bot.send_message(uid if target=="off" else int(target), notice, parse_mode=None)
        except Exception:
            if target != "off":
                await bot.send_message(uid, notice, parse_mode=None)
        chat = order.get("chat")
        if not chat or await db.get_user_setting(uid, "blocked:" + str(chat), "0") == "1":
            continue
        rules = await db.q("SELECT pattern,reply FROM seller_rules WHERE user=? AND event=? ORDER BY id", (uid,event))
        for rule in rules:
            if rule["pattern"] == "*" or rule["pattern"].casefold() in order["title"].casefold():
                await operate(uid, slot, "send", chat, rule["reply"], seller=seller)
                break
    await db.set_user_setting(uid, marker, "1")


async def activity(bot, account, kind):
    uid,slot,seller=account["user"],account["platform"],account["label"]
    rows=await operate(uid,slot,kind,seller=seller)
    marker=f"activity:{slot}:{seller}:{kind}"
    ready=await db.get_user_setting(uid,marker,"0")=="1"
    for row in reversed(rows):
        async with db.transaction() as conn:
            cur=await conn.execute("INSERT OR IGNORE INTO seen_activity VALUES(?,?,?,?,?)",(uid,slot,seller,kind,row["id"]))
            fresh=cur.rowcount==1
        if not fresh or not ready:
            continue
        if kind=="reviews":
            dest=await db.get_user_setting(uid,"forum","off")
            await bot.send_message(uid if dest=="off" else int(dest),f"{seller} · Новый отзыв\n{row['title'][:1500]}",parse_mode=None)
        elif await db.get_user_setting(uid,"blocked:"+row["id"],"0")!="1":
            rules=await db.q("SELECT pattern,reply FROM seller_rules WHERE user=? AND event='welcome' ORDER BY id",(uid,))
            for rule in rules:
                if rule["pattern"]=="*" or rule["pattern"].casefold() in row["title"].casefold():
                    await operate(uid,slot,"send",row["id"],rule["reply"],seller=seller)
                    break
    await db.set_user_setting(uid,marker,"1")


async def monitor(bot):
    while True:
        accounts = await db.q("""SELECT a.user,a.platform,a.label FROM marketplace_accounts a JOIN users u ON u.id=a.user
            JOIN user_settings s ON s.user=u.id AND s.key='alerts' AND s.value='on'
            WHERE u.banned=0 AND (u.sub_until>? OR u.id=?)""", (int(time.time()),ADMIN_ID))
        for account in accounts:
            if account["platform"].split("/")[0] in ("starvell","playerok"):
                try:
                    await observe(bot, account)
                except Exception as exc:
                    logging.warning("Seller monitor: %s", type(exc).__name__)
                for kind in ("reviews","chats"):
                    try:
                        await activity(bot,account,kind)
                    except Exception as exc:
                        logging.warning("Seller %s: %s",kind,type(exc).__name__)
        try:
            await offer_jobs.tick()
        except Exception as exc:
            logging.warning('Offer scheduler: %s', type(exc).__name__)
        await asyncio.sleep(60)
