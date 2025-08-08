#!/usr/bin/env python3
# GiftSniper — VPS edition (no sleep, dynamic watchdog, safe non-blocking notifications)

from __future__ import annotations
import argparse, asyncio, json, os, random, sys, urllib.parse, urllib.request, ssl
from datetime import datetime, timedelta, timezone, date, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import (
    AuthKeyDuplicated, BadRequest, FloodWait, InternalServerError, PeerFlood
)
try:
    from pyrogram.raw.functions import Ping
except Exception:
    Ping = None  # fallback: no raw ping

# ────────── базовые настройки ──────────
VERBOSE             = True
NO_NEW_EVERY_SEC    = 60        # в stdout "новинок нет" не чаще
KEEPALIVE_PERIOD    = 90        # секунд
RECONNECT_TRIES     = 5
RECONNECT_PAUSE     = 3
SAFE_NOTIFY_TIMEOUT = float(os.getenv("NOTIFY_TIMEOUT", "2.5"))  # таймаут отправки уведомления

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

NOTIFY_ENABLED    = to_bool(os.getenv("NOTIFY_ENABLED"), True)
NOTIFY_HOURLY     = to_bool(os.getenv("NOTIFY_HOURLY"),  True)
NOTIFY_DAILY      = to_bool(os.getenv("NOTIFY_DAILY"),   True)
NOTIFY_RECONNECT  = to_bool(os.getenv("NOTIFY_RECONNECT"), True)
NOTIFY_ERRORS     = to_bool(os.getenv("NOTIFY_ERRORS"),  True)

def fatal(msg): print("[FATAL]", msg); sys.exit(1)
if not (API_ID and API_HASH): fatal("TG_API_ID / TG_API_HASH пусты")
if not SESSION and not Path("TgAccount.session").exists(): fatal("нужен TG_SESSION или файл TgAccount.session")
if BUY_GIFT and not ID_TO_BUY: fatal("ID_TO_BUY обязателен при BUY_GIFT=true")

def compute_watchdog_period() -> int:
    env = os.getenv("WATCHDOG_PERIOD")
    if env:
        try:
            v = int(env)
            if v > 0: return v
        except Exception:
            pass
    return max(180, POLL_MAX * 4, KEEPALIVE_PERIOD * 2)

WATCHDOG_PERIOD = compute_watchdog_period()

# ────────── нотификатор ──────────
class Notifier:
    """Пробует Bot API, затем MTProto. НИКОГДА не бросает исключения наружу."""
    def __init__(self, bot_token: str, chat_id: str, cli: Client):
        self.bot_token = bot_token or ""
        self.chat_id   = chat_id or ""
        self.cli       = cli
        self.http_timeout = SAFE_NOTIFY_TIMEOUT
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = True

    async def send(self, text: str) -> bool:
        """Возвращает True/False; не бросает исключений."""
        if not NOTIFY_ENABLED:
            return True
        text = (text or "").strip()
        if not text:
            return True
        # 1) Bot API
        if self.bot_token and self.chat_id:
            ok = await asyncio.to_thread(self._send_botapi_blocking, text)
            if ok:
                return True
        # 2) MTProto (юзер)
        if self.chat_id:
            try:
                # пытаемся как int
                try:
                    await self.cli.send_message(chat_id=int(self.chat_id), text=text, disable_web_page_preview=True)
                    return True
                except ValueError:
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
                "disable_web_page_preview": "true"
            }).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=self.http_timeout, context=self.ctx) as r:
                return r.status == 200
        except Exception:
            return False

def fire_and_forget(coro: asyncio.Future) -> None:
    """Запустить корутину в фоне, съесть любые исключения."""
    async def _wrap():
        try:
            await asyncio.wait_for(coro, timeout=SAFE_NOTIFY_TIMEOUT + 0.5)
        except Exception:
            pass
    asyncio.create_task(_wrap())

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
                raise e
        if bought:
            print(f"[BUY] {g['title']} ×{bought}")
        return bought

    async def tick(self, only_new: bool=False) -> Tuple[bool, bool]:
        """Возвращает (были_покупки, были_новые_подарки)"""
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

    polls = buys = reconnects = 0
    last_ok = now()
    day_anchor = midnight_msk(now())

    hour_anchor = now()
    hour_polls = hour_buys = hour_new = 0

    def touch_ok():
        nonlocal last_ok
        last_ok = now()

    notifier = Notifier(BOT_TOKEN, NOTIFY_CHAT_ID, cli)

    try:
        await cli.start()
        # стартовое сообщение — фоном и необязательно
        fire_and_forget(notifier.send(
            f"▶️ GiftSniper запущен ({fmt_d(now())} MSK)\n"
            f"poll={POLL_MIN}-{POLL_MAX}s, keepalive={KEEPALIVE_PERIOD}s, watchdog={WATCHDOG_PERIOD}s (auto)"
        ))

        if args.check or args.check_all:
            bought, new = await sniper.tick(only_new=not args.check_all)
            txt = "✅ Проверка: есть покупки." if bought else ("ℹ️ Проверка: есть новые, без покупок." if new else "ℹ️ Проверка: новинок нет.")
            fire_and_forget(notifier.send(txt))
            return

        ka_task = asyncio.create_task(keepalive(cli, touch_ok))

        while True:
            # dynamic watchdog
            if (now() - last_ok).seconds >= WATCHDOG_PERIOD:
                print("[WARN] watchdog: stale connection, reconnecting…")
                if await reconnect(cli):
                    reconnects += 1
                    touch_ok()
                    if NOTIFY_RECONNECT:
                        fire_and_forget(notifier.send("♻️ Watchdog: переподключились."))
                    try:
                        ka_task.cancel()
                    except Exception:
                        pass
                    ka_task = asyncio.create_task(keepalive(cli, touch_ok))
                else:
                    if NOTIFY_ERRORS:
                        fire_and_forget(notifier.send("❗ Watchdog: не удалось переподключиться, попробуем позже."))

            # основной тик
            try:
                bought, new = await sniper.tick()
                polls += 1
                hour_polls += 1
                if bought:
                    buys += 1
                    hour_buys += 1
                if new:
                    hour_new += 1
                touch_ok()
            except (OSError, ConnectionError) as e:
                print("[ERR] connection:", e)
                if await reconnect(cli):
                    reconnects += 1
                    touch_ok()
                    if NOTIFY_RECONNECT:
                        fire_and_forget(notifier.send("♻️ Reconnect после сетевой ошибки."))
                    try:
                        ka_task.cancel()
                    except Exception:
                        pass
                    ka_task = asyncio.create_task(keepalive(cli, touch_ok))
                else:
                    if NOTIFY_ERRORS:
                        fire_and_forget(notifier.send("❗ Reconnect после сетевой ошибки не удался."))
            except Exception as e:
                print("[ERR] main:", e)
                if NOTIFY_ERRORS:
                    fire_and_forget(notifier.send(f"❗ Ошибка цикла: {e!r}"))

            # hourly: если не было новых подарков за прошедший час — отправим заметку
            if NOTIFY_HOURLY and (now() - hour_anchor) >= timedelta(hours=1):
                if hour_new == 0:
                    fire_and_forget(notifier.send(
                        f"🕐 За последний час новинок не было ({fmt_d(now())} MSK)\n"
                        f"polls={hour_polls}, reconnects={reconnects}"
                    ))
                hour_anchor = now()
                hour_polls = hour_buys = hour_new = 0

            # daily summary (в полночь МСК)
            if NOTIFY_DAILY and now() >= (day_anchor + timedelta(days=1)):
                fire_and_forget(notifier.send(
                    f"📊 Daily summary ({day_anchor.date()}): polls={polls}, buys={buys}, reconnects={reconnects}"
                ))
                day_anchor = midnight_msk(now())
                polls = buys = reconnects = 0

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
