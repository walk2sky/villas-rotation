"""
Делает две вещи за один проход:

1. Пересылает админу сообщения, которые люди написали боту в личку.
2. Следит за группой-базой: новые посты с маркером сам добавляет в villas.json.

Маркер и стоп-хештеги настраиваются в villas.json, блок auto_add.
Остальные хештеги поста складываются в поле note, чтобы объект
было легко найти в списке.

Альбом определяется автоматически - Telegram помечает все его элементы
общим идентификатором, скрипт берёт минимальный номер и количество.
Пост попадает в ротацию не мгновенно, а через один цикл (15-30 минут),
чтобы альбом успел догрузиться целиком.

Пока этот скрипт работает, НЕ открывай getUpdates в браузере:
оба способа читают одну очередь и будут воровать сообщения друг у друга.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}/"

CONFIG_FILE = "villas.json"
STATE_FILE = "inbox_state.json"

# сколько секунд после последнего элемента альбома ждать, прежде чем считать его целым
SETTLE_SEC = 300

# сколько символов максимум в note
NOTE_LIMIT = 90

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
    """Это группа-база?"""
    if isinstance(source, str) and source.startswith("@"):
        return (chat.get("username") or "").lower() == source[1:].lower()
    return str(chat.get("id")) == str(source)


def tags_of(text):
    """Все хештеги поста в порядке появления, без повторов."""
    out = []
    seen = set()
    for t in HASHTAG_RE.findall(text or ""):
        low = t.lower()
        if low not in seen:
            seen.add(low)
            out.append(t)
    return out


# ---------------- входящие в личку ----------------

def handle_private(msg, admin):
    frm = msg.get("from", {})
    if frm.get("id") == admin:
        return False

    name = esc(" ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])))
    uname = frm.get("username")
    contact = f"@{uname}" if uname else f'<a href="tg://user?id={frm["id"]}">написать</a>'

    call("sendMessage", chat_id=admin, text=f"📩 <b>Написали боту</b>\n{name} · {contact}",
         parse_mode="HTML", disable_web_page_preview=True)
    call("forwardMessage", chat_id=admin,
         from_chat_id=msg["chat"]["id"], message_id=msg["message_id"])
    return True


# ---------------- новые виллы в базе ----------------

def wants_rotation(msg, auto):
    """Проверить маркер, стоп-хештеги и тему."""
    if not (msg.get("photo") or msg.get("video")):
        return False

    thread = auto.get("thread")
    if thread and msg.get("message_thread_id") != thread:
        return False

    caption = msg.get("caption") or ""
    tags = {t.lower() for t in tags_of(caption)}

    marker = (auto.get("hashtag") or "").strip().lower()
    if marker and marker not in tags:
        return False

    for skip in auto.get("skip_hashtags", []):
        if skip.strip().lower() in tags:
            return False

    return True


def collect(msg, pending):
    """Накопить элементы поста, сгруппировав альбом."""
    key = msg.get("media_group_id") or f"single_{msg['message_id']}"
    item = pending.setdefault(key, {
        "start": msg["message_id"],
        "count": 0,
        "caption": "",
        "last_date": 0,
    })

    item["start"] = min(item["start"], msg["message_id"])
    item["count"] += 1
    item["last_date"] = max(item["last_date"], msg.get("date", 0))

    caption = (msg.get("caption") or "").strip()
    if len(caption) > len(item["caption"]):
        item["caption"] = caption

    return key


def make_note(caption, auto):
    """Собрать note из хештегов поста, кроме маркера."""
    marker = (auto.get("hashtag") or "").strip().lower()
    tags = [t for t in tags_of(caption) if t.lower() != marker]

    if tags:
        note = " ".join(tags)
    else:
        note = (caption or "").split("\n")[0].strip()

    return note[:NOTE_LIMIT].strip()


def finalize(pending, cfg, auto):
    """Перенести отстоявшиеся посты в villas.json."""
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
    state = load(STATE_FILE, {"offset": 0, "pending": {}})
    state.setdefault("pending", {})

    res = call("getUpdates", offset=state["offset"], timeout=0,
               allowed_updates=["message"])
    if not res.get("ok"):
        print(f"ОШИБКА: {res.get('description')}")
        return

    forwarded = 0
    seen = 0

    for u in res.get("result", []):
        state["offset"] = u["update_id"] + 1
        msg = u.get("message")
        if not msg:
            continue

        chat = msg["chat"]

        if chat["type"] == "private":
            if handle_private(msg, admin):
                forwarded += 1
            continue

        if auto.get("enabled") and is_source(chat, cfg["source_chat"]):
            if wants_rotation(msg, auto):
                collect(msg, state["pending"])
                seen += 1

    added = []
    if auto.get("enabled"):
        added = finalize(state["pending"], cfg, auto)
        if added:
            save(CONFIG_FILE, cfg)

    save(STATE_FILE, state)

    print(f"Переслано в личку: {forwarded}")
    print(f"Замечено элементов в базе: {seen}")
    for entry in added:
        print(f"Добавлено в ротацию: {entry['start']} ({entry['count']} шт.) {entry['note']}")
        call("sendMessage", chat_id=admin,
             text=f"🏠 В ротацию добавлено: {entry['note']}\n"
                  f"пост {entry['start']}, элементов {entry['count']}")


if __name__ == "__main__":
    main()
