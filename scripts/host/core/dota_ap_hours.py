#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import logging
import sys
import time
from typing import Dict, Iterable, List, Set

import requests

# Логгер
logger = logging.getLogger("dota_ap_hours")

# Попытаемся использовать генератор из valvepython/steam, но оставим резервный вариант


API_BASE = "https://api.opendota.com/api"
# Dota 2 game_mode: 1 — All Pick, 5 — Single Draft.
# Ranked All Pick (22) intentionally is not included.
UNRANKED_MODES: Set[int] = {1, 5}
OPEN_DOTA_LIMIT = 100


def get_steam_id(username: str, password: str) -> str:
    """Return SteamID64 through the current Steam credentials-auth endpoint."""
    try:
        from scripts.host.core.steamid import SteamCredentialsError, get_steamid64
    except ImportError:
        logger.error(
            "Не установлена зависимость 'steam'. Запустите скрипт Python из venv "
            "или установите зависимости из requirements.txt."
        )
        sys.exit(1)

    try:
        return get_steamid64(username, password)
    except SteamCredentialsError as e:
        logger.error("Steam не подтвердил учётные данные: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Не удалось получить SteamID: %s", e)
        sys.exit(1)


def steam64_to_account32(steam64: str) -> int:
    base = 76561197960265728
    try:
        return int(int(steam64) - base)
    except Exception:
        logger.error("Некорректный steamID64: %s", steam64)
        sys.exit(1)


def fetch_all_matches_account(account_id: int, modes: Set[int]) -> List[Dict]:
    """
    Листаем историю матчей через OpenDota:
    GET /players/{account_id}/matches?limit=100&significant=0&less_than_match_id=...
    """
    all_matches: List[Dict] = []
    less_than = None
    session = requests.Session()
    session.headers.update({"User-Agent": "dota-ap-hours-script/1.1"})

    page = 0
    while True:
        page += 1
        params = {
            "limit": str(OPEN_DOTA_LIMIT),
            "significant": "0",
        }
        if less_than:
            params["less_than_match_id"] = str(less_than)

        url = f"{API_BASE}/players/{account_id}/matches"
        try:
            r = session.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            logger.error("Сетевая ошибка запроса к OpenDota: %s", e)
            sys.exit(1)

        if r.status_code != 200:
            logger.error("OpenDota ответил HTTP %s: %.200s", r.status_code, r.text)
            sys.exit(1)
        batch = r.json()

        if not isinstance(batch, list) or not batch:
            logger.debug("Пустая партия матчей, страница %s — завершаем.", page)
            break

        before_count = len(all_matches)
        # фильтруем по режимам
        for m in batch:
            try:
                if int(m.get("game_mode", -1)) in modes:
                    all_matches.append(m)
            except Exception:
                continue

        logger.debug(
            "Стр. %s: получено %s матчей, добавлено %s AP, всего %s",
            page,
            len(batch),
            len(all_matches) - before_count,
            len(all_matches),
        )

        less_than = batch[-1].get("match_id")
        # троттлинг, чтобы не долбить API
        time.sleep(0.2)

    return all_matches


def sum_durations(matches: Iterable[Dict]) -> Dict[str, float]:
    total_sec = 0
    for m in matches:
        try:
            total_sec += int(m.get("duration", 0))
        except Exception:
            pass
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    hours_float = total_sec / 3600.0
    return {
        "total_sec": total_sec,
        "hours": int(hours),
        "minutes": int(minutes),
        "hours_float": hours_float,
    }


def parse_log_level(level_str: str) -> int:
    level_str = (level_str or "INFO").upper()
    mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    if level_str not in mapping:
        valid = ", ".join(mapping.keys())
        print(
            f"Неизвестный уровень логов: {level_str}. Допустимо: {valid}",
            file=sys.stderr,
        )
        sys.exit(1)
    return mapping[level_str]


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Сумма часов в неранговых All Pick и Single Draft по истории матчей "
            "OpenDota."
        )
    )
    parser.add_argument("--login", required=True, help="Steam логин")
    parser.add_argument("--password", required=True, help="Steam пароль")
    parser.add_argument(
        "--mafile",
        help="Необязательный устаревший параметр; для расчёта часов больше не нужен.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--only-all-pick",
        action="store_true",
        help="Считать только неранговый All Pick (game_mode=1).",
    )
    mode_group.add_argument(
        "--only-single-draft",
        action="store_true",
        help="Считать только Single Draft (game_mode=5).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Уровень логирования: DEBUG|INFO|WARNING|ERROR|CRITICAL (по умолчанию INFO)",
    )
    args = parser.parse_args()

    # Настройка логгера
    level = parse_log_level(args.log_level)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.mafile:
        logger.warning("--mafile больше не используется и может быть убран из команды.")

    logger.info("Получаем SteamID…")
    steam_id64 = get_steam_id(args.login, args.password)
    logger.info("Ваш SteamID64: %s", steam_id64)

    account_id = steam64_to_account32(steam_id64)
    logger.info("account_id (OpenDota): %s", account_id)

    # режимы
    if args.only_all_pick:
        modes = {1}
    elif args.only_single_draft:
        modes = {5}
    else:
        modes = UNRANKED_MODES

    logger.info("Загружаем матчи из OpenDota… Режимы: %s", sorted(modes))
    matches = fetch_all_matches_account(account_id, modes)
    logger.info("Найдено матчей: %d", len(matches))

    totals = sum_durations(matches)
    if modes == {1}:
        label = "Неранговый All Pick"
    elif modes == {5}:
        label = "Single Draft"
    else:
        label = "Неранговые All Pick + Single Draft"

    logger.info("— — — — —")
    logger.info(
        "%s: %d ч %d мин (~%.2f ч)",
        label,
        totals["hours"],
        totals["minutes"],
        totals["hours_float"],
    )
    logger.debug("Всего секунд: %s", totals["total_sec"])


if __name__ == "__main__":
    main()
