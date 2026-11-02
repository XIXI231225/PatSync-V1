"""Project source rows into the existing master-table vocabulary and content form.

Source evidence stays in run files. Existing names, identifiers, title translations
and status distinctions are never silently replaced by a provider's representation.
"""
from __future__ import annotations

import json
import re
import unicodedata

from core import normalize_application_no

POLICY_VERSION = "20261021-master-format-v2"
FORMAL_FIELDS = (
    "代理卷号", "应用领域分类", "业务分类", "序号", "公开(公告)号", "专利类型", "简单同族编号",
    "PCT国际申请公开号", "申请号", "标题", "标题(译)(简体中文)", "法律状态/事件", "授权日",
    "[标]当前申请(专利权)人", "[标]原始申请(专利权)人", "发明人", "申请日", "公开(公告)日", "受理局",
)
ALIASES = {"当前申请(专利权)人": "[标]当前申请(专利权)人", "原始申请(专利权)人": "[标]原始申请(专利权)人"}
CANONICAL = {
    "申请号": "application_no", "标题": "title_original", "标题(译)(简体中文)": "title_cn",
    "法律状态/事件": "legal_event", "授权日": "grant_date", "[标]当前申请(专利权)人": "applicant_current",
    "[标]原始申请(专利权)人": "applicant_original", "发明人": "inventors", "申请日": "application_date",
    "公开(公告)日": "publication_date", "受理局": "country", "简单同族编号": "family_no", "专利类型": "patent_type",
}
NAMES = {"[标]当前申请(专利权)人", "[标]原始申请(专利权)人", "发明人"}
CHINESE_FIELDS = {"[标]当前申请(专利权)人", "[标]原始申请(专利权)人", "标题(译)(简体中文)"}
PROTECTED = NAMES | {"标题", "标题(译)(简体中文)", "法律状态/事件", "申请日", "授权日", "专利类型", "受理局"}


def present(value):
    return value not in (None, "", "-", "—")


def compact(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def name_tokens(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"(?<=[\u4e00-\u9fff]),(?=[\u4e00-\u9fff])", "|", value)
    return tuple(sorted(compact(v) for v in re.split(r"[|｜;；、\r\n]+", value) if v.strip()))


def equivalent(field, old, new):
    if field in NAMES:
        return name_tokens(old) == name_tokens(new)
    if field in {"申请号", "公开(公告)号", "PCT国际申请公开号"}:
        return normalize_application_no(old) == normalize_application_no(new)
    return compact(old) == compact(new)


def old_value(current, raw, field):
    return current.get(CANONICAL[field], "") if field in CANONICAL else raw.get(field, "")


def project_row(source, current):
    """Return formal proposal + reviews, retaining source status outside raw_json."""
    raw = json.loads(current["raw_json"]) if current else {}
    row, reviews = {}, []
    for key, value in source.items():
        field = ALIASES.get(key, key)
        if field not in FORMAL_FIELDS or not present(value):
            continue
        if field in row and not equivalent(field, row[field], value):
            raise ValueError("数据源同一字段的标准名和别名值冲突")
        row[field] = str(value).strip()
    if current:
        row["申请号"] = current["application_no"]
        for field in list(row):
            before, incoming = old_value(current, raw, field), row[field]
            if field in CHINESE_FIELDS and not present(before) and not re.search(r"[\u4e00-\u9fff]", incoming):
                reviews.append({"field": field, "before": before, "after": incoming,
                                "reason": "来源仅提供外文，标准中文内容待核对，本次不写入"})
                row.pop(field)
                continue
            if present(before) and equivalent(field, before, incoming):
                row[field] = before
            elif field in PROTECTED and present(before):
                reviews.append({"field": field, "before": before, "after": incoming,
                                "reason": "名称、语言或内容有差异，保留原总表值"})
                row[field] = before
            elif field == "简单同族编号" and str(before or "").isdigit() and before != incoming:
                reviews.append({"field": field, "before": before, "after": incoming, "reason": "已有同族编号变化，需核对"})
                row[field] = before
        # Keep the exact existing representation even when a provider omits it.
        for field in PROTECTED | {"申请号"}:
            value = old_value(current, raw, field)
            if present(value): row[field] = value
        # Source legal status is an observation, not a replacement for the business state.
        proposed = source.get("__source_status")
        if proposed and proposed != "状态待确认" and proposed != current["current_status"]:
            reviews.append({"field": "当前状态", "before": current["current_status"], "after": proposed,
                            "reason": "来源为简单法律状态，保留原有具体状态"})
    else:
        # A new member is a candidate until its Chinese parties and portfolio scope
        # have been reviewed; it cannot enter the master through the bulk button.
        for field in NAMES:
            if field in row:
                row[field] = " | ".join(v.strip() for v in re.split(r"[|｜;；、\r\n]+", row[field]) if v.strip())
        if row.get("标题(译)(简体中文)") and not re.search(r"[\u4e00-\u9fff]", row["标题(译)(简体中文)"]):
            row.pop("标题(译)(简体中文)")
        reviews.append({"field": "新增同族", "before": "", "after": row.get("申请号", ""),
                        "reason": "新增记录需核对标准中文主体及集团/参考专利范围，本次不写入"})
    return row, reviews


def actual_diffs(item, current):
    raw = json.loads(current["raw_json"]) if current else {}
    diffs = []
    for field in item.get("changes", []):
        before = current["current_status"] if current and field == "当前状态" else old_value(current, raw, field) if current else ""
        after = item["status"] if field == "当前状态" else item["merged_raw"].get(field, "")
        if not equivalent(field, before, after):
            diffs.append({"field": field, "before": before, "after": after})
    return diffs
