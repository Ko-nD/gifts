#!/usr/bin/env python3
# GiftSniper — VPS edition (premium-aware, per-user caps, robust reconnect, throttled alerts)
# - учитывает поля: per_user_remains / limited_per_user / per_user_total / require_premium
# - НЕ завязан на Bot API (покупки идут через MTProto); уведомления опциональны
# - метрика "fetch_ok_*" = количество УСПЕШНЫХ get_available_gifts()

from __future__ import annotations
import argparse, asyncio, json, os, random, sys, ssl, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone, date, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import AuthKeyDuplicated, BadRequest, FloodWait, InternalServerError, PeerFlood

try:
    # В PyroFork есть raw Ping; используем если доступен
    from pyrogram.raw.functions import Ping
except Exception:
    Ping = None

# ───────────── базовые настройки ─────────────
VERBOSE             = True
NO_NEW_EVERY_SEC    = 60            # «новинок нет» — не чаще
KEEPALIVE_PERIOD    = 90            # сек
RECONNECT_TRIES     = 5             # попыток soft/hard reconnect
RECONNECT_PAUSE     = 3             # пауза между попытками
SAFE_NOTIFY_TIMEOUT = float(os.getenv("NOTIFY_TIMEOUT", "2.5"))  # таймаут отправки уведомлений (сек)

STORAGE = Path("gifts.json")

# ───────────── время / MSK ─────────────
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

# ───────────── окружение ─────────────
load_dotenv()
API_ID   = int(os.getenv("TG_API_ID", 0))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION  = os.getenv("TG_SESSION") or None   # если пусто — будет файл TgAccount.session

ID_TO_BUY = int(os.getenv("ID_TO_BUY", 0))
BUY_GIFT  = os.getenv("BUY_GIFT", "false").lower() == "true"

# Фильтры
P_FROM, P_TO = int(os.getenv("PRICE_LIMIT_FROM", 500)),  int(os.getenv("PRICE_LIMIT_TO", 50_000))
S_FROM, S_TO = int(os.getenv("SUPPLY_LIMIT_FROM", 1)),   int(os.getenv("SUPPLY_LIMIT_TO", 60_000))
POLL_MIN, POLL_MAX = int(os.getenv("POLL_INTERVAL_FROM", 25)), int(os.getenv("POLL_INTERVAL_TO", 35))

# Новые флаги (премиум и лимиты)
HAS_PREMIUM_DEFAULT = os.getenv("HAS_PREMIUM", "true").lower() in ("1","true","yes","on")
ONLY_PREMIUM        = os.getenv("ONLY_PREMIUM", "false").lower() in ("1","true","yes","on")  # покупать только require_premium
CAP_FIRST_RARE      = int(os.getenv("CAP_FIRST_RARE", "10"))   # максимум для 1-го лимитированного
CAP_SECOND_RARE     = int(os.getenv("CAP_SECOND_RARE", "25"))  # максимум для 2-го лимитированного

# Уведомления (опционально)
BOT_TOKEN       = (os.getenv("BOT_TOKEN") or "").strip()         # токен бота (только для уведомлений)
NOTIFY_CHAT_ID  = (os.getenv("NOTIFY_CHAT_ID") or "").strip()

def to_bool(s: Optional[str], default=True) -> bool:
    if s is None: return default
    return s.strip().lower() in ("1","true","yes","on")

NOTIFY_ENABLED     = to_bool(os.getenv("NOTIFY_ENABLED"), True)
NOTIFY_HOURLY      = to_bool(os.getenv("NOTIFY_HOURLY"),  True)
NOTIFY_DAILY       = to_bool(os.getenv("NOTIFY_DAILY"),   True)
NOTIFY_RECONNECT   = to_bool(os.getenv("NOTIFY_RECONNECT"), True)
NOTIFY_ERRORS      = to_bool(os.getenv("NOTIFY_ERRORS"),  True)
FAIL_ALERT_MIN_ITV = int(os.getenv("NOTIFY_RECONNECT_MIN_INTERVAL", "900"))  # сек, минимум между «плохими новостями»

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
    # Бережный дефолт: ~7 минут или 6× poll_max, что больше
    return max(420, POLL_MAX * 6, KEEPALIVE_PERIOD * 3)

WATCHDOG_PERIOD = compute_watchdog_period()

# ───────────── уведомлялка (не блокирует покупки) ─────────────
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

        # 1) через Bot API (если есть токен и chat_id)
        if self.bot_token and self.chat_id:
            ok = await asyncio.to_thread(self._send_botapi_blocking, text)
            if ok: return True

        # 2) через MTProto пользователя (если chat_id есть)
        if self.chat_id:
            try:
                try:
                    await self.cli.send_message(chat_id=int(self.chat_id), text=text, disable_web_page_preview=True)
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
    async def _wrap():
        try:
            await asyncio.wait_for(coro, timeout=SAFE_NOTIFY_TIMEOUT + 0.5)
        except Exception:
            pass
    asyncio.create_task(_wrap())

# ───────────── ядро: снайпер ─────────────
class GiftSniper:
    def __init__(self, cli: Client, has_premium: bool):
        self.u = cli
        self.has_premium = has_premium
        self.seen: set[int] = set()
        if STORAGE.exists():
            try:
                data = json.loads(STORAGE.read_text())
                self.seen |= {int(x if not isinstance(x, dict) else x.get("id")) for x in data}
            except Exception as e:
                print("[WARN] gifts.json:", e)
        self._last_no_new: Optional[datetime] = None

        # метрики именно успешных fetch-ов
        self.fetch_ok_total: int = 0
        self.fetch_ok_hour:  int = 0

    def reset_hour_metrics(self):
        self.fetch_ok_hour = 0

    def snapshot_fetch_metrics(self) -> Tuple[int, int]:
        return self.fetch_ok_total, self.fetch_ok_hour

    @staticmethod
    def _get(g: Any, name: str, default=None):
        if isinstance(g, dict): return g.get(name, default)
        return getattr(g, name, default)

    @staticmethod
    def _norm(g: Any) -> Dict[str, Any]:
        # приводим подарок к единому виду
        SUP_KEYS = ("supply", "total_count", "total_amount", "amount")
        sup = None
        if isinstance(g, dict):
            sup = next((g.get(k) for k in SUP_KEYS if g.get(k) is not None), None)
            price = g.get("price") or g.get("star_count")
            emoji = (g.get("sticker") or {}).get("emoji")
            if emoji == "🎁": emoji = None
            return dict(
                id=g.get("id"),
                title=emoji or f"ID-{g.get('id')}",
                price=price,
                supply=sup,
                is_limited=g.get("is_limited", sup is not None),
                per_user_remains=g.get("per_user_remains"),
                limited_per_user=g.get("limited_per_user"),
                per_user_total=g.get("per_user_total"),
                require_premium=g.get("require_premium", False),
            )

        # объект
        sup = next((getattr(g, k, None) for k in SUP_KEYS if getattr(g, k, None) is not None), None)
        price = getattr(g, "price", getattr(g, "star_count", None))
        emoji = getattr(getattr(g, "sticker", None), "emoji", None)
        if emoji == "🎁": emoji = None
        return dict(
            id=getattr(g, "id", None),
            title=emoji or f"ID-{getattr(g, 'id', None)}",
            price=price,
            supply=sup,
            is_limited=getattr(g, "is_limited", sup is not None),
            per_user_remains=getattr(g, "per_user_remains", None),
            limited_per_user=getattr(g, "limited_per_user", None),
            per_user_total=getattr(g, "per_user_total", None),
            require_premium=getattr(g, "require_premium", False),
        )

    async def _fetch(self) -> List[Dict[str, Any]]:
        # два шанса на InternalServerError
        try:
            raw = await self.u.get_available_gifts()
        except InternalServerError:
            await asyncio.sleep(1)
            raw = await self.u.get_available_gifts()

        # здесь запрос точно «успешный» → считаем
        self.fetch_ok_total += 1
        self.fetch_ok_hour  += 1

        gifts = [self._norm(x) for x in (raw or [])]
        gifts.sort(key=lambda x: x["supply"] if x["supply"] is not None else float("inf"))
        return gifts

    def _eligible_by_filters(self, g: Dict[str, Any]) -> bool:
        return (
            g["price"] is not None and P_FROM <= g["price"] <= P_TO and
            g["supply"] is not None and S_FROM <= g["supply"] <= S_TO
        )

    def _user_cap(self, g: Dict[str, Any]) -> int:
        """
        Сколько нам разрешено купить ЭТИМ аккаунтом по новым полям.
        Логика:
          - если gift требует премиум, а у нас его нет → 0
          - базовый пер-юзер кап: per_user_remains (если есть), иначе limited_per_user (если есть), иначе «много»
          - на всякий случай не превышаем текущий supply
        """
        if g.get("require_premium") and not self.has_premium:
            return 0

        # per_user_remains — самое точное: "сколько ещё можно этому пользователю"
        if isinstance(g.get("per_user_remains"), int):
            cap = g["per_user_remains"]
        elif isinstance(g.get("limited_per_user"), int):
            cap = g["limited_per_user"]
        elif isinstance(g.get("per_user_total"), int):
            # общее допустимое число на пользователя (если нет remains — возьмём как верхнюю границу)
            cap = g["per_user_total"]
        else:
            cap = 10**9  # «без ограничений» с точки зрения пользователя

        if isinstance(g.get("supply"), int):
            cap = max(0, min(cap, g["supply"]))

        return max(0, cap)

    async def _buy(self, g: Dict[str, Any], want_qty: int) -> int:
        left = want_qty
        bought = 0
        while left > 0:
            try:
                await self.u.send_gift(ID_TO_BUY, g["id"], False)
                left -= 1; bought += 1
                # маленькая пауза, чтобы не уткнуться в FloodWait мгновенно
                await asyncio.sleep(random.uniform(0.7, 1.5))
            except FloodWait as fw:
                print("[FW]", fw.value, "s")
                await asyncio.sleep(fw.value)
            except (BadRequest, PeerFlood) as e:
                print("[ERR] buy:", e)
                break
            except Exception as e:
                # отдаём выше — пусть main решит reconnect
                raise e

        if bought:
            print(f"[BUY] {g['title']} ×{bought}")
        return bought

    async def tick(self, only_new: bool=False) -> Tuple[bool, bool]:
        gifts = await self._fetch()
        rare_i = 0
        bought_smth = False
        new_found   = False

        if only_new:
            gifts = [g for g in gifts if g["id"] not in self.seen]

        for g in gifts:
            if g["id"] in self.seen and not only_new:
                continue
            self.seen.add(g["id"])
            new_found = True

            # фильтры по цене/тиражу
            if not self._eligible_by_filters(g):
                continue

            # премиум-фильтр (если включён режим “только премиум”)
            if ONLY_PREMIUM and not g.get("require_premium", False):
                continue

            # базовый «насколько редкий» лимит
            rarity_cap = 0
            if g["is_limited"]:
                if rare_i == 0:
                    rarity_cap = CAP_FIRST_RARE
                elif rare_i == 1:
                    rarity_cap = CAP_SECOND_RARE
                rare_i += 1

            if rarity_cap and BUY_GIFT:
                # учесть per_user* ограничения
                user_cap = self._user_cap(g)
                # итоговый желаемый объём
                want = max(0, min(rarity_cap, user_cap))
                if want > 0:
                    bought = await self._buy(g, want)
                    if bought > 0:
                        bought_smth = True

        if new_found:
            try:
                STORAGE.write_text(json.dumps(sorted(self.seen), ensure_ascii=False, indent=2))
            except Exception as e:
                print("[WARN] save gifts.json:", e)

        if VERBOSE:
            t = fmt(now())
            if bought_smth:
                print(f"[{t}] ✅ купили"); self._last_no_new = now()
            elif (self._last_no_new is None or (now() - self._last_no_new).seconds >= NO_NEW_EVERY_SEC):
                print(f"[{t}] — новинок нет"); self._last_no_new = now()

        return bought_smth, new_found

# ───────────── коннект ─────────────
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

# ───────────── main ─────────────
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="однократная проверка новых")
    ap.add_argument("--check-all", action="store_true", help="однократная проверка всех")
    args = ap.parse_args()

    cli = build_client()

    # определим is_premium из API (если получится), иначе — из .env
    has_premium = HAS_PREMIUM_DEFAULT
    try:
        me = await cli.start();  # start вернёт self; get_me требует started-клиента
        try:
            user = await cli.get_me()
            if hasattr(user, "is_premium") and isinstance(user.is_premium, bool):
                has_premium = user.is_premium
        except Exception:
            pass
    finally:
        # если выше не упали, клиент уже стартанут и не надо останавливать
        pass

    sniper   = GiftSniper(cli, has_premium=has_premium)
    notifier = Notifier(BOT_TOKEN, NOTIFY_CHAT_ID, cli)

    reconnects_total = 0
    reconnects_hour  = 0
    last_ok = now()
    last_fail_alert = now() - timedelta(seconds=FAIL_ALERT_MIN_ITV)
    day_anchor  = midnight_msk(now())
    hour_anchor = now()
    hour_new    = 0   # сколько «нашли новинок» в текущем часу

    def touch_ok():
        nonlocal last_ok
        last_ok = now()

    try:
        # если не стартовали (вдруг упали выше) — стартанём
        if not getattr(cli, "is_connected", False):
            await cli.start()

        fire_and_forget(notifier.send(
            "▶️ GiftSniper запущен ({}) MSK\n"
            "poll={}–{}s, keepalive={}s, watchdog={}s\n"
            "has_premium={}, only_premium={}".format(
                fmt_d(now()), POLL_MIN, POLL_MAX, KEEPALIVE_PERIOD, WATCHDOG_PERIOD,
                "yes" if sniper.has_premium else "no",
                "yes" if ONLY_PREMIUM else "no"
            )
        ))

        if args.check or args.check_all:
            bought, new = await sniper.tick(only_new=not args.check_all)
            ok_total, _ = sniper.snapshot_fetch_metrics()
            txt = (
                f"ℹ️ Проверка завершена. fetch_ok_total={ok_total}. " +
                ("✅ были покупки." if bought else ("🆕 были новинки." if new else "новинок нет."))
            )
            fire_and_forget(notifier.send(txt))
            return

        ka_task = asyncio.create_task(keepalive(cli, touch_ok))

        while True:
            # watchdog — давно не было успешных RPC?
            if (now() - last_ok).seconds >= WATCHDOG_PERIOD:
                print("[WARN] watchdog: stale connection → reconnect…")
                ok = False
                try:
                    ok = await soft_reconnect(cli)
                except AuthKeyDuplicated:
                    fatal("AuthKeyDuplicated — та же MTProto-сессия активна где-то ещё. Закройте лишние сессии или выдайте новый TG_SESSION.")
                if not ok:
                    new_cli = await hard_reconnect(cli)
                    if new_cli is not None:
                        cli = new_cli
                        sniper.u = cli
                        notifier.cli = cli
                        ok = True
                if ok:
                    reconnects_total += 1
                    reconnects_hour  += 1
                    touch_ok()
                    if NOTIFY_RECONNECT:
                        fire_and_forget(notifier.send("♻️ Watchdog: переподключились."))
                    try: ka_task.cancel()
                    except Exception: pass
                    ka_task = asyncio.create_task(keepalive(cli, touch_ok))
                else:
                    if (now() - last_fail_alert).seconds >= FAIL_ALERT_MIN_ITV and NOTIFY_ERRORS:
                        fire_and_forget(notifier.send("❗ Watchdog: не удалось переподключиться, попробуем позже."))
                        last_fail_alert = now()

            # основной тик
            try:
                bought, new = await sniper.tick()
                if new:
                    hour_new += 1
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
                        cli = new_cli
                        sniper.u = cli
                        notifier.cli = cli
                        ok = True
                if ok:
                    reconnects_total += 1
                    reconnects_hour  += 1
                    touch_ok()
                    if NOTIFY_RECONNECT:
                        fire_and_forget(notifier.send("♻️ Reconnect после сетевой ошибки."))
                    try: ka_task.cancel()
                    except Exception: pass
                    ka_task = asyncio.create_task(keepalive(cli, touch_ok))
                else:
                    if (now() - last_fail_alert).seconds >= FAIL_ALERT_MIN_ITV and NOTIFY_ERRORS:
                        fire_and_forget(notifier.send("❗ Reconnect после сетевой ошибки не удался."))
                        last_fail_alert = now()
            except Exception as e:
                print("[ERR] main:", e)
                if (now() - last_fail_alert).seconds >= FAIL_ALERT_MIN_ITV and NOTIFY_ERRORS:
                    fire_and_forget(notifier.send(f"❗ Ошибка цикла: {e!r}"))
                    last_fail_alert = now()

            # hourly heartbeat — показываем УСПЕШНЫЕ FETCH-и за час
            if NOTIFY_HOURLY and (now() - hour_anchor) >= timedelta(hours=1):
                ok_total, ok_hour = sniper.snapshot_fetch_metrics()
                fire_and_forget(notifier.send(
                    f"🕐 Heartbeat {fmt_d(now())} MSK\n"
                    f"fetch_ok_hour={ok_hour}, new_detected_hour={hour_new}, reconnects_hour={reconnects_hour}\n"
                    f"fetch_ok_total={ok_total}, reconnects_total={reconnects_total}"
                ))
                hour_anchor = now()
                reconnects_hour = 0
                hour_new = 0
                sniper.reset_hour_metrics()

            # daily summary — итог за день по успешным FETCHам
            if NOTIFY_DAILY and now() >= (midnight_msk(day_anchor) + timedelta(days=1)):
                ok_total, _ = sniper.snapshot_fetch_metrics()
                fire_and_forget(notifier.send(
                    f"📊 Daily summary ({day_anchor.date()}): fetch_ok_total={ok_total}, reconnects_total={reconnects_total}"
                ))
                day_anchor = midnight_msk(now())

            await asyncio.sleep(random.randint(POLL_MIN, POLL_MAX))

    except AuthKeyDuplicated:
        fatal("AuthKeyDuplicated — та же сессия уже активна (закрой другой процесс/устройство или выдай новый TG_SESSION).")
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
