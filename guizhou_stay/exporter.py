from __future__ import annotations

import csv
import io
import json

from guizhou_stay import coverage
from guizhou_stay.digest import digest
from guizhou_stay.quality import scan


def frozen_csv(document: dict) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(("package_id", "edition", "source_revision", "category", "identity_id",
                     "item_id", "state", "source", "session", "page", "page_digest", "proof"))
    for category, result in document["items"].items():
        for finding in result["findings"]:
            context = (document["package_id"], document["revision"], document["source_revision"],
                       category, finding["identity_id"], finding["item_id"], finding["state"])
            if not finding["witnesses"]:
                writer.writerow((*context, "", "", "", "", ""))
            for witness in finding["witnesses"]:
                proofs = "|".join(sorted(value["evidence_id"] for value in witness["proofs"]))
                for page in witness["captures"]:
                    writer.writerow((*context, witness["item"]["source"], page["session_id"],
                                     page["page_no"], page["page_digest"], proofs))
    return output.getvalue()


def package_document(db, package_id: str, revision: int | None = None) -> dict:
    document = coverage.package(db, package_id, revision)
    stored = document["digest"]
    if digest({key: value for key, value in document.items() if key != "digest"}) != stored:
        raise RuntimeError("已冻结供给包摘要与内容不一致")
    return document


def export_package(db, package_id: str, revision: int | None = None) -> str:
    return json.dumps(package_document(db, package_id, revision), ensure_ascii=False, indent=2)


def operational_report(db, region, policy, at, revision):
    return scan(db, region, policy, at, revision)
