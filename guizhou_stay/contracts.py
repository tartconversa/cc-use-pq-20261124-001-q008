from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
Category = Literal["residence", "route", "medical", "food", "insurance", "language", "transport", "culture"]
Status = Literal["active", "withdrawn", "pending"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetimes(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("时间必须带时区")
            return value.astimezone(timezone.utc).replace(microsecond=0)
        return value


class Page(StrictModel):
    session_id: Key
    source: Key
    query: Text
    page_no: Annotated[int, Field(ge=1, le=10_000)]
    total_pages: Annotated[int, Field(ge=1, le=10_000)]
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    captured_at: datetime
    viewport: Text
    rows: list[dict] = Field(max_length=500)
    has_next: bool
    next_token: Key | None = None
    request_token: Key | None = None
    locale: Text = "zh-HK"

    @model_validator(mode="after")
    def page_bounds(self):
        if self.page_no > self.total_pages or self.has_next != (self.page_no < self.total_pages):
            raise ValueError("分页边界与下一页标记不一致")
        if self.has_next and self.next_token is None:
            raise ValueError("存在下一页时必须有游标")
        if not self.has_next and self.next_token is not None:
            raise ValueError("末页不能携带下一页游标")
        return self


class SessionOpen(StrictModel):
    session_id: Key
    source: Key
    query: Text
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    expected_pages: Annotated[int, Field(ge=1, le=10_000)]
    locale: Text = "zh-HK"


class Supply(StrictModel):
    item_id: Key
    kind: Literal["property", "route", "service"]
    title: Text
    provider_id: Key
    city: Text
    region: Text
    category: Category
    status: Status = "active"
    updated_at: datetime
    source: Key
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    address_hint: Text | None = None
    price_band: Text | None = None
    service_window: Text | None = None
    tags: list[Text] = Field(default_factory=list, max_length=20)

    @field_validator("updated_at")
    @classmethod
    def no_future(cls, value: datetime) -> datetime:
        if value > datetime.now(timezone.utc):
            raise ValueError("供给更新时间不能在未来")
        return value


class Evidence(StrictModel):
    evidence_id: Key
    supply_id: Key
    category: Category
    issuer: Text
    issued_at: datetime
    expires_at: datetime
    source: Key
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    document_ref: Text
    status: Literal["verified", "pending", "rejected"]

    @model_validator(mode="after")
    def evidence_period(self):
        if self.expires_at <= self.issued_at:
            raise ValueError("证明有效期必须为正区间")
        return self


class CoveragePolicy(StrictModel):
    required_categories: list[Category] = Field(min_length=1, max_length=8)
    minimum_active_items: Annotated[int, Field(ge=1, le=10_000)] = 1
    max_source_age_days: Annotated[int, Field(ge=1, le=3650)] = 90
    require_verified_evidence: bool = True
    allow_pending: bool = False

    @model_validator(mode="after")
    def unique_categories(self):
        if len(set(self.required_categories)) != len(self.required_categories):
            raise ValueError("覆盖类别不能重复")
        return self


class PublishRequest(StrictModel):
    package_id: Key
    region: Text
    policy: CoveragePolicy
    requested_at: datetime
    expected_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    plan_id: Key | None = None
    expected_package_revision: Annotated[int, Field(ge=0, le=1_000_000)] | None = None
    preview_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None


def valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("需要有效的 IANA 时区") from exc
    return value

