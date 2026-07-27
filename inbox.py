"""
Забирает сообщения, которые люди написали боту, и пересылает их в личку админу.
Запускается по расписанию каждые 15 минут.

Пока этот скрипт работает, НЕ открывай getUpdates в браузере:
оба способа читают одну очередь и будут воровать сообщения друг у друга.
"""

import json
import os
import urllib.error
import urllib.request

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}/"

CONFIG_FILE = "villas.json"
STATE_FILE = "inbox_state.json"


def call(method, **params):
    body = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        API + method, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "description": str(e)}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    cfg = load(CONFIG_FILE)
    admin = cfg["admin_id"]
    state = load(STATE_FILE, {"offset": 0})

    res = call("getUpdates", offset=state["offset"], timeout=0,
               allowed_updates=["message"])
    if not res.get("ok"):
        print(f"ОШИБКА: {res.get('description')}")
        return

    updates = res["result"]
    if not updates:
        print("Новых сообщений нет")
        return

    sent = 0
    for u in updates:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message")
        if not msg or msg["chat"]["type"] != "private":
            continue

        frm = msg.get("from", {})
        if frm.get("id") == admin:
            continue

        name = esc(" ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])))
        uname = frm.get("username")
        contact = f"@{uname}" if uname else f'<a href="tg://user?id={frm["id"]}">написать</a>'

        header = f"📩 <b>Написали боту</b>\n{name} · {contact}"
        call("sendMessage", chat_id=admin, text=header, parse_mode="HTML",
             disable_web_page_preview=True)
        call("forwardMessage", chat_id=admin,
             from_chat_id=msg["chat"]["id"], message_id=msg["message_id"])
        sent += 1

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    print(f"Переслано: {sent}")


if __name__ == "__main__":
    main()
