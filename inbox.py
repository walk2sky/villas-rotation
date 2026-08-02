"""
Делает три вещи за один проход:

1. Пересылает админу сообщения, которые люди написали боту в личку.
2. Доставляет ответы: админ отвечает РЕПЛАЕМ на пересланное сообщение,
   бот пересылает этот ответ автору. Задержка до 15 минут - бот
   просыпается по расписанию, а не живёт постоянно.
3. Ведёт журнал обращений в contacts.md: дата, имя, ссылка на профиль,
   начало сообщения.

Плюс следит за группой-базой: новые посты с маркером добавляет в villas.json.

ВАЖНО про альбомы: в Telegram подпись есть только у ПЕРВОГО сообщения
альбома. Поэтому сначала собираются ВСЕ фото/видео, и только когда группа
"устоялась", проверяется хештег по итоговой подписи.

Токен берётся из переменной окружения, в файле его нет и быть не должно.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}/"

CONFIG_FILE = "villas.json"
STATE_FILE = "inbox_state.json"
CONTACTS_FILE = "contacts.md"

SETTLE_SEC = 300
NOTE_LIMIT = 90

# сколько связок "сообщение у админа -> автор" помнить,
# то есть на какую глубину переписки можно отвечать реплаем
REPLY_MEMORY = 500

BALI_TZ = timezone(timedelta(hours=8))

HASHTAG_RE = re.compile(r"#\w+", re.UNICODE)


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


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_source(chat, source):
    if isinstance(source, str) and source.startswith("@"):
        return (chat.get("username") or "").lower() == source[1:].lower()
    return str(chat.get("id")) == str(source)


def tags_of(text):
    out, seen = [], set()
    for t in HASHTAG_RE.findall(text or ""):
        low = t.lower()
        if low not in seen:
            seen.add(low)
            out.append(t)
    return out


def full_name(frm):
    return " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])) or "без имени"


# ---------------- журнал обращений ----------------

def log_contact(msg):
    """Дописать строку в contacts.md."""
    frm = msg.get("from", {})
    when = datetime.fromtimestamp(msg.get("date", time.time()), BALI_TZ)

    uname = frm.get("username")
    link = f"https://t.me/{uname}" if uname else ""
    contact = f"[@{uname}]({link})" if uname else f"id {frm.get('id')}"

    text = (msg.get("text") or msg.get("caption") or "").replace("\n", " ").replace("|", "/")
    if not text:
        text = "(медиа без текста)"
    text = text[:70]

    new_file = not os.path.exists(CONTACTS_FILE)
    with open(CONTACTS_FILE, "a", encoding="utf-8") as f:
        if new_file:
            f.write("# Обращения в бота\n\n")
            f.write("Время по Бали (UTC+8).\n\n")
            f.write("| Дата | Имя | Контакт | Сообщение |\n")
            f.write("|---|---|---|---|\n")
        f.write(f"| {when:%d.%m.%Y %H:%M} | {full_name(frm)} | {contact} | {text} |\n")


# ---------------- личка ----------------

def deliver_reply(msg, state, admin):
    """Админ ответил реплаем - доставить ответ автору."""
    reply_to = msg.get("reply_to_message")
    if not reply_to:
        return False

    user_id = state["reply_map"].get(str(reply_to["message_id"]))
    if not user_id:
        call("sendMessage", chat_id=admin,
             text="⚠️ Не знаю, кому это адресовано. Отвечай реплаем на пересланное "
                  "сообщение клиента, а не на своё или на старое.")
        return False

    res = call("copyMessage", chat_id=user_id,
               from_chat_id=msg["chat"]["id"], message_id=msg["message_id"])

    if res.get("ok"):
        call("sendMessage", chat_id=admin, text="✅ Ответ доставлен")
        print(f"Ответ доставлен пользователю {user_id}")
        return True

    desc = res.get("description", "")
    call("sendMessage", chat_id=admin, text=f"❌ Не доставлено: {desc}")
    print(f"Ответ не доставлен: {desc}")
    return False


def handle_incoming(msg, state, admin):
    """Сообщение от клиента: переслать админу, запомнить связку, записать в журнал."""
    frm = msg.get("from", {})

    uname = frm.get("username")
    contact = f"@{uname}" if uname else f'<a href="tg://user?id={frm["id"]}">написать</a>'
    header = (f"📩 <b>Написали боту</b>\n{esc(full_name(frm))} · {contact}\n"
              f"<i>Ответь реплаем на следующее сообщение</i>")

    h = call("sendMessage", chat_id=admin, text=header,
             parse_mode="HTML", disable_web_page_preview=True)
    f = call("forwardMessage", chat_id=admin,
             from_chat_id=msg["chat"]["id"], message_id=msg["message_id"])

    # реплай сработает и на шапку, и на само пересланное сообщение
    for res in (h, f):
        if res.get("ok"):
            state["reply_map"][str(res["result"]["message_id"])] = frm["id"]

    log_contact(msg)
    return True


def trim_reply_map(state):
    """Держать связки в разумном размере."""
    rm = state["reply_map"]
    if len(rm) <= REPLY_MEMORY:
        return
    for key in sorted(rm, key=int)[:len(rm) - REPLY_MEMORY]:
        rm.pop(key, None)


# ---------------- сбор альбомов из базы ----------------

def in_source_scope(msg, cfg, auto):
    if not is_source(msg["chat"], cfg["source_chat"]):
        return False
    if not (msg.get("photo") or msg.get("video")):
        return False
    thread = auto.get("thread")
    if thread and msg.get("message_thread_id") != thread:
        return False
    return True


def collect(msg, pending):
    key = msg.get("media_group_id") or f"single_{msg['message_id']}"
    item = pending.setdefault(key, {
        "start": msg["message_id"], "count": 0, "caption": "", "last_date": 0,
    })
    item["start"] = min(item["start"], msg["message_id"])
    item["count"] += 1
    item["last_date"] = max(item["last_date"], msg.get("date", 0))

    caption = (msg.get("caption") or "").strip()
    if len(caption) > len(item["caption"]):
        item["caption"] = caption
    return key


def matches_marker(caption, auto):
    tags = {t.lower() for t in tags_of(caption)}
    marker = (auto.get("hashtag") or "").strip().lower()
    if marker and marker not in tags:
        return False
    for skip in auto.get("skip_hashtags", []):
        if skip.strip().lower() in tags:
            return False
    return True


def make_note(caption, auto):
    marker = (auto.get("hashtag") or "").strip().lower()
    tags = [t for t in tags_of(caption) if t.lower() != marker]
    note = " ".join(tags) if tags else (caption or "").split("\n")[0].strip()
    return note[:NOTE_LIMIT].strip()


def finalize(pending, cfg, auto):
    now = int(time.time())
    known = {v["start"] for v in cfg["rotation"]}
    added = []

    for key in list(pending):
        item = pending[key]
        if now - item["last_date"] < SETTLE_SEC:
            continue
        pending.pop(key)

        if item["start"] in known:
            continue
        if not matches_marker(item["caption"], auto):
            continue

        entry = {
            "start": item["start"],
            "count": item["count"],
            "note": make_note(item["caption"], auto) or f"пост {item['start']}",
        }
        cfg["rotation"].append(entry)
        added.append(entry)

    return added


# ---------------- главное ----------------

def main():
    cfg = load(CONFIG_FILE)
    admin = cfg["admin_id"]
    auto = cfg.get("auto_add", {})

    state = load(STATE_FILE, {})
    state.setdefault("offset", 0)
    state.setdefault("pending", {})
    state.setdefault("reply_map", {})

    res = call("getUpdates", offset=state["offset"], timeout=0,
               allowed_updates=["message"])
    if not res.get("ok"):
        print(f"ОШИБКА: {res.get('description')}")
        return

    forwarded = 0
    replied = 0
    seen = 0

    for u in res.get("result", []):
        state["offset"] = u["update_id"] + 1
        msg = u.get("message")
        if not msg:
            continue

        if msg["chat"]["type"] == "private":
            if msg.get("from", {}).get("id") == admin:
                if deliver_reply(msg, state, admin):
                    replied += 1
            else:
                if handle_incoming(msg, state, admin):
                    forwarded += 1
            continue

        if auto.get("enabled") and in_source_scope(msg, cfg, auto):
            collect(msg, state["pending"])
            seen += 1

    trim_reply_map(state)

    added = []
    if auto.get("enabled"):
        added = finalize(state["pending"], cfg, auto)
        if added:
            save(CONFIG_FILE, cfg)

    save(STATE_FILE, state)

    print(f"Переслано в личку: {forwarded}")
    print(f"Доставлено ответов: {replied}")
    print(f"Замечено элементов в базе: {seen}")
    for entry in added:
        print(f"Добавлено в ротацию: {entry['start']} ({entry['count']} шт.) {entry['note']}")
        call("sendMessage", chat_id=admin,
             text=f"🏠 В ротацию добавлено: {entry['note']}\n"
                  f"пост {entry['start']}, элементов {entry['count']}")


if __name__ == "__main__":
    main()
