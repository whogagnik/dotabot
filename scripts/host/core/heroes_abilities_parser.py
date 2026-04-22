from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from scripts.host.core.config import (
    DEFAULT_HEADERS,
    HTTP_BACKOFF_BASE_SECONDS,
    HTTP_RETRIES,
    HTTP_TIMEOUT_SECONDS,
    JSON_ENSURE_ASCII,
    JSON_INDENT,
    SLEEP_BETWEEN_REQUESTS_SECONDS,
    DOTABUFF_REFERER,
    DOTABUFF_HERO_BUILDS_URL,
    DOTABUFF_HERO_ABILITIES_URL,
    DEFAULT_ID_HERO_TO_NAME,
    DEFAULT_OUT,
)

try:
    import cloudscraper
except ImportError as e:
    raise RuntimeError(
        "Нужно установить зависимости: pip install cloudscraper beautifulsoup4"
    ) from e


@dataclass
class AbilityInfo:
    slot: Optional[str]
    name: str
    ability_type_raw: Optional[str] = None
    target_kind: Optional[str] = None
    affects: Optional[str] = None
    damage_type: Optional[str] = None
    dispellable: Optional[str] = None
    pierces_debuff_immunity: Optional[str] = None
    description: Optional[str] = None
    extra: Dict[str, str] = field(default_factory=dict)


@dataclass
class AbilityBuild:
    build_rate: Optional[str]
    winrate: Optional[str]
    levels: Dict[str, List[int]]
    talents: Dict[int, str]


@dataclass
class HeroAbilitiesBuilds:
    hero: str
    hero_slug: str
    source: str
    builds_url: str
    abilities_url: str
    abilities: List[AbilityInfo]
    most_popular_builds: List[AbilityBuild]
    # Через get_text() тут надежно хранить только slot -> list[pick_rates]
    point_choices_by_level: Dict[str, List[str]]


def _log(logger, level: str, msg: str) -> None:
    if logger is None:
        return
    fn = getattr(logger, level, None)
    if callable(fn):
        fn(msg)


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


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
            response = session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            text = response.text or ""

            if response.status_code in (403, 429, 500, 502, 503, 504):
                wait = min(HTTP_BACKOFF_BASE_SECONDS * attempt, 8.0)
                snippet = text[:300].replace("\n", " ")
                _log(
                    logger,
                    "warning",
                    f"[dotabuff] status={response.status_code} for {url}; retry in {wait:.1f}s; body={snippet}",
                )
                time.sleep(wait)
                continue

            response.raise_for_status()

            if _looks_like_cloudflare_block(text):
                wait = min(HTTP_BACKOFF_BASE_SECONDS * attempt, 8.0)
                _log(
                    logger,
                    "warning",
                    f"[dotabuff] cloudflare page for {url}; retry in {wait:.1f}s",
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
                f"[dotabuff] request failed for {url}: {e}; retry in {wait:.1f}s",
            )
            time.sleep(wait)

    raise RuntimeError(f"Failed to fetch {url}") from last_err


def _dotabuff_hero_slug(hero_name: str) -> str:
    slug = hero_name.strip().lower()
    slug = unicodedata.normalize("NFKD", slug)
    slug = slug.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[’'`]", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug


def _extract_lines(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [
        _collapse_ws(x)
        for x in soup.get_text("\n", strip=True).splitlines()
        if _collapse_ws(x)
    ]


def _is_slot_token(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]", _collapse_ws(text)))


def _is_int_token(text: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}", _collapse_ws(text)))


def _is_unsigned_percent(text: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?%", _collapse_ws(text)))


def _is_signed_percent(text: str) -> bool:
    return bool(re.fullmatch(r"[+-]\d+(?:\.\d+)?%", _collapse_ws(text)))


def _looks_like_ability_name(text: str) -> bool:
    text = _collapse_ws(text)
    if not text:
        return False

    bad_names = {
        "DOTABUFF",
        "Dotabuff",
        "Home",
        "Esports",
        "Heroes",
        "Items",
        "Players",
        "Matches",
        "Blog",
        "Forums",
        "Plus",
        "Overview",
        "Guides",
        "Abilities",
        "Ability Builds",
        "More",
        "Counters",
        "Clips",
        "Trends",
        "Player Rankings",
        "Hero Attributes",
        "Hero Talents Talent Tree",
        "Hero Talents",
        "Talent Tree",
        "Dotabuff Plus",
        "Popularity",
        "Win Rate",
        "First Point At Level",
        "Ability Maxed at Level",
        "Talent Trends",
        "Most Popular Priorities",
        "Most Popular Builds",
    }
    if text in bad_names:
        return False
    if _is_slot_token(text):
        return False
    if _is_int_token(text):
        return False
    if _is_unsigned_percent(text) or _is_signed_percent(text):
        return False
    if text.endswith(":"):
        return False
    return True


def _normalize_target_kind(
    ability_type_raw: Optional[str], extra: Dict[str, str]
) -> str:
    ability_type = _collapse_ws(ability_type_raw or "").lower()

    if not ability_type:
        return "unknown"
    if "passive" in ability_type:
        return "passive"
    if "unit target" in ability_type:
        return "unit"
    if "point target" in ability_type:
        if "RADIUS" in extra or "EXPLOSION RADIUS" in extra or "EFFECT RADIUS" in extra:
            return "area"
        return "point"
    if "no target" in ability_type:
        return "no_target"
    if "toggle" in ability_type:
        return "toggle"
    if "vector target" in ability_type:
        return "vector"
    return "unknown"


def _consume_stat_block(lines: List[str], i: int) -> tuple[Dict[str, str], int]:
    stat_keys = {
        "ABILITY",
        "AFFECTS",
        "DAMAGE TYPE",
        "DISPELLABLE",
        "PIERCES DEBUFF IMMUNITY",
        "DISPEL TYPE",
        "RECENT KILL WINDOW",
        "BASE BONUS GOLD",
        "EXTRA BONUS GOLD",
        "BOUNTY RUNE MULTIPLIER",
        "MAX BONUS GOLD PER KILL",
        "RADIUS",
        "DURATION",
        "DAMAGE PER SECOND",
        "ARMOR REDUCTION",
        "MAX STUN",
        "MAX DAMAGE",
        "EXPLOSION RADIUS",
        "MOVE SPEED BONUS",
        "MAX STACKS",
        "DEBUFF DURATION",
        "MOVEMENT SLOW PER STACK",
        "BASE ATTACK DAMAGE REDUCTION PER STACK",
        "ATTACK SPEED",
        "HP REGEN",
        "MOVE SPEED",
        "BASE ATTACK TIME",
        "BONUS HEALTH REGEN",
        "BONUS MOVE SPEED",
    }

    extra: Dict[str, str] = {}

    while i < len(lines):
        key = _collapse_ws(lines[i])

        if key in {
            "Hero Talents Talent Tree",
            "Hero Talents",
            "Talent Tree",
            "Hero Attributes",
            "Dotabuff Plus",
        }:
            break

        if _looks_like_ability_name(key) and i + 1 < len(lines):
            nxt = _collapse_ws(lines[i + 1])
            if nxt == "Innate Ability" or _is_slot_token(nxt):
                break

        if not key.endswith(":"):
            break

        stat_key = key[:-1].strip()
        if stat_key not in stat_keys:
            break

        i += 1
        values: List[str] = []

        while i < len(lines):
            cur = _collapse_ws(lines[i])

            if cur in {
                "Hero Talents Talent Tree",
                "Hero Talents",
                "Talent Tree",
                "Hero Attributes",
                "Dotabuff Plus",
            }:
                break

            if cur.endswith(":") and cur[:-1].strip() in stat_keys:
                break

            if _looks_like_ability_name(cur) and i + 1 < len(lines):
                nxt = _collapse_ws(lines[i + 1])
                if nxt == "Innate Ability" or _is_slot_token(nxt):
                    break

            values.append(cur)
            i += 1

        extra[stat_key] = " ".join(values).strip()

    return extra, i


def _parse_abilities_page(html: str) -> List[AbilityInfo]:
    lines = _extract_lines(html)
    abilities: List[AbilityInfo] = []

    start_idx = None
    for idx, line in enumerate(lines):
        if _collapse_ws(line) == "Player Rankings":
            start_idx = idx + 1
            break

    if start_idx is None:
        return []

    i = start_idx
    while i < len(lines):
        cur = _collapse_ws(lines[i])

        if cur in {
            "Hero Talents Talent Tree",
            "Hero Talents",
            "Talent Tree",
            "Hero Attributes",
            "Dotabuff Plus",
        }:
            break

        if not _looks_like_ability_name(cur):
            i += 1
            continue

        if i + 1 >= len(lines):
            i += 1
            continue

        nxt = _collapse_ws(lines[i + 1])
        if nxt != "Innate Ability" and not _is_slot_token(nxt):
            i += 1
            continue

        name = cur
        slot = "innate" if nxt == "Innate Ability" else nxt
        i += 2

        description_parts: List[str] = []

        while i < len(lines):
            token = _collapse_ws(lines[i])

            if token in {
                "Hero Talents Talent Tree",
                "Hero Talents",
                "Talent Tree",
                "Hero Attributes",
                "Dotabuff Plus",
            }:
                break

            if token.endswith(":"):
                break

            if _looks_like_ability_name(token) and i + 1 < len(lines):
                probe = _collapse_ws(lines[i + 1])
                if probe == "Innate Ability" or _is_slot_token(probe):
                    break

            if (
                token
                and not _is_int_token(token)
                and not _is_unsigned_percent(token)
                and not _is_signed_percent(token)
            ):
                description_parts.append(token)

            i += 1

        stats, i = _consume_stat_block(lines, i)

        ability_type_raw = stats.pop("ABILITY", None)
        affects = stats.pop("AFFECTS", None)
        damage_type = stats.pop("DAMAGE TYPE", None)
        dispellable = stats.pop("DISPELLABLE", None)
        pierces = stats.pop("PIERCES DEBUFF IMMUNITY", None)

        abilities.append(
            AbilityInfo(
                slot=slot,
                name=name,
                ability_type_raw=ability_type_raw,
                target_kind=_normalize_target_kind(ability_type_raw, stats),
                affects=affects,
                damage_type=damage_type,
                dispellable=dispellable,
                pierces_debuff_immunity=pierces,
                description=" ".join(description_parts).strip() or None,
                extra=stats,
            )
        )

    return abilities


def _cleanup_abilities(abilities: List[AbilityInfo]) -> List[AbilityInfo]:
    cleaned: List[AbilityInfo] = []

    for ab in abilities:
        if not ab.name:
            continue

        if ab.affects:
            ab.affects = re.sub(
                r"\b(Allied Units)(?:\s+\1)+\b",
                r"\1",
                ab.affects,
            ).strip()

        if ab.dispellable:
            ab.dispellable = re.sub(
                r"\b(Strong Dispels Only|Cannot be dispelled|Yes|No)(?:\s+\1)+\b",
                r"\1",
                ab.dispellable,
            ).strip()

        for field_name in [
            "affects",
            "damage_type",
            "dispellable",
            "pierces_debuff_immunity",
        ]:
            val = getattr(ab, field_name)
            if not val:
                continue

            val = val.split(" Alchemist ", 1)[0]
            val = val.split(" Sprays ", 1)[0]
            val = val.split(" A powerful solvent ", 1)[0]
            val = val.split(" A silver lining ", 1)[0]
            val = val.split(" After Razzil", 1)[0]
            val = val.split(" The brew ", 1)[0]
            setattr(ab, field_name, val.strip())

        new_extra: Dict[str, str] = {}
        for k, v in ab.extra.items():
            if not v:
                continue

            v = v.split("Hero Talents Talent Tree", 1)[0]
            v = v.split("Hero Talents", 1)[0]
            v = v.split("Talent Tree", 1)[0]
            v = v.split(" While it is not ", 1)[0]
            v = v.split(" Using traditional ", 1)[0]
            v = v.split(" A silver lining ", 1)[0]
            v = v.split(" A powerful solvent ", 1)[0]
            v = v.split(" After Razzil", 1)[0]
            v = v.split(" The brew ", 1)[0]
            new_extra[k] = v.strip()

        ab.extra = new_extra
        cleaned.append(ab)

    return cleaned


def _repair_build_levels(
    levels: Dict[str, List[int]], talents: Dict[int, str]
) -> Dict[str, List[int]]:
    fixed = {k: sorted(dict.fromkeys(v)) for k, v in levels.items()}

    talent_levels = {int(x) for x in talents.keys()}

    for slot, vals in list(fixed.items()):
        fixed[slot] = [v for v in vals if v not in talent_levels]

    # Точечный фикс для broken plain-text:
    # E 10 12 14 15 / R 6 13 18 16 11
    # ->
    # E [12,14] R [6,11,13,18]
    # переносим 11 назад в E
    if "E" in fixed and "R" in fixed:
        e_vals = fixed["E"]
        r_vals = fixed["R"]

        if len(e_vals) in {2, 3} and len(r_vals) == 4:
            extra_r = [x for x in r_vals if x not in {6, 12, 13, 18}]
            if extra_r:
                moved = min(extra_r)
                fixed["R"] = [x for x in r_vals if x != moved]
                fixed["E"] = sorted(dict.fromkeys(e_vals + [moved]))

    return {k: sorted(dict.fromkeys(v)) for k, v in fixed.items() if v}


def _parse_most_popular_builds(html: str) -> List[AbilityBuild]:
    lines = _extract_lines(html)

    start_idx = None
    for idx, line in enumerate(lines):
        if _collapse_ws(line) == "Most Popular Builds":
            start_idx = idx + 1
            break

    if start_idx is None:
        return []

    section: List[str] = []
    i = start_idx
    while i < len(lines):
        cur = _collapse_ws(lines[i])
        if cur in {"Languages", "Dotabuff", "Copyright"}:
            break
        section.append(cur)
        i += 1

    builds: List[AbilityBuild] = []
    i = 0
    talent_levels = {10, 15, 16, 20, 25}

    while i < len(section):
        while i < len(section) and not _is_slot_token(section[i]):
            i += 1

        if i >= len(section):
            break

        levels: Dict[str, List[int]] = {}
        talents: Dict[int, str] = {}
        build_rate: Optional[str] = None
        winrate: Optional[str] = None
        current_slot: Optional[str] = None

        while i < len(section):
            token = _collapse_ws(section[i])

            if token == "Build Rate":
                if i > 0 and _is_unsigned_percent(section[i - 1]):
                    build_rate = _collapse_ws(section[i - 1])
                i += 1
                continue

            if token == "Win Rate":
                if i > 0 and _is_unsigned_percent(section[i - 1]):
                    winrate = _collapse_ws(section[i - 1])
                i += 1
                break

            if _is_slot_token(token):
                current_slot = token
                levels.setdefault(current_slot, [])
                i += 1
                continue

            if _is_int_token(token):
                lvl = int(token)

                if lvl in talent_levels:
                    talents.setdefault(lvl, "")
                    i += 1
                    continue

                if current_slot is not None:
                    levels[current_slot].append(lvl)

                i += 1
                continue

            i += 1

        levels = _repair_build_levels(levels, talents)

        if levels or talents or build_rate or winrate:
            builds.append(
                AbilityBuild(
                    build_rate=build_rate,
                    winrate=winrate,
                    levels=levels,
                    talents=talents,
                )
            )

        if len(builds) >= 5:
            break

    return builds


def _parse_point_choices_by_level(html: str) -> Dict[str, List[str]]:
    lines = _extract_lines(html)

    start_idx = None
    for idx, line in enumerate(lines):
        if _collapse_ws(line) == "First Point At Level":
            start_idx = idx + 1
            break

    if start_idx is None:
        return {}

    i = start_idx
    while i < len(lines) and _is_int_token(lines[i]):
        i += 1

    section: List[str] = []
    while i < len(lines):
        cur = _collapse_ws(lines[i])

        if cur.startswith(
            "Pick and Win rate change for each ability based on when the first point is spent."
        ):
            break
        if cur == "Ability Maxed at Level":
            break

        section.append(cur)
        i += 1

    out: Dict[str, List[str]] = {}

    j = 0
    while j < len(section):
        token = _collapse_ws(section[j])

        if not _is_slot_token(token):
            j += 1
            continue

        slot = token
        j += 1

        picks: List[str] = []
        while j < len(section):
            cur = _collapse_ws(section[j])

            if _is_slot_token(cur):
                break

            if _is_unsigned_percent(cur):
                picks.append(cur)

            j += 1

        out[slot] = picks

    return out


def parse_dotabuff_hero_abilities_builds(
    session, hero_name: str, logger=None
) -> HeroAbilitiesBuilds:
    hero_slug = _dotabuff_hero_slug(hero_name)
    builds_url = DOTABUFF_HERO_BUILDS_URL.format(hero_slug=hero_slug)
    abilities_url = DOTABUFF_HERO_ABILITIES_URL.format(hero_slug=hero_slug)

    builds_html = _request_html(session, builds_url, logger=logger)
    abilities_html = _request_html(session, abilities_url, logger=logger)

    abilities = _cleanup_abilities(_parse_abilities_page(abilities_html))
    most_popular_builds = _parse_most_popular_builds(builds_html)
    point_choices_by_level = _parse_point_choices_by_level(builds_html)

    _log(
        logger,
        "debug",
        f"[dotabuff] hero={hero_name} slug={hero_slug} abilities={len(abilities)} builds={len(most_popular_builds)} point_choices={len(point_choices_by_level)}",
    )

    return HeroAbilitiesBuilds(
        hero=hero_name,
        hero_slug=hero_slug,
        source="dotabuff",
        builds_url=builds_url,
        abilities_url=abilities_url,
        abilities=abilities,
        most_popular_builds=most_popular_builds,
        point_choices_by_level=point_choices_by_level,
    )


def debug_dump_builds_sections(hero_name: str, logger=None) -> None:
    session = make_dotabuff_session()
    hero_slug = _dotabuff_hero_slug(hero_name)
    url = DOTABUFF_HERO_BUILDS_URL.format(hero_slug=hero_slug)
    html = _request_html(session, url, logger=logger)
    lines = _extract_lines(html)

    print("=" * 100)
    print(f"DEBUG BUILDS | hero={hero_name} | slug={hero_slug}")
    print("=" * 100)

    markers = {
        "First Point At Level",
        "Ability Maxed at Level",
        "Talent Trends",
        "Most Popular Priorities",
        "Most Popular Builds",
    }

    for idx, line in enumerate(lines):
        if line in markers or line == "Build Rate" or line == "Win Rate":
            print(f"{idx:04d}: {line}")

    print("\n--- BLOCK: First Point At Level ---")
    try:
        start = lines.index("First Point At Level")
        for idx in range(start, min(start + 80, len(lines))):
            print(f"{idx:04d}: {lines[idx]}")
    except ValueError:
        print("NOT FOUND")

    print("\n--- BLOCK: Most Popular Builds ---")
    try:
        start = lines.index("Most Popular Builds")
        for idx in range(start, min(start + 140, len(lines))):
            print(f"{idx:04d}: {lines[idx]}")
    except ValueError:
        print("NOT FOUND")

    print("\n--- PARSED point_choices_by_level ---")
    parsed_points = _parse_point_choices_by_level(html)
    print(json.dumps(parsed_points, ensure_ascii=False, indent=2))

    print("\n--- PARSED most_popular_builds ---")
    parsed_builds = _parse_most_popular_builds(html)
    print(json.dumps([asdict(x) for x in parsed_builds], ensure_ascii=False, indent=2))


def debug_dump_abilities_sections(hero_name: str, logger=None) -> None:
    session = make_dotabuff_session()
    hero_slug = _dotabuff_hero_slug(hero_name)
    url = DOTABUFF_HERO_ABILITIES_URL.format(hero_slug=hero_slug)
    html = _request_html(session, url, logger=logger)
    lines = _extract_lines(html)

    print("=" * 100)
    print(f"DEBUG ABILITIES | hero={hero_name} | slug={hero_slug}")
    print("=" * 100)

    try:
        start = lines.index("Player Rankings")
    except ValueError:
        start = 0

    for idx in range(start, min(start + 260, len(lines))):
        print(f"{idx:04d}: {lines[idx]}")

    print("\n--- PARSED abilities ---")
    parsed = _cleanup_abilities(_parse_abilities_page(html))
    print(json.dumps([asdict(x) for x in parsed], ensure_ascii=False, indent=2))


def _error_record(hero_name: str, hero_slug: str, error: str) -> Dict[str, Any]:
    return {
        "hero": hero_name,
        "hero_slug": hero_slug,
        "source": "dotabuff",
        "builds_url": None,
        "abilities_url": None,
        "abilities": [],
        "most_popular_builds": [],
        "point_choices_by_level": {},
        "error": error,
    }


def parse_dotabuff_hero_abilities_from_map(
    hero_names: List[str],
    out_path: str | Path,
    logger=None,
) -> List[Dict[str, Any]]:
    session = make_dotabuff_session()
    out: List[Dict[str, Any]] = []

    for hero_name in hero_names:
        try:
            parsed = parse_dotabuff_hero_abilities_builds(
                session, hero_name, logger=logger
            )
            out.append(asdict(parsed))
        except Exception as e:
            _log(logger, "warning", f"[dotabuff] parse failed for '{hero_name}': {e}")
            out.append(_error_record(hero_name, _dotabuff_hero_slug(hero_name), str(e)))

        time.sleep(SLEEP_BETWEEN_REQUESTS_SECONDS)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=JSON_ENSURE_ASCII, indent=JSON_INDENT)

    _log(logger, "info", f"[dotabuff] saved {len(out)} records to {out_path}")
    return out


def parse_dotabuff_hero_abilities_from_id_file(
    id_hero_to_name_path: str | Path,
    out_path: str | Path,
    logger=None,
) -> List[Dict[str, Any]]:
    id_hero_to_name_path = Path(id_hero_to_name_path)

    with id_hero_to_name_path.open("r", encoding="utf-8") as f:
        hero_map = json.load(f)

    if not isinstance(hero_map, dict):
        raise ValueError(
            "id_hero_to_name.json must contain a JSON object: {id: hero_name}"
        )

    hero_names = list(hero_map.values())
    return parse_dotabuff_hero_abilities_from_map(
        hero_names=hero_names,
        out_path=out_path,
        logger=logger,
    )


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("dotabuff-abilities")

    DEBUG_MODE = True
    DEBUG_ONE_HERO = "Alchemist"

    if DEBUG_MODE:
        debug_dump_builds_sections(DEBUG_ONE_HERO, logger=log)
        print("\n" + "#" * 100 + "\n")
        debug_dump_abilities_sections(DEBUG_ONE_HERO, logger=log)
    else:
        parse_dotabuff_hero_abilities_from_id_file(
            id_hero_to_name_path=DEFAULT_ID_HERO_TO_NAME,
            out_path=DEFAULT_OUT,
            logger=log,
        )
