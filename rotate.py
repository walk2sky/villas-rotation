"""
Ротация объектов: копирует посты из группы-базы в группы и канал.

Логика:
  - каждый запуск публикует в каждую цель следующий объект по кругу
  - объект, убранный из villas.json, удаляется из всех целей (cleanup_on_remove)
  - delete_previous=true дополнительно сносит прошлую копию перед каждой публикацией

Ничего в этом файле менять не нужно, все настройки в villas.json.
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


def chunks(lst, n=100):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def delete_ids(chat, ids):
    for part in chunks(ids):
        call("deleteMessages", chat_id=chat, message_ids=part)


def cleanup(cfg, state):
    """Удалить копии объектов, которых больше нет в списке ротации."""
    active = {str(v["start"]) for v in cfg["rotation"]}
    for chat, items in state["published"].items():
        for key in list(items):
            if key in active:
                continue
            ids = items.pop(key)
            if ids:
                print(f"{chat}: снят объект {key}, удаляю {len(ids)} сообщений")
                delete_ids(chat, ids)


def publish(cfg, state, n, target):
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

    if res.get("ok"):
        new = [m["message_id"] for m in res["result"]]
        state["published"].setdefault(chat, {}).setdefault(key, []).extend(new)
        print(f"  ок, {len(new)} сообщений")
    else:
        print(f"  ОШИБКА: {res.get('description')}")

    state["index"][chat] = (idx + 1) % len(rotation)


def main():
    cfg = load(CONFIG_FILE)
    state = load(STATE_FILE, {"index": {}, "published": {}})
    state.setdefault("index", {})
    state.setdefault("published", {})

    if not cfg.get("rotation"):
        print("Список ротации пуст")
        return

    if cfg.get("cleanup_on_remove", True):
        cleanup(cfg, state)

    targets = cfg["targets"]
    pause = cfg.get("pause_between_targets_sec", 300)

    for n, target in enumerate(targets):
        publish(cfg, state, n, target)
        save(STATE_FILE, state)
        if n < len(targets) - 1:
            time.sleep(pause)

    save(STATE_FILE, state)


if __name__ == "__main__":
    main()
