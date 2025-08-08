#!/usr/bin/env python3
# GiftSniper — VPS hardening edition (no night sleep)
# - keepalive + dynamic watchdog reconnect
# - hourly heartbeat & daily report (stdout + optional DM)
# - reconnect/error notifications to DM (configurable)
# - check / check-all modes

from __future__ import annotations
import argparse, asyncio, json, os, random, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import (
    AuthKeyDuplicated, BadRequest, FloodWait, InternalServerError, PeerFlood
)

try:
    # raw ping (PyroFork/Pyrogram 2.x)
    from pyrogram.raw.functions import Ping
except Exception:
    Ping = None

# ───── base config ─────
VERBOSE             = True
NO_NEW_EVERY_SEC    = 60
KEEPALIVE_PERIOD    = 90           # seconds
STORAGE             = Path("gifts.json")

# ───── MSK time ─────
def msk_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Moscow")
    except Exception:
        return timezone(timedelta(hours=3))

MSK = msk_tz()
now = lambda: datetime.now(MSK)

def tstamp() -> str:
    return now().strftime("%d/%m/%y %H:%M:%S")

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

# ---- DM notifications (optional)
_notify_raw = (os.getenv("NOTIFY_CHAT_ID") or "").strip()
if _notify_raw.lower() == "me":
    NOTIFY_CHAT_ID: Optional[object] = "me"
elif _notify_raw.isdigit():
    NOTIFY_CHAT_ID = int(_notify_raw)
else:
    NOTIFY_CHAT_ID = None

NOTIFY_HOURLY     = os.getenv("NOTIFY_HOURLY", "true").lower() == "true"
NOTIFY_DAILY      = os.getenv("NOTIFY_DAILY",  "true").lower() == "true"
NOTIFY_RECONNECT  = os.getenv("NOTIFY_RECONNECT", "true").lower() == "true"
NOTIFY_ERRORS     = os.getenv("NOTIFY_ERRORS", "true").lower() == "true"
# не спамим каждую строку в ЛС
DM_MIN_INTERVAL_S = int(os.getenv("DM_MIN_INTERVAL_S", "300"))  # 5 мин

# dynamic watchdog: 2 missed keepalives OR 3 missed polls (whichever is longer) + slack
WATCHDOG_PERIOD = max(2*KEEPALIVE_PERIOD + 15, 3*POLL_MAX + 20)

def fatal(msg): print(f"{tstamp()} [FATAL] {msg}"); sys.exit(1)
if not (API_ID and API_HASH): fatal("TG_API_ID / TG_API_HASH пусты")
if not SESSION and not Path("TgAccount.session").exists(): fatal("нужен TG_SESSION или файл TgAccount.session")
if BUY_GIFT and not ID_TO_BUY: fatal("ID_TO_BUY обязателен при BUY_GIFT=true")

# ───── logging helpers ─────
def log_info(msg: str):  print(f"{tstamp()} [INFO] {msg}")
def log_warn(msg: str):  print(f"{tstamp()} [WARN] {msg}")
def log_err(msg: str):   print(f"{tstamp()} [ERR]  {msg}")
def log_beat(msg: str):  print(f"{tstamp()} [HEALTH] {msg}")
def log_user(msg: str):  print(f"{tstamp()} {msg}")

# ───── health/metrics ─────
class Health:
    def __init__(self):
        self.reset_all()

    def reset_all(self):
        self.rpc_ok = 0
        self.rpc_fail = 0
        self.reconnects = 0
        self.reconnect_fail = 0
        self.floodwaits = 0
        self.buys_attempted = 0
        self.buys_success = 0
        self.soldout = 0
        self.balance_low = 0
        self.badrequest_other = 0
        self.conn_lost = 0
        self.last_ok = now()
        self.last_hour = now().replace(minute=0, second=0, microsecond=0)
        # daily report at 03:05 MSK
        base = now().replace(hour=3, minute=5, second=0, microsecond=0)
        self.next_daily = base + (timedelta(days=1) if now() >= base else timedelta())
        self._last_dm = None

    def touch_ok(self):
        self.rpc_ok += 1
        self.last_ok = now()

    def touch_fail(self, conn=False):
        self.rpc_fail += 1
        if conn: self.conn_lost += 1

    def inc_reconnect(self, ok: bool):
        if ok: self.reconnects += 1
        else:  self.reconnect_fail += 1

    def inc_buy_try(self): self.buys_attempted += 1
    def inc_buy_ok(self):  self.buys_success += 1
    def inc_fw(self):      self.floodwaits += 1
    def inc_soldout(self): self.soldout += 1
    def inc_balance(self): self.balance_low += 1
    def inc_badreq(self):  self.badrequest_other += 1

    def hour_beat_needed(self) -> bool:
        return NOTIFY_HOURLY and now() >= self.last_hour + timedelta(hours=1)

    def daily_needed(self) -> bool:
        return NOTIFY_DAILY and now() >= self.next_daily

    def hour_beat(self) -> str:
        self.last_hour = now().replace(minute=0, second=0, microsecond=0)
        return (f"ok={self.rpc_ok} fail={self.rpc_fail} reconnects={self.reconnects}"
                f" fw={self.floodwaits} buys={self.buys_success}/{self.buys_attempted}"
                f" last_ok={(now()-self.last_ok).seconds}s ago")

    def daily_report(self) -> str:
        msg = (
            "Daily report:\n"
            f"  RPC ok/fail:     {self.rpc_ok}/{self.rpc_fail}\n"
            f"  Reconnects:      {self.reconnects} (fail {self.reconnect_fail})\n"
            f"  FloodWait:       {self.floodwaits}\n"
            f"  Buys:            {self.buys_success}/{self.buys_attempted}\n"
            f"  Sold out hits:   {self.soldout}\n"
            f"  Balance low:     {self.balance_low}\n"
            f"  BadRequest misc: {self.badrequest_other}\n"
            f"  Conn lost:       {self.conn_lost}\n"
            f"  Last OK age:     {(now()-self.last_ok).seconds}s\n"
        )
        # next report and reset counters
        base = now().replace(hour=3, minute=5, second=0, microsecond=0)
        self.next_daily = base + timedelta(days=1)
        self.reset_all()
        return msg

HEALTH = Health()
RECONNECT_TRIES, RECONNECT_PAUSE = 5, 3
RECONN_LOCK = asyncio.Lock()

# ───── DM notifications ─────
async def maybe_notify(cli: Client, text: str, force=False):
    if NOTIFY_CHAT_ID is None:
        return
    # throttle DM unless forced
    if not force and HEALTH._last_dm and (now() - HEALTH._last_dm).seconds < DM_MIN_INTERVAL_S:
        return
    try:
        await cli.send_message(NOTIFY_CHAT_ID, text)
        HEALTH._last_dm = now()
    except Exception as e:
        log_warn(f"notify failed: {e}")

# ───── sniper ─────
class GiftSniper:
    def __init__(self, cli: Client):
        self.u = cli
        self.seen: set[int] = set()
        if STORAGE.exists():
            try:
                data = json.loads(STORAGE.read_text())
                self.seen |= {int(x if not isinstance(x, dict) else x.get("id")) for x in data}
            except Exception as e:
                log_warn(f"gifts.json: {e}")
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
        try:
            raw = await self.u.get_available_gifts()
            HEALTH.touch_ok()
        except InternalServerError:
            await asyncio.sleep(1)
            try:
                raw = await self.u.get_available_gifts()
                HEALTH.touch_ok()
            except Exception as e:
                HEALTH.touch_fail(conn=True)
                raise e
        except Exception as e:
            HEALTH.touch_fail(conn=True)
            raise e

        gifts = [self._norm(x) for x in (raw or [])]
        gifts.sort(key=lambda x: x["supply"] if x["supply"] is not None else float("inf"))
        return gifts

    def _eligible(self, g: Dict[str, Any]) -> bool:
        return (g["price"] is not None and P_FROM <= g["price"] <= P_TO and
                g["supply"] is not None and S_FROM <= g["supply"] <= S_TO)

    async def _buy(self, g: Dict[str, Any], max_q: int) -> int:
        left, bought = max_q, 0
        while left:
            HEALTH.inc_buy_try()
            try:
                await self.u.send_gift(ID_TO_BUY, g["id"], False)
                HEALTH.touch_ok()
                HEALTH.inc_buy_ok()
                bought += 1; left -= 1
                await asyncio.sleep(random.uniform(0.7, 1.5))
            except FloodWait as fw:
                HEALTH.inc_fw()
                log_warn(f"FloodWait {fw.value}s")
                await asyncio.sleep(fw.value)
            except BadRequest as br:
                msg = str(br)
                if "STARGIFT_USAGE_LIMITED" in msg:
                    HEALTH.inc_soldout()
                    log_info(f"{g['title']}: sold out")
                elif "BALANCE_TOO_LOW" in msg:
                    HEALTH.inc_balance()
                    log_err("BALANCE_TOO_LOW — не хватает ⭐")
                else:
                    HEALTH.inc_badreq()
                    log_err(f"BadRequest: {msg}")
                break
            except PeerFlood as pf:
                HEALTH.inc_badreq()
                log_err(f"PeerFlood: {pf}")
                break
            except Exception as e:
                HEALTH.touch_fail(conn=True)
                raise e

        if bought:
            log_info(f"[BUY] {g['title']} ×{bought}")
            if NOTIFY_CHAT_ID:
                await maybe_notify(self.u, f"✅ Купили: {g['title']} ×{bought}", force=True)
        return bought

    async def tick(self, only_new: bool=False) -> Tuple[bool, bool]:
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
                try:
                    if await self._buy(g, max_q):
                        bought = True
                except Exception:
                    raise  # сетевой краш отдаём наверх

        if new:
            try:
                STORAGE.write_text(json.dumps(sorted(self.seen), ensure_ascii=False, indent=2))
            except Exception as e:
                log_warn(f"save gifts.json: {e}")

        if VERBOSE:
            clock = now().strftime("%H:%M:%S")
            if bought:
                log_user(f"[{clock}] ✅ что-то купили")
            else:
                if not self._last_no_new or (now() - self._last_no_new).seconds >= NO_NEW_EVERY_SEC:
                    log_user(f"[{clock}] — новинок нет")
                    self._last_no_new = now()

        return bought, new

# ───── keepalive & reconnect ─────
async def keepalive(cli: Client):
    while True:
        try:
            if Ping is not None:
                await cli.invoke(Ping(ping_id=random.randint(1, 1 << 31)))
                HEALTH.touch_ok()
            else:
                # fallback: дешёвый RPC
                await cli.get_me()
                HEALTH.touch_ok()
        except Exception as e:
            log_warn(f"keepalive: {e}")
            HEALTH.touch_fail(conn=True)
        await asyncio.sleep(KEEPALIVE_PERIOD)

async def do_reconnect(cli: Client, reason: str) -> bool:
    async with RECONN_LOCK:
        for n in range(1, RECONNECT_TRIES + 1):
            try:
                await cli.stop(); await asyncio.sleep(1); await cli.start()
                HEALTH.inc_reconnect(True)
                msg = f"Reconnect OK #{n} ({reason})"
                log_info(msg)
                if NOTIFY_CHAT_ID and NOTIFY_RECONNECT:
                    await maybe_notify(cli, f"♻️ {msg}", force=True)
                return True
            except Exception as e:
                HEALTH.inc_reconnect(False)
                log_warn(f"Reconnect #{n} failed ({reason}): {e}")
                await asyncio.sleep(RECONNECT_PAUSE)
        log_err(f"Reconnect failed after all tries ({reason})")
        if NOTIFY_CHAT_ID and NOTIFY_ERRORS:
            await maybe_notify(cli, "❌ Reconnect failed after all tries", force=True)
        return False

# ───── main ─────
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="однократная проверка новых")
    ap.add_argument("--check-all", action="store_true", help="однократная проверка всех")
    args = ap.parse_args()

    cli = Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=SESSION) \
          if SESSION else Client("TgAccount", api_id=API_ID, api_hash=API_HASH)
    sniper = GiftSniper(cli)

    try:
        await cli.start()

        if args.check or args.check_all:
            await sniper.tick(only_new=not args.check_all)
            return

        # optional init dump
        if os.getenv("INIT_DUMP_ALL", "true").lower() == "true":
            try:
                gifts = await sniper._fetch()
                log_info(f"Initial dump: {len(gifts)} gifts")
                for g in gifts:
                    print(f"  {g['title']} | {g['price']}⭐ | left: {g['supply']}")
                log_info("init-dump done")
            except Exception as e:
                log_warn(f"init dump failed: {e}")

        ka_task = asyncio.create_task(keepalive(cli))

        while True:
            # hourly heartbeat
            if HEALTH.hour_beat_needed():
                beat = HEALTH.hour_beat()
                log_beat(beat)
                if NOTIFY_CHAT_ID and NOTIFY_HOURLY:
                    await maybe_notify(cli, f"Heartbeat: {beat}")

            # daily report
            if HEALTH.daily_needed():
                report = HEALTH.daily_report()
                for line in report.splitlines():
                    if line:
                        log_beat(line)
                if NOTIFY_CHAT_ID and NOTIFY_DAILY:
                    await maybe_notify(cli, report, force=True)

            # watchdog
            if (now() - HEALTH.last_ok).seconds >= WATCHDOG_PERIOD:
                reason = f"watchdog stale {WATCHDOG_PERIOD}s"
                log_warn(f"{reason}; reconnecting…")
                if await do_reconnect(cli, reason):
                    try: ka_task.cancel()
                    except Exception: pass
                    ka_task = asyncio.create_task(keepalive(cli))

            try:
                await sniper.tick()
            except (OSError, ConnectionError) as e:
                log_err(f"connection: {e}")
                if await do_reconnect(cli, "loop connection error"):
                    try: ka_task.cancel()
                    except Exception: pass
                    ka_task = asyncio.create_task(keepalive(cli))
            except Exception as e:
                log_err(f"loop error: {e}")
                if await do_reconnect(cli, "loop generic error"):
                    try: ka_task.cancel()
                    except Exception: pass
                    ka_task = asyncio.create_task(keepalive(cli))

            await asyncio.sleep(random.randint(POLL_MIN, POLL_MAX))

    except AuthKeyDuplicated:
        fatal("AuthKeyDuplicated — та же сессия активна где-то ещё.")
    finally:
        try:
            await cli.stop()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print(f"{tstamp()} ⏹️ exit")
