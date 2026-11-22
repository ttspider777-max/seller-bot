"""Scheduled changes to explicitly selected offers."""
import re
import time
from decimal import Decimal, InvalidOperation

import aiohttp
from aiogram import Router
from aiogram.filters import Command
from PlayerokAPI import Account

import db
from marketplaces import _decrypt, _has_access, _playerok_token, _starvell_cookie

r = Router()
SCHEMA = """
CREATE TABLE IF NOT EXISTS offer_jobs(
 id INTEGER PRIMARY KEY, user INTEGER, slot TEXT, seller TEXT, kind TEXT, target TEXT,
 args TEXT, minutes INTEGER, due INTEGER DEFAULT 0, error TEXT DEFAULT '',
 UNIQUE(user,slot,seller,kind,target));
"""
HELP = """Выберите аккаунт командой /accounts и скопируйте его код.
 /offer bump аккаунт ID_игры ID_категории интервал_минут — поднятие категории Starvell
 /offer stock аккаунт ID_лота количество интервал_минут — пополнение Starvell
 /offer price аккаунт свой_ID чужой_ID минимум шаг интервал_минут — цена по выбранному конкуренту, Playerok и Starvell
 /jobs — расписание и результат, /stopjob номер — удалить."""
ID = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")


@r.message(Command("accounts", "offer", "jobs", "stopjob"))
async def configure(m):
    uid = m.from_user.id
    if not await _has_access(uid):
        return await m.answer("Нужна активная подписка и включённый плагин площадок.")
    cmd, *args = m.text.split()
    cmd = cmd.split("@")[0]
    if cmd == "/accounts":
        rows = await db.get_marketplace_accounts(uid)
        return await m.answer("\n".join(x["platform"] + " · " + x["label"] for x in rows) or "Нет подключений.", parse_mode=None)
    if cmd == "/jobs":
        rows = await db.q("SELECT * FROM offer_jobs WHERE user=? ORDER BY id", (uid,))
        if not rows:
            return await m.answer("Расписание пусто.")
        for i in range(0,len(rows),10):
            await m.answer("\n".join(f"{x['id']} · {x['slot']} · {x['kind']} · {x['target']} · {x['minutes']} мин · {x['error'] or 'в очереди'}" for x in rows[i:i+10]), parse_mode=None)
        return
    if cmd == "/stopjob":
        if len(args)!=1 or not args[0].isdigit():
            return await m.answer("/stopjob номер")
        await db.ex("DELETE FROM offer_jobs WHERE user=? AND id=?", (uid,args[0]))
        return await m.answer("Задание удалено.")
    try:
        kind, slot, target, *params, interval = args
        minutes = int(interval)
        platform = slot.split("/")[0]
        if not (1 <= minutes <= 43200 and ID.fullmatch(target) and platform in ("starvell","playerok")):
            raise ValueError
        if kind == "price":
            ref, minimum, step = params
            if not ID.fullmatch(ref) or ref == target or any(not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?", x) or Decimal(x)<=0 for x in (minimum,step)):
                raise ValueError
        elif kind in ("stock","bump") and platform == "starvell":
            if len(params)!=1 or not params[0].isascii() or not params[0].isdigit() or not 1<=int(params[0])<=100000000:
                raise ValueError
            if kind=="bump" and not target.isdecimal():
                raise ValueError
        else:
            raise ValueError
        row=await db.get_marketplace_secret(uid,slot)
        if not row:
            raise ValueError
    except (ValueError,InvalidOperation):
        return await m.answer(HELP)
    await db.ex("""INSERT INTO offer_jobs(user,slot,seller,kind,target,args,minutes) VALUES(?,?,?,?,?,?,?)
       ON CONFLICT(user,slot,seller,kind,target) DO UPDATE SET args=excluded.args,minutes=excluded.minutes,due=0,error=''""",
        (uid,slot,row["label"],kind,target," ".join(params),minutes))
    await m.answer("Расписание сохранено.")


def next_price(current, competitor, minimum, step):
    current, competitor, minimum, step = map(lambda n: Decimal(str(n)), (current, competitor, minimum, step))
    if competitor <= 0 or current <= 0:
        raise ValueError("unexpected price")
    return max(minimum, min(current, competitor-step)).quantize(Decimal("0.01"))


async def change(job, secret):
    kind,target,params = job["kind"],job["target"],job["args"].split()
    if job["slot"].split("/")[0]=="playerok":
        async with Account(token=_playerok_token(secret)) as account:
            item=await account.items.get(item_id=target)
            if not item.is_mine:
                raise ValueError("ownership")
            ref=await account.items.get(item_id=params[0])
            price=next_price(item.price,ref.price,*params[1:])
            if price!=Decimal(str(item.price)):
                await account.items.update(target,{"price":float(price)})
        return
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=18),headers={"Cookie":_starvell_cookie(secret)}) as session:
        async def api(method,path,payload=None):
            async with session.request(method,"https://starvell.com/api/offers/"+path,json=payload,allow_redirects=False) as res:
                if not 200 <= res.status < 300:
                    raise ValueError("HTTP " + str(res.status))
                if method=="GET":
                    body=await res.json()
                    return body.get("data",body)
        if kind=="bump":
            return await api("POST","bump",{"gameId":int(target),"categoryIds":[int(params[0])]})
        item=await api("GET",target)
        if kind=="stock":
            field="availability"
            value=int(params[0])
            if int(item[field])>=value:
                return
        else:
            field="price"
            ref=await api("GET",params[0])
            value=str(next_price(item[field],ref[field],*params[1:]))
            if Decimal(value)==Decimal(str(item[field])):
                return
        await api("POST",target+"/partial-update",{field:value})


async def tick():
    now=int(time.time())
    rows=await db.q("""SELECT j.*,a.secret FROM offer_jobs j JOIN marketplace_accounts a
        ON a.user=j.user AND a.platform=j.slot AND a.label=j.seller JOIN users u ON u.id=j.user
        WHERE j.due<=? AND u.banned=0""",(now,))
    for job in rows:
        if not await _has_access(job["user"]):
            continue
        async with db.transaction() as conn:
            cursor=await conn.execute("UPDATE offer_jobs SET due=? WHERE id=? AND due<=?",(now+job["minutes"]*60,job["id"],now))
            if cursor.rowcount!=1:
                continue
        try:
            await change(job,_decrypt(job["secret"]))
            status="выполнено"
        except Exception as exc:
            status="ошибка "+type(exc).__name__
        await db.ex("UPDATE offer_jobs SET error=? WHERE id=?",(status,job["id"]))
