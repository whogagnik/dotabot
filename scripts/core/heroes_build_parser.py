from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional
from pathlib import Path
from bs4 import BeautifulSoup, Tag

from scripts.core.CONSTANTS import (
    DEFAULT_HEADERS,
    DEFAULT_MAX_ITEMS,
    DOTABUFF_HERO_URL,
    DOTABUFF_REFERER,
    DOTA2PROTRACKER_HERO_URL,
    DOTA2PROTRACKER_REFERER,
    HTTP_BACKOFF_BASE_SECONDS,
    HTTP_RETRIES,
    HTTP_TIMEOUT_SECONDS,
    JSON_ENSURE_ASCII,
    JSON_INDENT,
    OUT_FILE_HERO_BUILDS,
    SLEEP_BETWEEN_REQUESTS_SECONDS,
)

try:
    import cloudscraper
except ImportError as e:
    raise RuntimeError(
        "Нужно установить зависимости: pip install cloudscraper beautifulsoup4"
    ) from e


@dataclass
class BuildItem:
    name: str
    timing: Optional[str] = None
    purchase_rate: Optional[str] = None
    winrate: Optional[str] = None
    matches: Optional[str] = None
    is_core: bool = False


@dataclass
class HeroBuild:
    hero: str
    hero_slug: str
    source: str
    role: Optional[str]
    patch: Optional[str]
    starting_items: List[BuildItem]
    core_items: List[BuildItem]
    situational_items: List[BuildItem]
    raw_url: str


def _log(logger, level: str, msg: str) -> None:
    if logger is None:
        return
    fn = getattr(logger, level, None)
    if callable(fn):
        fn(msg)


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _safe_text(el: Optional[Tag]) -> str:
    if el is None:
        return ""
    return _collapse_ws(el.get_text(" ", strip=True))


def _slugify_hero_name_for_d2pt(hero_name: str) -> str:
    return hero_name.strip().replace(" ", "-")


def _slugify_hero_name_for_dotabuff(hero_name: str) -> str:
    return hero_name.strip().lower().replace(" ", "-")


def _make_session(referer: str):
    session = cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "mobile": False,
        }
    )
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = referer
    session.headers.update(headers)
    return session


def make_d2pt_session():
    return _make_session(DOTA2PROTRACKER_REFERER)


def make_dotabuff_session():
    return _make_session(DOTABUFF_REFERER)


def _looks_like_cloudflare_block(html: str) -> bool:
    text = (html or "").lower()
    return (
        "just a moment" in text
        or "cf-browser-verification" in text
        or ("cloudflare" in text and "enable javascript" in text)
    )


def _request_html(session, url: str, logger=None) -> str:
    last_err: Optional[Exception] = None

    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            text = r.text or ""

            if r.status_code in (403, 429, 500, 502, 503, 504):
                snippet = text[:300].replace("\n", " ")
                wait = min(HTTP_BACKOFF_BASE_SECONDS * attempt, 8.0)
                _log(
                    logger,
                    "warning",
                    f"[builds] status={r.status_code} for {url}; retry in {wait:.1f}s; body={snippet}",
                )
                time.sleep(wait)
                continue

            r.raise_for_status()

            if _looks_like_cloudflare_block(text):
                wait = min(HTTP_BACKOFF_BASE_SECONDS * attempt, 8.0)
                _log(
                    logger,
                    "warning",
                    f"[builds] cloudflare page for {url}; retry in {wait:.1f}s",
                )
                time.sleep(wait)
                continue

            return text

        except Exception as e:
            last_err = e
            wait = min(HTTP_BACKOFF_BASE_SECONDS * attempt, 8.0)
            _log(
                logger,
                "warning",
                f"[builds] request failed for {url}: {e}; retry in {wait:.1f}s",
            )
            time.sleep(wait)

    raise RuntimeError(f"Failed to fetch {url}") from last_err


def _extract_item_name_from_text(text: str) -> Optional[str]:
    text = _collapse_ws(text)
    if not text:
        return None

    if text.upper() == "CORE":
        return None
    if re.fullmatch(r"\d+(\.\d+)?%", text):
        return None
    if re.fullmatch(r"\d[\d,]*", text):
        return None
    if re.fullmatch(r"\d+m.*", text, flags=re.IGNORECASE):
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        return None

    return text


def _clean_item_name(name: str) -> str:
    name = _collapse_ws(name)
    name = name.replace("#|n|#", "").strip()
    return name


def _is_bad_item_name(name: str) -> bool:
    if not name:
        return True

    bad_exact = {
        "Основа",
        "На этой неделе",
        "больше",
        "Предмет",
        "Матчи",
        "Победы",
        "Доля побед",
        "Популярное руководство",
        "Наиболее популярная сборка",
        "Часто используемые предметы",
        "LH @ 10/20/30",
        "Старт",
        "Ранняя игра",
        "Середина игры",
        "Поздняя игра",
        "Item",
        "Purchase Rate",
        "Average Time",
        "Table View Normal View",
        "Show Core Items (≥50% purchase rate)",
        "Show Core Items (>=50% purchase rate)",
    }

    if name in bad_exact:
        return True

    if name.startswith("#|n|#"):
        return True

    if "LH @" in name:
        return True

    bad_single_words = {
        "Alert",
        "Mystical",
        "Quickened",
        "Timeless",
    }
    if name in bad_single_words:
        return True

    return False


def _dedupe_items(items: List[BuildItem]) -> List[BuildItem]:
    out: List[BuildItem] = []
    seen = set()

    for item in items:
        key = item.name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)

    return out


def parse_d2pt_hero_build(session, hero_name: str, logger=None) -> HeroBuild:
    hero_slug = _slugify_hero_name_for_d2pt(hero_name)
    url = DOTA2PROTRACKER_HERO_URL.format(hero_slug=hero_slug)

    html = _request_html(session, url, logger=logger)
    soup = BeautifulSoup(html, "html.parser")

    page_text = soup.get_text("\n", strip=True)
    lines = [_collapse_ws(x) for x in page_text.splitlines() if _collapse_ws(x)]

    patch = None
    m_patch = re.search(r"Patch\s+([0-9.]+[a-z]?)", page_text, flags=re.IGNORECASE)
    if m_patch:
        patch = m_patch.group(1)

    role = None
    role_match = re.search(
        r"stats for .*?\b(Carry|Mid|Offlane|Support|Hard Support)\b",
        page_text,
        flags=re.IGNORECASE,
    )
    if role_match:
        role = _collapse_ws(role_match.group(1))

    starting_items: List[BuildItem] = []
    core_items: List[BuildItem] = []
    situational_items: List[BuildItem] = []

    in_item_stats = False
    for i, line in enumerate(lines):
        low = line.lower()

        if low == "item stats":
            in_item_stats = True
            continue

        if not in_item_stats:
            continue

        if low in {
            "matchups & synergies",
            "off-meta builds",
            "loading…",
            "loading...",
        }:
            break

        item_name = _extract_item_name_from_text(line)
        if item_name is None:
            continue

        item_name = _clean_item_name(item_name)
        if _is_bad_item_name(item_name):
            continue

        next1 = lines[i + 1] if i + 1 < len(lines) else ""
        next2 = lines[i + 2] if i + 2 < len(lines) else ""

        purchase_rate = next1 if re.search(r"\d+(\.\d+)?%", next1) else None
        timing = next2 if re.search(r"\b\d+m", next2) else None
        is_core = next1.upper() == "CORE"

        item = BuildItem(
            name=item_name,
            timing=timing,
            purchase_rate=purchase_rate,
            is_core=is_core,
        )

        if is_core and len(core_items) < DEFAULT_MAX_ITEMS:
            core_items.append(item)
        elif len(situational_items) < DEFAULT_MAX_ITEMS:
            situational_items.append(item)

    core_items = _dedupe_items(core_items)
    situational_items = _dedupe_items(situational_items)

    if not core_items:
        fallback_core: List[BuildItem] = []
        for item in situational_items[:6]:
            fallback_core.append(
                BuildItem(
                    name=item.name,
                    timing=item.timing,
                    purchase_rate=item.purchase_rate,
                    winrate=item.winrate,
                    matches=item.matches,
                    is_core=True,
                )
            )
        core_items = fallback_core

    return HeroBuild(
        hero=hero_name,
        hero_slug=hero_slug,
        source="dota2protracker",
        role=role,
        patch=patch,
        starting_items=starting_items,
        core_items=core_items,
        situational_items=situational_items,
        raw_url=url,
    )


def parse_dotabuff_hero_build(session, hero_name: str, logger=None) -> HeroBuild:
    hero_slug = _slugify_hero_name_for_dotabuff(hero_name)
    url = DOTABUFF_HERO_URL.format(hero_slug=hero_slug)

    html = _request_html(session, url, logger=logger)
    soup = BeautifulSoup(html, "html.parser")

    page_text = soup.get_text("\n", strip=True)
    lines = [_collapse_ws(x) for x in page_text.splitlines() if _collapse_ws(x)]

    role = None
    patch = None

    starting_items: List[BuildItem] = []
    situational_items: List[BuildItem] = []

    in_common_items = False
    for i, line in enumerate(lines):
        low = line.lower()

        if low.startswith("часто используемые предметы"):
            in_common_items = True
            continue

        if in_common_items and (
            low.startswith("силён против")
            or low.startswith("слаб против")
            or low.startswith("popular guides")
            or low.startswith("популярное руководство")
        ):
            break

        if not in_common_items:
            continue

        item_name = _extract_item_name_from_text(line)
        if item_name is None:
            continue

        item_name = _clean_item_name(item_name)
        if _is_bad_item_name(item_name):
            continue

        m = re.match(r"^(.*?)(\d[\d,]*)$", item_name)
        if m:
            item_name = _collapse_ws(m.group(1))

        if not item_name or _is_bad_item_name(item_name):
            continue

        next1 = lines[i + 1] if i + 1 < len(lines) else ""
        next2 = lines[i + 2] if i + 2 < len(lines) else ""

        matches = next1 if re.fullmatch(r"\d[\d,]*", next1) else None
        winrate = next2 if re.fullmatch(r"\d+(\.\d+)?%", next2) else None

        situational_items.append(
            BuildItem(
                name=item_name,
                matches=matches,
                winrate=winrate,
            )
        )

        if len(situational_items) >= DEFAULT_MAX_ITEMS:
            break

    situational_items = _dedupe_items(situational_items)

    core_items: List[BuildItem] = []
    for item in situational_items[:6]:
        core_items.append(
            BuildItem(
                name=item.name,
                timing=item.timing,
                purchase_rate=item.purchase_rate,
                winrate=item.winrate,
                matches=item.matches,
                is_core=True,
            )
        )

    return HeroBuild(
        hero=hero_name,
        hero_slug=hero_slug,
        source="dotabuff",
        role=role,
        patch=patch,
        starting_items=starting_items,
        core_items=core_items,
        situational_items=situational_items,
        raw_url=url,
    )


def _error_record(hero_name: str, hero_slug: str, source: str, error: str) -> Dict[str, Any]:
    return {
        "hero": hero_name,
        "hero_slug": hero_slug,
        "source": source,
        "role": None,
        "patch": None,
        "starting_items": [],
        "core_items": [],
        "situational_items": [],
        "raw_url": None,
        "error": error,
    }


def parse_hero_builds(hero_names: List[str], logger=None) -> List[Dict[str, Any]]:
    d2pt_session = make_d2pt_session()
    dotabuff_session = make_dotabuff_session()

    out: List[Dict[str, Any]] = []

    for hero_name in hero_names:
        try:
            d2pt = parse_d2pt_hero_build(d2pt_session, hero_name, logger=logger)
            out.append(asdict(d2pt))
        except Exception as e:
            _log(logger, "warning", f"[builds] d2pt parse failed for '{hero_name}': {e}")
            out.append(
                _error_record(
                    hero_name=hero_name,
                    hero_slug=_slugify_hero_name_for_d2pt(hero_name),
                    source="dota2protracker",
                    error=str(e),
                )
            )

        time.sleep(SLEEP_BETWEEN_REQUESTS_SECONDS)

        try:
            db = parse_dotabuff_hero_build(dotabuff_session, hero_name, logger=logger)
            out.append(asdict(db))
        except Exception as e:
            _log(logger, "warning", f"[builds] dotabuff parse failed for '{hero_name}': {e}")
            out.append(
                _error_record(
                    hero_name=hero_name,
                    hero_slug=_slugify_hero_name_for_dotabuff(hero_name),
                    source="dotabuff",
                    error=str(e),
                )
            )

        time.sleep(SLEEP_BETWEEN_REQUESTS_SECONDS)

    OUT_FILE_HERO_BUILDS.parent.mkdir(parents=True, exist_ok=True)

    with OUT_FILE_HERO_BUILDS.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=JSON_ENSURE_ASCII, indent=JSON_INDENT)

    _log(logger, "info", f"[builds] saved {len(out)} records to {OUT_FILE_HERO_BUILDS}")
    return out


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("hero-builds")

    parse_hero_builds(
        ["Anti-Mage", "Juggernaut", "Invoker"],
        logger=log,
    )