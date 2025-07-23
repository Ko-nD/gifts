#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
gifts_sniper.py — минимальный Telegram‑юзербот для тихой скупки подарков Stars.
Никаких уведомлений: бот лишь мониторит и покупает.
"""

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
from loguru import logger
from pyrogram import Client
from pyrogram.errors import FloodWait, BadRequest, InternalServerError, PeerFlood

# ──────────────────────
# Заглушка DummyContextManager
# ──────────────────────
if not hasattr(asyncio, "DummyContextManager"):
    @asynccontextmanager
    async def _dummy_ctx():
        yield
    asyncio.DummyContextManager = _dummy_ctx

# ─── Чтение .env ──────────────────────────────────────────────
load_dotenv()
API_ID = int(os.getenv("TG_API_ID", 0))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION_STR = os.getenv("TG_SESSION") or None

ID_TO_BUY = int(os.getenv("ID_TO_BUY", 0))          # куда дарим

BUY_GIFT = os.getenv("BUY_GIFT", "false").lower() == "true"

PRICE_FROM = int(os.getenv("PRICE_LIMIT_FROM", 500))
PRICE_TO   = int(os.getenv("PRICE_LIMIT_TO", 50000))
SUPPLY_FROM = int(os.getenv("SUPPLY_LIMIT_FROM", 1))
SUPPLY_TO   = int(os.getenv("SUPPLY_LIMIT_TO", 60_000))
GIFT_COUNT  = int(os.getenv("GIFT_COUNT_TO_BUY", 3))

POLL_SEC_MIN = int(os.getenv("POLL_INTERVAL_FROM", 8))
POLL_SEC_MAX = int(os.getenv("POLL_INTERVAL_TO", 15))

NIGHT_BREAK_START_HOUR = 3
NIGHT_BREAK_END_HOUR   = 9
NIGHT_BREAK_VARIATION_MIN = 1
NIGHT_BREAK_VARIATION_MAX = 30

DAY_BREAK_START_HOUR = 12
DAY_BREAK_END_HOUR   = 14
DAY_BREAK_VARIATION_MIN = 5
DAY_BREAK_VARIATION_MAX = 20

SLOW_PRICE_THRESHOLD  = 1000
SLOW_SUPPLY_THRESHOLD = 50_000

STORAGE = Path("gifts.json")

# ─── Проверка настроек ────────────────────────────────────────
if not (API_ID and API_HASH):
    logger.error("TG_API_ID / TG_API_HASH отсутствуют в .env")
    sys.exit(1)
if not SESSION_STR and not Path("TgAccount.session").exists():
    logger.error("Нужна строка TG_SESSION или файл TgAccount.session")
    sys.exit(1)
if BUY_GIFT and not ID_TO_BUY:
    logger.error("ID_TO_BUY обязателен при BUY_GIFT=true")
    sys.exit(1)


# ═════════════════════════════════════════════════════════════
#                       GiftSniper
# ═════════════════════════════════════════════════════════════
class GiftSniper:
    def __init__(self, user: Client, id_to_buy: int) -> None:
        self.user = user
        self.id_to_buy = id_to_buy
        self.seen_ids: set[int] = set()
        self._load_seen()

    # ── seen.json ────────────────────────────────────────────
    def _load_seen(self) -> None:
        if STORAGE.exists():
            try:
                data = json.loads(STORAGE.read_text())
                self.seen_ids = {int(i) if not isinstance(i, dict) else int(i["id"]) for i in data}
            except Exception as exc:
                logger.warning(f"Не удалось прочитать {STORAGE}: {exc}")

    def _save_seen(self) -> None:
        try:
            STORAGE.write_text(json.dumps(sorted(self.seen_ids), ensure_ascii=False, indent=2))
        except Exception as exc:
            logger.error(f"Не удалось записать {STORAGE}: {exc}")

    # ── Нормализация подарка ─────────────────────────────────
    @staticmethod
    def _gift_to_dict(g: Any) -> Dict[str, Any]:
        """Преобразует объект / dict подарка в единый словарь."""
        POSSIBLE_SUPPLY_KEYS = ("supply", "total_count", "total_amount", "amount")

        # ①  Словарь (Pyrofork чаще всего даёт именно dict)
        if isinstance(g, dict):
            emoji = (g.get("sticker") or {}).get("emoji")
            if emoji == "🎁":
                emoji = None
            supply = next((g.get(k) for k in POSSIBLE_SUPPLY_KEYS if g.get(k) is not None), None)
            price  = g.get("price") or g.get("star_count")
            return {
                "id": g.get("id"),
                "title": emoji or f"ID-{g.get('id')}",
                "price": price,
                "supply": supply,
                "is_limited": g.get("is_limited", supply is not None),
            }

        # ②  Объект Gift (если Pyrogram отдаст именно объект)
        emoji = getattr(getattr(g, "sticker", None), "emoji", None)
        if emoji == "🎁":
            emoji = None
        supply = next(
            (getattr(g, k, None) for k in POSSIBLE_SUPPLY_KEYS if getattr(g, k, None) is not None),
            None,
        )
        price = getattr(g, "price", getattr(g, "star_count", None))
        return {
            "id": getattr(g, "id", None),
            "title": emoji or f"ID-{getattr(g, 'id', None)}",
            "price": price,
            "supply": supply,
            "is_limited": getattr(g, "is_limited", supply is not None),
        }


    # ── Получаем список подарков (user‑API) ──────────────────
    async def fetch_gifts(self) -> List[Dict[str, Any]]:
        try:
            raw = await self.user.get_available_gifts()
        except InternalServerError:
            await asyncio.sleep(random.uniform(1, 3))
            try:
                raw = await self.user.get_available_gifts()
            except Exception as e:
                logger.error(f"get_available_gifts(): {e}")
                return []
        except Exception as e:
            logger.error(f"get_available_gifts(): {e}")
            return []

        gifts = [self._gift_to_dict(g) for g in raw or []]
        gifts.sort(key=lambda x: x["supply"] if x["supply"] is not None else float("inf"))
        return gifts

    @staticmethod
    def _should_buy(g: Dict[str, Any]) -> bool:
        return (
            g["price"] is not None
            and PRICE_FROM <= g["price"] <= PRICE_TO
            and g["supply"] is not None
            and SUPPLY_FROM <= g["supply"] <= SUPPLY_TO
        )

    # ── Покупка ──────────────────────────────────────────────
    async def _buy(self, g: Dict[str, Any], max_qty: int) -> None:
        qty_total = min(max_qty, g.get("supply", max_qty))
        slow = g["price"] <= SLOW_PRICE_THRESHOLD or g["supply"] <= SLOW_SUPPLY_THRESHOLD
        batch = 5 if slow else 10

        left = qty_total
        bought = 0
        while left > 0:
            want = min(left, batch)
            success = 0
            for _ in range(want):
                try:
                    await self.user.send_gift(
                        chat_id=self.id_to_buy,
                        gift_id=g["id"],
                        pay_for_upgrade=False,
                    )
                    success += 1
                    await asyncio.sleep(random.uniform(1, 2) if slow else random.uniform(0.5, 1))
                except FloodWait as fw:
                    logger.warning(f"FloodWait {fw.value}s")
                    await asyncio.sleep(fw.value + random.uniform(1, 3))
                    break
                except BadRequest as br:
                    if "STARGIFT_USAGE_LIMITED" in str(br):
                        logger.info(f"{g['title']} распродан.")
                    elif "BALANCE_TOO_LOW" in str(br):
                        logger.error("Не хватает ⭐")
                    else:
                        logger.error(f"BadRequest: {br}")
                    return
                except PeerFlood as pf:
                    logger.error(f"PeerFlood: {pf}")
                    return
                except Exception as exc:
                    logger.error(f"Ошибка send_gift: {exc}")
                    return
            if success == 0:
                break
            bought += success
            left -= success
            await asyncio.sleep(random.uniform(5, 10) if slow else random.uniform(2, 5))
        logger.info(f"Куплено {bought}/{qty_total} шт. {g['title']}")

    # ── Один цикл проверки ──────────────────────────────────
    async def tick(self) -> None:
        gifts = await self.fetch_gifts()
        rare_idx = 0
        for g in gifts:
            if g["id"] in self.seen_ids:
                continue
            self.seen_ids.add(g["id"])

            # лимитируем количество
            max_q = 0
            if g["is_limited"]:
                rare_idx += 1
                max_q = 10 if rare_idx == 1 else 25 if rare_idx == 2 else 0

            if max_q and self._should_buy(g) and BUY_GIFT:
                await self._buy(g, max_q)

        if gifts:
            self._save_seen()

    # ── Разовый вывод (debug) ───────────────────────────────
    async def check_once(self, only_new: bool = False) -> None:
        gifts = await self.fetch_gifts()
        if only_new:
            gifts = [g for g in gifts if g["id"] not in self.seen_ids]
        for g in gifts:
            print(f"{g['title']} | {g['price']}⭐ | остаток: {g['supply']} | limited: {g['is_limited']}")


# ═════════════════════════════════════════
# main
# ═════════════════════════════════════════
async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    p.add_argument("--check-all", action="store_true")
    args = p.parse_args()

    check_mode = args.check or args.check_all
    only_new   = args.check and not args.check_all

    user = (
        Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STR)
        if SESSION_STR else Client("TgAccount", api_id=API_ID, api_hash=API_HASH)
    )

    sniper = GiftSniper(user, ID_TO_BUY)

    async with user:  # MTProto‑сессия
        if check_mode:
            await sniper.check_once(only_new)
            return

        while True:
            now = datetime.now(ZoneInfo("Europe/Moscow"))
            # Ночной перерыв
            if NIGHT_BREAK_START_HOUR <= now.hour < NIGHT_BREAK_END_HOUR:
                wake = now.replace(hour=NIGHT_BREAK_END_HOUR, minute=0, second=0, microsecond=0)
                wake += timedelta(minutes=random.randint(NIGHT_BREAK_VARIATION_MIN, NIGHT_BREAK_VARIATION_MAX))
                logger.info(f"🌙 Сoncat до {wake.time().replace(microsecond=0)}")
                await asyncio.sleep((wake - now).total_seconds())
                continue
            # Дневной перерыв
            if DAY_BREAK_START_HOUR <= now.hour < DAY_BREAK_END_HOUR:
                wake = now.replace(hour=DAY_BREAK_END_HOUR, minute=0, second=0, microsecond=0)
                wake += timedelta(minutes=random.randint(DAY_BREAK_VARIATION_MIN, DAY_BREAK_VARIATION_MAX))
                logger.info(f"☀️ Пауза до {wake.time().replace(microsecond=0)}")
                await asyncio.sleep((wake - now).total_seconds())
                continue

            try:
                await sniper.tick()
            except Exception as exc:
                logger.error(f"Ошибка цикла: {exc}", exc_info=True)

            await asyncio.sleep(random.randint(POLL_SEC_MIN, POLL_SEC_MAX))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("⏹️ Завершено пользователем")
