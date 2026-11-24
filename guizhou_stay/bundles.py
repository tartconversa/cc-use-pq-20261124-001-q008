from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import io
import json
import re
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo, BadZipFile

from guizhou_stay import exporter
from guizhou_stay.digest import canonical, digest
from guizhou_stay.errors import conflict, invalid

MAX_UNCOMPRESSED = 25_000_000
MAX_MEMBERS = 1000
CAPTURE_NAME = re.compile(r"pages/[a-z][a-z0-9-]{0,47}/[1-9][0-9]{0,4}\.html")


def checksum(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def name_for(session_id: str, page: int) -> str:
    name = f"pages/{session_id}/{page}.html"
    if not CAPTURE_NAME.fullmatch(name):
        raise invalid("bundle_page", "页面凭证编号无法生成导出路径")
    return name


@dataclass(frozen=True)
class Part:
    name: str
    content: bytes

    def receipt(self):
        return {"name": self.name, "sha256": checksum(self.content), "size": len(self.content)}


def capture_witnesses(document: dict):
    seen = set()
    for category in document["items"].values():
        for finding in category["findings"]:
            for witness in finding["witnesses"]:
                for capture in witness["captures"]:
                    name = name_for(capture["session_id"], capture["page_no"])
                    if name not in seen:
                        seen.add(name)
                        yield name, capture


def capture_part(db, name: str, entry: dict) -> Part | None:
    if entry["html_digest"] is None:
        return None
    row = db.execute(
        """SELECT raw_html,html_digest FROM captures WHERE session_id=? AND page_no=?""",
        (entry["session_id"], entry["page_no"]),
    ).fetchone()
    if row is None:
        raise conflict("bundle_capture_missing", "封存所引用的原始页面已不可用",
                       {"path": name})
    encoded = row["raw_html"].encode("utf-8")
    if checksum(encoded) != entry["html_digest"] or row["html_digest"] != entry["html_digest"]:
        raise conflict("bundle_capture_changed", "原始页面与封存引用不一致", {"path": name})
    return Part(name, encoded)


def parts_for(db, document: dict) -> tuple[list[Part], list[str]]:
    parts = [
        Part("package.json", canonical(document).encode("utf-8")),
        Part("witnesses.csv", exporter.frozen_csv(document).encode("utf-8")),
    ]
    size = sum(len(part.content) for part in parts)
    if size > MAX_UNCOMPRESSED:
        raise invalid("bundle_limit", "结构化归档内容超过导出大小限制")
    structured = []
    for name, entry in capture_witnesses(document):
        part = capture_part(db, name, entry)
        if part is None:
            structured.append(name)
            continue
        size += len(part.content)
        if size > MAX_UNCOMPRESSED or len(parts) + 2 > MAX_MEMBERS:
            raise invalid("bundle_limit", "供给包的原始页面超过导出大小限制")
        parts.append(part)
    parts.sort(key=lambda item: item.name)
    return parts, sorted(structured)


def encode(parts: list[Part]) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for part in sorted(parts, key=lambda value: value.name):
            info = ZipInfo(part.name)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, part.content)
    return output.getvalue()


def build(db, package_id: str, edition: int | None) -> bytes:
    document = exporter.package_document(db, package_id, edition)
    parts, structured = parts_for(db, document)
    manifest = {
        "format": 1,
        "package_digest": document["digest"],
        "files": [part.receipt() for part in parts],
        "structured_only": structured,
    }
    parts.append(Part("manifest.json", canonical(manifest).encode("utf-8")))
    return encode(parts)


def check_members(archive: ZipFile) -> dict:
    members = archive.infolist()
    if len(members) > MAX_MEMBERS or sum(info.file_size for info in members) > MAX_UNCOMPRESSED:
        raise invalid("bundle_limit", "导入包超过核验容量")
    indexed = {}
    for info in members:
        name = info.filename
        if name not in {"package.json", "manifest.json", "witnesses.csv"} and not CAPTURE_NAME.fullmatch(name):
            raise invalid("bundle_entry", "归档中存在未登记的文件路径", {"path": name})
        if name in indexed:
            raise invalid("bundle_entry", "归档包含重复文件路径", {"path": name})
        indexed[name] = info
    return indexed


def read_object(archive: ZipFile, name: str) -> dict:
    try:
        document = json.loads(archive.read(name))
    except (ValueError, KeyError, UnicodeError) as exc:
        raise invalid("bundle_document", "归档未包含可读文档", {"path": name}) from exc
    if not isinstance(document, dict):
        raise invalid("bundle_document", "归档文档应为对象", {"path": name})
    return document


def manifest_files(manifest: dict) -> dict:
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), list):
        raise invalid("bundle_manifest", "缺少支持版本的归档清单")
    entries = {}
    for value in manifest["files"]:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise invalid("bundle_manifest", "清单文件记录不合法")
        name = value["name"]
        if name in entries:
            raise invalid("bundle_manifest", "清单包含重复文件路径", {"path": name})
        entries[name] = value
    return entries


def verify_contents(archive: ZipFile, members: dict, entries: dict) -> list[dict]:
    problems = []
    if set(members) - {"manifest.json"} != set(entries):
        problems.append({"code": "manifest_coverage"})
    for name in sorted(members.keys() & entries.keys()):
        raw = archive.read(name)
        expected = Part(name, raw).receipt()
        if entries[name] != expected:
            problems.append({"code": "file_digest", "path": name})
    return problems


def verify_references(archive: ZipFile, document: dict, members: dict, manifest: dict) -> list[dict]:
    problems = []
    required = {}
    structured = []
    for name, value in capture_witnesses(document):
        if value["html_digest"] is None:
            structured.append(name)
        else:
            required[name] = value["html_digest"]
    if sorted(structured) != manifest.get("structured_only"):
        problems.append({"code": "structured_only"})
    actual = {name for name in members if name.startswith("pages/")}
    if set(required) != actual:
        problems.append({"code": "page_coverage"})
    for name in sorted(required.keys() & actual):
        if checksum(archive.read(name)) != required[name]:
            problems.append({"code": "page_reference", "path": name})
    return problems


def verify(blob: bytes) -> dict:
    try:
        with ZipFile(io.BytesIO(blob)) as archive:
            members = check_members(archive)
            manifest = read_object(archive, "manifest.json")
            pkg = read_object(archive, "package.json")
            stored = pkg.get("digest")
            actual = digest({key: value for key, value in pkg.items() if key != "digest"})
            issues = verify_contents(archive, members, manifest_files(manifest))
            if not (stored == actual == manifest.get("package_digest")):
                issues.append({"code": "package_digest"})
            try:
                issues.extend(verify_references(archive, pkg, members, manifest))
            except (KeyError, TypeError, AttributeError) as exc:
                raise invalid("bundle_document", "供给包凭证结构不合法") from exc
            if "witnesses.csv" not in members or archive.read("witnesses.csv") != exporter.frozen_csv(pkg).encode("utf-8"):
                issues.append({"code": "csv_reference"})
    except (BadZipFile, NotImplementedError, RuntimeError, OSError) as exc:
        raise invalid("bundle_archive", "归档文件无法核验") from exc
    return {"package_id": pkg.get("package_id"), "revision": pkg.get("revision"),
            "digest": stored, "members": len(members), "valid": not issues, "issues": issues}
