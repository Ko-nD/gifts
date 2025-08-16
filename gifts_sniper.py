#!/usr/bin/env python3
# GiftSniper — VPS edition (burst 59..05, premium-aware, per-user caps, stars-balance safe-buy, robust reconnect)
# + instant "new gifts" notifications with stars balance in all logs

from __future__ import annotations
import argparse, asyncio, json, os, random, sys, ssl, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone, date, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import AuthKeyDuplicated, BadRequest, FloodWait, InternalServerError, PeerFlood

try:
    from pyrogram.raw.functions import Ping
except Exception:
    Ping = None

# ── базовые настройки ──
VERBOSE             = True
NO_NEW_EVERY_SEC    = 60        # «новинок нет» — не чаще
KEEPALIVE_PERIOD    = 90        # сек
RECONNECT_TRIES     = 5
RECONNECT_PAUSE     = 3         # сек
SAFE_NOTIFY_TIMEOUT = float(os.getenv("NOTIFY_TIMEOUT", "2.5"))

STORAGE       = Path("gifts.json")
STARS_STATE   = Path("stars_state.json")

# ── время (MSK) ──
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

# ── окружение ──
load_dotenv()
API_ID   = int(os.getenv("TG_API_ID", 0))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION  = os.getenv("TG_SESSION") or None

ID_TO_BUY = int(os.getenv("ID_TO_BUY", 0))
BUY_GIFT  = os.getenv("BUY_GIFT", "false").lower() == "true"

# Фильтры
P_FROM, P_TO = int(os.getenv("PRICE_LIMIT_FROM", 500)),  int(os.getenv("PRICE_LIMIT_TO", 50_000))
S_FROM, S_TO = int(os.getenv("SUPPLY_LIMIT_FROM", 1)),   int(os.getenv("SUPPLY_LIMIT_TO", 60_000))

# Обычный мониторинг: ~10 секунд по умолчанию
POLL_MIN = float(os.getenv("POLL_INTERVAL_FROM", "9.5"))
POLL_MAX = float(os.getenv("POLL_INTERVAL_TO",   "11.5"))

# Бурст-режим
BURST_EACH_HOUR   = (os.getenv("BURST_EACH_HOUR", "true").lower() in ("1","true","yes","on"))   # 59..05
BURST_WINDOWS     = (os.getenv("BURST_WINDOWS") or "").replace(" ", "")                         # "00:00-00:06,12:59-13:05"
BURST_POLL_MIN    = float(os.getenv("BURST_POLL_MIN", "1.0"))
BURST_POLL_MAX    = float(os.getenv("BURST_POLL_MAX", "1.5"))
BURST_PREWARM_SEC = int(os.getenv("BURST_PREWARM_SEC", "45"))
BURST_FORCE_MINUTES = int(os.getenv("BURST_FORCE_MINUTES", "0"))
BURST_ONLY        = (os.getenv("BURST_ONLY", "false").lower() in ("1","true","yes","on"))
BURST_NOTIFY      = (os.getenv("BURST_NOTIFY", "true").lower() in ("1","true","yes","on"))

# Премиум и новые лимиты
HAS_PREMIUM_DEFAULT = os.getenv("HAS_PREMIUM", "true").lower() in ("1","true","yes","on")
ONLY_PREMIUM        = os.getenv("ONLY_PREMIUM", "false").lower() in ("1","true","yes","on")
CAP_FIRST_RARE      = int(os.getenv("CAP_FIRST_RARE", "10"))
CAP_SECOND_RARE     = int(os.getenv("CAP_SECOND_RARE", "25"))

# Баланс звёзд
STARS_REFRESH_HOURS = int(os.getenv("STARS_REFRESH_HOURS", "24"))

# Уведомления (опционально; не влияют на покупки)
BOT_TOKEN       = (os.getenv("BOT_TOKEN") or "").strip()
NOTIFY_CHAT_ID  = (os.getenv("NOTIFY_CHAT_ID") or "").strip()
def to_bool(s: Optional[str], default=True) -> bool:
    if s is None: return default
    return s.strip().lower() in ("1","true","yes","on")
NOTIFY_ENABLED     = to_bool(os.getenv("NOTIFY_ENABLED"), True)
NOTIFY_HOURLY      = to_bool(os.getenv("NOTIFY_HOURLY"),  True)
NOTIFY_DAILY       = to_bool(os.getenv("NOTIFY_DAILY"),   True)
NOTIFY_RECONNECT   = to_bool(os.getenv("NOTIFY_RECONNECT"), False)
NOTIFY_ERRORS      = to_bool(os.getenv("NOTIFY_ERRORS"),  True)
FAIL_ALERT_MIN_ITV = int(os.getenv("NOTIFY_RECONNECT_MIN_INTERVAL", "1800"))
DEBUG_FETCH_TIMES  = to_bool(os.getenv("DEBUG_FETCH_TIMES"), False)

# Новые уведомления:
NOTIFY_NEW_GIFTS      = to_bool(os.getenv("NOTIFY_NEW_GIFTS"), True)     # присылать сразу при появлении
NEW_NOTIFY_MAX_LINES  = int(os.getenv("NEW_NOTIFY_MAX_LINES", "20"))     # макс строк в одном сообщении

def fatal(msg): print("[FATAL]", msg); sys.exit(1)
if not (API_ID and API_HASH): fatal("TG_API_ID / TG_API_HASH пусты")
if not SESSION and not Path("TgAccount.session").exists(): fatal("нужен TG_SESSION или файл TgAccount.session")
if BUY_GIFT and not ID_TO_BUY: fatal("ID_TO_BUY обязателен при BUY_GIFT=true")

def compute_watchdog_period() -> int:
    v = os.getenv("WATCHDOG_PERIOD")
    if v:
        try:
            n = int(v)
            if n > 0: return n
        except Exception:
            pass
    return max(420, int(POLL_MAX) * 6, KEEPALIVE_PERIOD * 3)
WATCHDOG_PERIOD = compute_watchdog_period()

# ── парсинг BURST_WINDOWS ──
def parse_windows(spec: str) -> List[Tuple[time,time]]:
    out: List[Tuple[time,time]] = []
    if not spec: return out
    for block in spec.split(","):
        if "-" not in block: continue
        a,b = block.split("-",1)
        try:
            ha,ma = map(int, a.split(":"))
            hb,mb = map(int, b.split(":"))
            out.append((time(ha,ma), time(hb,mb)))
        except Exception:
            continue
    return out
BURST_PARSED = parse_windows(BURST_WINDOWS)

def within_windows(dt: datetime, wins: List[Tuple[time,time]]) -> bool:
    if not wins: return False
    t = dt.time()
    for a,b in wins:
        if a <= b:
            if a <= t <= b: return True
        else:
            if t >= a or t <= b: return True
    return False

def in_hourly_burst(dt: datetime) -> bool:
    if not BURST_EACH_HOUR: return False
    m = dt.minute
    return (m >= 59) or (m <= 5)

def seconds_to_next_burst(dt: datetime) -> Optional[int]:
    candidates: List[datetime] = []
    if BURST_EACH_HOUR:
        base = dt.replace(second=0, microsecond=0)
        h59_this = base.replace(minute=59)
        if h59_this <= dt:
            h59_this = h59_this + timedelta(hours=1)
        candidates.append(h59_this)
    today = dt.date()
    for a,_ in BURST_PARSED:
        candidates.append(datetime.combine(today, a, MSK))
        candidates.append(datetime.combine(today + timedelta(days=1), a, MSK))
    future = [c for c in candidates if c > dt]
    if not future: return None
    delta = min((c - dt for c in future), key=lambda d: d.total_seconds())
    return max(0, int(delta.total_seconds()))

# ── уведомлялка ──
class Notifier:
    def __init__(self, bot_token: str, chat_id: str, cli: Client):
        self.bot_token = bot_token or ""
        self.chat_id   = chat_id or ""
        self.cli       = cli
        self.http_timeout = SAFE_NOTIFY_TIMEOUT
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = True

    async def send(self, text: str) -> bool:
        if not NOTIFY_ENABLED: return True
        text = (text or "").strip()
        if not text: return True
        # Bot API
        if self.bot_token and self.chat_id:
            ok = await asyncio.to_thread(self._send_botapi_blocking, text)
            if ok: return True
        # MTProto (юзер)
        if self.chat_id:
            try:
                try:    await self.cli.send_message(int(self.chat_id), text, disable_web_page_preview=True)
                except ValueError:
                        await self.cli.send_message(self.chat_id, text, disable_web_page_preview=True)
                return True
            except Exception as e:
                if VERBOSE: print("[WARN] notify MTProto failed:", e)
        return False

    def _send_botapi_blocking(self, text: str) -> bool:
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text, "disable_web_page_preview":"true"}).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=self.http_timeout, context=self.ctx) as r:
                return r.status == 200
        except Exception:
            return False

def fire_and_forget(coro: asyncio.Future) -> None:
    async def _wrap():
        try:    await asyncio.wait_for(coro, timeout=SAFE_NOTIFY_TIMEOUT + 0.5)
        except Exception: pass
    asyncio.create_task(_wrap())

# ── учёт баланса звёзд ──
class BalanceManager:
    def __init__(self, cli: Client):
        self.cli = cli
        self.balance: Optional[int] = None
        self.last_fetch: Optional[datetime] = None
        self.spent_today: int = 0
        self.spent_total: int = 0
        self._day = now().date()
        self._load()

    def _load(self):
        if STARS_STATE.exists():
            try:
                d = json.loads(STARS_STATE.read_text())
                self.balance = d.get("balance")
                ts = d.get("last_fetch")
                if ts: self.last_fetch = datetime.fromisoformat(ts)
                self.spent_today = int(d.get("spent_today", 0))
                self.spent_total = int(d.get("spent_total", 0))
                day = d.get("day")
                if day:
                    try: self._day = date.fromisoformat(day)
                    except Exception: pass
            except Exception as e:
                print("[WARN] stars_state load:", e)

    def _save(self):
        try:
            data = {
                "balance": self.balance,
                "last_fetch": self.last_fetch.isoformat() if self.last_fetch else None,
                "spent_today": self.spent_today,
                "spent_total": self.spent_total,
                "day": self._day.isoformat()
            }
            STARS_STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            print("[WARN] stars_state save:", e)

    def ensure_day_rollover(self):
        if now().date() != self._day:
            self._day = now().date()
            self.spent_today = 0
            self._save()

    async def fetch(self) -> Optional[int]:
        """Пробуем достать баланс через raw payments.GetStarsStatus()."""
        try:
            from pyrogram.raw.functions.payments import GetStarsStatus
            res = await self.cli.invoke(GetStarsStatus())
            val = None
            for k in ("balance", "stars", "amount", "available_balance", "total"):
                v = getattr(res, k, None)
                if isinstance(v, int):
                    val = v
                    break
            if val is not None:
                self.balance = val
                self.last_fetch = now()
                self._save()
                return val
        except Exception as e:
            print("[WARN] fetch stars failed:", e)
        return None

    async def refresh_if_due(self, hours: int) -> Optional[int]:
        if self.balance is None or self.last_fetch is None:
            return await self.fetch()
        if (now() - self.last_fetch).total_seconds() >= hours * 3600:
            return await self.fetch()
        return self.balance

    def affordable_qty(self, price: Optional[int], max_want: int) -> int:
        if price is None or price <= 0:
            return max_want
        if self.balance is None:
            return max_want
        return max(0, min(max_want, self.balance // price))

    def deduct(self, price: int, qty: int = 1):
        if self.balance is not None:
            self.balance = max(0, self.balance - price * qty)
        self.spent_today += price * qty
        self.spent_total += price * qty
        self._save()

    def mark_insufficient(self):
        self.balance = 0
        self._save()

# ── снайпер ──
def _gift_line(g: Dict[str, Any], you_cap: Optional[int]=None) -> str:
    title = g.get("title") or f"ID-{g.get('id')}"
    price = g.get("price")
    supply= g.get("supply")
    prem  = "P" if g.get("require_premium") else "-"
    cap_s = f" cap:{you_cap}" if you_cap is not None else ""
    return f"• {title} | {price}⭐ | left:{supply} | prem:{prem}{cap_s}"

class GiftSniper:
    def __init__(self, cli: Client, has_premium: bool, bal: BalanceManager):
        self.u = cli
        self.has_premium = has_premium
        self.bal = bal
        self.seen: set[int] = set()
        if STORAGE.exists():
            try:
                data = json.loads(STORAGE.read_text())
                self.seen |= {int(x if not isinstance(x, dict) else x.get("id")) for x in data}
            except Exception as e:
                print("[WARN] gifts.json:", e)
        self._last_no_new: Optional[datetime] = None
        self.fetch_ok_total: int = 0
        self.fetch_ok_hour:  int = 0

    def reset_hour_metrics(self): self.fetch_ok_hour = 0
    def snapshot_fetch_metrics(self) -> Tuple[int, int]: return self.fetch_ok_total, self.fetch_ok_hour

    @staticmethod
    def _norm(g: Any) -> Dict[str, Any]:
        SUP_KEYS = ("supply","total_count","total_amount","amount")
        if isinstance(g, dict):
            sup = next((g.get(k) for k in SUP_KEYS if g.get(k) is not None), None)
            price = g.get("price") or g.get("star_count")
            emoji = (g.get("sticker") or {}).get("emoji");  emoji = None if emoji=="🎁" else emoji
            return dict(id=g.get("id"), title=emoji or f"ID-{g.get('id')}", price=price, supply=sup,
                        is_limited=g.get("is_limited", sup is not None),
                        per_user_remains=g.get("per_user_remains"),
                        limited_per_user=g.get("limited_per_user"),
                        per_user_total=g.get("per_user_total"),
                        require_premium=g.get("require_premium", False))
        sup   = next((getattr(g,k,None) for k in SUP_KEYS if getattr(g,k,None) is not None), None)
        price = getattr(g,"price", getattr(g,"star_count", None))
        emoji = getattr(getattr(g,"sticker", None),"emoji", None);  emoji = None if emoji=="🎁" else emoji
        return dict(id=getattr(g,"id", None), title=emoji or f"ID-{getattr(g,'id',None)}", price=price, supply=sup,
                    is_limited=getattr(g,"is_limited", sup is not None),
                    per_user_remains=getattr(g,"per_user_remains", None),
                    limited_per_user=getattr(g,"limited_per_user", None),
                    per_user_total=getattr(g,"per_user_total", None),
                    require_premium=getattr(g,"require_premium", False))

    async def _fetch(self) -> List[Dict[str, Any]]:
        try:
            raw = await self.u.get_available_gifts()
        except InternalServerError:
            await asyncio.sleep(1)
            raw = await self.u.get_available_gifts()
        self.fetch_ok_total += 1
        self.fetch_ok_hour  += 1
        if DEBUG_FETCH_TIMES:
            print(f"[{fmt(now())}] fetch_ok")
        gifts = [self._norm(x) for x in (raw or [])]
        gifts.sort(key=lambda x: x["supply"] if x["supply"] is not None else float("inf"))
        return gifts

    def _eligible_by_filters(self, g: Dict[str, Any]) -> bool:
        return (g["price"] is not None and P_FROM <= g["price"] <= P_TO and
                g["supply"] is not None and S_FROM <= g["supply"] <= S_TO)

    def _user_cap(self, g: Dict[str, Any]) -> int:
        if g.get("require_premium") and not self.has_premium:
            return 0
        if isinstance(g.get("per_user_remains"), int):
            cap = g["per_user_remains"]
        elif isinstance(g.get("limited_per_user"), int):
            cap = g["limited_per_user"]
        elif isinstance(g.get("per_user_total"), int):
            cap = g["per_user_total"]
        else:
            cap = 10**9
        if isinstance(g.get("supply"), int):
            cap = max(0, min(cap, g["supply"]))
        return max(0, cap)

    async def _buy(self, g: Dict[str, Any], want_qty: int) -> int:
        left = want_qty
        bought = 0
        price = int(g["price"] or 0)
        while left > 0:
            if self.bal.balance is not None and price > 0 and self.bal.balance < price:
                break
            try:
                await self.u.send_gift(ID_TO_BUY, g["id"], False)
                left -= 1; bought += 1
                if price > 0:
                    self.bal.deduct(price, 1)
                await asyncio.sleep(random.uniform(0.6, 1.1))
            except FloodWait as fw:
                print("[FW]", fw.value, "s"); await asyncio.sleep(fw.value)
            except BadRequest as e:
                emsg = str(e)
                if "STAR" in emsg.upper() and ("LOW" in emsg.upper() or "FEW" in emsg.upper() or "BALANCE" in emsg.upper()):
                    print("[ERR] buy: not enough stars -> stop")
                    self.bal.mark_insufficient()
                    break
                print("[ERR] buy:", e); break
            except PeerFlood as e:
                print("[ERR] buy:", e); break
            except Exception as e:
                raise e
        if bought: print(f"[BUY] {g['title']} ×{bought}")
        return bought

    async def tick(self, only_new: bool=False) -> Tuple[bool, bool, List[Dict[str, Any]]]:
        gifts = await self._fetch()
        if only_new:
            gifts = [g for g in gifts if g["id"] not in self.seen]
        rare_i = 0
        bought_smth = False
        new_found   = False
        new_items   = []
        for g in gifts:
            if g["id"] in self.seen and not only_new: continue
            self.seen.add(g["id"]); new_found = True; new_items.append(g)

            if not self._eligible_by_filters(g): continue
            if ONLY_PREMIUM and not g.get("require_premium", False): continue

            rarity_cap = 0
            if g["is_limited"]:
                if rare_i == 0:   rarity_cap = CAP_FIRST_RARE
                elif rare_i == 1: rarity_cap = CAP_SECOND_RARE
                rare_i += 1
            if rarity_cap and BUY_GIFT:
                base_cap = max(0, min(rarity_cap, self._user_cap(g)))
                want = self.bal.affordable_qty(g.get("price"), base_cap)
                if want > 0:
                    if await self._buy(g, want): bought_smth = True

        if new_found:
            try: STORAGE.write_text(json.dumps(sorted(self.seen), ensure_ascii=False, indent=2))
            except Exception as e: print("[WARN] save gifts.json:", e)

        if VERBOSE:
            t = fmt(now())
            if bought_smth:
                print(f"[{t}] ✅ купили"); self._last_no_new = now()
            elif (self._last_no_new is None or (now() - self._last_no_new).seconds >= NO_NEW_EVERY_SEC):
                print(f"[{t}] — новинок нет"); self._last_no_new = now()
        return bought_smth, new_found, new_items

# ── сеть ──
def build_client() -> Client:
    if SESSION:
        return Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=SESSION)
    return Client("TgAccount", api_id=API_ID, api_hash=API_HASH)

async def soft_reconnect(cli: Client) -> bool:
    for n in range(1, RECONNECT_TRIES + 1):
        try:
            try: await cli.stop()
            except Exception: pass
            await asyncio.sleep(1)
            await cli.start()
            print(f"[INFO] soft-reconnect ok #{n}")
            return True
        except AuthKeyDuplicated:
            raise
        except Exception as e:
            print(f"[WARN] soft-reconnect #{n} fail:", e)
            await asyncio.sleep(RECONNECT_PAUSE * n)
    return False

async def hard_reconnect(cur_cli: Client) -> Client | None:
    try:
        try: await cur_cli.stop()
        except Exception: pass
        await asyncio.sleep(1)
        new_cli = build_client()
        await new_cli.start()
        print("[INFO] hard-reconnect ok (new Client)")
        return new_cli
    except AuthKeyDuplicated:
        raise
    except Exception as e:
        print("[ERR] hard-reconnect failed:", e)
        return None

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

def midnight_msk(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)

# ── main ──
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check",      action="store_true", help="однократная проверка новых")
    ap.add_argument("--check-all",  action="store_true", help="однократная проверка всех")
    ap.add_argument("--check-balance", action="store_true", help="показать баланс звёзд и выйти")
    ap.add_argument("--burst-only", action="store_true", help="вне бурста не опрашивать вообще")
    ap.add_argument("--burst-now",  type=int, default=0, help="форсировать бурст на N минут с текущего момента")
    args = ap.parse_args()

    cli = build_client()
    await cli.start()

    # определим is_premium
    has_premium = HAS_PREMIUM_DEFAULT
    try:
        user = await cli.get_me()
        if hasattr(user, "is_premium") and isinstance(user.is_premium, bool):
            has_premium = user.is_premium
    except Exception:
        pass

    bal      = BalanceManager(cli)
    sniper   = GiftSniper(cli, has_premium=has_premium, bal=bal)
    notifier = Notifier(BOT_TOKEN, NOTIFY_CHAT_ID, cli)

    # баланс при старте / по запросу
    await bal.refresh_if_due(STARS_REFRESH_HOURS)
    if args.check_balance:
        print("Stars balance:", bal.balance if bal.balance is not None else "unknown")
        return

    reconnects_total = 0
    reconnects_hour  = 0
    last_ok = now()
    last_fail_alert = now() - timedelta(seconds=FAIL_ALERT_MIN_ITV)
    day_anchor  = midnight_msk(now())
    hour_anchor = now()
    hour_new    = 0

    # принудительный бурст
    forced_until: Optional[datetime] = None
    if args.burst_now > 0:
        forced_until = now() + timedelta(minutes=args.burst_now)
    elif BURST_FORCE_MINUTES > 0:
        forced_until = now() + timedelta(minutes=BURST_FORCE_MINUTES)

    # режим burst-only
    burst_only = args.burst_only or BURST_ONLY

    def in_burst(dt: datetime) -> bool:
        return ((forced_until is not None and dt < forced_until)
                or in_hourly_burst(dt)
                or within_windows(dt, BURST_PARSED))

    in_burst_state = in_burst(now())
    burst_fetch_ok = 0

    def touch_ok():
        nonlocal last_ok
        last_ok = now()

    def with_star_tail(msg: str) -> str:
        bal_str = (str(bal.balance) if bal.balance is not None else "unknown")
        return f"{msg}\n⭐ stars={bal_str}, spent_today={bal.spent_today}"

    try:
        fire_and_forget(notifier.send(
            with_star_tail(
                "▶️ GiftSniper запущен ({}) MSK\n"
                "poll≈{:.1f}–{:.1f}s, burst≈{:.1f}–{:.1f}s (59..05{}), keepalive={}s, watchdog={}s\n"
                "has_premium={}, only_premium={}, burst_only={}".format(
                    fmt_d(now()),
                    POLL_MIN, POLL_MAX, BURST_POLL_MIN, BURST_POLL_MAX,
                    (", +windows" if BURST_PARSED else ""),
                    KEEPALIVE_PERIOD, WATCHDOG_PERIOD,
                    "yes" if sniper.has_premium else "no",
                    "yes" if ONLY_PREMIUM else "no",
                    "yes" if burst_only else "no",
                )
            )
        ))

        if args.check or args.check_all:
            bought, new, new_items = await sniper.tick(only_new=not args.check_all)
            ok_total, _ = sniper.snapshot_fetch_metrics()
            base = f"ℹ️ Проверка: fetch_ok_total={ok_total}"
            base = with_star_tail(base)
            fire_and_forget(notifier.send(
                base + ("\n✅ была покупка." if bought else ("\n🆕 найдены новинки." if new else "\nновинок нет."))
            ))
            # если есть новые — пришлём их список
            if new and NOTIFY_NEW_GIFTS:
                lines = []
                for g in new_items[:NEW_NOTIFY_MAX_LINES]:
                    lines.append(_gift_line(g, you_cap=sniper._user_cap(g)))
                more = len(new_items) - len(lines)
                tail = f"\n… и ещё {more}" if more > 0 else ""
                fire_and_forget(notifier.send(with_star_tail("🆕 Новые подарки:\n" + "\n".join(lines) + tail)))
            return

        ka_task = asyncio.create_task(keepalive(cli, touch_ok))

        while True:
            # rollover по дате
            bal.ensure_day_rollover()
            # периодическое обновление баланса
            await bal.refresh_if_due(STARS_REFRESH_HOURS)

            # прогрев
            sec_to = seconds_to_next_burst(now())
            if not in_burst(now()) and sec_to is not None and 0 < sec_to <= BURST_PREWARM_SEC:
                try:
                    await cli.get_me()
                    if Ping is not None:
                        await cli.invoke(Ping(ping_id=random.randint(1,1<<31)))
                    touch_ok()
                except Exception:
                    pass

            # вход/выход бурста
            currently_in = in_burst(now())
            if currently_in and not in_burst_state:
                in_burst_state = True
                burst_fetch_ok = sniper.fetch_ok_total
                print(f"[{fmt(now())}] ⚡ Входим в бурст-режим")
                if BURST_NOTIFY: fire_and_forget(notifier.send(with_star_tail("⚡ Вошли в бурст-режим опроса.")))
            elif (not currently_in) and in_burst_state:
                in_burst_state = False
                made = sniper.fetch_ok_total - burst_fetch_ok
                print(f"[{fmt(now())}] ✅ Выходим из бурста (успешных fetch: {made})")
                if BURST_NOTIFY: fire_and_forget(notifier.send(with_star_tail(f"✅ Выход из бурста. Успешных fetch: {made}")))

            # watchdog
            if (now() - last_ok).seconds >= WATCHDOG_PERIOD:
                print("[WARN] watchdog: stale connection → reconnect…")
                ok = False
                try:
                    ok = await soft_reconnect(cli)
                except AuthKeyDuplicated:
                    fatal("AuthKeyDuplicated — та же MTProto-сессия активна где-то ещё.")
                if not ok:
                    new_cli = await hard_reconnect(cli)
                    if new_cli is not None:
                        cli = new_cli; sniper.u = cli; notifier.cli = cli; ok = True
                if ok:
                    reconnects_total += 1; reconnects_hour += 1; touch_ok()
                    if NOTIFY_RECONNECT: fire_and_forget(notifier.send(with_star_tail("♻️ Watchdog: переподключились.")))
                    try: ka_task.cancel()
                    except Exception: pass
                    ka_task = asyncio.create_task(keepalive(cli, touch_ok))
                else:
                    if (now() - last_fail_alert).seconds >= FAIL_ALERT_MIN_ITV and NOTIFY_ERRORS:
                        fire_and_forget(notifier.send(with_star_tail("❗ Watchdog: не удалось переподключиться, попробуем позже.")))
                        last_fail_alert = now()

            # основной тик
            try:
                if not burst_only or in_burst_state:
                    bought, new, new_items = await sniper.tick()
                    if new:
                        hour_new += 1
                        if NOTIFY_NEW_GIFTS:
                            lines = []
                            for g in new_items[:NEW_NOTIFY_MAX_LINES]:
                                lines.append(_gift_line(g, you_cap=sniper._user_cap(g)))
                            more = len(new_items) - len(lines)
                            tail = f"\n… и ещё {more}" if more > 0 else ""
                            fire_and_forget(notifier.send(with_star_tail("🆕 Новые подарки:\n" + "\n".join(lines) + tail)))
                    touch_ok()
            except (OSError, ConnectionError) as e:
                print("[ERR] connection:", e)
                ok = False
                try:
                    ok = await soft_reconnect(cli)
                except AuthKeyDuplicated:
                    fatal("AuthKeyDuplicated — та же MTProto-сессия активна где-то ещё.")
                if not ok:
                    new_cli = await hard_reconnect(cli)
                    if new_cli is not None:
                        cli = new_cli; sniper.u = cli; notifier.cli = cli; ok = True
                if ok:
                    reconnects_total += 1; reconnects_hour += 1; touch_ok()
                    if NOTIFY_RECONNECT:
                        fire_and_forget(notifier.send(with_star_tail("♻️ Reconnect после сетевой ошибки.")))
                    try: ka_task.cancel()
                    except Exception: pass
                    ka_task = asyncio.create_task(keepalive(cli, touch_ok))
                else:
                    if (now() - last_fail_alert).seconds >= FAIL_ALERT_MIN_ITV and NOTIFY_ERRORS:
                        fire_and_forget(notifier.send(with_star_tail("❗ Reconnect после сетевой ошибки не удался.")))
                        last_fail_alert = now()
            except Exception as e:
                print("[ERR] main:", e)
                if (now() - last_fail_alert).seconds >= FAIL_ALERT_MIN_ITV and NOTIFY_ERRORS:
                    fire_and_forget(notifier.send(with_star_tail(f"❗ Ошибка цикла: {e!r}")))
                    last_fail_alert = now()

            # heartbeat — с балансом
            if NOTIFY_HOURLY and (now() - hour_anchor) >= timedelta(hours=1):
                ok_total, ok_hour = sniper.snapshot_fetch_metrics()
                fire_and_forget(notifier.send(
                    with_star_tail(
                        f"🕐 Heartbeat {fmt_d(now())} MSK\n"
                        f"fetch_ok_hour={ok_hour}, new_detected_hour={hour_new}, reconnects_hour={reconnects_hour}\n"
                        f"fetch_ok_total={ok_total}, reconnects_total={reconnects_total}"
                    )
                ))
                hour_anchor = now(); reconnects_hour = 0; hour_new = 0; sniper.reset_hour_metrics()

            # daily summary — с балансом
            if NOTIFY_DAILY and now() >= (midnight_msk(day_anchor) + timedelta(days=1)):
                ok_total, _ = sniper.snapshot_fetch_metrics()
                fire_and_forget(notifier.send(
                    with_star_tail(
                        f"📊 Daily summary ({day_anchor.date()}): fetch_ok_total={ok_total}, reconnects_total={reconnects_total}"
                    )
                ))
                day_anchor = midnight_msk(now())

            # сон по режиму
            if burst_only and not in_burst_state:
                await asyncio.sleep(2.0)
            else:
                low, high = (BURST_POLL_MIN, BURST_POLL_MAX) if in_burst_state else (POLL_MIN, POLL_MAX)
                await asyncio.sleep(random.uniform(low, high))

    except AuthKeyDuplicated:
        fatal("AuthKeyDuplicated — та же сессия уже активна (закрой другой процесс/устройство или выдай новый TG_SESSION).")
    finally:
        try: await cli.stop()
        except Exception: pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("⏹️ exit")
