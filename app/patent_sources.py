"""Replaceable patent sources. Providers return Chinese table rows and evidence.

Public extension point: register_source(name, factory); factory(config, secret_loader)
returns a provider with fetch(scope, run_dir, progress) -> list[dict]. Providers
must retrieve every result, retain raw evidence, and reject incomplete responses.
No provider writes the master database. See 智慧芽接入.md for the MCP contract.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
import re
import threading
import time
import uuid

from core import normalize_application_no
from patent_source_client import Mcp, PatsnapApi, SourceError

PATSNAP_MCP = "https://connect.zhihuiya.com/1458a4/mcp"
MAX_RECORDS = 5000
TABLE_FIELDS = {
    "申请号", "公开(公告)号", "标题", "标题(译)(简体中文)", "法律状态/事件", "授权日",
    "[标]当前申请(专利权)人", "[标]原始申请(专利权)人", "当前申请(专利权)人", "原始申请(专利权)人",
    "发明人", "申请日", "公开(公告)日", "受理局", "简单同族编号", "专利类型", "PCT国际申请公开号",
    "来源简单法律状态", "__source_status", "__source_warning",
}
STATUSES = {"已授权", "申请中", "已失效", "已驳回", "已撤回", "状态待确认"}


def save_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(10):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                # Windows readers briefly deny replacement while read_text is open.
                if attempt == 9: raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else default


def text(value):
    if isinstance(value, dict):
        if "data_value" not in value:
            raise SourceError("文本字段出现不支持的对象结构")
        return text(value["data_value"])
    if isinstance(value, list):
        return "|".join(dict.fromkeys(text(v) for v in value if v not in (None, "")))
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise SourceError("文本字段类型异常")
    return str(value).strip()


def day(value):
    value = text(value)
    if not value:
        return ""
    compact = value.replace("-", "").replace("/", "")
    try:
        return datetime.strptime(compact, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        raise SourceError("数据源日期不是完整有效日期") from None


def scope_query(scope, *, family_template="FAM_ID:({values})", application_template="APNO:({values})"):
    clauses = []
    families = scope["family_ids"]
    numbers = scope["application_numbers"]
    if families:
        if not all(re.fullmatch(r"\d+", v) for v in families):
            raise SourceError("源总表同族编号异常")
        clauses.append(family_template.replace("{values}", " OR ".join(families)))
    if numbers:
        if not all(re.fullmatch(r"[A-Za-z0-9./_-]+", v) for v in numbers):
            raise SourceError("源总表存在不能直接检索的申请号，请先核对")
        clauses.append(application_template.replace("{values}", " OR ".join('"' + v + '"' for v in numbers)))
    if not clauses:
        raise SourceError("总表为空，未发起查询")
    return " OR ".join("(" + part + ")" for part in clauses)


def count_value(value):
    if type(value) is not int or value < 0 or value > MAX_RECORDS:
        raise SourceError(f"结果数量异常或超过 {MAX_RECORDS} 条，未截断导入")
    return value


def path_value(value, path):
    try:
        for part in path.split(".") if path else []:
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, TypeError, ValueError):
        raise SourceError("MCP 字段路径不存在，请核对映射配置") from None
    return value


def one_detail(result, pid, pn):
    rows = result.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise SourceError("MCP 未返回唯一的专利详情")
    row = rows[0]
    if row.get("patent_id") != pid or row.get("pn") != pn:
        raise SourceError("MCP 返回专利与请求标识不一致")
    return row


def normalize_detail(source, detail):
    pid, pn = text(source.get("patent_id") or source.get("PATENT_ID")), text(source.get("pn") or source.get("PN"))
    b = one_detail(detail["bibliography"], pid, pn)["bibliographic_data"]
    family = one_detail(detail["family"], pid, pn)["patent_family"]
    missing_legal = detail["legal"].get("data") == []
    legal = [] if missing_legal else one_detail(detail["legal"], pid, pn).get("simple_legal_status")
    if not isinstance(legal, list) or (not legal and not missing_legal) or not all(isinstance(v, str) for v in legal):
        raise SourceError("专利简单法律状态缺失或异常")
    family_id = family.get("simple_family_id")
    if type(family_id) is not int or family_id <= 0:
        raise SourceError("详情缺少有效简单同族编号")
    type_names = {"APPLICATION": "发明申请", "GRANTED_PATENT": "授权发明", "GRANT": "授权发明",
                  "UTILITY_MODEL": "实用新型", "DESIGN": "外观设计", "PCT": "PCT"}
    patent_type = text(source.get("PATENT_TYPE")) or type_names.get(b.get("patent_type"), text(b.get("patent_type")))
    # Some MCP search results use the generic PATENT enum for US grants.
    # Restrict this conversion to US B-kind publications; do not infer all jurisdictions.
    if patent_type == "PATENT" and re.fullmatch(r"US\d+B\d", pn):
        patent_type = "授权发明"
    row = {"申请号": text(source.get("apno") or source.get("APNO")), "公开(公告)号": pn,
           "专利类型": patent_type, "简单同族编号": str(family_id)}
    if missing_legal:
        row["__source_status"] = "状态待确认"
        row["__source_warning"] = "来源未提供简单法律状态；已有状态保留，新记录待确认"
    else:
        row["来源简单法律状态"] = "|".join(legal)
    titles = b.get("invention_title", [])
    cn_titles = [t["text"] for t in titles if t.get("lang", "").upper() == "CN"]
    original = [t["text"] for t in titles if t.get("data_format") == "original"]
    row["标题"] = text(source.get("TTL") or source.get("title") or original[:1])
    if cn_titles:
        row["标题(译)(简体中文)"] = cn_titles[0]
    parties = b.get("parties", {})
    for field, part in (("当前申请(专利权)人", "assignees"), ("原始申请(专利权)人", "applicants"), ("发明人", "inventors")):
        values = parties.get(part, [])
        # Preserve one language per name sequence instead of duplicating translations.
        names = {}
        for item in values:
            seq = item.get("sequence")
            if seq not in names or item.get("lang", "").upper() == "CN":
                names[seq] = item.get("name", "")
        row[field] = text(list(names.values()))
    row["申请日"] = day(source.get("APDT") or source.get("apdt") or b.get("application_reference", {}).get("date"))
    row["公开(公告)日"] = day(source.get("PBDT") or source.get("pbdt") or b.get("publication_reference", {}).get("date"))
    row["授权日"] = day(source.get("ISDT"))
    country = text(source.get("COUNTRY") or source.get("authority") or b.get("publication_reference", {}).get("country"))
    row["受理局"] = {"CN": "中国", "US": "美国", "EP": "欧洲专利局", "WO": "世界知识产权组织", "JP": "日本", "KR": "韩国"}.get(country, country)
    if pn.startswith("WO"):
        row["PCT国际申请公开号"] = pn
    statuses = set(legal)
    if len(statuses) == 1:
        state = next(iter(statuses))
        if state == "失效":
            row["__source_status"] = "已失效"
        elif state in {"审中", "PCT指定期内", "PCT指定期满"}:
            row["__source_status"] = "申请中" if state != "PCT指定期满" else "状态待确认"
        elif state == "有效" and any(v in patent_type for v in ("授权", "实用新型", "外观设计")):
            row["__source_status"] = "已授权"
    return row


class PatsnapProvider:
    def __init__(self, config, credentials, *, mode="api"):
        self.config, self.credentials, self.mode = config, credentials, mode
        self.endpoint = config.get("patsnap_mcp_endpoint", PATSNAP_MCP)

    def mcp(self):
        return Mcp(self.endpoint, self.credentials("patsnap_mcp")).initialize()

    def fetch(self, scope, run_dir, progress):
        run_dir = Path(run_dir)
        query = scope_query(scope)
        save_json(run_dir / "query.json", {"query": query, "collapse_type": "APNO", "collapse_by": "PBD", "collapse_order": "LATEST"})
        mcp = self.mcp()
        args = {"query_text": query, "limit": 1, "offset": 0, "collapse_type": "APNO", "collapse_by": "PBD", "collapse_order": "LATEST"}
        search = mcp.call("search_patents", args)
        total = count_value(search["data"]["total_search_result_count"])
        if total == 0:
            raise SourceError("智慧芽检索为空，未更新总表")
        progress(f"检索到 {total} 条，正在获取数据")
        if self.mode == "api":
            api = PatsnapApi(self.credentials("patsnap_api"))
            raw_path, task_path = run_dir / "export.json", run_dir / "remote-task.json"
            if not raw_path.exists():
                task = read_json(task_path)
                if not task:
                    intent = run_dir / "export-intent.json"
                    if intent.exists():
                        raise SourceError("上次创建导出任务结果未知，请核对智慧芽任务记录后补入任务编号，勿重复导出")
                    save_json(intent, {"query": query, "expected": total})
                    try:
                        task = api.create(query, total)
                    except SourceError as exc:
                        if not exc.uncertain:
                            intent.unlink(missing_ok=True)
                        raise
                    if not isinstance(task, dict) or not task.get("task_id") or task.get("count") != total:
                        raise SourceError("导出任务编号或记录数量异常，未导入")
                    save_json(task_path, {"task_id": task["task_id"], "count": task["count"]})
                for attempt in range(90):
                    detail = api.task(task["task_id"])
                    if detail.get("export_status") == "SUCCESS":
                        if detail.get("count") != total:
                            raise SourceError("导出数量与检索总数不一致，未导入")
                        source, raw = api.download(detail.get("url"))
                        raw_path.write_bytes(raw)
                        break
                    if detail.get("export_status") != "PENDING":
                        raise SourceError("智慧芽导出任务失败或状态异常")
                    progress(f"等待智慧芽导出，共 {total} 条")
                    time.sleep(3)
                else:
                    raise SourceError("导出仍在处理中；可点击继续获取，复用已有任务")
            source = read_json(raw_path)
            if not isinstance(source, list) or len(source) != total:
                raise SourceError("下载记录数与检索总数不一致，未导入")
        else:
            source = []
            for offset in range(0, total, 50):
                result = mcp.call("search_patents", {**args, "limit": 50, "offset": offset})
                save_json(run_dir / f"search-{offset}.json", result)
                page = result["data"]["results"]
                if (result["data"]["total_search_result_count"] != total or not isinstance(page, list)
                        or len(page) != min(50, total - offset)):
                    raise SourceError("MCP 检索分页不完整或数量变化，未导入")
                source.extend(page)
        if len({text(v.get("PATENT_ID") or v.get("patent_id")) for v in source}) != total:
            raise SourceError("检索记录存在重复专利标识，未导入")
        local = threading.local()

        def enrich(record):
            pid = text(record.get("PATENT_ID") or record.get("patent_id"))
            if not re.fullmatch(r"[A-Za-z0-9-]+", pid):
                raise SourceError("专利标识无效")
            cache = run_dir / ("detail-" + pid + ".json")
            evidence = read_json(cache)
            if evidence is None:
                if not hasattr(local, "mcp"):
                    local.mcp = self.mcp()
                evidence = {}
                for key, name in (("bibliography", "bibliography"), ("family", "family"), ("legal", "get_patent_legal_status")):
                    evidence[key] = local.mcp.call(name, {"patent_id": pid})
                save_json(cache, evidence)
            return normalize_detail(record, evidence)

        rows = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(enrich, record) for record in source]
            try:
                for future in as_completed(futures):
                    rows.append(future.result())
                    progress(f"已核对 {len(rows)}/{total} 条")
            except Exception:
                for future in futures:
                    future.cancel()
                raise
        return sorted(rows, key=lambda r: normalize_application_no(r["申请号"]))


class GenericMcpProvider:
    """Configurable paged MCP source; no dependency on PatSnap tool names."""
    def __init__(self, config, credentials):
        self.config = config["generic_mcp"]
        self.credentials = credentials

    def fetch(self, scope, run_dir, progress):
        c = self.config
        query = scope_query(scope, family_template=c.get("family_query_template", "FAM_ID:({values})"),
                            application_template=c.get("application_query_template", "APNO:({values})"))
        mcp = Mcp(c["endpoint"], self.credentials("generic_mcp")).initialize()
        if c["tool"] not in {t["name"] for t in mcp.tools()}:
            raise SourceError("配置的 MCP 工具不存在")
        offset, total, rows = 0, None, []
        size = c.get("page_size", 50)
        if type(size) is not int or not 1 <= size <= 100:
            raise SourceError("MCP 每页数量须为 1 至 100")
        mapping = c["field_map"]
        if "申请号" not in mapping or set(mapping) - TABLE_FIELDS:
            raise SourceError("MCP 字段映射缺少申请号或包含非来源字段")
        while total is None or offset < total:
            def replace(value):
                if value == "{offset}": return offset
                if value == "{limit}": return size
                if value == "{query}": return query
                if isinstance(value, dict): return {k: replace(v) for k, v in value.items()}
                if isinstance(value, list): return [replace(v) for v in value]
                return value
            result = mcp.call(c["tool"], replace(c["arguments"]))
            save_json(Path(run_dir) / f"mcp-page-{offset}.json", result)
            current_total = count_value(path_value(result, c["total_path"]))
            page = path_value(result, c["rows_path"])
            if total is None: total = current_total
            if current_total != total or not isinstance(page, list) or len(page) != min(size, total-offset):
                raise SourceError("MCP 分页数量不完整或变化，未导入")
            for item in page:
                row = {field: text(path_value(item, path)) for field, path in mapping.items()}
                if "__source_status" in row:
                    row["__source_status"] = c.get("status_map", {}).get(row["__source_status"], row["__source_status"])
                    if row["__source_status"] not in STATUSES:
                        raise SourceError("MCP 状态未映射到系统状态，请补充 status_map")
                rows.append(row)
            offset += len(page)
            progress(f"已获取 {offset}/{total} 条")
        if not rows:
            raise SourceError("MCP 检索为空，未更新总表")
        return rows


PROVIDERS = {}


def register_source(name, factory):
    PROVIDERS[name] = factory


def create_source(name, config, credentials):
    if name not in PROVIDERS:
        raise ValueError("未知数据源")
    return PROVIDERS[name](config, credentials)


register_source("patsnap_api", lambda c, s: PatsnapProvider(c, s, mode="api"))
register_source("patsnap_mcp", lambda c, s: PatsnapProvider(c, s, mode="mcp"))
register_source("generic_mcp", GenericMcpProvider)
