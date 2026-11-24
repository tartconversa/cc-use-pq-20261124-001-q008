from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from guizhou_stay import bundles, catalog, collector, crawl, verification
from guizhou_stay.contracts import Page, SessionOpen
from guizhou_stay.errors import DomainError, conflict
from guizhou_stay.portal import FolderReader
from guizhou_stay.storage import Store


def open_session(store, args):
    body = SessionOpen(session_id=args.session, source=args.source, query=args.query,
                       source_revision=args.revision, expected_pages=args.pages)
    with store.transaction(write=True) as db:
        return crawl.open_session(db, body)


def import_page(store, args):
    body = Page.model_validate_json(args.json_file.read_text(encoding="utf-8"))
    with store.transaction(write=True) as db:
        if db.execute("SELECT 1 FROM collections WHERE session_id=?", (body.session_id,)).fetchone():
            raise conflict("managed_collection", "采集会话由采集员入口推进")
        receipt = crawl.save_page(db, body)
        if not receipt.get("stale") and not receipt["replayed"]:
            receipt["ingest"] = catalog.ingest_page(db, body)
    if receipt.get("stale"):
        raise conflict("source_changed", "页面范围已变化，当前会话需要重开", receipt["stale"])
    return receipt


def collect(store, args):
    directory = args.directory.resolve()
    return collector.run(store, args.session, directory.as_uri(), FolderReader(directory), args.limit)


def report(store, args):
    with store.transaction() as db:
        return crawl.session_report(db, args.session)


def audit(store, args):
    with store.transaction() as db:
        return verification.review(db, args.session)


def export(store, args):
    with store.transaction() as db:
        blob = bundles.build(db, args.package, args.edition)
    with args.destination.open("xb") as output:
        output.write(blob)
    return {"path": str(args.destination), "bytes": len(blob)}


def verify(_store, args):
    with args.archive.open("rb") as handle:
        raw = handle.read(26_000_001)
    if len(raw) > 26_000_000:
        from guizhou_stay.errors import invalid

        raise invalid("bundle_limit", "文件超出离线检查大小")
    return bundles.verify(raw)


def parser():
    value = argparse.ArgumentParser(prog="python -m guizhou_stay.cli")
    value.add_argument("--db", type=Path)
    commands = value.add_subparsers(dest="command", required=True)

    open_cmd = commands.add_parser("open", help="登记人工提交会话")
    open_cmd.add_argument("session")
    open_cmd.add_argument("source")
    open_cmd.add_argument("query")
    open_cmd.add_argument("--revision", type=int, required=True)
    open_cmd.add_argument("--pages", type=int, required=True)
    open_cmd.set_defaults(handle=open_session)

    page_cmd = commands.add_parser("page", help="提交人工采集的结构化页面")
    page_cmd.add_argument("json_file", type=Path)
    page_cmd.set_defaults(handle=import_page)

    crawl_cmd = commands.add_parser("collect", help="从本地 HTML 按游标采集")
    crawl_cmd.add_argument("session")
    crawl_cmd.add_argument("directory", type=Path)
    crawl_cmd.add_argument("--limit", type=int, default=10)
    crawl_cmd.set_defaults(handle=collect)

    for name, handler in (("report", report), ("audit", audit)):
        command = commands.add_parser(name)
        command.add_argument("session")
        command.set_defaults(handle=handler)

    archive = commands.add_parser("bundle", help="导出封存供给包")
    archive.add_argument("package")
    archive.add_argument("destination", type=Path)
    archive.add_argument("--edition", type=int)
    archive.set_defaults(handle=export)

    check = commands.add_parser("verify-bundle", help="独立核对供给包与页面摘要")
    check.add_argument("archive", type=Path)
    check.set_defaults(handle=verify)
    return value


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    if args.handle is not verify and args.db is None:
        command.error("该命令需要 --db")
    try:
        store = Store(args.db) if args.handle is not verify else None
        result = args.handle(store, args)
    except DomainError as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, ValidationError) as exc:
        print(json.dumps({"code": "input_error", "message": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("valid") is False or result.get("state") in {"failed", "stale"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
