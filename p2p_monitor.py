"""
P2P монитор (Binance P2P) -> алерты в Telegram, управление кнопками и командами.
Секреты и тех. настройки: config.json (заполнить один раз)
Фильтры: меню/команды в Telegram, хранятся в filters.json
Запуск:  python p2p_monitor.py     (нужен: pip install requests)
"""
import json
import os
import sys
import time
import logging
import threading
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
FILTERS_PATH = os.path.join(BASE, "filters.json")
LOG_PATH = os.path.join(BASE, "p2p_monitor.log")

DEFAULT_CONFIG = {
    "tg_token": "PUT_BOT_TOKEN",
    "tg_chat_id": "PUT_CHAT_ID",
    "poll_seconds": 10,
    "rows": 20,
    "alert_existing": True,   # слать алерт по уже существующим подходящим (при старте и смене фильтров)
    "debug": True,
}

DEFAULT_FILTERS = {
    "asset": "USDT",
    "fiat": "UAH",
    "trade_type": "BUY",      # BUY = ты покупаешь крипту, SELL = продаёшь
    "pay_types": [],          # пусто = любые
    "amount": None,
    "max_price": None,
    "min_price": None,
    "min_rate": 0.0,          # % выполнения, 0..100
    "min_orders": 0,
    "merchants_only": False,
    "paused": False,
}

MAX_ALERTS_PER_CYCLE = 5
PAGE_SIZE = 10
BASE_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv"
HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}


# ---------------- config / filters ----------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"Создан {CONFIG_PATH}. Впиши tg_token и tg_chat_id и запусти снова.")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = {**DEFAULT_CONFIG, **json.load(f)}
    cfg["tg_token"] = str(cfg["tg_token"]).strip()
    cfg["tg_chat_id"] = str(cfg["tg_chat_id"]).strip()
    if "PUT_" in cfg["tg_token"] or "PUT_" in cfg["tg_chat_id"]:
        print(f"Заполни tg_token и tg_chat_id в {CONFIG_PATH}")
        sys.exit(1)
    return cfg


CFG = load_config()

logging.basicConfig(
    level=logging.DEBUG if CFG["debug"] else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("p2p")
logging.getLogger("urllib3").setLevel(logging.WARNING)

_lock = threading.Lock()
rescan = threading.Event()   # поднимается при смене фильтров
pending = {}                 # ожидание ввода числа/текста после нажатия кнопки


def load_filters():
    if os.path.exists(FILTERS_PATH):
        try:
            with open(FILTERS_PATH, encoding="utf-8") as f:
                return {**DEFAULT_FILTERS, **json.load(f)}
        except Exception:
            log.exception("filters.json повреждён, беру значения по умолчанию")
    return dict(DEFAULT_FILTERS)


FILTERS = load_filters()


def get_filters():
    with _lock:
        return dict(FILTERS)


def set_filter(field, val):
    with _lock:
        FILTERS[field] = val
        with open(FILTERS_PATH, "w", encoding="utf-8") as f:
            json.dump(FILTERS, f, indent=2, ensure_ascii=False)
    log.info("Фильтр изменён: %s = %s", field, val)
    rescan.set()


def reset_filters():
    with _lock:
        FILTERS.clear()
        FILTERS.update(DEFAULT_FILTERS)
        with open(FILTERS_PATH, "w", encoding="utf-8") as f:
            json.dump(FILTERS, f, indent=2, ensure_ascii=False)
    rescan.set()


# ---------------- telegram ----------------
def tg_call(method, http_timeout=15, **params):
    r = requests.post(f"https://api.telegram.org/bot{CFG['tg_token']}/{method}",
                      json=params, timeout=http_timeout)
    if r.status_code != 200:
        if "not modified" in r.text:
            return None
        log.error("Telegram %s HTTP %s: %s", method, r.status_code, r.text[:300])
        return None
    return r.json()


def tg(text, reply_markup=None):
    try:
        params = {"chat_id": CFG["tg_chat_id"], "text": text, "disable_web_page_preview": True}
        if reply_markup:
            params["reply_markup"] = reply_markup
        ok = tg_call("sendMessage", **params)
        if ok:
            log.info("Telegram: отправлено")
        return bool(ok)
    except requests.RequestException as e:
        log.error("Telegram: ошибка сети: %s", e)
        return False


REPLY_KB = {"keyboard": [[{"text": "📋 Меню"}, {"text": "🔝 Топ"}]], "resize_keyboard": True}

HELP = """P2P монитор. Управление кнопками: /menu или кнопка «📋 Меню».

Команды:
/filters — текущие фильтры
/top — 5 лучших объявлений сейчас
/methods — список способов оплаты для твоей валюты
/pause, /resume — пауза / продолжить
/reset — сбросить фильтры
/cancel — отменить ввод

/set <параметр> <значение>:
  max 41.5 · min 40 · rate 95 · orders 50
  pay ощад моно приват · amount 10000
  merchants on|off · type BUY|SELL
  asset USDT · fiat UAH
Очистить: /set max none"""


def v(x, suffix=""):
    return f"{x}{suffix}" if x not in (None, "", []) else "—"


def filters_text(f):
    return (
        f"{'⏸ ПАУЗА' if f['paused'] else '▶️ Работает'}\n"
        f"Пара: {f['asset']}/{f['fiat']}, {f['trade_type']}\n"
        f"Цена: от {v(f['min_price'])} до {v(f['max_price'])}\n"
        f"Мин. выполнение: {v(f['min_rate'] or None, '%')}\n"
        f"Мин. ордеров: {v(f['min_orders'] or None)}\n"
        f"Оплата: {', '.join(f['pay_types']) if f['pay_types'] else 'любая'}\n"
        f"Сумма: {v(f['amount'])}\n"
        f"Только мерчанты: {'да' if f['merchants_only'] else 'нет'}"
    )


# ---------------- способы оплаты ----------------
_methods_cache = {}   # fiat -> (time, [(identifier, name)])

ALIASES = {  # кириллица -> латиница для поиска
    "ощад": "oschad", "моно": "mono", "приват": "privat", "пумб": "pumb",
    "абанк": "abank", "а-банк": "abank", "райф": "raif", "укрсиб": "ukrsib",
    "сенс": "sense", "ощадбанк": "oschad", "універсал": "mono", "универсал": "mono",
}


def get_methods(fiat):
    c = _methods_cache.get(fiat)
    if c and time.time() - c[0] < 3600:
        return c[1]
    try:
        r = requests.post(f"{BASE_URL}/filter-conditions", json={"fiat": fiat},
                          headers=HEADERS, timeout=15)
        data = (r.json().get("data") or {})
        out = [(m["identifier"], m.get("tradeMethodName") or m["identifier"])
               for m in (data.get("tradeMethods") or []) if m.get("identifier")]
        if out:
            _methods_cache[fiat] = (time.time(), out)
        return out
    except Exception:
        log.exception("Не удалось получить список способов оплаты")
        return []


def resolve_pay(tokens, fiat):
    methods = get_methods(fiat)
    if not methods:
        log.warning("Список способов оплаты недоступен — беру названия как есть")
        return tokens
    result = []
    for t in tokens:
        low = ALIASES.get(t.lower(), t.lower())
        exact = [i for i, n in methods if i.lower() == low or n.lower() == low]
        part = [i for i, n in methods if low in i.lower() or low in n.lower()]
        found = exact or part
        if not found:
            raise ValueError(f"способ оплаты «{t}» не найден для {fiat}. Смотри /methods")
        for i in found[:3]:
            if i not in result:
                result.append(i)
    return result


# ---------------- парсинг значений ----------------
SETTERS = {
    "asset": "asset", "fiat": "fiat", "type": "trade_type", "pay": "pay_types",
    "amount": "amount", "max": "max_price", "min": "min_price", "rate": "min_rate",
    "orders": "min_orders", "merchants": "merchants_only",
}


def parse_value(field, raw, f):
    low = raw.lower().strip()
    clear = low in ("none", "-", "нет", "clear")
    if field in ("asset", "fiat"):
        val = raw.upper().strip()
        if not val.isalpha() or not 2 <= len(val) <= 10:
            raise ValueError("ожидается код, например USDT или UAH")
        return val
    if field == "trade_type":
        val = raw.upper().strip()
        if val not in ("BUY", "SELL"):
            raise ValueError("только BUY или SELL")
        return val
    if field == "pay_types":
        return [] if clear else resolve_pay(raw.replace(",", " ").split(), f["fiat"])
    if field == "merchants_only":
        return low in ("on", "yes", "да", "1", "true")
    if field in ("amount", "max_price", "min_price"):
        return None if clear else float(raw.replace(",", "."))
    if field == "min_rate":
        val = 0.0 if clear else float(raw.replace(",", "."))
        if not 0 <= val <= 100:
            raise ValueError("процент от 0 до 100")
        return val
    if field == "min_orders":
        return 0 if clear else int(raw)
    raise ValueError("неизвестный параметр")


# ---------------- меню (inline-кнопки) ----------------
def btn(t, d):
    return {"text": t, "callback_data": d}


def render(screen, f):
    back = [btn("⬅️ Назад", "m:main")]
    if screen == "main":
        return filters_text(f), [
            [btn("▶️ Запустить" if f["paused"] else "⏸ Пауза", "a:pause"), btn("🔝 Топ", "a:top")],
            [btn("💰 Макс. цена", "m:max"), btn("💰 Мин. цена", "m:min")],
            [btn("⭐ Рейтинг", "m:rate"), btn("📦 Ордера", "m:orders")],
            [btn("🏦 Оплата", "m:pay:0"), btn("⚙️ Ещё", "m:more")],
        ]
    if screen in ("max", "min"):
        cur = f["max_price"] if screen == "max" else f["min_price"]
        name = "Максимальная" if screen == "max" else "Минимальная"
        return f"{name} цена: {v(cur)}", [
            [btn("−0.5", f"p:{screen}:-0.5"), btn("−0.1", f"p:{screen}:-0.1"),
             btn("+0.1", f"p:{screen}:0.1"), btn("+0.5", f"p:{screen}:0.5")],
            [btn("✏️ Ввести число", f"p:{screen}:ask"), btn("🚫 Убрать", f"p:{screen}:clr")],
            back,
        ]
    if screen == "rate":
        return f"Мин. % выполнения: {v(f['min_rate'] or None, '%')}", [
            [btn(("✅ " if f["min_rate"] == x else "") + (f"{x}%" if x else "Любой"), f"r:{x}")
             for x in (0, 90, 95, 98)], back]
    if screen == "orders":
        return f"Мин. ордеров за месяц: {v(f['min_orders'] or None)}", [
            [btn(("✅ " if f["min_orders"] == x else "") + (str(x) if x else "Любое"), f"o:{x}")
             for x in (0, 30, 100, 500)], back]
    if screen == "more":
        other = "SELL" if f["trade_type"] == "BUY" else "BUY"
        return (f"Тип: {f['trade_type']}\nПара: {f['asset']}/{f['fiat']}\n"
                f"Мерчанты: {'да' if f['merchants_only'] else 'нет'}\nСумма: {v(f['amount'])}"), [
            [btn(f"🔄 Сменить на {other}", "a:type")],
            [btn("🏪 Мерчанты: " + ("выкл" if f["merchants_only"] else "вкл"), "a:merch")],
            [btn("🪙 Монета", "e:asset"), btn("💱 Валюта", "e:fiat"), btn("💵 Сумма", "e:amount")],
            [btn("♻️ Сброс фильтров", "a:reset")],
            back,
        ]
    if screen.startswith("pay"):
        page = int(screen.split(":")[1]) if ":" in screen else 0
        methods = get_methods(f["fiat"])
        if not methods:
            return ("Не удалось загрузить список способов оплаты.\n"
                    "Задай вручную: /set pay monobank"), [back]
        pages = (len(methods) + PAGE_SIZE - 1) // PAGE_SIZE
        page = max(0, min(page, pages - 1))
        chunk = methods[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        sel = f["pay_types"]
        rows, row = [], []
        for ident, name in chunk:
            row.append(btn(("✅ " if ident in sel else "") + name[:20], f"y:{page}:{ident}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([btn("◀️", f"m:pay:{page-1}"), btn(f"{page+1}/{pages}", "noop"),
                     btn("▶️", f"m:pay:{page+1}")])
        rows.append([btn("🧹 Любая оплата", f"yc:{page}"), back[0]])
        return f"Способы оплаты ({f['fiat']}). Выбрано: {', '.join(sel) if sel else 'любая'}", rows
    return filters_text(f), [back]


def show(mid, screen):
    text, rows = render(screen, get_filters())
    markup = {"inline_keyboard": rows}
    if mid:
        tg_call("editMessageText", chat_id=CFG["tg_chat_id"], message_id=mid, text=text,
                reply_markup=markup)
    else:
        tg(text, markup)


def best_price(f):
    items = fetch(f)
    return float(items[0]["adv"]["price"]) if items else None


def send_top():
    try:
        items = fetch(get_filters())[:5]
    except Exception as e:
        tg(f"Ошибка запроса: {e}")
        return
    if not items:
        tg("Стакан пустой (проверь пару / оплату / сумму)")
        return
    lines = []
    for i in items:
        a, u = i["adv"], i["advertiser"]
        lines.append(f"{a['price']} | {a['minSingleTransAmount']}–{a['maxSingleTransAmount']} | "
                     f"{u['nickName']} ({float(u.get('monthFinishRate') or 0)*100:.0f}%, "
                     f"{u.get('monthOrderCount')})")
    tg("Топ стакана:\n" + "\n".join(lines))


ASK_TEXT = {
    "max_price": "Отправь максимальную цену числом, например 41.5",
    "min_price": "Отправь минимальную цену числом, например 40",
    "asset": "Отправь код монеты, например USDT",
    "fiat": "Отправь код валюты, например UAH",
    "amount": "Отправь свою сумму в валюте, например 10000 (none — убрать)",
}


def handle_callback(cb):
    tg_call("answerCallbackQuery", callback_query_id=cb["id"])
    msg = cb.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != CFG["tg_chat_id"]:
        return
    mid = msg.get("message_id")
    parts = (cb.get("data") or "").split(":")
    kind = parts[0]
    f = get_filters()
    screen = "main"

    if kind == "noop":
        return
    elif kind == "m":
        screen = ":".join(parts[1:])
    elif kind == "a":
        act = parts[1]
        if act == "pause":
            set_filter("paused", not f["paused"])
        elif act == "top":
            send_top()
            return
        elif act == "type":
            set_filter("trade_type", "SELL" if f["trade_type"] == "BUY" else "BUY")
            screen = "more"
        elif act == "merch":
            set_filter("merchants_only", not f["merchants_only"])
            screen = "more"
        elif act == "reset":
            reset_filters()
    elif kind == "p":
        key, op = parts[1], parts[2]
        field = "max_price" if key == "max" else "min_price"
        screen = key
        if op == "ask":
            pending["field"] = field
            tg(ASK_TEXT[field] + "\n(/cancel — отмена)")
            return
        elif op == "clr":
            set_filter(field, None)
        else:
            base = f[field]
            if base is None:
                try:
                    base = best_price(f)
                except Exception:
                    base = None
                if base is None:
                    tg("Не могу взять базовую цену из стакана — введи число вручную")
                    return
            set_filter(field, round(base + float(op), 2))
    elif kind == "r":
        set_filter("min_rate", float(parts[1]))
        screen = "rate"
    elif kind == "o":
        set_filter("min_orders", int(parts[1]))
        screen = "orders"
    elif kind == "y":
        page, ident = parts[1], ":".join(parts[2:])
        sel = list(f["pay_types"])
        sel.remove(ident) if ident in sel else sel.append(ident)
        set_filter("pay_types", sel)
        screen = f"pay:{page}"
    elif kind == "yc":
        set_filter("pay_types", [])
        screen = f"pay:{parts[1]}"
    elif kind == "e":
        pending["field"] = parts[1] if parts[1] != "amount" else "amount"
        tg(ASK_TEXT[pending["field"]] + "\n(/cancel — отмена)")
        return
    show(mid, screen)


# ---------------- команды ----------------
def handle_command(text):
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    pending.clear()

    if cmd in ("/start", "/help"):
        tg(HELP, REPLY_KB)
        show(None, "main")
    elif cmd == "/menu":
        show(None, "main")
    elif cmd == "/cancel":
        tg("Ок, отменено")
    elif cmd == "/filters":
        tg(filters_text(get_filters()))
    elif cmd in ("/pause", "/resume"):
        set_filter("paused", cmd == "/pause")
        tg("⏸ Мониторинг на паузе" if cmd == "/pause" else "▶️ Мониторинг запущен")
    elif cmd == "/reset":
        reset_filters()
        tg("Фильтры сброшены\n\n" + filters_text(get_filters()))
    elif cmd == "/top":
        send_top()
    elif cmd == "/methods":
        f = get_filters()
        m = get_methods(f["fiat"])
        tg(f"Способы оплаты {f['fiat']}:\n" + "\n".join(f"{n}  →  {i}" for i, n in m)
           if m else "Не удалось загрузить список")
    elif cmd == "/set":
        if len(args) < 2:
            tg("Формат: /set <параметр> <значение>\nНапример: /set max 41.5\n/help — все параметры")
            return
        key = args[0].lower()
        if key not in SETTERS:
            tg(f"Неизвестный параметр «{key}». /help — список")
            return
        field = SETTERS[key]
        try:
            val = parse_value(field, " ".join(args[1:]), get_filters())
        except ValueError as e:
            tg(f"Неверное значение: {e}")
            return
        set_filter(field, val)
        tg("✅ Сохранено\n\n" + filters_text(get_filters()))
    else:
        tg("Не понял команду. /help")


def handle_text(text):
    t = text.strip()
    if t in ("📋 Меню", "Меню"):
        pending.clear()
        show(None, "main")
    elif t in ("🔝 Топ", "Топ"):
        send_top()
    elif pending.get("field"):
        field = pending["field"]
        try:
            val = parse_value(field, t, get_filters())
        except ValueError as e:
            tg(f"Неверное значение: {e}\nПопробуй ещё раз или /cancel")
            return
        except Exception:
            tg("Нужно число. Попробуй ещё раз или /cancel")
            return
        pending.clear()
        set_filter(field, val)
        show(None, "main")


def command_listener():
    try:
        tg_call("setMyCommands", commands=[
            {"command": "menu", "description": "Меню с кнопками"},
            {"command": "filters", "description": "Текущие фильтры"},
            {"command": "top", "description": "Топ стакана сейчас"},
            {"command": "methods", "description": "Способы оплаты"},
            {"command": "pause", "description": "Пауза"},
            {"command": "resume", "description": "Продолжить"},
            {"command": "help", "description": "Помощь"},
        ])
    except requests.RequestException:
        pass
    offset = None
    while True:
        try:
            params = {"timeout": 25}
            if offset:
                params["offset"] = offset
            data = tg_call("getUpdates", http_timeout=40, **params)
            if not data:
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    if "callback_query" in upd:
                        handle_callback(upd["callback_query"])
                        continue
                    msg = upd.get("message") or {}
                    chat_id = str((msg.get("chat") or {}).get("id"))
                    text = msg.get("text") or ""
                    if chat_id != CFG["tg_chat_id"]:
                        log.warning("Игнорирую сообщение от чужого чата %s", chat_id)
                        continue
                    log.info("Сообщение: %s", text)
                    if text.startswith("/"):
                        handle_command(text)
                    else:
                        handle_text(text)
                except Exception:
                    log.exception("Ошибка обработки обновления")
                    tg("Ошибка при обработке, см. лог")
        except requests.RequestException as e:
            log.error("getUpdates: ошибка сети: %s", e)
            time.sleep(5)
        except Exception:
            log.exception("Ошибка в listener")
            time.sleep(5)


# ---------------- binance ----------------
def fetch(f):
    body = {
        "asset": f["asset"],
        "fiat": f["fiat"],
        "tradeType": f["trade_type"],
        "page": 1,
        "rows": CFG["rows"],
        "payTypes": f["pay_types"],
        "publisherType": "merchant" if f["merchants_only"] else None,
        "transAmount": str(f["amount"]) if f["amount"] else None,
    }
    r = requests.post(f"{BASE_URL}/search", json=body, headers=HEADERS, timeout=15)
    log.debug("Binance HTTP %s", r.status_code)
    if r.status_code != 200:
        log.error("Binance вернул %s: %s", r.status_code, r.text[:300])
        return []
    j = r.json()
    if j.get("code") not in (None, "000000"):
        log.error("Binance API code=%s msg=%s", j.get("code"), j.get("message"))
    return j.get("data") or []


def reject_reason(item, f):
    adv, user = item["adv"], item["advertiser"]
    price = float(adv["price"])
    rate = float(user.get("monthFinishRate") or 0) * 100
    orders = user.get("monthOrderCount") or 0
    if f["max_price"] is not None and price > f["max_price"]:
        return f"цена {price} > max {f['max_price']}"
    if f["min_price"] is not None and price < f["min_price"]:
        return f"цена {price} < min {f['min_price']}"
    if rate < f["min_rate"]:
        return f"выполнение {rate:.0f}% < {f['min_rate']}%"
    if orders < f["min_orders"]:
        return f"ордеров {orders} < {f['min_orders']}"
    return None


def fmt(item, f):
    adv, user = item["adv"], item["advertiser"]
    methods = ", ".join(
        (m.get("tradeMethodName") or m.get("identifier") or "?") for m in adv["tradeMethods"])
    return (
        f"{f['trade_type']} {f['asset']}/{f['fiat']}\n"
        f"Цена: {adv['price']}\n"
        f"Лимиты: {adv['minSingleTransAmount']} – {adv['maxSingleTransAmount']} {f['fiat']}\n"
        f"Доступно: {adv['tradableQuantity']} {f['asset']}\n"
        f"Оплата: {methods}\n"
        f"Продавец: {user['nickName']} "
        f"({float(user.get('monthFinishRate') or 0)*100:.0f}%, {user.get('monthOrderCount')} ордеров)\n"
        f"https://p2p.binance.com/en/advertiserDetail?advertiserNo={user['userNo']}"
    )


def monitor_loop():
    seen = set()      # только для чистоты логов
    alerted = set()   # по ним уже слали алерт (не повторяем при смене фильтров)
    first = not CFG["alert_existing"]
    paused_logged = False
    while True:
        try:
            if rescan.is_set():
                rescan.clear()
                seen.clear()
                first = not CFG["alert_existing"]
                paused_logged = False
                log.info("Фильтры изменились — пересканирую стакан")
            if len(alerted) > 5000:
                alerted.clear()
            f = get_filters()
            if f["paused"]:
                if not paused_logged:
                    log.info("Пауза")
                    paused_logged = True
            else:
                items = fetch(f)
                if not items:
                    log.warning("Стакан пустой или запрос не удался (проверь пару/оплату/сумму)")
                else:
                    prices = [float(i["adv"]["price"]) for i in items]
                    new = matched = sent = 0
                    for item in items:
                        adv_no = item["adv"]["advNo"]
                        is_new = adv_no not in seen
                        seen.add(adv_no)
                        new += is_new
                        reason = reject_reason(item, f)
                        if reason:
                            if is_new:
                                log.debug("  ✗ %s %s — %s", item["advertiser"]["nickName"],
                                          item["adv"]["price"], reason)
                            continue
                        if adv_no in alerted:
                            continue
                        matched += 1
                        alerted.add(adv_no)
                        if first:
                            log.info("  ✓ подходит (существующее, пропуск): %s %s",
                                     item["advertiser"]["nickName"], item["adv"]["price"])
                            continue
                        if sent >= MAX_ALERTS_PER_CYCLE:
                            continue
                        log.info("  ✓ ПОДХОДИТ: %s %s — шлю алерт",
                                 item["advertiser"]["nickName"], item["adv"]["price"])
                        tg("🔔 Новое объявление\n" + fmt(item, f))
                        sent += 1
                    if matched > sent and not first:
                        tg(f"…и ещё {matched - sent} подходящих (лимит {MAX_ALERTS_PER_CYCLE} за цикл). /top")
                    log.info("Стакан: %d объявл., цены %.2f–%.2f | новых: %d, к алерту: %d",
                             len(items), min(prices), max(prices), new, matched)
                first = False
        except Exception:
            log.exception("Ошибка в цикле монитора")
        rescan.wait(timeout=CFG["poll_seconds"])


def main():
    log.info("Старт. Фильтры:\n%s", filters_text(get_filters()))
    tg("✅ P2P монитор запущен\n\n" + filters_text(get_filters()) + "\n\n/menu — кнопки, /help — команды",
       REPLY_KB)
    threading.Thread(target=command_listener, daemon=True).start()
    monitor_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Остановлено")