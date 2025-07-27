from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import FloodWait, BadRequest, InternalServerError, PeerFlood, AuthKeyDuplicated

# ──────────────── Настройки «говорливости» ────────────────
VERBOSE = True            # False → почти молчаливый режим
POLL_PRINT_EVERY = 60     # сек: как часто печатать «спим ещё …»

# ───────────── Заглушка DummyContextManager (Py 3.13) ─────
if not hasattr(asyncio, "DummyContextManager"):
    @asynccontextmanager
    async def _dummy_ctx():
        yield
    asyncio.DummyContextManager = _dummy_ctx  # type: ignore[attr-defined]

# ──────────────── Чтение .env ─────────────────────────────
load_dotenv()
API_ID         = int(os.getenv("TG_API_ID", 0))
API_HASH       = os.getenv("TG_API_HASH", "")
SESSION_STR    = os.getenv("TG_SESSION") or None
ID_TO_BUY      = int(os.getenv("ID_TO_BUY", 0))
BUY_GIFT       = os.getenv("BUY_GIFT", "false").lower() == "true"

PRICE_FROM     = int(os.getenv("PRICE_LIMIT_FROM", 500))
PRICE_TO       = int(os.getenv("PRICE_LIMIT_TO", 50_000))
SUPPLY_FROM    = int(os.getenv("SUPPLY_LIMIT_FROM", 1))
SUPPLY_TO      = int(os.getenv("SUPPLY_LIMIT_TO", 60_000))
GIFT_COUNT     = int(os.getenv("GIFT_COUNT_TO_BUY", 3))

POLL_SEC_MIN   = int(os.getenv("POLL_INTERVAL_FROM", 8))
POLL_SEC_MAX   = int(os.getenv("POLL_INTERVAL_TO", 15))

# «человеческие» паузы
NIGHT_BREAK_START_HOUR, NIGHT_BREAK_END_HOUR = 3, 9     # 03:00–09:00 MSK
DAY_BREAK_START_HOUR,   DAY_BREAK_END_HOUR   = 12, 14   # 12:00–14:00 MSK
NIGHT_BREAK_VARIATION_MIN, NIGHT_BREAK_VARIATION_MAX = 1, 30   # мин
DAY_BREAK_VARIATION_MIN,   DAY_BREAK_VARIATION_MAX   = 5, 20

# замедление при дешёвых/редких
SLOW_PRICE_THRESHOLD  = 1_000
SLOW_SUPPLY_THRESHOLD = 50_000

STORAGE = Path("gifts.json")

# ───────── Проверка переменных окружения ──────────
def _fatal(msg: str):
    print(f"[FATAL] {msg}")
    sys.exit(1)

if not (API_ID and API_HASH):
    _fatal("TG_API_ID / TG_API_HASH отсутствуют в .env")
if not SESSION_STR and not Path("TgAccount.session").exists():
    _fatal("Нужна строка TG_SESSION или файл TgAccount.session")
if BUY_GIFT and not ID_TO_BUY:
    _fatal("ID_TO_BUY обязателен при BUY_GIFT=true")

# ════════════════════════════════
#             Снайпер
# ════════════════════════════════
class GiftSniper:
    def __init__(self, user: Client, id_to_buy: int) -> None:
        self.user       = user
        self.id_to_buy  = id_to_buy
        self.seen_ids: set[int] = set()
        self._load_seen()

    # ── storage ──
    def _load_seen(self) -> None:
        if STORAGE.exists():
            try:
                data = json.loads(STORAGE.read_text())
                self.seen_ids = {int(i) if not isinstance(i, dict) else int(i["id"]) for i in data}
            except Exception as exc:
                print(f"[WARN] Не удалось прочитать {STORAGE}: {exc}")

    def _save_seen(self) -> None:
        try:
            STORAGE.write_text(json.dumps(sorted(self.seen_ids), ensure_ascii=False, indent=2))
        except Exception as exc:
            print(f"[ERR]  Не удалось записать {STORAGE}: {exc}")

    # ── нормализация подарка ──
    @staticmethod
    def _gift_to_dict(g: Any) -> Dict[str, Any]:
        SUPPLY_KEYS = ("supply", "total_count", "total_amount", "amount")

        if isinstance(g, dict):                    # dict от PyroFork
            emoji  = (g.get("sticker") or {}).get("emoji")
            if emoji == "🎁":
                emoji = None
            supply = next((g.get(k) for k in SUPPLY_KEYS if g.get(k) is not None), None)
            price  = g.get("price") or g.get("star_count")
            return {
                "id": g.get("id"),
                "title": emoji or f"ID-{g.get('id')}",
                "price": price,
                "supply": supply,
                "is_limited": g.get("is_limited", supply is not None),
            }

        # объект Gift
        emoji = getattr(getattr(g, "sticker", None), "emoji", None)
        if emoji == "🎁":
            emoji = None
        supply = next((getattr(g, k, None) for k in SUPPLY_KEYS if getattr(g, k, None) is not None), None)
        price  = getattr(g, "price", getattr(g, "star_count", None))
        return {
            "id": getattr(g, "id", None),
            "title": emoji or f"ID-{getattr(g, 'id', None)}",
            "price": price,
            "supply": supply,
            "is_limited": getattr(g, "is_limited", supply is not None),
        }

    # ── получить список подарков ──
    async def fetch_gifts(self) -> List[Dict[str, Any]]:
        try:
            raw = await self.user.get_available_gifts()
        except InternalServerError:
            await asyncio.sleep(random.uniform(1, 3))
            try:
                raw = await self.user.get_available_gifts()
            except Exception as e:
                print(f"[ERR] повтор get_available_gifts(): {e}")
                return []
        except Exception as e:
            print(f"[ERR] get_available_gifts(): {e}")
            return []

        gifts = [self._gift_to_dict(g) for g in raw or []]
        gifts.sort(key=lambda x: x["supply"] if x["supply"] is not None else float("inf"))
        return gifts

    # ── критерий покупки ──
    @staticmethod
    def _should_buy(g: Dict[str, Any]) -> bool:
        return (
            g["price"] is not None and PRICE_FROM <= g["price"] <= PRICE_TO and
            g["supply"] is not None and SUPPLY_FROM <= g["supply"] <= SUPPLY_TO
        )

    # ── покупка ──
    async def _buy(self, g: Dict[str, Any], max_qty: int) -> None:
        qty_total = min(max_qty, g.get("supply", max_qty))
        slow      = g["price"] <= SLOW_PRICE_THRESHOLD or g["supply"] <= SLOW_SUPPLY_THRESHOLD
        batch_sz  = 5 if slow else 10

        left, bought = qty_total, 0
        while left > 0:
            want, success = min(left, batch_sz), 0
            for _ in range(want):
                try:
                    await self.user.send_gift(chat_id=self.id_to_buy,
                                              gift_id=g["id"],
                                              pay_for_upgrade=False)
                    success += 1
                    await asyncio.sleep(random.uniform(1, 2) if slow else random.uniform(0.5, 1))
                except FloodWait as fw:
                    print(f"[WARN] FloodWait {fw.value}s")
                    await asyncio.sleep(fw.value + random.uniform(1, 3))
                    break
                except BadRequest as br:
                    msg = str(br)
                    if "STARGIFT_USAGE_LIMITED" in msg:
                        print(f"[INFO] {g['title']} распродан")
                    elif "BALANCE_TOO_LOW" in msg:
                        print("[ERR]  Не хватает ⭐")
                    else:
                        print(f"[ERR]  BadRequest: {msg}")
                    return
                except PeerFlood as pf:
                    print(f"[ERR]  PeerFlood: {pf}")
                    return
                except Exception as exc:
                    print(f"[ERR]  send_gift: {exc}")
                    return
            if success == 0:
                break
            bought += success
            left   -= success
            await asyncio.sleep(random.uniform(5, 10) if slow else random.uniform(2, 5))

        print(f"[BUY]  Куплено {bought}/{qty_total} × {g['title']}")

    # ── один polling-цикл ──
    async def tick(self) -> None:
        gifts = await self.fetch_gifts()
        rare_idx, bought_smth, new_found = 0, False, False

        for g in gifts:
            if g["id"] in self.seen_ids:
                continue
            self.seen_ids.add(g["id"])
            new_found = True

            max_q = 0
            if g["is_limited"]:
                rare_idx += 1
                max_q = 10 if rare_idx == 1 else 25 if rare_idx == 2 else 0

            if max_q and self._should_buy(g) and BUY_GIFT:
                await self._buy(g, max_q)
                bought_smth = True

        if new_found:
            self._save_seen()

        if VERBOSE:
            now_str = datetime.now(ZoneInfo("Europe/Moscow")).time().replace(microsecond=0)
            if bought_smth:
                print(f"[{now_str}] ✅ что-то купили")
            else:
                print(f"[{now_str}] — новых подарков нет")

    # ── однократный вывод (debug) ──
    async def check_once(self, only_new: bool = False) -> None:
        gifts = await self.fetch_gifts()
        if only_new:
            gifts = [g for g in gifts if g["id"] not in self.seen_ids]
        for g in gifts:
            print(f"{g['title']} | {g['price']}⭐ | остаток: {g['supply']} | limited: {g['is_limited']}")

# ════════════════════════════
#              main
# ════════════════════════════
async def main() -> None:
    argp = argparse.ArgumentParser()
    argp.add_argument("--check", action="store_true")
    argp.add_argument("--check-all", action="store_true")
    args = argp.parse_args()

    check_mode = args.check or args.check_all
    only_new   = args.check and not args.check_all

    user = (Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STR)
            if SESSION_STR else Client("TgAccount", api_id=API_ID, api_hash=API_HASH))

    sniper = GiftSniper(user, ID_TO_BUY)

    try: 
        async with user:
            if check_mode:
                await sniper.check_once(only_new)
                return

            # первый дамп
            if os.getenv("INIT_DUMP_ALL", "true").lower() == "true":
                gifts = await sniper.fetch_gifts()
                print(f"Initial dump: {len(gifts)} подарков")
                for g in gifts:
                    print(f"  {g['title']} | {g['price']}⭐ | остаток: {g['supply']}")
                print("✅ init-dump done")

            while True:
                now = datetime.now(ZoneInfo("Europe/Moscow"))

                # ночной сон
                if NIGHT_BREAK_START_HOUR <= now.hour < NIGHT_BREAK_END_HOUR:
                    wake = now.replace(hour=NIGHT_BREAK_END_HOUR, minute=0, second=0, microsecond=0)
                    wake += timedelta(minutes=random.randint(NIGHT_BREAK_VARIATION_MIN,
                                                            NIGHT_BREAK_VARIATION_MAX))
                    if VERBOSE:
                        total = int((wake - now).total_seconds())
                        print(f"🌙 Ночной сон. До пробуждения {total//3600}ч {total%3600//60}м")
                    while (left := (wake - datetime.now(ZoneInfo('Europe/Moscow'))).total_seconds()) > 0:
                        await asyncio.sleep(min(POLL_PRINT_EVERY, left))
                        if VERBOSE:
                            print(f"   ...спим ещё {int(left)//60} мин")
                    continue

                # обед
                if DAY_BREAK_START_HOUR <= now.hour < DAY_BREAK_END_HOUR:
                    wake = now.replace(hour=DAY_BREAK_END_HOUR, minute=0, second=0, microsecond=0)
                    wake += timedelta(minutes=random.randint(DAY_BREAK_VARIATION_MIN,
                                                            DAY_BREAK_VARIATION_MAX))
                    if VERBOSE:
                        total = int((wake - now).total_seconds())
                        print(f"☀️ Обеденный перерыв {total//60} мин")
                    while (left := (wake - datetime.now(ZoneInfo('Europe/Moscow'))).total_seconds()) > 0:
                        await asyncio.sleep(min(POLL_PRINT_EVERY, left))
                        if VERBOSE:
                            print(f"   ...обед ещё {int(left)//60} мин")
                    continue

                try:
                    await sniper.tick()
                except Exception as exc:
                    print(f"[ERR] главный цикл: {exc}", file=sys.stderr)

                await asyncio.sleep(random.randint(POLL_SEC_MIN, POLL_SEC_MAX))

    except AuthKeyDuplicated:
        print("[FATAL] AuthKeyDuplicated ➜ та же сессия уже активна.")
        print("Удалите *.session или задайте новую TG_SESSION и перезапустите.")
        return
    
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("⏹️ Завершено пользователем")
