"""
Ротация объектов: копирует посты из группы-базы в группы и канал.

Что делает:
  - каждый запуск публикует в каждую цель следующий объект по кругу
  - если пост удалён из базы, считает неудачи и после N подряд снимает объект
    с ротации, удаляя все его копии во всех целях
  - объект, убранный из villas.json руками, тоже удаляется отовсюду
  - обо всех автоснятиях и проблемах с доступом пишет админу в личку

Все настройки в villas.json, этот файл править не нужно.
"""

import json
import os
import time
import urllib.error
import urllib.request

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}/"

CONFIG_FILE = "villas.json"
STATE_FILE = "state.json"

# формулировки Telegram, означающие "поста больше нет"
# всё остальное считается проблемой доступа и объект не снимается
GONE_MARKERS = (
    "there are no messages to forward",
    "message to copy not found",
    "message not found",
    "messages not found",
)


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


def notify(admin, text):
    if admin:
        call("sendMessage", chat_id=admin, text=text, disable_web_page_preview=True)


def chunks(lst, n=100):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def delete_ids(chat, ids):
    for part in chunks(ids):
        call("deleteMessages", chat_id=chat, message_ids=part)


def is_gone(desc):
    d = (desc or "").lower()
    return any(m in d for m in GONE_MARKERS)


# ---------------- уборка копий ----------------

def cleanup(cfg, state):
    """Удалить копии объектов, которых больше нет в списке ротации."""
    active = {str(v["start"]) for v in cfg["rotation"]}
    removed = 0
    for chat, items in state["published"].items():
        for key in list(items):
            if key in active:
                continue
            ids = items.pop(key)
            if ids:
                print(f"{chat}: снят объект {key}, удаляю {len(ids)} сообщений")
                delete_ids(chat, ids)
                removed += len(ids)
    return removed


# ---------------- публикация ----------------

def publish(cfg, state, n, target):
    """Вернуть ('ok'|'gone'|'error', key, описание)."""
    chat = target["chat"]
    rotation = cfg["rotation"]

    idx = state["index"].get(chat, n) % len(rotation)
    villa = rotation[idx]
    key = str(villa["start"])
    ids = list(range(villa["start"], villa["start"] + villa["count"]))

    print(f"{chat}: {villa.get('note', key)} ({villa['count']} шт.)")

    if cfg.get("delete_previous"):
        old = state["published"].get(chat, {}).get(key, [])
        if old:
            delete_ids(chat, old)
            state["published"][chat][key] = []

    params = {"chat_id": chat, "from_chat_id": cfg["source_chat"], "message_ids": ids}
    if target.get("thread"):
        params["message_thread_id"] = target["thread"]

    res = call("copyMessages", **params)
    state["index"][chat] = (idx + 1) % len(rotation)

    if res.get("ok"):
        new = [m["message_id"] for m in res["result"]]
        state["published"].setdefault(chat, {}).setdefault(key, []).extend(new)
        state["failures"].pop(key, None)
        print(f"  ок, {len(new)} сообщений")
        return "ok", key, ""

    desc = res.get("description", "")
    if is_gone(desc):
        print(f"  поста нет в базе: {desc}")
        return "gone", key, desc

    print(f"  ОШИБКА ДОСТУПА: {desc}")
    return "error", key, desc


# ---------------- автоснятие ----------------

def auto_remove(cfg, state, admin):
    ar = cfg.get("auto_remove", {})
    if not ar.get("enabled", True):
        return False

    threshold = ar.get("failures_before_remove", 3)
    max_per_run = ar.get("max_per_run", 2)

    doomed = [k for k, v in state["failures"].items() if v >= threshold]
    if not doomed:
        return False

    if len(doomed) > max_per_run:
        names = ", ".join(doomed)
        print(f"ПРЕДОХРАНИТЕЛЬ: сразу {len(doomed)} объектов не найдены, ничего не снимаю")
        notify(admin,
               f"⚠️ Похоже на сбой доступа, а не на удаление постов.\n"
               f"Сразу {len(doomed)} объектов не находятся в базе: {names}\n"
               f"Ничего не снял. Проверь права бота в группе-базе.")
        return False

    kept = []
    gone = []
    for v in cfg["rotation"]:
        if str(v["start"]) in doomed:
            gone.append(v)
        else:
            kept.append(v)

    cfg["rotation"] = kept
    for v in gone:
        state["failures"].pop(str(v["start"]), None)
        print(f"СНЯТ С РОТАЦИИ: {v['start']} {v.get('note', '')}")
        notify(admin,
               f"🗑 Снял с ротации: {v.get('note', v['start'])}\n"
               f"пост {v['start']} удалён из базы. Копии убраны из всех групп.")

    return True


# ---------------- главное ----------------

def main():
    cfg = load(CONFIG_FILE)
    admin = cfg.get("admin_id")
    state = load(STATE_FILE, {})
    state.setdefault("index", {})
    state.setdefault("published", {})
    state.setdefault("failures", {})

    if not cfg.get("rotation"):
        print("Список ротации пуст")
        return

    if cfg.get("cleanup_on_remove", True):
        cleanup(cfg, state)

    targets = cfg["targets"]
    pause = cfg.get("pause_between_targets_sec", 300)

    gone_this_run = set()
    access_errors = []

    for n, target in enumerate(targets):
        status, key, desc = publish(cfg, state, n, target)
        if status == "gone":
            gone_this_run.add(key)
        elif status == "error":
            access_errors.append(f"{target['chat']}: {desc}")
        save(STATE_FILE, state)
        if n < len(targets) - 1:
            time.sleep(pause)

    # счётчик неудач: не более одной на объект за прогон
    for key in gone_this_run:
        state["failures"][key] = state["failures"].get(key, 0) + 1
        print(f"объект {key}: неудача #{state['failures'][key]}")

    # выкинуть счётчики объектов, которых уже нет в списке
    active = {str(v["start"]) for v in cfg["rotation"]}
    state["failures"] = {k: v for k, v in state["failures"].items() if k in active}

    if auto_remove(cfg, state, admin):
        save(CONFIG_FILE, cfg)
        cleanup(cfg, state)

    save(STATE_FILE, state)

    if access_errors:
        notify(admin, "⚠️ Проблемы с доступом при публикации:\n" + "\n".join(access_errors))


if __name__ == "__main__":
    main()
