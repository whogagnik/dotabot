import json
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

import requests

from scripts.core.config import OUT_FILE_PARSER_ITEMS


FANDOM_API = "https://dota2.fandom.com/api.php"
FANDOM_WIKI_BASE = "https://dota2.fandom.com/wiki/"

# Надёжный источник данных по предметам
DOTACONSTANTS_ITEMS_JSON = "https://raw.githubusercontent.com/odota/dotaconstants/master/build/items.json"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "dotabot-items-parser/1.0 (requests; contact: you@example.com)",
            "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
        }
    )
    return s


def _looks_like_json(text: str) -> bool:
    t = text.lstrip()
    return t.startswith("{") or t.startswith("[")


def api_get_json(
    session: requests.Session,
    url: str,
    params: Dict[str, Any],
    logger: Callable[[str], None],
    retries: int = 4,
) -> Dict[str, Any]:
    last_err: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, params=params, timeout=40)
            status = r.status_code
            text = r.text or ""

            if status in (429, 500, 502, 503, 504):
                wait = min(2.0 * attempt, 8.0)
                logger(f"[WARN] {status} от сервера. Повтор через {wait:.1f}s ...")
                time.sleep(wait)
                continue

            ct = (r.headers.get("Content-Type") or "").lower()
            if ("json" not in ct) and (not _looks_like_json(text)):
                snippet = text[:500].replace("\n", "\\n")
                raise RuntimeError(
                    f"Ожидали JSON, но получили Content-Type='{ct}', status={status}. "
                    f"Первые 500 символов: {snippet}"
                )

            try:
                return r.json()
            except Exception as e:
                snippet = text[:500].replace("\n", "\\n")
                raise RuntimeError(
                    f"Не удалось распарсить JSON (status={status}). Ответ: {snippet}"
                ) from e

        except Exception as e:
            last_err = e
            wait = min(1.5 * attempt, 6.0)
            logger(f"[WARN] Ошибка запроса: {e}. Повтор через {wait:.1f}s ...")
            time.sleep(wait)

    raise RuntimeError(f"Не удалось получить JSON после {retries} попыток") from last_err


def fetch_all_item_titles(
    session: requests.Session,
    logger: Callable[[str], None],
) -> List[str]:
    """
    Получаем список предметов из Fandom Category:Items.
    Используем только как источник названий и source_page.
    """
    titles: List[str] = []
    cmcontinue: Optional[str] = None

    while True:
        params = {
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "list": "categorymembers",
            "cmtitle": "Category:Items",
            "cmnamespace": 0,
            "cmlimit": 500,
            "redirects": 1,
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue

        data = api_get_json(session, FANDOM_API, params, logger=logger)
        query = data.get("query") or {}
        members = query.get("categorymembers") or []

        for m in members:
            t = m.get("title")
            if isinstance(t, str) and t.strip():
                titles.append(t.strip())

        cont = data.get("continue") or {}
        cmcontinue = cont.get("cmcontinue")
        if not cmcontinue:
            break

        time.sleep(0.2)

    seen = set()
    uniq: List[str] = []
    for t in titles:
        if t not in seen:
            seen.add(t)
            uniq.append(t)

    return uniq


def title_to_source_page(title: str) -> str:
    slug = title.replace(" ", "_")
    slug = urllib.parse.quote(slug, safe="():_-")
    return FANDOM_WIKI_BASE + slug


def load_dotaconstants_index(session: requests.Session) -> Dict[str, Dict[str, Any]]:
    """
    Возвращает индекс по internal key:
      blade_mail -> {...}
      recipe_blade_mail -> {...}
    """
    r = session.get(DOTACONSTANTS_ITEMS_JSON, timeout=60)
    r.raise_for_status()
    data = r.json()

    out: Dict[str, Dict[str, Any]] = {}
    for key, obj in data.items():
        if isinstance(obj, dict):
            out[str(key)] = obj
    return out


def build_display_name_map(all_items: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """
    Строит отображение:
      "Blade Mail" -> "blade_mail"
    """
    out: Dict[str, str] = {}
    for key, obj in all_items.items():
        dname = obj.get("dname")
        if isinstance(dname, str) and dname.strip():
            out[dname.strip()] = key
    return out


def item_from_dotaconstants(
    item_key: str,
    title: str,
    obj: Dict[str, Any],
    all_items: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    raw_components = obj.get("components") or []
    if not isinstance(raw_components, list):
        raw_components = []

    components_display: List[str] = []
    components_cost_sum = 0

    for comp_key in raw_components:
        if not isinstance(comp_key, str):
            continue

        comp_obj = all_items.get(comp_key) or {}
        comp_name = comp_obj.get("dname") or comp_obj.get("name") or comp_key
        components_display.append(str(comp_name))

        comp_cost = comp_obj.get("cost")
        if isinstance(comp_cost, (int, float)):
            components_cost_sum += int(comp_cost)

    item_cost = obj.get("cost")
    item_cost_int = int(item_cost) if isinstance(item_cost, (int, float)) else None

    recipe_key = f"recipe_{item_key}"
    recipe_obj = all_items.get(recipe_key)
    has_recipe = recipe_obj is not None

    if recipe_obj is not None:
        recipe_name = recipe_obj.get("dname") or recipe_obj.get("name") or f"Recipe: {title}"
        components_display.append(str(recipe_name))

    if (
        recipe_obj is None
        and item_cost_int is not None
        and components_cost_sum > 0
        and item_cost_int > components_cost_sum
    ):
        has_recipe = True
        components_display.append(f"Recipe: {title}")

    return {
        "title": title,
        "cost": item_cost_int,
        "components": components_display,
        "has_recipe": has_recipe,
        "source_page": title_to_source_page(title),
        "error": None,
    }


def fallback_from_dotaconstants_only(
    session: requests.Session,
    logger: Callable[[str], None],
) -> List[Dict[str, Any]]:
    """
    Если Fandom API для списка предметов упал, просто отдаём все предметы из dotaconstants.
    """
    logger("Пробуем резервный источник dotaconstants/items.json ...")
    dc_index = load_dotaconstants_index(session)

    results: List[Dict[str, Any]] = []
    for item_key, obj in sorted(dc_index.items(), key=lambda kv: kv[0].lower()):
        if item_key.startswith("recipe_"):
            continue

        title = obj.get("dname") or obj.get("name") or item_key
        if not isinstance(title, str):
            continue

        results.append(item_from_dotaconstants(item_key, title, obj, dc_index))

    results.sort(key=lambda x: x["title"].lower())
    return results


def parse_items(logger: Callable[[str], None]) -> None:
    log = logger
    session = make_session()

    log("1) Получаю список предметов из Fandom Category:Items ...")
    try:
        titles = fetch_all_item_titles(session, logger=log)
    except Exception as e:
        log(f"[ERROR] Не смог получить список из Fandom API: {e}")
        items = fallback_from_dotaconstants_only(session, logger=log)
        with open(OUT_FILE_PARSER_ITEMS, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        log(f"Готово (fallback). Сохранено в: {OUT_FILE_PARSER_ITEMS}")
        return

    log(f"Найдено страниц: {len(titles)}")
    log("2) Загружаю данные предметов из dotaconstants ...")

    try:
        dc_index = load_dotaconstants_index(session)
        display_to_key = build_display_name_map(dc_index)
    except Exception as e:
        log(f"[ERROR] Не смог получить dotaconstants/items.json: {e}")
        raise

    log("3) Собираю итоговый JSON ...")
    results: List[Dict[str, Any]] = []

    for i, title in enumerate(titles, 1):
        item: Dict[str, Any] = {
            "title": title,
            "cost": None,
            "components": [],
            "has_recipe": False,
            "source_page": title_to_source_page(title),
            "error": None,
        }

        item_key = display_to_key.get(title)
        if item_key is None:
            item["error"] = "Не найден в dotaconstants/items.json"
        else:
            obj = dc_index[item_key]
            item = item_from_dotaconstants(item_key, title, obj, dc_index)

        results.append(item)

        if i % 25 == 0:
            log(f"  ... {i}/{len(titles)}")

    log("4) Сохраняю JSON ...")
    with open(OUT_FILE_PARSER_ITEMS, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    log(f"Готово. Сохранено в: {OUT_FILE_PARSER_ITEMS}")


if __name__ == "__main__":
    parse_items(logger=print)