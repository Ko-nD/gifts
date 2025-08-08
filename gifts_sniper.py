#!/usr/bin/env python3
# GiftSniper — VPS edition (no-sleep, notifier, watchdog)
# - keepalive ping + watchdog reconnect
# - /check, /check-all modes
# - Telegram notify via Bot API (hourly pulse, reconnects, errors)

from __future__ import annotations
import argparse, asyncio, json, os, random, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.request, urllib.parse

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import (
    AuthKeyDuplicated, BadRequest, FloodWait, InternalServerError, PeerFlood
)
try:
    from pyrogram.raw.functions import Ping
except Exception:
    Ping = None

# ───── базовые настройки ─────
VERBOSE             = True
NO_NEW_EVERY_SEC    = 60           # «новинок нет» — не чаще чем раз в N секунд
KEEPALIVE_PERIOD    = 90           # ping каждые N сек
WATCHDOG_PERIOD     = 600          # если 10+ мин нет успешных RPC → reconnect
RECONNECT_TRIES     = 5
RECONNECT_PAUSE     = 3

STORAGE = Path("gifts.json")

# ───── часовой пояс (МСК, без зависимости от tzdata) ─────
def msk_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Moscow")
    except Exception:
        return timezone(timedelta(hours=3))
MSK = msk_tz()
now = lambda: datetime.now(MSK)
fmt = lambda dt: dt.strftime("%H:%M:%S")

# ───── env ─────
load_dotenv()
API_ID   = int(os.getenv("TG_API_ID", 0))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION  = os.getenv("TG_SESSION") or None

ID_TO_BUY= int(os.getenv("ID_TO_BUY", 0))
BUY_GIFT = os.getenv("BUY_GIFT", "false").lower() == "true"

P_FROM, P_TO = int(os.getenv("PRICE_LIMIT_FROM", 500)),  int(os.getenv("PRICE_LIMIT_TO", 50_000))
S_FROM, S_TO = int(os.getenv("SUPPLY_LIMIT_FROM", 1)),   int(os.getenv("SUPPLY_LIMIT_TO", 60_000))

POLL_MIN, POLL_MAX = int(os.getenv("POLL_INTERVAL_FROM", 25)), int(os.getenv("POLL_INTERVAL_TO", 35))

# notifier env (не обязателен)
BOT_TOKEN        = os.getenv("BOT_TOKEN", "").strip()
NOTIFY_CHAT_ID   = os.getenv("NOTIFY_CHAT_ID", "").strip()
NOTIFY_HOURLY    = os.getenv("NOTIFY_HOURLY", "true").lower() == "true"
NOTIFY_DAILY     = os.getenv("NOTIFY_DAILY",  "true").lower() == "true"
NOTIFY_RECONNECT = os.getenv("NOTIFY_RECONNECT","true").lower() == "true"
NOTIFY_ERRORS    = os.getenv("NOTIFY_ERRORS", "true").lower() == "true"

def fatal(msg): print("[FATAL]", msg); sys.exit(1)
if not (API_ID and API_HASH): fatal("TG_API_ID / TG_API_HASH пусты")
if not SESSION and not Path("TgAccount.session").exists(): fatal("нужен TG_SESSION или файл TgAccount.session")
if BUY_GIFT and not ID_TO_BUY: fatal("ID_TO_BUY обязателен при BUY_GIFT=true")

# ───── простая отправка в TG через Bot API ─────
async def tg_notify(text: str, parse_mode: str = "HTML") -> bool:
    token = BOT_TOKEN
    chat  = NOTIFY_CHAT_ID
    if not token or not chat:
        return False

    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true"
    }).encode()

    def _do_req():
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded"
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200

    try:
        ok = await asyncio.to_thread(_do_req)
        if not ok:
            print("[WARN] notify: HTTP not OK")
        return ok
    except Exception as e:
        print("[WARN] notify failed:", e)
        return False

# ───── учёт статистики ─────
class Stats:
    def __init__(self) -> None:
        self.fetch_ok = 0
        self.fetch_err = 0
        self.buys = 0
        self.reconnects = 0
        self.last_buy: Optional[datetime] = None
        self.last_new: Optional[datetime] = None

    def snapshot(self) -> str:
        parts = [
            f"fetch_ok={self.fetch_ok}",
            f"fetch_err={self.fetch_err}",
            f"buys={self.buys}",
            f"reconnects={self.reconnects}",
            f"last_buy={fmt(self.last_buy) if self.last_buy else '—'}",
            f"last_new={fmt(self.last_new) if self.last_new else '—'}",
        ]
        return " | ".join(parts)

STATS = Stats()

# ───── код снайпера ─────
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
            STATS.fetch_ok += 1
        except InternalServerError:
            await asyncio.sleep(1)
            raw = await self.u.get_available_gifts()
            STATS.fetch_ok += 1
        except Exception as e:
            STATS.fetch_err += 1
            raise e
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
            STATS.buys += bought
            STATS.last_buy = now()
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
            STATS.last_new = now()
            STORAGE.write_text(json.dumps(sorted(self.seen), ensure_ascii=False, indent=2))

        if VERBOSE:
            t = fmt(now())
            if bought:
                print(f"[{t}] ✅ купили")
            elif (self._last_no_new is None or (now() - self._last_no_new).seconds >= NO_NEW_EVERY_SEC):
                print(f"[{t}] — новинок нет"); self._last_no_new = now()

        return bought, new

# ───── keepalive & watchdog ─────
async def keepalive(cli: Client, touch_ok):
    while True:
        try:
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
            STATS.reconnects += 1
            return True
        except Exception as e:
            print(f"[WARN] reconnect #{n} fail:", e)
            await asyncio.sleep(RECONNECT_PAUSE)
    return False

async def hourly_pulse():
    while True:
        if NOTIFY_HOURLY:
            msg = f"⏱️ Hourly pulse\n{STATS.snapshot()}"
            await tg_notify(msg)
        await asyncio.sleep(3600)

def seconds_until(hour: int, minute: int) -> int:
    n = now()
    target = n.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= n:
        target += timedelta(days=1)
    return int((target - n).total_seconds())

async def daily_summary():
    while True:
        await asyncio.sleep(seconds_until(23, 59))
        if NOTIFY_DAILY:
            msg = f"📊 Daily summary\n{STATS.snapshot()}"
            await tg_notify(msg)

# ───── main ─────
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="однократная проверка новых")
    ap.add_argument("--check-all", action="store_true", help="однократная проверка всех")
    args = ap.parse_args()

    cli = Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=SESSION) \
          if SESSION else Client("TgAccount", api_id=API_ID, api_hash=API_HASH)
    sniper = GiftSniper(cli)

    last_ok = now()
    def touch_ok():
        nonlocal last_ok
        last_ok = now()

    try:
        await cli.start()

        # приветное уведомление, чтобы проверить BOT_TOKEN/чат
        await tg_notify("✅ GiftSniper started")

        if args.check or args.check_all:
            await sniper.tick(only_new=not args.check_all)
            return

        # init dump (по желанию)
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
                if NOTIFY_ERRORS:
                    await tg_notify(f"⚠️ Init dump failed: <code>{e}</code>")

        # фоновые задачи
        ka_task = asyncio.create_task(keepalive(cli, touch_ok))
        hp_task = asyncio.create_task(hourly_pulse())
        ds_task = asyncio.create_task(daily_summary())

        while True:
            # watchdog
            if (now() - last_ok).seconds >= WATCHDOG_PERIOD:
                print("[WARN] watchdog: stale connection, reconnecting…")
                if NOTIFY_RECONNECT:
                    await tg_notify("♻️ Watchdog: reconnecting…")
                if not await reconnect(cli):
                    print("[ERR] watchdog: reconnect failed; retry later")
                    if NOTIFY_ERRORS:
                        await tg_notify("❌ Watchdog: reconnect failed")
                else:
                    touch_ok()
                    # перезапуск keepalive
                    try:
                        ka_task.cancel()
                    except Exception:
                        pass
                    ka_task = asyncio.create_task(keepalive(cli, touch_ok))

            try:
                await sniper.tick()
                touch_ok()
            except (OSError, ConnectionError) as e:
                print("[ERR] connection:", e)
                if NOTIFY_RECONNECT:
                    await tg_notify(f"♻️ Connection error → reconnect: <code>{e}</code>")
                if await reconnect(cli):
                    touch_ok()
                    try:
                        ka_task.cancel()
                    except Exception:
                        pass
                    ka_task = asyncio.create_task(keepalive(cli, touch_ok))
                else:
                    print("[ERR] reconnect failed; will retry after sleep")
                    if NOTIFY_ERRORS:
                        await tg_notify("❌ Reconnect failed; will retry")

            await asyncio.sleep(random.randint(POLL_MIN, POLL_MAX))

    except AuthKeyDuplicated:
        fatal("AuthKeyDuplicated — та же сессия уже активна (закройте другой процесс).")
    finally:
        try:
            await cli.stop()
        except Exception:
            pass

if __name__ == "__main__":
    # важное: без буферизации (см. unit-файл тоже)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("⏹️ exit")
