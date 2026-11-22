"""Authenticated seller operations on the currently selected marketplace account."""
import aiohttp
from PlayerokAPI import Account
from marketplaces import _playerok_token, _starvell_cookie


async def request(platform, secret, operation, chat_id=None, text=None):
    if platform == "playerok":
        async with Account(token=_playerok_token(secret)) as account:
            if operation in ("orders","reviews"):
                page = await account.deals.search(filter={"direction": "OUT"}, first=20)
                if operation == "reviews":
                    return [{"id":d.testimonial.id, "title":d.testimonial.text or str(d.testimonial.rating)} for d in page.items if d.testimonial and d.testimonial.id]
                return [{"id": d.id, "product": getattr(d.item,"id",None), "title": getattr(d.item, "name", "") or d.id, "status": str(d.status or ""), "chat": d.chat_id} for d in page.items]
            if operation == "chats":
                page = await account.chats.search(first=20)
                return [{"id": c.id, "title": ", ".join(p.username for p in c.participants if p.username) or c.id} for c in page.items]
            if operation == "messages":
                page = await account.chats.messages(chat_id, limit=10)
                return [m.text or "[вложение]" for m in page.items]
            if operation == "read":
                return await account.chats.mark_read(chat_id)
            if operation == "send":
                return await account.send_message(chat_id, text)
    if platform != "starvell":
        raise ValueError("unsupported platform")
    route, body = {
        "reviews": ("reviews/list", {}),
        "orders": ("orders/list", {"filter": {"userType": "seller"}, "limit": 20}),
        "chats": ("chats/list", {"limit": 20}),
        "messages": ("messages/list-v2", {"chatId": chat_id, "limit": 10}),
        "read": ("chats/read", {"chatId": chat_id}),
        "send": ("messages/send", {"chatId": chat_id, "content": text}),
    }[operation]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
        if operation == "reviews":
            async with http.get("https://starvell.com/api/profiles/me",headers={"Cookie":_starvell_cookie(secret)},allow_redirects=False) as response:
                response.raise_for_status()
                profile=await response.json()
                profile=profile.get("data",profile)
                body={"filter":{"recipientPublicId":profile["publicId"]},"pagination":{"limit":20}}
        async with http.post("https://starvell.com/api/" + route, json=body, headers={"Cookie": _starvell_cookie(secret)}, allow_redirects=False) as response:
            if not 200 <= response.status < 300:
                raise ValueError(f"HTTP {response.status}")
            if operation in ("read", "send"):
                return
            data = await response.json(content_type=None)
    while isinstance(data, dict):
        data = next((data[k] for k in (operation, "items", "data", "results") if isinstance(data.get(k), (dict, list))), None)
    if not isinstance(data, list):
        raise ValueError("unexpected marketplace response")
    if operation == "reviews":
        return [{"id":str(m["id"]), "title":str(m.get("content") or m.get("text") or m.get("rating") or m["id"])} for m in data]
    if operation == "messages":
        return [m.get("content") or "[вложение]" for m in data]
    return [{"id": str(row["id"]), "product": (row.get("offerPublicId") or (row.get("offer") or {}).get("publicId") or row.get("offerId") or (row.get("offer") or {}).get("id")) if operation=="orders" else None, "title": str(row.get("title") or row.get("shortId") or row["id"]), "status": row.get("status", ""), "chat": row.get("chatId")} for row in data]
