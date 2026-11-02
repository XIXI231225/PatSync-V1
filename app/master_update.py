"""Master-table update orchestration, independent of source/MCP vendor."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from core import normalize_application_no
from database import normalize_family_id, patent_state_hash
from patent_source_client import SourceCredentials, SourceError, validate_endpoint
from patent_sources import (PATSNAP_MCP, PROVIDERS, STATUSES, TABLE_FIELDS, create_source, day,
                            read_json, save_json)
from master_table_format import POLICY_VERSION, project_row, actual_diffs


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


@contextmanager
def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def acquire(path):
    handle = open(path, "a+b")
    handle.seek(0, os.SEEK_END)
    if not handle.tell():
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError("总表获取或更新正在运行，请稍后查看结果") from None
    return handle


def release(handle):
    handle.close()


class MasterUpdateService:
    def __init__(self, data_dir, db, *, credentials=None, provider_factory=None):
        self.db = db
        self.root = Path(data_dir) / "master-update"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        self.config_path = self.root / "config.json"
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "update.lock"
        self.credential_loader = credentials or (lambda name: SourceCredentials(name).load())
        self.provider_factory = provider_factory or create_source
        self.thread = None

    def config(self):
        return read_json(self.config_path, {"provider": "patsnap_api", "patsnap_mcp_endpoint": PATSNAP_MCP})

    def settings(self):
        config = self.config()
        return {**config, "providers": list(PROVIDERS), "credentials": {name: bool(self.credential_loader(name)) for name in
                                         ("patsnap_api", "patsnap_mcp", "generic_mcp")}}

    def save_settings(self, payload):
        handle = acquire(self.lock_path)
        try:
            config = self.config()
            name = payload.get("provider", config["provider"])
            if name not in PROVIDERS:
                raise ValueError("数据源不存在")
            config["provider"] = name
            config["patsnap_mcp_endpoint"] = validate_endpoint(payload.get("patsnap_mcp_endpoint", config.get("patsnap_mcp_endpoint", PATSNAP_MCP)))
            if "generic_mcp" in payload:
                generic = payload["generic_mcp"]
                required = {"endpoint", "tool", "arguments", "rows_path", "total_path", "field_map"}
                allowed = required | {"page_size", "family_query_template", "application_query_template", "status_map"}
                if not isinstance(generic, dict) or not required.issubset(generic) or set(generic) - allowed:
                    raise ValueError("其他 MCP 配置字段不完整或存在不支持的配置")
                validate_endpoint(generic["endpoint"])
                if not isinstance(generic["arguments"], dict) or not isinstance(generic["field_map"], dict):
                    raise ValueError("MCP 参数及字段映射须为 JSON 对象")
                raw = json.dumps(generic, ensure_ascii=False)
                if re.search(r"(?i)sk-|bearer\s|api[_-]?key|secret|token|authorization", raw):
                    raise ValueError("MCP 配置中不能包含密钥，请用单独的密钥栏")
                if "申请号" not in generic["field_map"] or set(generic["field_map"]) - TABLE_FIELDS:
                    raise ValueError("MCP 字段映射缺少申请号或包含非来源字段")
                for template in (generic.get("family_query_template", "{values}"), generic.get("application_query_template", "{values}")):
                    if "{values}" not in template:
                        raise ValueError("检索模板须保留 {values} 占位符")
                config["generic_mcp"] = generic
            for name in ("patsnap_api", "patsnap_mcp", "generic_mcp"):
                value = str(payload.get(name + "_key") or "").strip()
                if value:
                    SourceCredentials(name).save(value)
            save_json(self.config_path, config)
        finally:
            release(handle)
        return self.settings()

    def _state(self, state, **changes):
        state.update(changes, updated_at=now())
        save_json(self.root / "runs" / state["run_id"] / "state.json", state)
        save_json(self.state_path, state)

    def status(self):
        state = read_json(self.state_path, {"status": "idle", "message": "尚未获取"})
        if state.get("status") == "fetching":
            try:
                handle = acquire(self.lock_path)
            except ValueError:
                pass
            else:
                release(handle)
                state = {**state, "status": "interrupted", "message": "上次获取已中断，可继续获取并复用已下载数据"}
        if state.get("run_id"):
            state["evidence_dir"] = str(self.root / "runs" / state["run_id"])
        return state

    def _snapshot(self):
        with readonly(self.db.path) as conn:
            signature = patent_state_hash(conn)
            records = [dict(row) for row in conn.execute("SELECT * FROM patents ORDER BY app_key")]
        families = sorted({normalize_family_id(row["family_no"]) for row in records} - {""}, key=int)
        scope = {"family_ids": families, "application_numbers": [row["application_no"] for row in records]}
        return signature, records, scope

    def start(self, resume=False):
        handle = acquire(self.lock_path)
        try:
            config = self.config()
            old = read_json(self.state_path, {})
            if resume:
                if old.get("status") not in {"failed", "fetching", "interrupted"}:
                    raise ValueError("当前没有可继续的获取任务")
                state = old
                run_dir = self.root / "runs" / state["run_id"]
                config = read_json(run_dir / "config.json")
                signature, _, scope = self._snapshot()
                if signature != state["base_state"]:
                    raise ValueError("总表已变化，请重新获取")
            else:
                # Never create a second export after losing the first creation response.
                if old.get("run_id"):
                    old_dir = self.root / "runs" / old["run_id"]
                    if (old_dir / "export-intent.json").exists() and not (old_dir / "remote-task.json").exists():
                        raise ValueError("上次导出创建结果未知，请先核对智慧芽任务编号")
                signature, _, scope = self._snapshot()
                if not scope["application_numbers"]:
                    raise ValueError("总表为空，未发起查询")
                if any(normalize_application_no(v).startswith("DEMO") for v in scope["application_numbers"]):
                    raise ValueError("参赛库含虚构编号；请用参赛样例离线预览，实时 MCP 请用 Skill 的公开样本入口")
                state = {"run_id": uuid.uuid4().hex, "provider": config["provider"], "base_state": signature,
                         "policy_version": POLICY_VERSION, "created_at": now()}
                run_dir = self.root / "runs" / state["run_id"]
                run_dir.mkdir()
                save_json(run_dir / "config.json", config)
                save_json(run_dir / "scope.json", scope)
            required = {"patsnap_api": ("patsnap_api", "patsnap_mcp"), "patsnap_mcp": ("patsnap_mcp",), "generic_mcp": ("generic_mcp",)}.get(config["provider"], ())
            if not all(self.credential_loader(name) for name in required) and config["provider"] != "generic_mcp":
                raise ValueError("请先保存所需数据源密钥")
            self._state(state, status="fetching", message="正在连接数据源", can_apply=False)
            self.thread = threading.Thread(target=self._fetch, args=(state, config, scope, run_dir, handle), daemon=True)
            self.thread.start()
        except Exception:
            release(handle)
            raise
        return self.status()

    def _fetch(self, state, config, scope, run_dir, handle):
        try:
            provider = self.provider_factory(config["provider"], config, self.credential_loader)
            def progress(message):
                self._state(state, message=message)
            rows = provider.fetch(scope, run_dir, progress)
            save_json(run_dir / "normalized-rows.json", rows)
            self._prepare(state, rows)
        except (SourceError, ValueError) as exc:
            self._state(state, status="failed", message=str(exc), can_apply=False)
        except Exception:
            self._state(state, status="failed", message="数据源格式或本地环境异常，未更新总表；请核对接口配置", can_apply=False)
        finally:
            release(handle)

    def _prepare(self, state, rows):
        signature, records, scope = self._snapshot()
        if signature != state["base_state"]:
            raise ValueError("获取期间总表已有变化，请重新获取")
        if not isinstance(rows, list) or not rows:
            raise ValueError("数据源没有有效记录")
        existing = {row["app_key"]: row for row in records}
        families, seen, prepared = set(scope["family_ids"]), set(), []
        seeds = {}
        for current in records:
            fid = normalize_family_id(current["family_no"])
            raw = json.loads(current["raw_json"])
            for field in ("应用领域分类", "业务分类"):
                value = raw.get(field)
                if fid and value not in (None, "", "-", "—"):
                    seeds.setdefault((fid, field), set()).add(str(value))
        # A newly discovered family link contributes its existing manual labels,
        # but never makes those labels identical across separate applications.
        for source in rows:
            if not isinstance(source, dict):
                raise ValueError("数据源记录格式异常")
            current = existing.get(normalize_application_no(source.get("申请号")))
            fid = normalize_family_id(source.get("简单同族编号"))
            if current and fid:
                raw = json.loads(current["raw_json"])
                for field in ("应用领域分类", "业务分类"):
                    if raw.get(field) not in (None, "", "-", "—"):
                        seeds.setdefault((fid, field), set()).add(str(raw[field]))
        statuses, warnings, reviews = {}, {}, {}
        for index, source in enumerate(rows, 1):
            if not isinstance(source, dict) or set(source) - TABLE_FIELDS:
                raise ValueError("数据源包含不支持的字段或人工维护字段，未导入")
            key = normalize_application_no(source.get("申请号"))
            if not key or key in seen:
                raise ValueError("数据源申请号为空或重复，未导入")
            seen.add(key)
            current = existing.get(key)
            current_raw = json.loads(current["raw_json"]) if current else {}
            fid = normalize_family_id(source.get("简单同族编号"))
            if source.get("简单同族编号") not in (None, "", "-", "—") and not fid:
                raise ValueError("数据源简单同族编号格式异常")
            if key not in existing and fid not in families:
                raise ValueError("数据源含检索范围外记录，未导入")
            source = dict(source)
            for field in ("申请日", "公开(公告)日", "授权日"):
                if source.get(field): source[field] = day(source[field])
            row, reviews[key] = project_row(source, current)
            fid = normalize_family_id(row.get("简单同族编号"))
            row_warnings = [source.get("__source_warning", "")]
            for field in ("应用领域分类", "业务分类"):
                values = seeds.get((fid, field), set())
                if current_raw.get(field) not in (None, "", "-", "—"):
                    row[field] = current_raw[field]
                elif len(values) == 1:
                    row[field] = next(iter(values))
                elif len(values) > 1:
                    row_warnings.append(f"同族{field}不唯一，分类待确认")
            status = source.get("__source_status")
            warnings[key] = "；".join(filter(None, row_warnings))
            if status is not None and status not in STATUSES:
                raise ValueError("数据源当前状态映射无效")
            statuses[key] = status
            row["__row__"] = index
            prepared.append(row)
        preview = self.db.build_import_preview(prepared, inherit_family=False)
        for item in preview:
            if item["action"] == "跳过":
                raise ValueError("数据含重复、分类冲突或无效记录，整批未导入")
            current = existing.get(item["app_key"])
            status = statuses[item["app_key"]]
            if warnings[item["app_key"]]:
                item["message"] = "；".join(filter(None, (item.get("message"), warnings[item["app_key"]])))
            if not current and status:
                item["status"] = status
            if current:
                item["status"] = current["current_status"]
                item["changes"] = [field for field in item["changes"] if field != "当前状态"]
            item["diffs"] = actual_diffs(item, current)
            item["changes"] = [d["field"] for d in item["diffs"]]
            item["review"] = reviews[item["app_key"]]
            item["action"] = ("更新" if item["diffs"] else "不变") if current else "待确认"
        final_signature, _, _ = self._snapshot()
        if final_signature != signature:
            raise ValueError("生成预览期间总表已有变化，请重新获取")
        run_dir = self.root / "runs" / state["run_id"]
        save_json(run_dir / "preview.json", preview)
        counts = {name: sum(i["action"] == name for i in preview) for name in ("新增", "更新", "不变", "跳过", "待确认")}
        missing = [record["application_no"] for key, record in existing.items() if key not in seen]
        public = [{k: item[k] for k in ("row", "application_no", "action", "changes", "message", "diffs", "review")} for item in preview]
        self._state(state, status="ready", policy_version=POLICY_VERSION,
                    message="已按原总表格式整理；待核对字段保留原值，新增候选本次不写入",
                    can_apply=counts["更新"] + counts["新增"] > 0,
                    review_rows=sum(bool(i["review"]) for i in preview),
                    review_fields=sum(len(i["review"]) for i in preview),
                    counts=counts, total=len(preview), missing_applications=missing, items=public)

    def rebuild_preview(self, source_run_id):
        """Re-project saved source evidence using current rules without API calls."""
        if not re.fullmatch(r"[0-9a-f]{32}", str(source_run_id)):
            raise ValueError("来源批次无效")
        handle = acquire(self.lock_path)
        try:
            origin = self.root / "runs" / source_run_id
            rows = read_json(origin / "normalized-rows.json")
            if not rows: raise ValueError("该批次没有已下载的数据")
            signature, _, scope = self._snapshot()
            state = {"run_id": uuid.uuid4().hex, "provider": read_json(origin / "config.json", self.config())["provider"],
                     "base_state": signature, "policy_version": POLICY_VERSION, "source_run_id": source_run_id,
                     "created_at": now()}
            run = self.root / "runs" / state["run_id"]
            run.mkdir()
            save_json(run / "config.json", read_json(origin / "config.json", self.config()))
            save_json(run / "scope.json", scope)
            save_json(run / "normalized-rows.json", rows)
            self._prepare(state, rows)
            return self.status()
        finally:
            release(handle)

    def apply(self, run_id):
        if not re.fullmatch(r"[0-9a-f]{32}", str(run_id)):
            raise ValueError("更新任务编号无效")
        handle = acquire(self.lock_path)
        try:
            state = read_json(self.state_path, {})
            if state.get("run_id") != run_id:
                raise ValueError("预览已被替换，请刷新")
            operation = "数据源更新:" + run_id
            with readonly(self.db.path) as conn:
                committed = conn.execute("SELECT 1 FROM operations WHERE operation=? AND result='成功'", (operation,)).fetchone()
            if committed:
                self._state(state, status="applied", can_apply=False, message="此批更新已完成")
                return {"already_applied": True, **state.get("result", {})}
            if state.get("status") != "ready":
                raise ValueError("任务尚未形成可用预览")
            if state.get("policy_version") != POLICY_VERSION:
                raise ValueError("预览规则已升级，请重新生成预览")
            if not state.get("can_apply"):
                raise ValueError("本批没有可直接写入的变化；待核对项未写入")
            run_dir = self.root / "runs" / run_id
            with readonly(self.db.path) as conn, closing(sqlite3.connect(run_dir / "before-update.db")) as backup:
                conn.backup(backup)
            preview = read_json(run_dir / "preview.json")
            result = self.db.execute_import(preview, operation=operation, expected_state=state["base_state"])
            self._state(state, status="applied", can_apply=False, message="总表更新完成", result=result)
            return result
        finally:
            release(handle)
