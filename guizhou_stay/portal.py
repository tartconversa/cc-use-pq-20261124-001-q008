from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from pydantic import ValidationError

from guizhou_stay.contracts import Page, SessionOpen, Supply
from guizhou_stay.digest import digest_text
from guizhou_stay.errors import DomainError, invalid
from guizhou_stay.html_cards import extract_page


class CaptureFailure(DomainError):
    def __init__(self, code: str, message: str, *, retryable: bool, details=None):
        super().__init__(503 if retryable else 422, code, message, details)
        self.retryable = retryable


@dataclass(frozen=True)
class Capture:
    html: str
    url: str
    page: Page

    def receipt(self) -> dict:
        return {
            "url": self.url,
            "html_sha256": digest_text(self.html),
            "page_no": self.page.page_no,
            "captured_at": self.page.captured_at.isoformat(),
        }


class Reader(Protocol):
    def read(self, cursor: str | None) -> tuple[str, str]: ...


def decode_document(content: bytes, locator: str) -> str:
    if len(content) > 2_000_000:
        raise CaptureFailure("document_limit", "源页面超出长度限制", retryable=False)
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CaptureFailure("encoding", "页面未采用 UTF-8", retryable=False,
                             details={"locator": locator}) from exc


@dataclass(frozen=True)
class FolderReader:
    directory: Path
    base_url: str = "https://stay.example.test/catalog/"

    def read(self, cursor: str | None) -> tuple[str, str]:
        import re

        key = cursor or "first"
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", key):
            raise CaptureFailure("cursor", "页面游标不符合合作方格式", retryable=False)
        root = self.directory.resolve()
        path = (root / f"{key}.html").resolve()
        if not path.is_relative_to(root):
            raise CaptureFailure("document_path", "页面路径超出回放目录", retryable=False)
        try:
            with path.open("rb") as handle:
                content = handle.read(2_000_001)
        except OSError as exc:
            raise CaptureFailure("document_missing", "合作方回放页面暂不可读",
                                 retryable=True, details={"cursor": key}) from exc
        return decode_document(content, key), self.base_url + quote(key) + ".html"


class LoopbackReader:
    def __init__(self, base: str, client: httpx.Client):
        value = urlsplit(base)
        if (
            value.scheme != "http"
            or value.hostname not in {"127.0.0.1", "::1"}
            or value.username is not None
            or value.password is not None
            or value.query
            or value.fragment
        ):
            raise invalid("portal_origin", "HTTP 回放仅接受本机环回地址")
        self.base = urlunsplit((value.scheme, value.netloc, value.path, "", ""))
        self.client = client

    def read(self, cursor: str | None) -> tuple[str, str]:
        try:
            with self.client.stream("GET", self.base, params={"cursor": cursor or "first"},
                                    follow_redirects=False) as response:
                if response.status_code != 200:
                    raise CaptureFailure(
                        "portal_response",
                        "合作方页面响应异常",
                        retryable=response.status_code in {408, 429, 500, 502, 503, 504},
                        details={"status": response.status_code},
                    )
                if not response.headers.get("content-type", "").startswith("text/html"):
                    raise CaptureFailure("portal_media", "合作方响应不是 HTML", retryable=False)
                parts = bytearray()
                for chunk in response.iter_bytes():
                    parts.extend(chunk)
                    if len(parts) > 2_000_000:
                        raise CaptureFailure("document_limit", "源页面超出长度限制", retryable=False)
                locator = str(response.url)
        except httpx.TransportError as exc:
            raise CaptureFailure("portal_transport", "无法连接合作方回放服务",
                                 retryable=True) from exc
        return decode_document(parts, locator), locator


def require_metadata(values: dict) -> dict:
    keys = ("source", "query", "revision", "page", "pages", "locale", "viewport", "observed")
    absent = [key for key in keys if not values.get(key)]
    if absent:
        raise CaptureFailure("portal_metadata", "页面缺少采集范围标识",
                             retryable=False, details={"missing": absent})
    try:
        return {
            "source": values["source"],
            "query": values["query"],
            "source_revision": int(values["revision"]),
            "page_no": int(values["page"]),
            "total_pages": int(values["pages"]),
            "locale": values["locale"],
            "viewport": values["viewport"],
            "captured_at": datetime.fromisoformat(values["observed"]),
            "next_token": values.get("next") or None,
            "request_token": values.get("cursor") or None,
        }
    except ValueError as exc:
        raise CaptureFailure("portal_metadata", "页面修订、页码或时间不合法",
                             retryable=False) from exc


def normalize_cards(cards: list[dict], meta: dict) -> list[dict]:
    rows = []
    for card in cards:
        try:
            item = Supply.model_validate({
                "item_id": card["item_id"],
                "kind": card["kind"],
                "title": card["title"],
                "provider_id": card["provider_id"],
                "city": card["city"] or card["region"],
                "region": card["region"],
                "category": card["category"],
                "status": card["status"],
                "updated_at": card["updated_at"],
                "source": meta["source"],
                "source_revision": meta["source_revision"],
                "address_hint": card["address_hint"],
                "price_band": card["price_band"],
                "service_window": card["service_window"],
            })
        except ValidationError as exc:
            raise CaptureFailure(
                "portal_card", "可见卡片字段不符合采集合同", retryable=False,
                details={"position": card["source_position"],
                         "fields": [error["loc"][0] for error in exc.errors() if error["loc"]]},
            ) from exc
        rows.append(item.model_dump(mode="json", exclude={"source", "source_revision"}))
    return rows


def convert(session_id: str, html: str, url: str) -> Capture:
    extracted = extract_page(html, url)
    meta = require_metadata(extracted["metadata"]["capture"])
    if extracted["stats"]["invalid"]:
        raise CaptureFailure("portal_card", "存在无法识别的可见卡片",
                             retryable=False, details={"positions": extracted["stats"]["invalid"]})
    if not extracted["cards"] and extracted["metadata"]["capture"].get("empty") != "confirmed":
        raise CaptureFailure("unconfirmed_empty", "未确认空结果，不能认定页面无供给",
                             retryable=True)
    rows = normalize_cards(extracted["cards"], meta)
    try:
        page = Page(session_id=session_id, rows=rows, has_next=bool(meta["next_token"]), **meta)
    except ValidationError as exc:
        raise CaptureFailure("portal_page", "页面元数据不符合会话合同",
                             retryable=False) from exc
    return Capture(html, url, page)


def session_from_first(session_id: str, capture: Capture) -> SessionOpen:
    page = capture.page
    if page.page_no != 1 or page.request_token is not None:
        raise CaptureFailure("portal_first", "首次读取未返回首屏", retryable=False)
    return SessionOpen(session_id=session_id, source=page.source, query=page.query,
                       source_revision=page.source_revision, expected_pages=page.total_pages,
                       locale=page.locale)
