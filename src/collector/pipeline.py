from __future__ import annotations

import json


def stable_key(item: dict) -> str:
    for candidate in ("id", "bm", "uuid", "code"):
        if candidate in item:
            return f"{candidate}:{item[candidate]}"
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def normalize_items(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for item in items:
        key = stable_key(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
