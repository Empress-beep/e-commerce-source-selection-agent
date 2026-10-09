from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Settings:
    profile_name: str = "generic"
    base_url: str = ""
    entry_url: str = ""
    bootstrap_url: str = ""
    list_url: str = ""
    detail_url: str = ""
    entry_method: str = "GET"
    bootstrap_method: str = "GET"
    list_method: str = "GET"
    detail_method: str = "GET"
    list_body_kind: str = "query"
    transport_field: str = ""
    sign_field: str = "sign"
    timestamp_field: str = "timestamp"
    page_param: str = "page"
    category_param: str = "category"
    category_value: str = ""
    use_compact_json: bool = True
    sign_before_timestamp: bool = True
    sign_on_raw_payload: bool = True
    page_start: int = 1
    page_size: int = 20
    timeout: float = 20.0
    sleep_seconds: float = 0.2
    max_retries: int = 2
    output_json: str = "output/items.json"
    output_csv: str = "output/items.csv"
    base_headers: dict[str, str] = field(
        default_factory=lambda: {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
        }
    )
    entry_headers: dict[str, str] = field(default_factory=dict)
    bootstrap_headers: dict[str, str] = field(default_factory=dict)
    list_headers: dict[str, str] = field(default_factory=dict)
    detail_headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    base_payload: dict[str, object] = field(default_factory=dict)
