from __future__ import annotations

from typing import Any


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return _dict_items(payload)
    if isinstance(payload, dict):
        for key in ("list", "items", "rows", "records", "result", "data"):
            items = _dict_items(payload.get(key))
            if items:
                return items
            nested = payload.get(key)
            if isinstance(nested, dict):
                child = extract_items(nested)
                if child:
                    return child
    return []
