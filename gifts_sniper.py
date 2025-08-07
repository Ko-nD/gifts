#!/usr/bin/env python
# GiftSniper 0.9.8 — night-sleep старт 02 :30-03 :30 MSK, длит. 3-7 h

from __future__ import annotations
import argparse, asyncio, json, os, random, sys, signal
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, time, date
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pyrogram import Client                       # Pyrogram 2.0+  / PyroFork-2.3.x
from pyrogram.raw.functions import Ping           # одинаково для обеих веток
from pyrogram.errors import (AuthKeyDuplicated, BadRequest, FloodWait,
                             InternalServerError, PeerFlood)

# ─────────── «говорливость» и тайминги ───────────
VERBOSE              = True
NO_NEW_EVERY_SEC     = 3600              # «новинок нет» не чаще, с
KEEPALIVE_PERIOD     = 90              # MT-Ping период, с
RECONNECT_TRIES      = 3
RECONNECT_PAUSE      = 3               # между попытками, с

# окно начала сна (MSK)
SLEEP_START_FROM     = time(2, 30)
SLEEP_START_TO       = time(3, 30)
SLEEP_LEN_H_MIN      = 3               # спим 3-7 ч
SLEEP_LEN_H_MAX      = 7

# ── DummyContextManager (для Py 3.13) ──
if not hasattr(asyncio, "DummyContextManager"):
    @asynccontextmanager
    async def _dummy(): yield
    asyncio.DummyContextManager = _dummy     # type: ignore[attr-defined]

# ─────────── чтение .env ───────────
load_dotenv()
API_ID      = int(os.getenv("TG_API_ID", 0))
API_HASH    = os.getenv("TG_API_HASH", "")
SESSION     = os.getenv("TG_SESSION") or None
ID_TO_BUY   = int(os.getenv("ID_TO_BUY", 0))
BUY_GIFT    = os.getenv("BUY_GIFT", "false").lower() == "true"

P_FROM, P_TO   = int(os.getenv("PRICE_LIMIT_FROM", 500)),  int(os.getenv("PRICE_LIMIT_TO", 50_000))
S_FROM, S_TO   = int(os.getenv("SUPPLY_LIMIT_FROM", 1)),   int(os.getenv("SUPPLY_LIMIT_TO", 60_000))
POLL_MIN, POLL_MAX = int(os.getenv("POLL_INTERVAL_FROM", 8)), int(os.getenv("POLL_INTERVAL_TO", 15))

STORAGE = Path("gifts.json")

def fatal(msg: str):
    print("[FATAL]", msg)
    sys.exit(1)

if not (API_ID and API_HASH):
    fatal("TG_API_ID / TG_API_HASH отсутствуют в .env")
if not SESSION and not Path("TgAccount.session").exists():
    fatal("нужен TG_SESSION или *.session")
if BUY_GIFT and not ID_TO_BUY:
    fatal("ID_TO_BUY обязателен при BUY_GIFT=true")

# ═════════════════ GiftSniper ══════════════════
class GiftSniper:
    def __init__(self, cli: Client):
        self.u = cli
        self.seen: set[int] = set()
        if STORAGE.exists():
            try:
                self.seen |= {int(i) for i in json.loads(STORAGE.read_text())}
            except Exception as e:
                print("[WARN] gifts.json:", e)
        self._last_no_new: Optional[datetime] = None

    # ── нормализация подарка ──
    @staticmethod
    def _norm(g: Any) -> Dict[str, Any]:
        keys = ("supply", "total_count", "total_amount", "amount")
        if isinstance(g, dict):
            sup   = next((g[k] for k in keys if g.get(k) is not None), None)
            price = g.get("price") or g.get("star_count")
            emoji = (g.get("sticker") or {}).get("emoji")
            emoji = None if emoji == "🎁" else emoji
            return dict(id=g["id"], title=emoji or f"ID-{g['id']}",
                        price=price, supply=sup,
                        is_limited=g.get("is_limited", sup is not None))
        sup   = next((getattr(g, k, None) for k in keys if getattr(g, k, None) is not None), None)
        price = getattr(g, "price", getattr(g, "star_count", None))
        emoji = getattr(getattr(g, "sticker", None), "emoji", None)
        emoji = None if emoji == "🎁" else emoji
        return dict(id=g.id, title=emoji or f"ID-{g.id}",
                    price=price, supply=sup,
                    is_limited=getattr(g, "is_limited", sup is not None))

    async def _fetch(self) -> List[Dict[str, Any]]:
        try:
            raw = await self.u.get_available_gifts()
        except InternalServerError:
            await asyncio.sleep(1)
            raw = await self.u.get_available_gifts()
        gifts = [self._norm(x) for x in raw or []]
        gifts.sort(key=lambda x: x["supply"] if x["supply"] is not None else float("inf"))
        return gifts

    def _eligible(self, g):
        return g["price"] and P_FROM <= g["price"] <= P_TO and \
               g["supply"] and S_FROM <= g["supply"] <= S_TO

    async def _buy(self, g, max_q):
        left = max_q
        bought = 0
        while left:
            try:
                await self.u.send_gift(ID_TO_BUY, g["id"], False)
                left -= 1; bought += 1
                await asyncio.sleep(random.uniform(.7, 1.5))
            except FloodWait as fw:
                print("[FW]", fw.value, "s"); await asyncio.sleep(fw.value)
            except (BadRequest, PeerFlood) as e:
                print("[ERR] buy:", e); break
            except Exception as e:
                raise e
        if bought:
            print(f"[BUY] {g['title']} ×{bought}")
        return bought

    async def tick(self, list_all: bool = False):
        gifts = await self._fetch()
        rare_i = 0
        bought = False
        new    = False

        for g in gifts:
            if not list_all and g["id"] in self.seen:
                continue
            if g["id"] not in self.seen:
                self.seen.add(g["id"]); new = True

            max_q = 0
            if g["is_limited"]:
                if rare_i == 0:   max_q = 10
                elif rare_i == 1: max_q = 25
                rare_i += 1

            if max_q and self._eligible(g) and BUY_GIFT:
                if await self._buy(g, max_q):
                    bought = True

            if list_all:   # при --check-all печатаем всё сразу
                print(f"{g['title']} | {g['price']}⭐ | остаток: {g['supply']} | limited: {g['is_limited']}")

        if new:
            STORAGE.write_text(json.dumps(sorted(self.seen), ensure_ascii=False, indent=2))

        if VERBOSE and not list_all:
            now = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%H:%M:%S")
            if bought:
                print(f"[{now}] ✅ купили")
                self._last_no_new = datetime.now(ZoneInfo("Europe/Moscow"))
            elif (self._last_no_new is None or
                  (datetime.now(ZoneInfo("Europe/Moscow")) - self._last_no_new).seconds >= NO_NEW_EVERY_SEC):
                print(f"[{now}] — новинок нет")
                self._last_no_new = datetime.now(ZoneInfo("Europe/Moscow"))

# ═══════════ вспомогательные корутины ════════════
async def keepalive(cli: Client, stop_evt: asyncio.Event):
    while not stop_evt.is_set():
        if cli.is_connected:
            try:
                await cli.invoke(Ping(ping_id=random.randint(1, 1 << 31)))
            except Exception as e:
                print("[WARN] keepalive:", e)
        await asyncio.sleep(KEEPALIVE_PERIOD)

async def reconnect(cli: Client, stop_evt: asyncio.Event):
    """возвращает True, если удалось восстановить соединение"""
    stop_evt.set()          # останавливаем текущий keepalive
    await asyncio.sleep(0)  # даём ему шанс выйти
    for n in range(1, RECONNECT_TRIES + 1):
        try:
            await cli.stop(); await asyncio.sleep(1)
            await cli.start()
            print(f"[INFO] reconnect ok #{n}")
            return True
        except Exception as e:
            print(f"[WARN] reconnect #{n} fail:", e)
            await asyncio.sleep(RECONNECT_PAUSE)
    return False

def next_sleep_start() -> datetime:
    today = date.today()
    base  = datetime.combine(today, time(3, 0), ZoneInfo("Europe/Moscow"))
    delta = random.randint(-30, +30)          # ±30 мин
    return base + timedelta(minutes=delta)

# ═════════════════════════════ main ═════════════════════════════
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check",      action="store_true", help="показать только новые подарки и выйти")
    ap.add_argument("--check-all",  action="store_true", help="показать ВСЕ доступные подарки и выйти")
    args = ap.parse_args()

    cli = (Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=SESSION)
           if SESSION else Client("TgAccount", api_id=API_ID, api_hash=API_HASH))
    sniper = GiftSniper(cli)

    try:
        await cli.start()

        # Режимы однократной проверки
        if args.check or args.check_all:
            await sniper.tick(list_all=args.check_all)
            return

        # keepalive-таска управляется через Event
        stop_keep = asyncio.Event()
        ka_task   = asyncio.create_task(keepalive(cli, stop_keep))

        next_sleep = next_sleep_start()
        if datetime.now(ZoneInfo("Europe/Moscow")) >= next_sleep:
            next_sleep += timedelta(days=1)

        while True:
            now = datetime.now(ZoneInfo("Europe/Moscow"))
            if now >= next_sleep:
                dur_h = random.uniform(SLEEP_LEN_H_MIN, SLEEP_LEN_H_MAX)
                h, m = int(dur_h), int((dur_h - int(dur_h)) * 60)
                print(f"😴 сон {h}ч{m:02d}м")
                await asyncio.sleep(int(dur_h * 3600))
                print("🌅 проснулись")
                next_sleep = next_sleep_start() + timedelta(days=1)
                continue

            try:
                await sniper.tick()
                await asyncio.sleep(random.randint(POLL_MIN, POLL_MAX))
            except (OSError, ConnectionError) as e:
                print("[ERR] connection:", e)
                if await reconnect(cli, stop_keep):
                    # перезапускаем новый keepalive
                    stop_keep = asyncio.Event()
                    ka_task   = asyncio.create_task(keepalive(cli, stop_keep))
                    continue
                else:
                    break
            except Exception as e:
                print("[ERR] main:", e)

    except AuthKeyDuplicated:
        fatal("AuthKeyDuplicated — та же сессия уже активна.")
    finally:
        stop_keep.set()
        await asyncio.sleep(0)      # даём keepalive корректно завершиться
        await cli.stop()

# graceful Ctrl-C в Windows/MINGW
def _sigint_handler(sig, frame):
    raise KeyboardInterrupt
signal.signal(signal.SIGINT, _sigint_handler)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("⏹️ exit")
