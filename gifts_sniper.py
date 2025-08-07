#!/usr/bin/env python
# GiftSniper 1.0.0 – auto-reconnect on silent Pyrogram failures (Amvera-safe)

from __future__ import annotations
import argparse, asyncio, json, os, random, sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, time, date
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.raw.functions import Ping            # совместимо с PyroFork-2.3
from pyrogram.errors import (AuthKeyDuplicated, BadRequest, FloodWait,
                             InternalServerError, PeerFlood)

# ─────────── настройки ───────────
VERBOSE              = True
NO_NEW_EVERY_SEC     = 60           # «новинок нет» реже, с
POLL_MIN, POLL_MAX   = 25, 35       # обычный рабочий опрос
KEEPALIVE_PERIOD     = 90           # MTProto-ping
WATCHDOG_PERIOD      = 300          # проверка .is_connected
RECONNECT_TRIES      = 5
RECONNECT_PAUSE      = 3

# окно сна MSK: старт 02 :30-03 :30, длит. 3-7 ч
SLEEP_START_FROM, SLEEP_START_TO = time(2,30), time(3,30)
SLEEP_LEN_H_MIN,  SLEEP_LEN_H_MAX = 3, 7

# ── DummyContextManager (Py 3.13) ──
if not hasattr(asyncio, "DummyContextManager"):
    @asynccontextmanager
    async def _dummy(): yield
    asyncio.DummyContextManager = _dummy         # type: ignore

# ───────── env ─────────
load_dotenv()
API_ID   = int(os.getenv("TG_API_ID", 0))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION  = os.getenv("TG_SESSION") or None
ID_TO_BUY= int(os.getenv("ID_TO_BUY", 0))
BUY_GIFT = os.getenv("BUY_GIFT", "false").lower() == "true"

P_FROM, P_TO = int(os.getenv("PRICE_LIMIT_FROM", 500)),   int(os.getenv("PRICE_LIMIT_TO", 50_000))
S_FROM, S_TO = int(os.getenv("SUPPLY_LIMIT_FROM", 1)),    int(os.getenv("SUPPLY_LIMIT_TO", 60_000))

STORAGE = Path("gifts.json")

def fatal(msg): print("[FATAL]", msg); sys.exit(1)
if not (API_ID and API_HASH): fatal("TG_API_ID / TG_API_HASH отсутствуют в .env")
if not SESSION and not Path("TgAccount.session").exists(): fatal("нужен TG_SESSION или *.session")
if BUY_GIFT and not ID_TO_BUY: fatal("ID_TO_BUY обязателен при BUY_GIFT=true")

# ═════════════ GiftSniper ═════════════
class GiftSniper:
    def __init__(self, cli: Client):
        self.u = cli
        self.seen: set[int] = set()
        if STORAGE.exists():
            try:
                self.seen |= {int(i) if isinstance(i, int) else int(i["id"])
                              for i in json.loads(STORAGE.read_text())}
            except Exception as e:
                print("[WARN] gifts.json:", e)
        self._last_no_new: Optional[datetime] = None

    @staticmethod
    def _norm(g: Any) -> Dict[str, Any]:
        keys = ("supply", "total_count", "total_amount", "amount")
        if isinstance(g, dict):
            sup   = next((g.get(k) for k in keys if g.get(k) is not None), None)
            price = g.get("price") or g.get("star_count")
            emoji = (g.get("sticker") or {}).get("emoji") or ""
        else:
            sup   = next((getattr(g, k, None) for k in keys if getattr(g, k, None) is not None), None)
            price = getattr(g, "price", getattr(g, "star_count", None))
            emoji = getattr(getattr(g, "sticker", None), "emoji", "") or ""
        if emoji == "🎁": emoji = ""
        return dict(id = g["id"] if isinstance(g, dict) else g.id,
                    title = emoji or f"ID-{g['id'] if isinstance(g, dict) else g.id}",
                    price = price, supply = sup,
                    is_limited = g.get("is_limited", sup is not None) if isinstance(g, dict)
                                 else getattr(g, "is_limited", sup is not None))

    async def _fetch(self) -> List[Dict[str, Any]]:
        try:
            raw = await self.u.get_available_gifts()
        except InternalServerError:
            await asyncio.sleep(1)
            raw = await self.u.get_available_gifts()
        gifts = [self._norm(x) for x in raw or []]
        gifts.sort(key=lambda x: x["supply"] if x["supply"] is not None else float("inf"))
        return gifts

    def _eligible(self, g):          # подходит ли под фильтры
        return g["price"]   and P_FROM <= g["price"]  <= P_TO and \
               g["supply"]  and S_FROM <= g["supply"] <= S_TO

    async def _buy(self, g, qty):
        bought, left = 0, qty
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
        if bought: print(f"[BUY] {g['title']} ×{bought}")
        return bought

    async def tick(self, show_all: bool = False):
        gifts = await self._fetch()
        rare_i = 0; bought = False; new = False
        for g in gifts:
            if not show_all and g["id"] in self.seen:
                continue
            if not show_all: self.seen.add(g["id"]); new = True
            max_q = 0
            if g["is_limited"]:
                max_q = 10 if rare_i == 0 else 25 if rare_i == 1 else 0
                rare_i += 1
            if max_q and self._eligible(g) and BUY_GIFT:
                if await self._buy(g, max_q): bought = True
            if show_all:   # режим --check-all: просто печатаем
                print(f"{g['title']} | {g['price']}⭐ | {g['supply']} | limited={g['is_limited']}")
        if new:
            STORAGE.write_text(json.dumps(sorted(self.seen), ensure_ascii=False, indent=2))
        if VERBOSE and not show_all:
            now = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%H:%M:%S")
            if bought:
                print(f"[{now}] ✅ купили"); self._last_no_new = datetime.now(ZoneInfo("Europe/Moscow"))
            elif (self._last_no_new is None or
                  (datetime.now(ZoneInfo("Europe/Moscow")) - self._last_no_new).seconds >= NO_NEW_EVERY_SEC):
                print(f"[{now}] — новинок нет"); self._last_no_new = datetime.now(ZoneInfo("Europe/Moscow"))

# ═════════════ вспомогательные корутины ═════════════
async def reconnect(cli: Client) -> bool:
    for n in range(1, RECONNECT_TRIES + 1):
        try:
            await cli.stop(); await asyncio.sleep(1); await cli.start()
            print(f"[INFO] reconnect OK, попытка {n}")
            return True
        except Exception as e:
            print(f"[WARN] reconnect {n} fail:", e); await asyncio.sleep(RECONNECT_PAUSE)
    return False

async def keepalive(cli: Client):
    while True:
        await asyncio.sleep(KEEPALIVE_PERIOD)
        if not cli.is_started or not cli.is_connected:      # клиент не готов
            continue
        try:
            await cli.invoke(Ping(ping_id=random.randint(1, 1 << 31)))
        except Exception as e:
            print("[WARN] keepalive:", e)
            await reconnect(cli)

def next_sleep_start() -> datetime:
    today = date.today()
    base  = datetime.combine(today, time(3, 0), ZoneInfo("Europe/Moscow"))
    delta = random.randint(-30, +30)          # −30…+30 мин
    return base + timedelta(minutes=delta)

# ═════════════════════════════ main ═════════════════════════════
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check",      action="store_true", help="показать только новые подарки и выйти")
    ap.add_argument("--check-all",  action="store_true", help="показать весь список и выйти")
    args = ap.parse_args()

    cli = Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=SESSION) \
          if SESSION else Client("TgAccount", api_id=API_ID, api_hash=API_HASH)
    sniper = GiftSniper(cli)

    await cli.start()
    # режим «один раз и выйти»
    if args.check or args.check_all:
        await sniper.tick(show_all=args.check_all)
        await cli.stop(); return

    ka_task = asyncio.create_task(keepalive(cli))
    next_sleep = next_sleep_start()
    if datetime.now(ZoneInfo("Europe/Moscow")) >= next_sleep:
        next_sleep += timedelta(days=1)
    last_watchdog = datetime.now()

    try:
        while True:
            now = datetime.now(ZoneInfo("Europe/Moscow"))

            # ночной сон
            if now >= next_sleep:
                dur_h = random.uniform(SLEEP_LEN_H_MIN, SLEEP_LEN_H_MAX)
                print(f"😴 сон {dur_h:.1f} ч")
                await asyncio.sleep(int(dur_h * 3600))
                print("🌅 проснулись")
                next_sleep = next_sleep_start() + timedelta(days=1)
                last_watchdog = datetime.now()

            # watchdog: каждые 5 мин проверяем, что клиент online
            if (now - last_watchdog).seconds >= WATCHDOG_PERIOD:
                if not cli.is_connected:
                    print("[WARN] watchdog: соед. потеряно – пытаемся восстановить")
                    await reconnect(cli)
                last_watchdog = now

            try:
                await sniper.tick()
            except (OSError, ConnectionError) as e:
                print("[ERR] connection:", e)
                await reconnect(cli)

            await asyncio.sleep(random.randint(POLL_MIN, POLL_MAX))

    except AuthKeyDuplicated:
        fatal("AuthKeyDuplicated — эта сессия уже запущена где-то ещё.")
    finally:
        ka_task.cancel()
        with asyncio.DummyContextManager():
            await cli.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("⏹️ exit")