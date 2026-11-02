from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any


MATERIAL_CATEGORIES = (
    "专利证书",
    "受理通知书",
    "审查通知书",
    "申请文件",
    "授权办登/登记手续",
    "补正通知书",
    "缴费/年费材料",
    "转让/变更材料",
    "复审/无效材料",
    "其他材料",
)
ATTACHMENT_FIELDS = MATERIAL_CATEGORIES
DEMO_GROUP_ALIASES = {'集团','我们','我司','我们公司'}
DEMO_GROUP_APPLICANTS = []
DEMO_GROUP_SEARCH_TERMS = []
COMPANY_NAME_ALIASES = {}


def normalize_application_no(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in {"", "-", "—"})


def clean(value: Any) -> str:
    if is_blank(value):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()


def derive_current_status(patent_type: Any, legal_event: Any) -> str:
    text = f"{patent_type or ''}|{legal_event or ''}"
    if any(x in text for x in ("全部无效", "权利终止", "专利权终止", "失效")):
        return "已失效"
    if "驳回" in text:
        return "已驳回"
    if any(x in text for x in ("视为撤回", "撤回")):
        return "已撤回"
    if "部分无效" in text or "授权" in text:
        return "已授权"
    if any(x in text for x in ("申请", "公开", "实质审查", "复审", "PCT", "指定期")):
        return "申请中"
    return "状态待确认"


ALIASES = (
    ("专利证书", ("授权证书", "专利证书", "letters patent", "egrant", "certificate of patent")),
    ("受理通知书", ("受理通知书", "受理决定", "notice of filing receipt", "filing receipt")),
    ("审查通知书", ("审查通知书", "审查意见", "审意", "office action", "examination report")),
    ("授权办登/登记手续", ("办理登记手续通知书", "授权办登", "办登通知", "登记手续通知")),
    ("补正通知书", ("补正通知书", "补正书", "补正文件")),
    ("缴费/年费材料", ("缴费通知书", "年费通知", "年费缴纳", "缴纳年费", "payment notice")),
    ("转让/变更材料", ("转让手续", "著录项目变更", "变更证明", "转让证明", "assignment",
                     "grant of transfer", "registration of transfer", "recordal request", "change of proprietor")),
    ("复审/无效材料", ("复审决定", "复审请求", "无效宣告", "无效决定", "reexamination", "invalidation")),
    ("申请文件", ("专利申请书", "请求书", "权利要求书", "说明书", "摘要附图", "application documents")),
)


def classify_material(text: str) -> tuple[str, bool]:
    text = str(text or "")
    lowered = text.lower()
    for category, aliases in ALIASES:
        if any(alias.lower() in lowered for alias in aliases):
            return category, True
    return "其他材料", False


def parse_material_name(filename: str) -> dict[str, Any]:
    stem = Path(filename).stem.strip()
    category, recognized = classify_material(stem)
    return {"application_no": "", "app_key": "", "category": category, "recognized": recognized,
            "status": "待匹配", "message": "正在从完整文件名中查找申请号或公开号"}


def safe_file_name(name: str) -> str:
    # Replace path separators before any path interpretation. Material categories
    # such as “转让/变更材料” are data, not folders, and must stay in the filename.
    return re.sub(r'[\\/:*?"<>|]+', "-", str(name)).strip(" .") or "未命名文件"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_text(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def configure_organization(payload=None):
    """Refresh mutable group mappings without embedding any business identity."""
    import sys
    if payload is None:
        path=Path(__file__).resolve().parent/'data/organization.json'
        payload=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    DEMO_GROUP_ALIASES.clear()
    DEMO_GROUP_ALIASES.update({'集团','我们','我司','我们公司'} | set(payload.get('group_aliases',[])))
    DEMO_GROUP_APPLICANTS[:]=payload.get('group_applicants',[])
    DEMO_GROUP_SEARCH_TERMS[:]=payload.get('group_search_terms',[])
    COMPANY_NAME_ALIASES.clear();COMPANY_NAME_ALIASES.update(payload.get('company_aliases',{}))
    module=sys.modules.get('feishu_sync')
    if module is not None:
        module.GROUP.clear();module.GROUP.update(module.party_name(v) for v in DEMO_GROUP_APPLICANTS)

configure_organization()
