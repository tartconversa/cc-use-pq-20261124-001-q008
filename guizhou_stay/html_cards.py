from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from guizhou_stay.errors import invalid


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    visible: bool
    children: list[Node | str] = field(default_factory=list)

    def content(self) -> str:
        if not self.visible:
            return ""
        return normalize("".join(child if isinstance(child, str) else child.content()
                                 for child in self.children))


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class TreeParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}
    HIDDEN = {"script", "style", "template", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {}, True)
        self.stack = [self.root]
        self.elements = 0

    def start(self, tag, attrs, *, closed):
        self.elements += 1
        if len(self.stack) > 80 or self.elements > 40_000:
            raise invalid("html_complexity", "页面嵌套或元素数超出采集限制")
        parent = self.stack[-1]
        props = {key: value or "" for key, value in attrs}
        style = props.get("style", "").casefold().replace(" ", "")
        visible = (
            parent.visible
            and tag not in self.HIDDEN
            and "hidden" not in props
            and props.get("aria-hidden", "").casefold() != "true"
            and "display:none" not in style
            and "visibility:hidden" not in style
        )
        node = Node(tag, props, visible)
        parent.children.append(node)
        if tag == "br" and visible:
            parent.children.append(" ")
        if not closed and tag not in self.VOID:
            self.stack.append(node)

    def handle_starttag(self, tag, attrs):
        self.start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self.start(tag, attrs, closed=True)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if self.stack[-1].visible:
            self.stack[-1].children.append(data)


def descendants(node: Node):
    for child in node.children:
        if isinstance(child, Node) and child.visible:
            yield child
            yield from descendants(child)


def has_class(node: Node, name: str) -> bool:
    return name in node.attrs.get("class", "").split()


def first(node: Node, *, tag: str | None = None, class_name: str | None = None) -> Node | None:
    for child in descendants(node):
        if (tag is None or child.tag == tag) and (class_name is None or has_class(child, class_name)):
            return child
    return None


def parse(html: str) -> Node:
    if len(html.encode("utf-8")) > 2_000_000:
        raise invalid("html_size", "页面超过单次解析大小限制")
    parser = TreeParser()
    parser.feed(html)
    parser.close()
    return parser.root


def text(node: Node, class_name: str) -> str | None:
    value = first(node, class_name=class_name)
    return value.content() or None if value else None


def reference(base: str, href: str) -> str:
    if not href:
        return ""
    resolved = urljoin(base, href.strip())
    try:
        value = urlsplit(resolved)
        if value.scheme in {"https", "http"} and value.hostname and not value.username:
            return resolved
    except ValueError:
        pass
    return ""


def card_fields(card: Node, position: int, base_url: str) -> dict:
    link = first(card, tag="a")
    return {
        "source_position": position,
        "item_id": card.attrs.get("data-id", ""),
        "kind": card.attrs.get("data-kind", ""),
        "category": card.attrs.get("data-category", ""),
        "provider_id": card.attrs.get("data-provider", ""),
        "status": card.attrs.get("data-status", "active"),
        "updated_at": card.attrs.get("data-updated", ""),
        "title": text(card, "title") or (link.content() if link else ""),
        "href": reference(base_url, link.attrs.get("href", "")) if link else "",
        "region": text(card, "region") or "",
        "city": text(card, "city"),
        "address_hint": text(card, "address"),
        "price_band": text(card, "price"),
        "service_window": text(card, "hours"),
        "text": card.content(),
    }


def collect(root: Node, base_url: str, card_class: str) -> list[dict]:
    nodes = [node for node in descendants(root) if has_class(node, card_class)]
    if len(nodes) > 500:
        raise invalid("card_limit", "页面的可见供给卡片超过采集限制")
    result = []
    seen = {}
    for position, node in enumerate(nodes, start=1):
        row = card_fields(node, position, base_url)
        key = row["item_id"]
        facts = {field: value for field, value in row.items() if field != "source_position"}
        if key and key in seen:
            if facts != seen[key]:
                raise invalid("duplicate_card", "同一页面的同号卡片存在不同内容",
                              {"item_id": key, "position": position})
            continue
        if key:
            seen[key] = facts
        result.append(row)
    return result


def metadata(root: Node) -> dict:
    title = first(root, tag="title")
    description = ""
    canonical = ""
    facts = {}
    for node in descendants(root):
        if node.tag == "meta":
            name = node.attrs.get("name", "").casefold()
            value = node.attrs.get("content", "")
            if name == "description":
                description = normalize(value)
            if name.startswith("stay-"):
                key = name.removeprefix("stay-")
                if key in facts:
                    raise invalid("duplicate_metadata", "同一采集标识在页面重复声明", {"name": name})
                facts[key] = value.strip()
        elif node.tag == "link" and node.attrs.get("rel", "").casefold() == "canonical":
            canonical = node.attrs.get("href", "")
    return {"title": title.content() if title else "", "description": description,
            "canonical": canonical, "capture": facts}


def parse_cards(html: str, base_url: str, *, card_class: str = "stay-card") -> list[dict]:
    return collect(parse(html), base_url, card_class)


def page_metadata(html: str) -> dict:
    return metadata(parse(html))


def card_stats(cards: list[dict]) -> dict:
    required = ("item_id", "title", "href", "region", "category", "provider_id", "kind", "updated_at")
    categories: dict[str, int] = {}
    regions: dict[str, int] = {}
    invalid_positions = []
    for card in cards:
        category = card["category"]
        region = card["region"]
        categories[category] = categories.get(category, 0) + 1
        regions[region] = regions.get(region, 0) + 1
        if any(not card[key] for key in required):
            invalid_positions.append(card["source_position"])
    return {"count": len(cards), "categories": dict(sorted(categories.items())),
            "regions": dict(sorted(regions.items())), "invalid": invalid_positions}


def extract_page(html: str, base_url: str, *, card_class: str = "stay-card") -> dict:
    root = parse(html)
    cards = collect(root, base_url, card_class)
    return {"metadata": metadata(root), "cards": cards, "stats": card_stats(cards)}
