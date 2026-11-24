from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from guizhou_stay.contracts import Supply
from guizhou_stay.errors import invalid


@dataclass(frozen=True)
class Filter:
    region: str | None = None
    category: str | None = None
    provider: str | None = None
    status: str | None = None
    text: str | None = None

    def payload(self):
        return {"region": self.region, "category": self.category, "provider": self.provider,
                "status": self.status, "text": self.text}


def validate(value: Filter) -> Filter:
    if value.status and value.status not in {"active", "withdrawn", "pending"}:
        raise invalid("status_filter", "状态过滤条件无效")
    return value


def apply(items: Iterable[Supply], value: Filter) -> list[Supply]:
    validate(value)
    text = value.text.casefold().strip() if value.text else None
    result = []
    for item in items:
        if value.region and item.region != value.region:
            continue
        if value.category and item.category != value.category:
            continue
        if value.provider and item.provider_id != value.provider:
            continue
        if value.status and item.status != value.status:
            continue
        if text and text not in " ".join((item.title, item.city, item.region, *item.tags)).casefold():
            continue
        result.append(item)
    return sorted(result, key=lambda item: (item.region, item.category, item.title, item.item_id))


def facets(items: Iterable[Supply]) -> dict:
    values = list(items)
    return {"regions": sorted({item.region for item in values}), "categories": sorted({item.category for item in values}),
            "providers": sorted({item.provider_id for item in values}), "statuses": sorted({item.status for item in values})}


def page(items: Iterable[Supply], offset: int, limit: int) -> dict:
    if offset < 0 or limit < 1 or limit > 1000:
        raise invalid("page", "结果分页参数无效")
    values = list(items)
    selected = values[offset:offset + limit]
    return {"items": [item.model_dump(mode="json") for item in selected], "total": len(values),
            "offset": offset, "limit": limit, "next_offset": offset + limit if offset + limit < len(values) else None,
            "facets": facets(values)}

