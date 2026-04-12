from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup, NavigableString, Tag

from scripts.core.CONSTANTS import (
    DEFAULT_HEADERS,
    DEFAULT_MAX_ITEMS,
    DEFAULT_HERO_BUILDS_DIR,
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
class StartingItemSet:
    items: List[str]
    matches: Optional[str] = None
    winrate: Optional[str] = None


@dataclass
class HeroBuild:
    hero: str
    hero_slug: str
    source: str
    role: Optional[str]
    patch: Optional[str]
    starting_items: List[StartingItemSet]
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


def _hero_slug(hero_name: str) -> str:
    return hero_name.strip()


def _hero_url_part(hero_name: str) -> str:
    return quote(hero_name.strip(), safe="")


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


def _clean_item_name(name: str) -> str:
    name = _collapse_ws(name).replace("#|n|#", "").strip()
    name = re.sub(r"^Image:\s*", "", name, flags=re.IGNORECASE).strip()

    words = name.split()
    if len(words) % 2 == 0 and words:
        half = len(words) // 2
        if words[:half] == words[half:]:
            name = " ".join(words[:half])

    return name


def _is_bad_item_name(name: str) -> bool:
    if not name:
        return True

    name = _collapse_ws(name)

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
        "Table View",
        "Normal View",
        "Table View Normal View",
        "Stats of all Items for this Build",
        "More Items",
        "Main Items",
        "Other Items",
        "Loading…",
        "Loading...",
        "CORE",
        "Different common options",
        "Builds Meta Analysis Matchups & Synergies Item Stats Off-Meta Builds",
    }
    if name in bad_exact:
        return True

    if name.startswith("Show Core Items"):
        return True

    if "purchase rate" in name.lower():
        return True

    if name.startswith("#|n|#"):
        return True

    if re.fullmatch(r"\d+(\.\d+)?%", name):
        return True

    if re.fullmatch(r"\d[\d,]*", name):
        return True

    if re.search(r"\b\d+m\b", name, flags=re.IGNORECASE):
        return True

    if re.fullmatch(r"\d{1,2}:\d{2}", name):
        return True

    if "matches" in name.lower() and "win rate" in name.lower():
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


def _dedupe_names(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _looks_like_time(line: str) -> bool:
    line = _collapse_ws(line)
    return bool(
        re.search(r"\b\d+m\b", line, flags=re.IGNORECASE)
        or re.fullmatch(r"\d{1,2}:\d{2}", line)
    )


def _looks_like_percent(line: str) -> bool:
    return bool(re.fullmatch(r"\d+(\.\d+)?%", _collapse_ws(line)))


def _looks_like_matches(line: str) -> bool:
    return bool(
        re.fullmatch(r"\d[\d,]*\s+matches", _collapse_ws(line), flags=re.IGNORECASE)
    )


def _parse_starting_stats_line(line: str) -> tuple[Optional[str], Optional[str]]:
    line = _collapse_ws(line)
    m = re.search(
        r"(\d[\d,]*)\s+matches.*?(\d+(?:\.\d+)?)%\s+win\s*rate",
        line,
        flags=re.IGNORECASE,
    )
    if not m:
        return None, None
    return f"{m.group(1)} matches", f"{m.group(2)}%"


def _extract_patch(page_text: str) -> Optional[str]:
    m = re.search(r"Patch\s+([0-9.]+[a-z]?)", page_text, flags=re.IGNORECASE)
    return m.group(1) if m else None


def _extract_role(page_text: str) -> Optional[str]:
    m = re.search(
        r"stats for .*?\b(Hard Support|Support|Offlane|Mid|Carry)\b",
        page_text,
        flags=re.IGNORECASE,
    )
    return _collapse_ws(m.group(1)) if m else None


def _extract_item_stats_section(lines: List[str]) -> List[str]:
    item_stats_indices = [
        i for i, line in enumerate(lines)
        if _collapse_ws(line).lower() == "item stats"
    ]
    if not item_stats_indices:
        return []

    start_idx = item_stats_indices[-1] + 1
    end_markers = {
        "Matchups & Synergies",
        "Off-Meta Builds",
        "Abilities & Talents",
        "Neutral Items",
        "Lategame",
    }

    section: List[str] = []
    for line in lines[start_idx:]:
        if line in end_markers:
            break
        section.append(line)

    return section


def _find_heading_tag(soup: BeautifulSoup, heading_text: str) -> Optional[Tag]:
    pattern = re.compile(rf"^\s*{re.escape(heading_text)}\s*$", flags=re.IGNORECASE)
    for tag in soup.find_all(True):
        text = _collapse_ws(tag.get_text(" ", strip=True))
        if pattern.fullmatch(text):
            return tag
    return None


def _collect_section_nodes(
    start_tag: Tag,
    end_heading_texts: List[str],
) -> List[Tag]:
    end_texts = {x.lower() for x in end_heading_texts}
    nodes: List[Tag] = []

    for sib in start_tag.next_siblings:
        if isinstance(sib, NavigableString):
            continue
        if not isinstance(sib, Tag):
            continue

        sib_text = _collapse_ws(sib.get_text(" ", strip=True)).lower()
        if sib_text in end_texts:
            break

        # иногда следующий заголовок вложен глубже
        heading_like = sib.find(
            lambda t: isinstance(t, Tag)
            and re.fullmatch(r"h[1-6]", t.name or "", flags=re.IGNORECASE)
            and _collapse_ws(t.get_text(" ", strip=True)).lower() in end_texts
        )
        if heading_like is not None:
            break

        nodes.append(sib)

    return nodes


def _extract_starting_items_from_html(soup: BeautifulSoup) -> List[StartingItemSet]:
    start_tag = _find_heading_tag(soup, "Starting Items")
    if start_tag is None:
        return []

    section_nodes = _collect_section_nodes(
        start_tag,
        end_heading_texts=["Core Item Build", "Item Stats", "Neutral Items"],
    )
    if not section_nodes:
        return []

    sets: List[StartingItemSet] = []
    current_items: List[str] = []
    current_matches: Optional[str] = None
    current_winrate: Optional[str] = None

    def flush_current():
        nonlocal current_items, current_matches, current_winrate
        current_items = _dedupe_names(
            [_clean_item_name(x) for x in current_items if not _is_bad_item_name(_clean_item_name(x))]
        )
        if current_items:
            sets.append(
                StartingItemSet(
                    items=current_items,
                    matches=current_matches,
                    winrate=current_winrate,
                )
            )
        current_items = []
        current_matches = None
        current_winrate = None

    for node in section_nodes:
        # 1) сначала пробуем собрать статистику с текста блока
        node_lines = [
            _collapse_ws(x)
            for x in node.get_text("\n", strip=True).splitlines()
            if _collapse_ws(x)
        ]
        for line in node_lines:
            matches, winrate = _parse_starting_stats_line(line)
            if matches or winrate:
                if current_items:
                    flush_current()
                current_matches = matches
                current_winrate = winrate

        # 2) достаем item names из img alt/title/data-* и ссылок
        extracted_names: List[str] = []

        for img in node.find_all("img"):
            candidates = [
                img.get("alt"),
                img.get("title"),
                img.get("aria-label"),
                img.get("data-tip"),
                img.get("data-original-title"),
            ]
            for cand in candidates:
                cand = _clean_item_name(cand or "")
                if cand and not _is_bad_item_name(cand):
                    extracted_names.append(cand)
                    break

        for tag in node.find_all(True):
            for attr in ("title", "aria-label", "data-tip", "data-original-title"):
                val = _clean_item_name(tag.get(attr, "") or "")
                if val and not _is_bad_item_name(val):
                    extracted_names.append(val)

        # fallback: иногда название предмета лежит только текстом внутри элемента
        for line in node_lines:
            cleaned = _clean_item_name(line)
            if cleaned and not _is_bad_item_name(cleaned):
                # не забираем строки статистики
                if _parse_starting_stats_line(cleaned) != (None, None):
                    continue
                extracted_names.append(cleaned)

        extracted_names = _dedupe_names(extracted_names)

        # отсекаем строки статистики, если случайно попали
        extracted_names = [
            x for x in extracted_names
            if _parse_starting_stats_line(x) == (None, None)
        ]

        if extracted_names:
            current_items.extend(extracted_names)

    if current_items:
        flush_current()

    return sets[:3]


def _parse_items_from_lines(lines: List[str]) -> tuple[List[BuildItem], List[BuildItem]]:
    ignored = {
        "Stats of all Items for this Build",
        "Table View",
        "Normal View",
        "Table View Normal View",
        "Show Core Items (≥50% purchase rate)",
        "Show Core Items (>=50% purchase rate)",
        "Show Core Items (â¥50% purchase rate)",
        "Item",
        "Purchase Rate",
        "Average Time",
        "Loading…",
        "Loading...",
    }

    cleaned = []
    for x in lines:
        x = _collapse_ws(x)
        if not x:
            continue
        if x in ignored:
            continue
        if x.startswith("Show Core Items"):
            continue
        cleaned.append(x)

    core_items: List[BuildItem] = []
    situational_items: List[BuildItem] = []

    i = 0
    while i < len(cleaned):
        name = _clean_item_name(cleaned[i])

        if (
            not name
            or name == "CORE"
            or _looks_like_percent(name)
            or _looks_like_time(name)
            or _looks_like_matches(name)
            or _is_bad_item_name(name)
        ):
            i += 1
            continue

        j = i + 1
        is_core = False
        purchase_rate = None
        timing = None

        if j < len(cleaned) and cleaned[j] == "CORE":
            is_core = True
            j += 1
        elif j < len(cleaned) and _looks_like_percent(cleaned[j]):
            purchase_rate = cleaned[j]
            j += 1

        if j < len(cleaned) and _looks_like_time(cleaned[j]):
            timing = cleaned[j]
            j += 1

        item = BuildItem(
            name=name,
            timing=timing,
            purchase_rate=purchase_rate,
            is_core=is_core,
        )

        if is_core:
            core_items.append(item)
        else:
            situational_items.append(item)

        i = j

    return _dedupe_items(core_items), _dedupe_items(situational_items)


def parse_d2pt_hero_build(session, hero_name: str, logger=None) -> HeroBuild:
    hero_slug = _hero_slug(hero_name)
    hero_url_part = _hero_url_part(hero_name)
    url = DOTA2PROTRACKER_HERO_URL.format(hero_slug=hero_url_part)

    html = _request_html(session, url, logger=logger)
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text("\n", strip=True)
    lines = [_collapse_ws(x) for x in page_text.splitlines() if _collapse_ws(x)]

    patch = _extract_patch(page_text)
    role = _extract_role(page_text)

    starting_items = _extract_starting_items_from_html(soup)

    item_stats_section = _extract_item_stats_section(lines)
    core_items, situational_items = _parse_items_from_lines(item_stats_section)

    core_items = core_items[:DEFAULT_MAX_ITEMS]
    situational_items = situational_items[:DEFAULT_MAX_ITEMS]

    if not core_items and situational_items:
        core_items = [
            BuildItem(
                name=item.name,
                timing=item.timing,
                purchase_rate=item.purchase_rate,
                is_core=True,
            )
            for item in situational_items[:6]
        ]

    _log(
        logger,
        "info",
        f"[builds] hero={hero_name} patch={patch} "
        f"starting={len(starting_items)} core={len(core_items)} situational={len(situational_items)}",
    )

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
                    hero_slug=_hero_slug(hero_name),
                    source="dota2protracker",
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

    with open(DEFAULT_HERO_BUILDS_DIR, "r", encoding="utf-8") as f:
        hero_map = dict(json.load(f))
        print(hero_map)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("hero-builds")

    parse_hero_builds(
        list(hero_map.values()),
        logger=log,
    )