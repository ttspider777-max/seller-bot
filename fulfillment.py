"""Personal plugin connections and signed delivery of paid order events."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import socket
import time
import uuid
from urllib.parse import urlsplit

import aiohttp
from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard

import db
from marketplaces import _decrypt, _encrypt, _has_access
from seller_desk import operate
from util import form_input

r = Router()
NAMES = {"stars":"⭐ Telegram Stars", "premium":"💎 Telegram Premium", "gift":"🎁 Telegram Подарки",
         "robux":"🧿 Robux", "plus":"🌟 Robux через Plus", "smm":"⚡ SMM"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_links(
 id TEXT PRIMARY KEY, user INTEGER NOT NULL, kind TEXT NOT NULL, slot TEXT NOT NULL, seller TEXT NOT NULL,
 product TEXT NOT NULL, endpoint TEXT NOT NULL, secret TEXT NOT NULL, ready INTEGER DEFAULT 0,
 status TEXT DEFAULT '', UNIQUE(user,slot,product));
CREATE TABLE IF NOT EXISTS plugin_orders(
 link TEXT NOT NULL, oid TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
 attempts INTEGER DEFAULT 0, due INTEGER DEFAULT 0, PRIMARY KEY(link,oid));
"""


class Setup(StatesGroup):
    product = State()
    endpoint = State()
    secret = State()


def button(label, data):
    return Button(text=label,callback_data=data)


def keyboard(rows):
    return Keyboard(inline_keyboard=rows)


async def catalog(uid):
    links=await db.q("SELECT kind,count(*) n FROM plugin_links WHERE user=? GROUP BY kind",(uid,))
    counts={x["kind"]:x["n"] for x in links}
    rows=[[button(label + (f" · {counts[k]}" if k in counts else ""), "plug:view:"+k)] for k,label in NAMES.items()]
    rows.append([button("Назад","userpanel:catalog")])
    return "Плагины продавца\nВыберите услугу для подключения своего поставщика.", keyboard(rows)


@r.callback_query(F.data=="plugin_list")
async def listing(c,state):
    await state.clear()
    text,kb=await catalog(c.from_user.id)
    await c.message.answer(text,reply_markup=kb)
    await c.answer()


@r.callback_query(F.data.startswith("plug:"))
async def action(c,state):
    uid=c.from_user.id
    _,cmd,key=c.data.split(":",2)
    if cmd=="remove":
        async with db.transaction() as conn:
            await conn.execute("DELETE FROM plugin_orders WHERE link IN (SELECT id FROM plugin_links WHERE user=? AND id=?)",(uid,key))
            await conn.execute("DELETE FROM plugin_links WHERE id=? AND user=?",(key,uid))
        return await listing(c,state)
    if key not in NAMES:
        return await c.answer("Плагин не найден.")
    await state.clear()
    links=await db.q("SELECT * FROM plugin_links WHERE user=? AND kind=?",(uid,key))
    if cmd=="view":
        rows=[[button("Подключить товар","plug:add:"+key)]]
        lines=[NAMES[key],"Оплаченные заказы передаются совместимому сервису по HTTPS."]
        for link in links:
            lines.append(f"{link['seller']} · {link['product']} · {link['status'] or 'ожидает первого опроса'}")
            rows.append([button("Отключить · "+link["product"][:40],"plug:remove:"+link["id"])])
        rows.append([button("Каталог","plugin_list")])
        await c.message.answer("\n".join(lines)[:4000],parse_mode=None,reply_markup=keyboard(rows))
    elif cmd=="add":
        if not await _has_access(uid):
            return await c.answer("Для подключения нужна активная подписка и включённые площадки.",show_alert=True)
        accounts=[x for x in await db.get_marketplace_accounts(uid) if x["platform"].split("/")[0] in ("playerok","starvell")]
        await state.update_data(plugin=key,accounts={x["platform"]:x["label"] for x in accounts})
        rows=[[button(x["label"]+" · "+x["platform"],"plugslot:"+x["platform"])] for x in accounts]
        rows.append([button("Мои аккаунты","marketplaces")])
        await c.message.answer("Выберите аккаунт продавца." if accounts else "Сначала подключите аккаунт Playerok или Starvell.",reply_markup=keyboard(rows))
    await c.answer()


@r.callback_query(F.data.startswith("plugslot:"))
async def slot(c,state):
    key=c.data.split(":",1)[1]
    data=await state.get_data()
    if key not in data.get("accounts",{}):
        return await c.answer("Откройте подключение заново.")
    await state.update_data(slot=key,seller=data["accounts"][key])
    await state.set_state(Setup.product)
    await c.message.answer("Введите точный ID объявления на площадке. Для каждого товара можно выбрать одного поставщика.")
    await c.answer()


@r.message(Setup.product,F.text,form_input)
async def product(m,state):
    value=m.text.strip()
    if not re.fullmatch(r"[\w-]{1,120}",value):
        return await m.answer("Нужен ID объявления без ссылки или пробелов.")
    await state.update_data(product=value)
    await state.set_state(Setup.endpoint)
    await m.answer("Вставьте HTTPS-адрес обработчика Seller Panel из настроек своего поставщика.")


def validate_url(url):
    parts=urlsplit(url)
    host=parts.hostname or ""
    if (parts.scheme!="https" or parts.username is not None or parts.password is not None or
        parts.port not in (None,443) or parts.fragment or parts.query or len(url)>512 or
        not re.fullmatch(r"(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,63}",host)):
        raise ValueError("invalid endpoint")


class PublicResolver(aiohttp.ThreadedResolver):
    async def resolve(self,host,port=0,family=socket.AF_INET):
        addresses=await super().resolve(host,port,family)
        for entry in addresses:
            addr=ipaddress.ip_address(entry["host"])
            if not addr.is_global or addr.is_multicast or addr.version!=4:
                raise ValueError("public IPv4 endpoint required")
        return addresses


async def transmit(url,secret,event):
    validate_url(url)
    body=json.dumps(event,ensure_ascii=False,separators=(",",":")).encode()
    headers={"Content-Type":"application/json","Idempotency-Key":event["event_id"],
             "X-Seller-Signature":hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()}
    connector=aiohttp.TCPConnector(resolver=PublicResolver(),family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector,timeout=aiohttp.ClientTimeout(total=10)) as client:
        async with client.post(url,data=body,headers=headers,allow_redirects=False) as response:
            if not 200<=response.status<300:
                raise ValueError("provider declined")
            raw=bytearray()
            async for chunk in response.content.iter_chunked(1024):
                raw.extend(chunk)
                if len(raw)>4096:
                    raise ValueError("large receipt")
            receipt=json.loads(raw)
            if receipt.get("accepted") is not True or receipt.get("event_id")!=event["event_id"]:
                raise ValueError("missing receipt")


@r.message(Setup.endpoint,F.text,form_input)
async def endpoint(m,state):
    url=m.text.strip()
    try:
        validate_url(url)
    except ValueError:
        return await m.answer("Введите публичный HTTPS-адрес без параметров и данных авторизации.")
    await state.update_data(endpoint=url)
    await state.set_state(Setup.secret)
    await m.answer("Пришлите общий ключ подписи, настроенный у поставщика (16–256 символов). Бот отправит проверочное событие.")


@r.message(Setup.secret,F.text,form_input)
async def connect(m,state):
    uid=m.from_user.id
    secret=m.text.strip()
    try:
        await m.delete()
    except Exception:
        pass
    data=await state.get_data()
    if not 16<=len(secret)<=256:
        return await m.answer("Проверьте длину ключа.")
    link=await db.get_marketplace_secret(uid,data["slot"])
    if not await _has_access(uid) or not link or link["label"]!=data["seller"]:
        await state.clear()
        return await m.answer("Аккаунт или подписка недоступны.")
    ident=uuid.uuid4().hex
    try:
        await transmit(data["endpoint"],secret,{"type":"connection.test","event_id":ident,"plugin":data["plugin"]})
    except Exception:
        await state.clear()
        return await m.answer("Сервис не подтвердил проверку. Проверьте адрес, ключ и поддержку протокола Seller Panel, затем повторите подключение.")
    async with db.transaction() as conn:
        cur=await conn.execute("INSERT OR IGNORE INTO plugin_links(id,user,kind,slot,seller,product,endpoint,secret) VALUES(?,?,?,?,?,?,?,?)",
            (ident,uid,data["plugin"],data["slot"],data["seller"],data["product"],data["endpoint"],_encrypt(secret)))
        added=cur.rowcount==1
    await state.clear()
    await m.answer("Поставщик подключён. После первого опроса начнётся обработка новых заказов." if added else "Этот товар уже подключён. Сначала отключите прежнего поставщика.")


async def collect(link,orders):
    async with db.transaction() as conn:
        for order in orders:
            if str(order.get("product") or "")!=link["product"]:
                continue
            paid=order["status"]==("PAID" if link["slot"].split("/")[0]=="playerok" else "CREATED")
            if link["ready"] and not paid:
                continue
            order_id=str(order["id"])
            event={"type":"order.paid","event_id":hashlib.sha256((link["id"]+":"+order_id).encode()).hexdigest(),
                   "plugin":link["kind"],"marketplace":link["slot"].split("/")[0],"account":link["seller"],
                   "order_id":order_id,"product_id":link["product"],"title":order["title"]}
            await conn.execute("INSERT OR IGNORE INTO plugin_orders(link,oid,payload,state) VALUES(?,?,?,?)",
                (link["id"],order_id,json.dumps(event,ensure_ascii=False),"pending" if link["ready"] else "historical"))
        await conn.execute("UPDATE plugin_links SET ready=1 WHERE id=?",(link["id"],))


async def cycle():
    links=await db.q("""SELECT p.* FROM plugin_links p JOIN marketplace_accounts a ON a.user=p.user AND a.platform=p.slot
        AND a.label=p.seller JOIN users u ON u.id=p.user WHERE u.banned=0""")
    snapshots={}
    for link in links:
        if not await _has_access(link["user"]):
            continue
        scope=(link["user"],link["slot"],link["seller"])
        try:
            if scope not in snapshots:
                snapshots[scope]=await operate(scope[0],scope[1],"orders",seller=scope[2])
            await collect(link,snapshots[scope])
        except Exception:
            await db.ex("UPDATE plugin_links SET status=? WHERE id=?",("ошибка чтения заказов",link["id"]))
            continue
        pending=await db.q("SELECT * FROM plugin_orders WHERE link=? AND state='pending' AND due<=? LIMIT 10",(link["id"],int(time.time())))
        paid_status="PAID" if scope[1].split("/")[0]=="playerok" else "CREATED"
        current={str(o["id"]):o["status"] for o in snapshots[scope]}
        for row in pending:
            if current.get(row["oid"])!=paid_status:
                if row["oid"] in current:
                    await db.ex("UPDATE plugin_orders SET state='closed' WHERE link=? AND oid=?",(link["id"],row["oid"]))
                continue
            if not await db.one("SELECT id FROM plugin_links WHERE id=?",(link["id"],)):
                break
            try:
                await transmit(link["endpoint"],_decrypt(link["secret"]),json.loads(row["payload"]))
                state,status="accepted","заказ принят поставщиком"
            except Exception:
                state,status="pending","ошибка доставки, запланирован повтор"
            async with db.transaction() as conn:
                await conn.execute("UPDATE plugin_orders SET state=?,attempts=attempts+1,due=? WHERE link=? AND oid=?",
                    (state,int(time.time())+min(3600,60*2**min(row["attempts"],6)),link["id"],row["oid"]))
                await conn.execute("UPDATE plugin_links SET status=? WHERE id=?",(status,link["id"]))
        if not pending and not link["status"]:
            await db.ex("UPDATE plugin_links SET status=? WHERE id=?",("ожидание заказов",link["id"]))


async def worker():
    while True:
        try:
            await cycle()
        except Exception as exc:
            logging.warning("Plugin delivery: %s",type(exc).__name__)
        await asyncio.sleep(60)

