from __future__ import annotations

import json
from http.cookiejar import CookieJar
from typing import Any
from urllib import error, parse, request


class ProtocolClient:
    def __init__(self, default_headers: dict[str, str] | None = None) -> None:
        self.cookie_jar = CookieJar()
        self.opener = request.build_opener(request.HTTPCookieProcessor(self.cookie_jar))
        self.default_headers = default_headers or {}

    def seed_cookies(self, cookies: dict[str, str]) -> None:
        if cookies:
            cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
            self.default_headers["Cookie"] = cookie_header

    def cookies_as_dict(self) -> dict[str, str]:
        return {cookie.name: cookie.value for cookie in self.cookie_jar}

    def request_text(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        form_body: dict[str, object] | None = None,
        raw_body: str | bytes | None = None,
        timeout: float = 20.0,
    ) -> str:
        final_url = url
        if params:
            query = parse.urlencode(params, doseq=True)
            separator = "&" if "?" in url else "?"
            final_url = f"{url}{separator}{query}"

        merged_headers = dict(self.default_headers)
        if headers:
            merged_headers.update(headers)

        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            merged_headers.setdefault("Content-Type", "application/json")
        elif form_body is not None:
            data = parse.urlencode(form_body, doseq=True).encode("utf-8")
            merged_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif raw_body is not None:
            data = raw_body if isinstance(raw_body, bytes) else raw_body.encode("utf-8")

        http_request = request.Request(
            final_url,
            data=data,
            headers=merged_headers,
            method=method.upper(),
        )
        try:
            with self.opener.open(http_request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} for {final_url}: {body[:300]}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Network failure for {final_url}: {exc.reason}") from exc

    def request_json(self, url: str, **kwargs: Any) -> Any:
        return json.loads(self.request_text(url, **kwargs))
