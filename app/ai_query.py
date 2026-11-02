from __future__ import annotations

import base64
import ctypes
import getpass
import hashlib
import json
import os
import platform
import re
import ssl
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import date
from pathlib import Path

from core import COMPANY_NAME_ALIASES, MATERIAL_CATEGORIES, DEMO_GROUP_ALIASES, DEMO_GROUP_APPLICANTS
from business_mappings import normalize_business_question


DEFAULT_MODEL = "deepseek-v4-flash"
API_URL = "https://api.deepseek.com/chat/completions"
ALLOWED_FILTERS = {
    "keyword", "applicant", "rightsholder", "co_applicants", "co_applicant_only", "inventor", "statuses", "title", "country",
    "application_from", "application_to", "grant_from", "grant_to", "unpublished_only", "publication_missing", "submitted_only",
    "needs_expiry_data", "count_intent", "overseas_only", "exclusive_party", "interpretation",
}
DATE_FIELDS = {"application_from", "application_to", "grant_from", "grant_to"}
BOOL_FIELDS = {"co_applicant_only", "unpublished_only", "publication_missing", "submitted_only", "needs_expiry_data", "count_intent", "overseas_only", "exclusive_party"}
LIST_FIELDS = {"co_applicants", "statuses"}


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _local_free(pointer) -> None:
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    local_free(ctypes.cast(pointer, ctypes.c_void_p))


def _fallback_key() -> bytes:
    identity = f"{getpass.getuser()}|{platform.node()}|专利查询-v1".encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(identity).digest())


def _fallback_encrypt(raw: bytes) -> str:
    try:
        from cryptography.fernet import Fernet
        return "fernet:" + Fernet(_fallback_key()).encrypt(raw).decode("ascii")
    except ImportError:
        return "plain:" + base64.b64encode(raw).decode("ascii")


def protect_secret(secret: str) -> str:
    raw = secret.encode("utf-8")
    # The launcher and the web process can run with different Windows token/profile
    # contexts. DPAPI-encrypted values may then fail with WinError 2 even for the same
    # person. Use the local machine/user-derived fallback so both launch paths agree.
    return _fallback_encrypt(raw)


def unprotect_secret(value: str) -> str:
    if not value:
        return ""
    prefix, _, payload = value.partition(":")
    if prefix == "fernet":
        from cryptography.fernet import Fernet
        return Fernet(_fallback_key()).decrypt(payload.encode("ascii")).decode("utf-8")
    encrypted = base64.b64decode(payload or prefix)
    if prefix == "plain" or os.name != "nt":
        return encrypted.decode("utf-8")
    source, source_buffer = _blob(encrypted)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
                                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        _local_free(output.pbData)
        del source_buffer


def load_settings(data_dir: Path, include_key: bool = False) -> dict:
    path = data_dir / "settings.json"
    raw = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    settings = {
        "enabled": bool(raw.get("enabled", True)),
        "model": str(raw.get("model") or DEFAULT_MODEL),
        "configured": bool(raw.get("api_key")),
        "mappings": raw.get("mappings") if isinstance(raw.get("mappings"), dict) else {},
    }
    if include_key and raw.get("api_key"):
        try:
            settings["api_key"] = unprotect_secret(str(raw["api_key"]))
        except Exception as exc:
            raise RuntimeError("本机保存的 DeepSeek API Key 无法读取，请在 AI 设置中重新保存") from exc
    return settings


def save_settings(data_dir: Path, payload: dict) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "settings.json"
    current = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    model = str(payload.get("model") or current.get("model") or DEFAULT_MODEL).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,80}", model):
        raise ValueError("模型名称格式不正确")
    current["model"] = model
    current["enabled"] = bool(payload.get("enabled", True))
    if "mapping_text" in payload:
        mappings = {}
        for line in str(payload.get("mapping_text") or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            source, separator, target = line.partition("=")
            source, target = source.strip(), target.strip()
            if separator and source and target and len(source) <= 40 and len(target) <= 80:
                mappings[source] = target
        current["mappings"] = dict(list(mappings.items())[:50])
    if payload.get("clear_key"):
        current.pop("api_key", None)
    elif str(payload.get("api_key") or "").strip():
        current["api_key"] = protect_secret(str(payload["api_key"]).strip())
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(current, ensure_ascii=False, indent=2), "utf-8")
    temp.replace(path)
    return load_settings(data_dir)


def public_settings(data_dir: Path) -> dict:
    settings = load_settings(data_dir)
    settings["api_key_masked"] = "已安全保存" if settings["configured"] else "未配置"
    settings["mapping_text"] = "\n".join(f"{source}={target}" for source, target in settings.pop("mappings", {}).items())
    return settings


def apply_question_mappings(question: str, data_dir: Path) -> str:
    normalized = normalize_business_question(question)
    for source, target in load_settings(data_dir).get("mappings", {}).items():
        normalized = normalized.replace(source, target)
    return normalized


def sanitize_filters(value: object, question: str = "") -> dict:
    if not isinstance(value, dict):
        raise ValueError("DeepSeek 没有返回可用的筛选条件")
    output: dict = {}
    for key, item in value.items():
        if key not in ALLOWED_FILTERS or item in (None, "", [], False):
            continue
        if key in BOOL_FIELDS:
            output[key] = bool(item)
        elif key in LIST_FIELDS:
            if not isinstance(item, list):
                item = re.split(r"[|,，、]+", str(item))
            values = [str(x).strip()[:80] for x in item if str(x).strip()]
            if values:
                output[key] = values[:8]
        else:
            text = str(item).strip()[:200]
            if key in DATE_FIELDS and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", text):
                continue
            if text:
                output[key] = text
    statuses = output.get("statuses", [])
    allowed_statuses = {"已授权", "申请中", "已驳回", "已撤回", "已失效", "状态待确认"}
    output["statuses"] = [x for x in statuses if x in allowed_statuses]
    if not output.get("statuses"):
        output.pop("statuses", None)
    return apply_business_mappings(output, question)


def _normalized_company_text(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("(", "（").replace(")", "）")


def _company_mentioned(question: str) -> str:
    normalized = _normalized_company_text(question)
    for alias, company in COMPANY_NAME_ALIASES.items():
        if _normalized_company_text(alias) in normalized:
            return company
    for company in DEMO_GROUP_APPLICANTS:
        if _normalized_company_text(company) in normalized:
            return company
    for alias in sorted(DEMO_GROUP_ALIASES, key=len, reverse=True):
        if alias in question:
            return "集团"
    return ""


def apply_business_mappings(filters: dict, question: str = "") -> dict:
    """少量确定性业务映射，避免模型对公司口径和状态口径自由发挥。"""
    result = dict(filters)
    country = str(result.get("country") or "").strip()
    asks_overseas = bool(re.search(r"海外|境外|国外|非中国", question))
    asks_china = bool(re.search(r"(?:中国|国内)(?:的)?(?:已授权|授权)?专利", question))
    if country in {"海外", "境外", "国外", "非中国", "海外专利"} or asks_overseas:
        result["overseas_only"] = True
        result.pop("country", None)
    elif country in {"国内", "中国专利", "国内专利"} or asks_china:
        result["country"] = "中国"
        result.pop("overseas_only", None)
    if result.get("applicant") in DEMO_GROUP_ALIASES:
        result["applicant"] = "集团"
    if result.get("rightsholder") in DEMO_GROUP_ALIASES:
        result["rightsholder"] = "集团"
    co_applicants = result.get("co_applicants") or []
    result["co_applicants"] = ["集团" if x in DEMO_GROUP_ALIASES or x == "公司" else x for x in co_applicants]
    if not result["co_applicants"]:
        result.pop("co_applicants", None)
    if result.get("publication_missing") and result.get("submitted_only") and "statuses" not in result:
        result["statuses"] = ["申请中"]
    mentioned = _company_mentioned(question)
    if mentioned:
        normalized_question = _normalized_company_text(question)
        matched_alias = next((alias for alias, company in COMPANY_NAME_ALIASES.items()
                              if company == mentioned and _normalized_company_text(alias) in normalized_question), "")
        if not matched_alias and mentioned == "集团":
            matched_alias = next((alias for alias in sorted(DEMO_GROUP_ALIASES, key=len, reverse=True)
                                  if alias in normalized_question), "")
        scope_token = _normalized_company_text(matched_alias or mentioned)
        tail = normalized_question.split(scope_token, 1)[1] if scope_token in normalized_question else ""
        asks_who_applied = bool(re.match(r".{0,8}申请(?:过|了)?(?:的)?[^，。；]{0,12}专利", tail))
        target = "applicant" if asks_who_applied else "rightsholder"
        other = "rightsholder" if target == "applicant" else "applicant"
        result[target] = mentioned
        result.pop(other, None)
        exclusive = bool(re.search(r"(?:仅有|只有|仅|只要|仅需要)" + re.escape(scope_token), normalized_question)
                         or re.search(r"单独|独有|独立申请|独立拥有|不含共有|排除共有|不要共有|非共有", normalized_question))
        if exclusive:
            result["exclusive_party"] = True
        original_interpretation = str(result.get("interpretation") or "").strip()
        scope_name = "原始申请主体" if target == "applicant" else "当前权利人"
        result["interpretation"] = f"按{scope_name}匹配{mentioned}" + ("，排除其他主体共有" if exclusive else "") + (f"；{original_interpretation}" if original_interpretation else "")
        keyword = str(result.get("keyword") or "")
        normalized_keyword = _normalized_company_text(keyword)
        if keyword and (_normalized_company_text(mentioned) in normalized_keyword or
                        (matched_alias and _normalized_company_text(matched_alias) in normalized_keyword)):
            result.pop("keyword", None)
    return result


def _system_prompt(today: date) -> str:
    group_names = "、".join(DEMO_GROUP_APPLICANTS)
    return f"""你是专利资料库的查询意图解析器。今天是 {today.isoformat()}。
只把用户问题转换为 JSON，不回答专利事实，不虚构字段。

允许的 JSON 字段：
- keyword/inventor/title/country：字符串
- applicant：原始申请主体。仅当用户明确问“XX申请的专利”时使用
- rightsholder：当前申请（专利权）人。公司归属问法默认使用此字段
- co_applicants：共同申请主体字符串数组；“我们/我司”统一写“示例”
- co_applicant_only：用户明确问“一起申请/共同申请”时为 true
- statuses：仅可使用 已授权、申请中、已驳回、已撤回、已失效、状态待确认
- application_from/application_to/grant_from/grant_to：YYYY-MM-DD
- publication_missing/submitted_only/needs_expiry_data/count_intent/overseas_only/exclusive_party：布尔值
- interpretation：一句简短中文，说明你理解的筛选口径

业务口径：
1. “下证/拿证/获证/授权”表示已授权；与今年、去年等时间同时出现时，时间作用于授权日。
2. “没用了/无效了/不要了”按已失效、已撤回、已驳回三种状态理解。
3. “还没公开但已经提交”表示 publication_missing=true、submitted_only=true、statuses=["申请中"]。
4. “快到期/年费到期”必须设置 needs_expiry_data=true；不能用申请日猜到期日。
5. 只有“XX申请的专利”才输出 applicant=XX。其他问法，例如“XX的专利”“XX授权的专利”“当前权利人是XX”，都输出 rightsholder=XX。
6. “示例科技”固定表示“示例智造科技有限公司”；“集团/示例/我们/我司”统一写为“集团”。集团固定包含：{group_names}。
7. “示例高校和我们一起申请”应输出 co_applicants=["示例高校","集团"]、co_applicant_only=true。
8. “研发员丙做的材料方面的专利”只需 inventor="研发员丙"；“材料方面”在本资料库里是泛称，不要作为 keyword。
9. “搞了多少/有多少”表示 count_intent=true，不要把这些口语放进 keyword。
10. 完整公司名称必须放 applicant 或 rightsholder，绝不能放 keyword。
11. “海外/境外/国外专利”必须输出 overseas_only=true，不能输出 country="海外"；海外表示排除中国，PCT及其他国家/地区仍保留。
12. “中国专利/国内专利”必须输出 country="中国"，不得省略国家字段。
13. “仅XX/只有XX/XX单独/XX独有/不含共有”设置 exclusive_party=true，排除与其他主体共有；集团可包含多家集团内部公司，但不能有集团外主体。明确说“申请”的作用于 applicant，其余作用于 rightsholder。未强调独有时保留共有专利。

示例 JSON：
{{"applicant":"集团","application_from":"2025-01-01","application_to":"2025-12-31","count_intent":true,"interpretation":"统计2025年集团申请的专利"}}
"""


def parse_with_deepseek(question: str, data_dir: Path) -> dict:
    settings = load_settings(data_dir, include_key=True)
    if not settings.get("enabled") or not settings.get("api_key"):
        raise RuntimeError("尚未配置并启用 DeepSeek API")
    normalized_question = normalize_business_question(question)
    for source, target in settings.get("mappings", {}).items():
        normalized_question = normalized_question.replace(source, target)
    payload = {
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": _system_prompt(date.today())},
            {"role": "user", "content": "请把下面问句解析为 json：\n" + normalized_question[:1000]},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "max_tokens": 800,
        "stream": False,
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + settings["api_key"], "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        if exc.code == 401:
            raise RuntimeError("DeepSeek API Key 无效，请到 AI 设置重新填写") from exc
        raise RuntimeError(f"DeepSeek 请求失败（{exc.code}）" + (f"：{detail}" if detail else "")) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(_connection_error_message(exc)) from exc
    except TimeoutError as exc:
        raise RuntimeError("连接 DeepSeek 超时，请检查防火墙或网络代理") from exc
    try:
        content = result["choices"][0]["message"]["content"]
        if not content:
            raise ValueError
        return sanitize_filters(json.loads(content), normalized_question)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("DeepSeek 返回内容为空或格式不正确，请重试") from exc


def analyze_material_document(data_dir: Path, filename: str, text: str,
                              candidates: list[dict] | None = None) -> dict:
    """Extract bounded archive metadata. The caller still owns all patent matching decisions."""
    settings = load_settings(data_dir, include_key=True)
    if not settings.get("enabled") or not settings.get("api_key"):
        raise RuntimeError("尚未配置并启用 DeepSeek API")
    candidate_rows = []
    for item in (candidates or [])[:8]:
        candidate_rows.append({key: item.get(key) for key in (
            "id", "application_no", "publication_no", "title", "applicant_current",
            "applicant_original", "inventors", "reason",
        )})
    categories = "、".join(MATERIAL_CATEGORIES)
    system_prompt = f"""你是专利材料归档助手。只输出 JSON，不作法律结论，不修改专利状态。
材料类型只能是：{categories}。
输出字段：
- category：材料类型
- category_confidence：只能是 high、medium、low
- category_reason：结合文件名和正文说明这份文件实际办理的业务，不超过两句话
- document_date：发文日期，格式 YYYY-MM-DD；不能确认则为空
- application_numbers/publication_numbers：从材料中看到的编号数组，保持原文
- primary_jurisdiction：该材料实际提交、审理或发文的国家/地区代码（如 BR、JP、CN、PCT）；正文仅提及的同族国家不得作为主办国家，不能确认则为空
- patent_title/applicants/inventors：字符串或字符串数组
- patent_title_cn/patent_title_original：材料明确记载的中英文名称；没有则留空
- filing_date：申请日，格式 YYYY-MM-DD；不要把受理通知书发文日当作申请日
- patent_type：只能是发明申请、实用新型申请、外观设计申请或申请
- record_confidence：仅表示能否据此建立新专利记录，只能是 high、medium、low
- summary：不超过两句话的事实摘要
- action_items：数组，每项包含 item、deadline、fee、evidence、page；不能确认的字段留空
- evidence：数组，每项包含 page、text、field，必须是材料中的短证据
- candidate_patent_ids：仅在没有明确编号时可从给定候选中选择，最多3个
- candidate_reason：候选理由；有明确编号时留空
判断材料类型时必须同时使用文件名和正文，并以正文反映的实际办理事项为准：
- “申请文件”只用于原始专利申请的请求书、说明书、权利要求书、摘要或附图；
- 文件名含 Patent Appln、application number、filed document 或“申请”不代表它是“申请文件”；
- 若正文是在办理权利转让、权利人变更、登记/备案或提交这些手续，即使文件名写 Copy of filed document，也应归“转让/变更材料”；
- Grant of Transfer、assignment、recordal、registration of transfer、change of proprietor 均优先考虑“转让/变更材料”；
- 仅当材料本身是受理通知书，且唯一申请号、名称、申请人/发明人及申请日有明确证据时，record_confidence 才可为 high；
- 证据不足时降低 category_confidence，不得仅凭一个泛化词高置信分类。
期限、费用和待办不得推算或虚构。"""
    user_payload = {
        "filename": filename,
        "document_text": str(text or "")[:30000],
        "candidate_patents_for_suggestion_only": candidate_rows,
    }
    payload = {
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "max_tokens": 1800,
        "stream": False,
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + settings["api_key"], "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        if exc.code == 401:
            raise RuntimeError("DeepSeek API Key 无效，请到 AI 设置重新填写") from exc
        raise RuntimeError(f"DeepSeek 材料整理失败（{exc.code}）" + (f"：{detail}" if detail else "")) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(_connection_error_message(exc)) from exc
    except TimeoutError as exc:
        raise RuntimeError("连接 DeepSeek 超时，请稍后重试材料整理") from exc
    try:
        raw = json.loads(result["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("DeepSeek 返回的材料整理结果格式不正确") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("DeepSeek 返回的材料整理结果格式不正确")
    category = str(raw.get("category") or "其他材料").strip()
    if category not in MATERIAL_CATEGORIES:
        category = "其他材料"
    category_confidence = str(raw.get("category_confidence") or "low").strip().lower()
    if category_confidence not in {"high", "medium", "low"}:
        category_confidence = "low"
    record_confidence = str(raw.get("record_confidence") or "low").strip().lower()
    if record_confidence not in {"high", "medium", "low"}:
        record_confidence = "low"
    document_date = str(raw.get("document_date") or "").strip()
    if document_date and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", document_date):
        document_date = ""
    filing_date = str(raw.get("filing_date") or "").strip()
    if filing_date and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", filing_date):
        filing_date = ""
    patent_type = str(raw.get("patent_type") or "申请").strip()
    if patent_type not in {"发明申请", "实用新型申请", "外观设计申请", "申请"}:
        patent_type = "申请"
    primary_jurisdiction = str(raw.get("primary_jurisdiction") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2,4}", primary_jurisdiction):
        primary_jurisdiction = ""
    allowed_candidate_ids = {int(item["id"]) for item in candidate_rows if item.get("id") is not None}
    candidate_ids = []
    for value in raw.get("candidate_patent_ids") or []:
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value in allowed_candidate_ids and value not in candidate_ids:
            candidate_ids.append(value)
    actions = []
    for value in raw.get("action_items") or []:
        if not isinstance(value, dict):
            continue
        actions.append({key: str(value.get(key) or "")[:300] for key in ("item", "deadline", "fee", "evidence", "page")})
    evidence = []
    for value in raw.get("evidence") or []:
        if isinstance(value, dict):
            evidence.append({key: str(value.get(key) or "")[:300] for key in ("page", "text", "field")})
        elif str(value).strip():
            evidence.append({"page": "", "text": str(value)[:300], "field": ""})
    return {
        "category": category,
        "category_confidence": category_confidence,
        "record_confidence": record_confidence,
        "category_reason": str(raw.get("category_reason") or "")[:500],
        "document_date": document_date,
        "filing_date": filing_date,
        "patent_type": patent_type,
        "primary_jurisdiction": primary_jurisdiction,
        "application_numbers": [str(x)[:100] for x in (raw.get("application_numbers") or []) if str(x).strip()][:20],
        "publication_numbers": [str(x)[:100] for x in (raw.get("publication_numbers") or []) if str(x).strip()][:20],
        "patent_title": str(raw.get("patent_title") or "")[:500],
        "patent_title_cn": str(raw.get("patent_title_cn") or "")[:500],
        "patent_title_original": str(raw.get("patent_title_original") or "")[:500],
        "applicants": raw.get("applicants") if isinstance(raw.get("applicants"), list) else [str(raw.get("applicants") or "")],
        "inventors": raw.get("inventors") if isinstance(raw.get("inventors"), list) else [str(raw.get("inventors") or "")],
        "summary": str(raw.get("summary") or "")[:800],
        "action_items": actions[:20],
        "evidence": evidence[:30],
        "candidate_patent_ids": candidate_ids[:3],
        "candidate_reason": str(raw.get("candidate_reason") or "")[:800],
    }


def _connection_error_message(exc: urllib.error.URLError) -> str:
    reason = exc.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "DeepSeek HTTPS 证书校验失败，请检查系统时间或 Python 证书"
    winerror = getattr(reason, "winerror", None)
    if winerror == 10060:
        return "连接 api.deepseek.com 超时，可能被防火墙、代理或当前网络拦截"
    if winerror == 10061:
        return "api.deepseek.com 拒绝连接，请检查网络代理"
    if winerror == 11001:
        return "无法解析 api.deepseek.com，请检查 DNS"
    if winerror == 10013:
        return "当前启动进程被 Windows 禁止联网，请关闭网页后用桌面的启动程序重新打开"
    detail = str(reason).strip()
    return "无法连接 DeepSeek" + (f"：{detail}" if detail else "，请检查网络后重试")


def test_deepseek_connection(data_dir: Path) -> dict:
    settings = load_settings(data_dir, include_key=True)
    if not settings.get("enabled") or not settings.get("api_key"):
        raise RuntimeError("尚未配置并启用 DeepSeek API")
    request = urllib.request.Request(
        "https://api.deepseek.com/models",
        headers={"Authorization": "Bearer " + settings["api_key"], "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError("DeepSeek API Key 无效") from exc
        raise RuntimeError(f"DeepSeek 连接测试失败（HTTP {exc.code}）") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(_connection_error_message(exc)) from exc
    except TimeoutError as exc:
        raise RuntimeError("连接 api.deepseek.com 超时，可能被防火墙、代理或当前网络拦截") from exc
    models = [str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]
    model = settings["model"]
    if models and model not in models:
        return {"ok": True, "message": f"API Key 和网络正常，但当前模型 {model} 不在账户模型列表中", "models": models}
    return {"ok": True, "message": f"连接成功，API Key 有效，模型 {model} 可用", "models": models}
