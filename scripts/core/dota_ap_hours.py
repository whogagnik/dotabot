#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import base64
import hmac
import json
import logging
import struct
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Set

import requests

# Логгер
logger = logging.getLogger("dota_ap_hours")

# Попытаемся использовать генератор из valvepython/steam, но оставим резервный вариант
try:
    from steam import webauth
    from steam.guard import generate_twofactor_code  # принимает shared_secret (base64)
    HAVE_STEAM_LIB = True
except Exception:
    HAVE_STEAM_LIB = False
    webauth = None

    # Резервный генератор 2FA кода для Steam (совместим с shared_secret)
    _STEAM_CHARS = "23456789BCDFGHJKMNPQRTVWXY"
    def generate_one_time_code(shared_secret_b64: str, timestamp: int = None) -> str:
        if timestamp is None:
            timestamp = int(time.time())
        try:
            secret = base64.b64decode(shared_secret_b64)
        except Exception as e:
            raise ValueError("Некорректный shared_secret в maFile") from e
        time_block = struct.pack(">Q", int(timestamp) // 30)
        hmac_sha1 = hmac.new(secret, time_block, 'sha1').digest()
        start = hmac_sha1[19] & 0x0F
        fullcode = struct.unpack(">I", hmac_sha1[start:start + 4])[0] & 0x7FFFFFFF

        out = []
        for _ in range(5):
            out.append(_STEAM_CHARS[fullcode % len(_STEAM_CHARS)])
            fullcode //= len(_STEAM_CHARS)
        return "".join(out)


API_BASE = "https://api.opendota.com/api"
ALL_PICK_MODES: Set[int] = {1, 22}  # All Pick и Ranked All Pick
OPEN_DOTA_LIMIT = 100


def read_mafile(path: Path) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Не удалось прочитать maFile: %s", e)
        sys.exit(1)


def login_with_guard(username: str, password: str, shared_secret_b64: str) -> str:
    if not HAVE_STEAM_LIB:
        logger.error("Библиотека 'steam' недоступна. Установите: pip install steam")
        sys.exit(1)

    code = generate_one_time_code(shared_secret_b64)
    wa = webauth.WebAuth(username)
    try:
        wa.login(password=password, twofactor_code=code)
    except Exception as e:
        logger.error("Ошибка логина в Steam: %s", e)
        sys.exit(1)

    if not getattr(wa, "steam_id", None):
        logger.error("Не удалось получить steam_id из сессии.")
        sys.exit(1)

    steam_id64 = str(wa.steam_id.as_64) if hasattr(wa.steam_id, "as_64") else str(wa.steam_id)
    return steam_id64


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
            page, len(batch), len(all_matches) - before_count, len(all_matches)
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
        print(f"Неизвестный уровень логов: {level_str}. Допустимо: {valid}", file=sys.stderr)
        sys.exit(1)
    return mapping[level_str]


def main():
    parser = argparse.ArgumentParser(
        description="Сумма наигранных часов в Dota 2 (All Pick) по истории матчей (OpenDota) с логином в Steam через Guard."
    )
    parser.add_argument("--login", required=True, help="Steam логин")
    parser.add_argument("--password", required=True, help="Steam пароль")
    parser.add_argument("--mafile", required=True, help="Путь к вашему maFile JSON (SDA)")
    parser.add_argument("--include_ranked_ap", action="store_true", default=True,
                        help="Включать Ranked All Pick (game_mode=22). По умолчанию включен.")
    parser.add_argument("--only_ranked", action="store_true",
                        help="Считать ТОЛЬКО Ranked All Pick (22).")
    parser.add_argument("--only_normal", action="store_true",
                        help="Считать ТОЛЬКО обычный All Pick (1).")
    parser.add_argument("--log-level", default="INFO",
                        help="Уровень логирования: DEBUG|INFO|WARNING|ERROR|CRITICAL (по умолчанию INFO)")
    args = parser.parse_args()

    # Настройка логгера
    level = parse_log_level(args.log_level)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ma = read_mafile(Path(args.mafile))

    shared_secret = ma.get("shared_secret")
    if not shared_secret:
        logger.error("В maFile отсутствует 'shared_secret'.")
        sys.exit(1)

    logger.info("Логинимся в Steam…")
    steam_id64 = login_with_guard(args.login, args.password, shared_secret)
    logger.info("Ваш SteamID64: %s", steam_id64)

    account_id = steam64_to_account32(steam_id64)
    logger.info("account_id (OpenDota): %s", account_id)

    # режимы
    if args.only_ranked and args.only_normal:
        logger.error("Нельзя одновременно указать --only_ranked и --only_normal.")
        sys.exit(1)
    if args.only_ranked:
        modes = {22}
    elif args.only_normal:
        modes = {1}
    else:
        modes = {1, 22}

    logger.info("Загружаем матчи из OpenDota… Режимы: %s", sorted(modes))
    matches = fetch_all_matches_account(account_id, modes)
    logger.info("Найдено матчей: %d", len(matches))

    totals = sum_durations(matches)
    if modes == {1}:
        label = "All Pick (обычный)"
    elif modes == {22}:
        label = "Ranked All Pick"
    else:
        label = "All Pick (обычный + ranked)"

    logger.info("— — — — —")
    logger.info(
        "%s: %d ч %d мин (~%.2f ч)",
        label, totals["hours"], totals["minutes"], totals["hours_float"]
    )
    logger.debug("Всего секунд: %s", totals["total_sec"])



