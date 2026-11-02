"""Reviewed aliases only; candidate workflow assignments are not executable."""
import re

ALIASES = {
    "下证": "已授权", "拿证": "已授权", "获证": "已授权",
    "受理中": "申请中", "审中": "申请中",
    "OA1": "第一次审查意见通知书", "OA2": "第二次审查意见通知书",
    "OA3": "第三次审查意见通知书", "一通": "第一次审查意见通知书",
    "二通": "第二次审查意见通知书", "三通": "第三次审查意见通知书",
    "授权证书": "专利证书", "优审": "优先审查",
}

def normalize_business_question(question):
    pattern = "|".join((r"(?<![A-Za-z0-9])" + re.escape(x) + r"(?![A-Za-z0-9])") if x.startswith("OA") else re.escape(x)
                       for x in sorted(ALIASES, key=len, reverse=True))
    return re.sub(pattern, lambda m: ALIASES.get(m.group(0), ALIASES.get(m.group(0).upper(), m.group(0))), question, flags=re.IGNORECASE)

def explicit_business_filters(question):
    result = {}
    # Negative predicates remain for the intent parser, never force positive filters.
    if any(x in question for x in ("没有", "没拿", "没下", "未拿", "未下", "不是", "不含", "排除", "不包括")):
        return result
    if any(x in question for x in ("下证", "拿证", "获证")):
        result["statuses"] = ["已授权"]
    if any(x in question for x in ("受理中", "审中")):
        result["statuses"] = ["申请中"]
    if "快到期" in question or "年费到期" in question:
        result["needs_expiry_data"] = True
    if "没公开" in question and ("提交" in question or "递交" in question):
        result.update(publication_missing=True, submitted_only=True, statuses=["申请中"])
    if any(x in question for x in ("专利证书", "授权证书")):
        result["material_category"] = "专利证书"
    elif "受理通知书" in question:
        result["material_category"] = "受理通知书"
    return result

def clean_mapped_keyword(keyword, question, filters):
    value = keyword
    if "statuses" in filters:
        for term in ("受理中", "审中", "申请中", "下证", "拿证", "获证", "已授权"):
            value = value.replace(term, "")
    if filters.get("material_category"):
        for term in ("专利证书", "授权证书", "受理通知书"):
            value = value.replace(term, "")
    return re.sub(r"^(?:的|了|有)|(?:的|了|有)$", "", value.strip()).strip()
