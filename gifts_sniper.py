#!/usr/bin/env python3
# GiftSniper — VPS edition (no sleep)
# - user session (MTProto) for all actions & purchases
# - notifications via Bot API if available, fallback to user MTProto
# - keepalive ping + watchdog reconnect
# - hourly heartbeat & daily summary
# - supports --check and --check-all

from __future__ import annotations
import argparse, asyncio, json, os, random, sys, urllib.parse, urllib.request, ssl
from datetime import datetime, timedelta, time, date, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import (
    AuthKeyDuplicated, BadRequest, FloodWait,
    InternalServerError, PeerFlood
)
try:
    from pyrogram.raw.functions import Ping
except Exception:
    Ping = None  # fallback: no raw ping

# ────────── базовые настройки ──────────
VERBOSE             = True
NO_NEW_EVERY_SEC    = 60        # "новинок нет" не чаще раза в N секунд
KEEPALIVE_PERIOD    = 90        # секунд
WATCHDOG_PERIOD     = 300       # 5 минут без успешных RPC → reconnect
RECONNECT_TRIES     = 5
RECONNECT_PAUSE     = 3

STORAGE = Path("gifts.json")

# ────────── часовой пояс МСК ──────────
def msk_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Moscow")
    except Exception:
        return timezone(timedelta(hours=3))
MSK = msk_tz()
now = lambda: datetime.now(MSK)
fmt = lambda dt: dt.strftime("%H:%M:%S")
fmt_d = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")

# ────────── env ──────────
load_dotenv()
API_ID   = int(os.getenv("TG_API_ID", 0))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION  = os.getenv("TG_SESSION") or None

ID_TO_BUY= int(os.getenv("ID_TO_BUY", 0))
BUY_GIFT = os.getenv("BUY_GIFT", "false").lower() == "true"

P_FROM, P_TO = int(os.getenv("PRICE_LIMIT_FROM", 500)),  int(os.getenv("PRICE_LIMIT_TO", 50_000))
S_FROM, S_TO = int(os.getenv("SUPPLY_LIMIT_FROM", 1)),   int(os.getenv("SUPPLY_LIMIT_TO", 60_000))

POLL_MIN, POLL_MAX = int(os.getenv("POLL_INTERVAL_FROM", 25)), int(os.getenv("POLL_INTERVAL_TO", 35))

BOT_TOKEN       = (os.getenv("BOT_TOKEN") or "").strip()
NOTIFY_CHAT_ID  = (os.getenv("NOTIFY_CHAT_ID") or "").strip()

def to_bool(s: Optional[str], default=True) -> bool:
    if s is None: return default
    return s.strip().lower() in ("1","true","yes","on")
NOTIFY_HOURLY      = to_bool(os.getenv("NOTIFY_HOURLY"), True)
NOTIFY_DAILY       = to_bool(os.getenv("NOTIFY_DAILY"), True)
NOTIFY_RECONNECT   = to_bool(os.getenv("NOTIFY_RECONNECT"), True)
NOTIFY_ERRORS      = to_bool(os.getenv("NOTIFY_ERRORS"), True)

def fatal(msg): print("[FATAL]", msg); sys.exit(1)
if not (API_ID and API_HASH): fatal("TG_API_ID / TG_API_HASH пусты")
if not SESSION and not Path("TgAccount.session").exists(): fatal("нужен TG_SESSION или файл TgAccount.session")
if BUY_GIFT and not ID_TO_BUY: fatal("ID_TO_BUY обязателен при BUY_GIFT=true")

# ────────── нотификатор ──────────
class Notifier:
    """Пытается отправить через Bot API; если не вышло → отправляет MTProto-юзером."""
    def __init__(self, bot_token: str, chat_id: str, cli: Client):
        self.bot_token = bot_token or ""
        self.chat_id   = chat_id or ""
        self.cli       = cli
        # короткие таймауты, чтобы логи не мешали работе
        self.http_timeout = 2.5

        # на некоторых VPS нужны «более добрые» SSL-опции
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = True

    async def send(self, text: str) -> bool:
        text = text.strip()
        if not text:
            return True
        # 1) Bot API (быстро и не блокирует основной контур)
        if self.bot_token and self.chat_id:
            ok = await asyncio.to_thread(self._send_botapi_blocking, text)
            if ok:
                return True
        # 2) fallback: MTProto от юзер-аккаунта
        if self.chat_id:
            try:
                await self.cli.send_message(chat_id=int(self.chat_id), text=text, disable_web_page_preview=True)
                return True
            except ValueError:
                # может быть строковый юзернейм/peer — попробуем как есть
                try:
                    await self.cli.send_message(chat_id=self.chat_id, text=text, disable_web_page_preview=True)
                    return True
                except Exception as e:
                    if VERBOSE:
                        print("[WARN] notify MTProto failed:", e)
        return False

    def _send_botapi_blocking(self, text: str) -> bool:
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=self.http_timeout, context=self.ctx) as r:
                # ждём только 2.5с и не парсим ответ во что-то сложное
                return r.status == 200
        except Exception:
            return False

# ────────── снайпер ──────────
class GiftSniper:
    def __init__(self, cli: Client):
        self.u = cli
        self.seen: set[int] = set()
        if STORAGE.exists():
            try:
                data = json.loads(STORAGE.read_text())
                self.seen |= {int(x if not isinstance(x, dict) else x.get("id")) for x in data}
            except Exception as e:
                print("[WARN] gifts.json:", e)
        self._last_no_new: Optional[datetime] = None

    @staticmethod
    def _norm(g: Any) -> Dict[str, Any]:
        keys = ("supply", "total_count", "total_amount", "amount")
        if isinstance(g, dict):
            sup = next((g.get(k) for k in keys if g.get(k) is not None), None)
            price = g.get("price") or g.get("star_count")
            emoji = (g.get("sticker") or {}).get("emoji")
            if emoji == "🎁": emoji = None
            return dict(id=g["id"], title=emoji or f"ID-{g['id']}",
                        price=price, supply=sup,
                        is_limited=g.get("is_limited", sup is not None))
        sup   = next((getattr(g, k, None) for k in keys if getattr(g, k, None) is not None), None)
        price = getattr(g, "price", getattr(g, "star_count", None))
        emoji = getattr(getattr(g, "sticker", None), "emoji", None)
        if emoji == "🎁": emoji = None
        return dict(id=g.id, title=emoji or f"ID-{g.id}",
                    price=price, supply=sup,
                    is_limited=getattr(g, "is_limited", sup is not None))

    async def _fetch(self) -> List[Dict[str, Any]]:
        raw = None
        try:
            raw = await self.u.get_available_gifts()
        except InternalServerError:
            await asyncio.sleep(1)
            raw = await self.u.get_available_gifts()
        gifts = [self._norm(x) for x in (raw or [])]
        gifts.sort(key=lambda x: x["supply"] if x["supply"] is not None else float("inf"))
        return gifts

    def _eligible(self, g: Dict[str, Any]) -> bool:
        return (g["price"] is not None and P_FROM <= g["price"] <= P_TO and
                g["supply"] is not None and S_FROM <= g["supply"] <= S_TO)

    async def _buy(self, g: Dict[str, Any], max_q: int) -> int:
        left = max_q
        bought = 0
        while left:
            try:
                await self.u.send_gift(ID_TO_BUY, g["id"], False)
                left -= 1; bought += 1
                await asyncio.sleep(random.uniform(0.7, 1.5))
            except FloodWait as fw:
                print("[FW]", fw.value, "s"); await asyncio.sleep(fw.value)
            except (BadRequest, PeerFlood) as e:
                print("[ERR] buy:", e); break
            except Exception as e:
                # отдаём наверх — пусть main решит reconnect
                raise e
        if bought:
            print(f"[BUY] {g['title']} ×{bought}")
        return bought

    async def tick(self, only_new: bool=False) -> Tuple[bool, bool]:
        """Возвращает (было_покупок, были_новые)"""
        gifts = await self._fetch()
        rare_i = 0
        bought = False
        new    = False

        if only_new:
            gifts = [g for g in gifts if g["id"] not in self.seen]

        for g in gifts:
            if g["id"] in self.seen and not only_new:
                continue
            self.seen.add(g["id"]); new = True

            max_q = 0
            if g["is_limited"]:
                if rare_i == 0:   max_q = 10
                elif rare_i == 1: max_q = 25
                rare_i += 1

            if max_q and self._eligible(g) and BUY_GIFT:
                if await self._buy(g, max_q):
                    bought = True

        if new:
            STORAGE.write_text(json.dumps(sorted(self.seen), ensure_ascii=False, indent=2))

        if VERBOSE:
            t = fmt(now())
            if bought:
                print(f"[{t}] ✅ купили"); self._last_no_new = now()
            elif (self._last_no_new is None or (now() - self._last_no_new).seconds >= NO_NEW_EVERY_SEC):
                print(f"[{t}] — новинок нет"); self._last_no_new = now()

        return bought, new

# ────────── keepalive & reconnect ──────────
async def keepalive(cli: Client, touch_ok):
    while True:
        try:
            if getattr(cli, "is_connected", False):
                if Ping is not None:
                    await cli.invoke(Ping(ping_id=random.randint(1, 1 << 31)))
                touch_ok()
        except Exception as e:
            # просто предупреждаем; watchdog займётся реконнектом
            print("[WARN] keepalive:", e)
        await asyncio.sleep(KEEPALIVE_PERIOD)

async def reconnect(cli: Client) -> bool:
    for n in range(1, RECONNECT_TRIES + 1):
        try:
            await cli.stop(); await asyncio.sleep(1); await cli.start()
            print(f"[INFO] reconnect ok #{n}")
            return True
        except Exception as e:
            print(f"[WARN] reconnect #{n} fail:", e)
            await asyncio.sleep(RECONNECT_PAUSE)
    return False

# ────────── утилиты ──────────
def midnight_msk(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)

# ────────── main ──────────
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="однократная проверка новых")
    ap.add_argument("--check-all", action="store_true", help="однократная проверка всех")
    args = ap.parse_args()

    cli = Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=SESSION) \
          if SESSION else Client("TgAccount", api_id=API_ID, api_hash=API_HASH)

    sniper = GiftSniper(cli)

    # счётчики для отчётов
    polls = 0
    buys  = 0
    reconnects = 0
    last_ok = now()
    last_heartbeat = now()
    day_anchor = midnight_msk(now())

    def touch_ok():
        nonlocal last_ok
        last_ok = now()

    notifier = Notifier(BOT_TOKEN, NOTIFY_CHAT_ID, cli)

    try:
        await cli.start()

        # стартовые сообщения
        await notifier.send(f"▶️ GiftSniper запущен ({fmt_d(now())} MSK)\n"
                            f"poll={POLL_MIN}-{POLL_MAX}s, watchdog={WATCHDOG_PERIOD}s")

        if args.check or args.check_all:
            bought, _ = await sniper.tick(only_new=not args.check_all)
            if bought:
                await notifier.send("✅ Проверка: есть покупки.")
            else:
                await notifier.send("ℹ️ Проверка: покупок нет.")
            return

        # первичный дамп (по желанию)
        if os.getenv("INIT_DUMP_ALL", "true").lower() == "true":
            try:
                gifts = await sniper._fetch()
                print(f"Initial dump: {len(gifts)} gifts")
                for g in gifts:
                    print(f"  {g['title']} | {g['price']}⭐ | left: {g['supply']}")
                print("✅ init-dump done")
                touch_ok()
            except Exception as e:
                print("[WARN] init dump failed:", e)

        # фоновые задачи
        ka_task = asyncio.create_task(keepalive(cli, touch_ok))

        while True:
            # watchdog
            if (now() - last_ok).seconds >= WATCHDOG_PERIOD:
                print("[WARN] watchdog: stale connection, reconnecting…")
                if await reconnect(cli):
                    reconnects += 1
                    touch_ok()
                    if NOTIFY_RECONNECT:
                        await notifier.send("♻️ Watchdog: переподключились.")
                    try:
                        ka_task.cancel()
                    except Exception:
                        pass
                    ka_task = asyncio.create_task(keepalive(cli, touch_ok))
                else:
                    if NOTIFY_ERRORS:
                        await notifier.send("❗ Watchdog: не удалось переподключиться, попробуем позже.")

            # тик
            try:
                bought, _ = await sniper.tick()
                polls += 1
                if bought:
                    buys += 1
                touch_ok()
            except (OSError, ConnectionError) as e:
                print("[ERR] connection:", e)
                if await reconnect(cli):
                    reconnects += 1
                    touch_ok()
                    if NOTIFY_RECONNECT:
                        await notifier.send("♻️ Reconnect после сетевой ошибки.")
                    try:
                        ka_task.cancel()
                    except Exception:
                        pass
                    ka_task = asyncio.create_task(keepalive(cli, touch_ok))
                else:
                    if NOTIFY_ERRORS:
                        await notifier.send("❗ Reconnect после сетевой ошибки не удался.")
            except Exception as e:
                print("[ERR] main:", e)
                if NOTIFY_ERRORS:
                    await notifier.send(f"❗ Ошибка цикла: {e!r}")

            # hourly heartbeat
            if NOTIFY_HOURLY and (now() - last_heartbeat) >= timedelta(hours=1):
                last_heartbeat = now()
                await notifier.send(
                    f"⏱️ Heartbeat {fmt_d(now())} MSK\n"
                    f"polls={polls}, buys={buys}, reconnects={reconnects}"
                )

            # daily summary (в момент перехода через полночь МСК)
            if NOTIFY_DAILY and now() >= (day_anchor + timedelta(days=1)):
                await notifier.send(
                    f"📊 Daily summary ({day_anchor.date()}): "
                    f"polls={polls}, buys={buys}, reconnects={reconnects}"
                )
                day_anchor = midnight_msk(now())
                polls = buys = reconnects = 0  # обнулим на новый день

            await asyncio.sleep(random.randint(POLL_MIN, POLL_MAX))

    except AuthKeyDuplicated:
        fatal("AuthKeyDuplicated — та же сессия уже активна (закройте другой процесс).")
    finally:
        try:
            await cli.stop()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("⏹️ exit")
