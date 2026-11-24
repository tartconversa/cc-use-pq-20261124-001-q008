from __future__ import annotations

from collections import Counter
import json

from guizhou_stay import coverage, reconcile

EXPLANATIONS = {
    "minimum_active_items": "收集缺失类别的有效供给卡片与证明",
    "source_conflict": "向列出的合作方核对相互冲突的字段",
    "pending_evidence": "取得待审核供给的正式状态",
    "stale_source": "重新采集供给状态及来源修订",
    "unobserved_at": "核对实际观察日期",
    "future_supply": "检查页面的更新时间与观察时间",
}


def attempts(db, revision: int) -> list[dict]:
    rows = db.execute(
        """SELECT s.session_id,s.source,s.query,s.state,s.expected_pages,s.reason,
                  COUNT(p.page_no) AS received
           FROM sessions AS s LEFT JOIN pages AS p USING(session_id)
           WHERE s.source_revision=?
           GROUP BY s.session_id ORDER BY s.source,s.query,s.session_id""", (revision,),
    )
    return [
        {**dict(row), "remaining_pages": row["expected_pages"] - row["received"]}
        for row in rows
    ]


def trace_gap(name: str, gap: dict, category: dict) -> dict:
    identifiers = set(gap.get("items", []))
    affected = [
        finding for finding in category["findings"]
        if not identifiers or finding["item_id"] in identifiers
    ]
    pages = {}
    for finding in affected:
        for witness in finding["witnesses"]:
            for capture in witness["captures"]:
                key = capture["session_id"], capture["page_no"]
                pages[key] = capture
    return {
        "category": name, "gap": gap["reason"], "action": EXPLANATIONS[gap["reason"]],
        "items": sorted(finding["item_id"] for finding in affected),
        "page_evidence": [pages[key] for key in sorted(pages)],
    }


def scan(db, region: str, policy, at, revision: int) -> dict:
    assessment = coverage.assess(db, region, policy, at, revision)
    categories = assessment["categories"]
    findings = [
        trace_gap(gap["category"], gap, categories[gap["category"]])
        for gap in assessment["gaps"]
    ]
    seen = attempts(db, revision)
    state_counts = Counter(value["state"] for value in seen)
    return {
        "assessment": assessment,
        "source_inventory": reconcile.source_inventory(db, revision, region),
        "collection_states": dict(sorted(state_counts.items())),
        "collection_checkpoints": [
            value for value in seen if value["state"] != "complete"
        ],
        "follow_up": findings,
    }
