"""One-way patent mirror. Patent data is read only from the existing SQLite DB.

The separate state DB stores delivery metadata and immutable pending requests,
never an independently editable patent register.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from core import DEMO_GROUP_APPLICANTS, normalize_application_no
from database import publication_no_from_raw
from feishu_client import CredentialStore, FeishuClient, FeishuError

CHINA = timezone(timedelta(hours=8))
FIELDS = {name: 1 for name in (
    "申请号", "同步键", "同步来源", "中文名称", "原文名称", "公开公告号", "当前状态",
    "法律状态事件", "当前权利人", "原始申请人", "发明人", "国家地区", "专利类型",
    "简单同族编号", "PCT公开号", "应用领域", "业务分类", "集团归属", "源更新时间", "最后同步时间")}
FIELDS.update({"申请日": 5, "公开日": 5, "授权日": 5})
OWNED_FIELDS = set(FIELDS) - {"最后同步时间"}


def now():
    return datetime.now(CHINA).isoformat(timespec="seconds")


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(packed(value).encode("utf-8")).hexdigest()


def party_name(value):
    return re.sub(r"\s+", "", str(value or "")).replace("（", "(").replace("）", ")")


GROUP = {party_name(value) for value in DEMO_GROUP_APPLICANTS}


def group_scope(holder, approval=None):
    parts = {party_name(v) for v in re.split(r"[|｜;；、,，\r\n]+", str(holder or "")) if party_name(v)}
    if not parts:
        return "待核对"
    exact = parts & GROUP
    embedded = any(g in party_name(holder) for g in GROUP)
    if approval and approval.get("owner_hash") == digest(holder) and embedded:
        return "集团对外共有"
    if not exact:
        return "待核对" if embedded else "非集团"
    if any(any(g in p for g in GROUP) and p not in GROUP for p in parts):
        return "待核对"
    if parts - GROUP:
        return "集团对外共有"
    return "集团内部共有" if len(parts) > 1 else "集团内部独有"


def date_value(value):
    text = str(value or "").strip()
    if text in {"", "-", "—"}:
        return None
    try:
        day = datetime.strptime(text[:10].replace("/", "-"), "%Y-%m-%d").replace(tzinfo=CHINA)
    except ValueError:
        raise ValueError("源库日期格式异常，需先核对") from None
    return int(day.timestamp() * 1000)


def text_value(value):
    if value is None:
        return ""
    if isinstance(value, list):
        if all(isinstance(v, dict) and "text" in v for v in value):
            return "".join(str(v["text"]) for v in value)
        raise ValueError("飞书文本字段格式异常")
    if not isinstance(value, (str, int, float)):
        raise ValueError("飞书字段格式异常")
    return str(value)


def source_snapshot(path, source_id, approvals=None):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("找不到源数据库，未创建空库，也不会执行同步")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rows = [dict(row) for row in conn.execute("SELECT * FROM patents ORDER BY id")]
    finally:
        conn.close()
    result, excluded, pending, seen, scopes = {}, [], [], set(), {}
    for row in rows:
        key = normalize_application_no(row["application_no"])
        if not key or key != row["app_key"] or key in seen:
            raise ValueError("源库申请号为空、不一致或重复，本轮禁止写入")
        seen.add(key)
        scope = group_scope(row["applicant_current"], (approvals or {}).get(key))
        scopes[scope] = scopes.get(scope, 0) + 1
        if scope == "非集团":
            excluded.append(row["application_no"])
            continue
        if scope == "待核对":
            pending.append({"application_no": row["application_no"], "holder": row["applicant_current"]})
            continue
        raw = json.loads(row["raw_json"] or "{}")
        if not isinstance(raw, dict):
            raise ValueError("源库原始字段格式异常")
        fields = {"申请号": row["application_no"], "同步键": key, "同步来源": source_id,
                  "中文名称": row["title_cn"] or "", "原文名称": row["title_original"] or "",
                  "公开公告号": publication_no_from_raw(raw), "当前状态": row["current_status"] or "",
                  "法律状态事件": row["legal_event"] or "", "当前权利人": row["applicant_current"] or "",
                  "原始申请人": row["applicant_original"] or "", "发明人": row["inventors"] or "",
                  "国家地区": row["country"] or "", "专利类型": row["patent_type"] or "",
                  "简单同族编号": row["family_no"] or "", "PCT公开号": raw.get("PCT国际申请公开号") or "",
                  "应用领域": raw.get("应用领域分类") or "", "业务分类": raw.get("业务分类") or "",
                  "集团归属": scope, "源更新时间": row["updated_at"] or "",
                  "申请日": date_value(row["application_date"]), "公开日": date_value(row["publication_date"]),
                  "授权日": date_value(row["grant_date"])}
        fields = {name: (str(value) if value is not None else "") if FIELDS[name] == 1 else value
                  for name, value in fields.items()}
        result[key] = fields
    return {"total": len(rows), "items": result, "excluded": excluded, "pending": pending, "scopes": scopes}


class BusyError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path):
        self.path, self.handle = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise BusyError("同步正在执行，请稍后查看结果") from None
        return self

    def __exit__(self, *args):
        if self.handle:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()


class SyncService:
    def __init__(self, data_dir, source_db=None, *, client_factory=None, credential_factory=CredentialStore):
        self.data_dir = Path(data_dir).resolve()
        self.folder = self.data_dir / "feishu_sync"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.source = Path(source_db or self.data_dir / "patents.db").resolve()
        self.config_path = self.folder / "settings.json"
        self.state_path, self.lock_path = self.folder / "state.sqlite3", self.folder / "sync.lock"
        self.client_factory = client_factory
        self.credential_factory = credential_factory
        self.thread = None
        with ProcessLock(self.lock_path):
            if not self.config_path.exists():
                self._write_config({"enabled": False, "mode": "test", "app_id": "", "app_token": "", "table_id": "",
                                    "source_id": "DEMO-V3.1-" + uuid.uuid4().hex, "scope_approvals": {},
                                    "secret_revision": "", "live_test": None})
            with self.connect() as conn:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs(
                  id TEXT PRIMARY KEY, target TEXT NOT NULL, trigger TEXT NOT NULL, started TEXT NOT NULL,
                  finished TEXT, status TEXT NOT NULL, counts TEXT NOT NULL DEFAULT '{}', message TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS items(
                  target TEXT NOT NULL, app_key TEXT NOT NULL, record_id TEXT, content_hash TEXT,
                  pending_token TEXT, pending_payload TEXT, pending_status TEXT, pending_at TEXT,
                  PRIMARY KEY(target,app_key));
                CREATE TABLE IF NOT EXISTS events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, app_key TEXT,
                  action TEXT NOT NULL, result TEXT NOT NULL, message TEXT NOT NULL, created TEXT NOT NULL);
                """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.state_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def config(self):
        value = json.loads(self.config_path.read_text("utf-8"))
        if not isinstance(value, dict) or not value.get("source_id"):
            raise ValueError("同步配置损坏，请恢复配置备份")
        return value

    def _write_config(self, value):
        temp = self.config_path.with_suffix(".tmp")
        temp.write_text(packed(value), encoding="utf-8")
        os.replace(temp, self.config_path)

    def save_settings(self, payload):
        with ProcessLock(self.lock_path):
            cfg = self.config()
            for name, pattern in (("app_id", r"cli_[A-Za-z0-9]+"), ("app_token", r"[A-Za-z0-9]+"),
                                  ("table_id", r"tbl[A-Za-z0-9]+")):
                value = str(payload.get(name, cfg[name])).strip()
                if value and not re.fullmatch(pattern, value):
                    raise ValueError(f"{name} 格式不正确，请填写标识而不是完整链接")
                cfg[name] = value
            mode = payload.get("mode", cfg["mode"])
            if mode not in {"test", "production"}:
                raise ValueError("请选择测试表或正式表")
            cfg["mode"] = mode
            if "enabled" in payload and type(payload["enabled"]) is not bool:
                raise ValueError("启用状态必须为布尔值")
            cfg["enabled"] = payload.get("enabled", cfg["enabled"])
            secret = payload.get("app_secret", "")
            if not isinstance(secret, str):
                raise ValueError("密钥格式不正确")
            if secret.strip():
                if not cfg["app_id"]:
                    raise ValueError("请先填写 App ID")
                self.credential_factory(cfg["app_id"]).save(secret.strip())
                cfg["secret_revision"] = uuid.uuid4().hex
                cfg["live_test"] = None
            self._write_config(cfg)
        return self.status()

    def approve_scope(self, expected):
        """Explicit human approvals are bound to the exact current owner string."""
        with ProcessLock(self.lock_path):
            cfg = self.config()
            conn = sqlite3.connect(self.source.as_uri() + "?mode=ro", uri=True)
            try:
                for key, holder in expected.items():
                    row = conn.execute("SELECT applicant_current FROM patents WHERE app_key=?", (key,)).fetchone()
                    if not row or row[0] != holder or group_scope(holder) != "待核对":
                        raise ValueError("待核对记录已变化，未应用旧确认")
                    cfg["scope_approvals"][key] = {"owner_hash": digest(holder), "confirmed_at": now()}
            finally:
                conn.close()
            self._write_config(cfg)

    @staticmethod
    def target(cfg):
        return packed([cfg["app_id"], cfg["app_token"], cfg["table_id"]])

    def client(self, cfg):
        if not all(cfg.get(k) for k in ("app_id", "app_token", "table_id")):
            raise ValueError("请先配置 App ID、app_token 和 table_id")
        if self.client_factory:
            return self.client_factory(cfg)
        secret = self.credential_factory(cfg["app_id"]).load()
        if not secret:
            raise ValueError("当前 Windows 账户未保存此应用的 App Secret")
        return FeishuClient(cfg, secret)

    def preview_source(self):
        cfg = self.config()
        value = source_snapshot(self.source, cfg["source_id"], cfg.get("scope_approvals"))
        return {"total": value["total"], "included": len(value["items"]), "excluded": len(value["excluded"]),
                "pending": value["pending"], "scopes": value["scopes"]}

    @staticmethod
    def check_schema(client):
        fields = client.list_fields()
        by_name = {f.get("field_name"): f.get("type") for f in fields}
        if len(by_name) != len(fields):
            raise ValueError("飞书存在重复字段名，请先核对")
        missing = [k for k in FIELDS if k not in by_name]
        wrong = [k for k, kind in FIELDS.items() if k in by_name and by_name[k] != kind]
        if missing or wrong:
            raise ValueError("飞书字段不匹配。缺少：" + "、".join(missing) + "；类型异常：" + "、".join(wrong))

    def prepare_fields(self):
        with ProcessLock(self.lock_path):
            cfg = self.config()
            if cfg["mode"] != "test":
                raise ValueError("只允许在测试表初始化字段；正式表请复制验证过的空表结构")
            client = self.client(cfg)
            records = client.list_records()
            if any(any(text_value(v).strip() for v in r.get("fields", {}).values() if v is not None) for r in records):
                raise ValueError("测试表已有内容，请使用独立空表初始化字段")
            names = {f["field_name"]: f["type"] for f in client.list_fields()}
            added = []
            for key, kind in FIELDS.items():
                if key not in names:
                    client.add_field(key, kind)
                    added.append(key)
                elif names[key] != kind:
                    raise ValueError(f"字段 {key} 类型不正确，未修改已有字段")
            self.check_schema(client)
            return {"message": f"字段检查通过，新增 {len(added)} 个字段"}

    def test_connection(self):
        with ProcessLock(self.lock_path):
            client = self.client(self.config())
            self.check_schema(client)
            records = client.list_records()
            return {"message": f"应用认证、字段和分页读取通过；共 {len(records)} 行。写权限由测试验收验证。"}

    def preview_diff(self):
        with ProcessLock(self.lock_path):
            cfg = self.config()
            snapshot = source_snapshot(self.source, cfg["source_id"], cfg.get("scope_approvals"))
            client = self.client(cfg)
            self.check_schema(client)
            remote = self.remote_index(client.list_records(), cfg["source_id"])
            desired = snapshot["items"]
            return {"added": [v["申请号"] for k, v in desired.items() if k not in remote],
                    "updated": [v["申请号"] for k, v in desired.items() if k in remote and not self.same_fields(remote[k]["fields"], v)],
                    "removed": [text_value(v["fields"].get("申请号")) for k, v in remote.items() if k not in desired],
                    "unchanged": sum(k in remote and self.same_fields(remote[k]["fields"], v) for k, v in desired.items()),
                    "pending": snapshot["pending"], "excluded": len(snapshot["excluded"])}

    def status(self):
        cfg = self.config()
        public = {k: cfg[k] for k in ("enabled", "mode", "app_id", "app_token", "table_id")}
        try:
            public["has_secret"] = bool(cfg["app_id"] and self.credential_factory(cfg["app_id"]).load())
        except RuntimeError:
            public["has_secret"] = False
        target = self.target(cfg)
        with self.connect() as conn:
            recent = [dict(r) for r in conn.execute("SELECT * FROM runs WHERE target=? ORDER BY rowid DESC LIMIT 15", (target,))]
            last = conn.execute("SELECT finished FROM runs WHERE target=? AND status='success' AND trigger!='acceptance' ORDER BY rowid DESC LIMIT 1", (target,)).fetchone()
            events = [dict(r) for r in conn.execute("SELECT e.* FROM events e JOIN runs r ON e.run_id=r.id WHERE r.target=? ORDER BY e.id DESC LIMIT 60", (target,))]
        for r in recent:
            r["counts"] = json.loads(r["counts"])
        busy = False
        try:
            with ProcessLock(self.lock_path):
                pass
        except BusyError:
            busy = True
        return {"settings": public, "last_success": last[0] if last else None, "running": busy,
                "runs": recent, "events": events, "live_test": cfg.get("live_test"),
                "source_db": str(self.source), "schedule": "每天 11:00，错过不补跑；需验收后另行安装任务"}

    def task_ready(self):
        cfg = self.config()
        proof = cfg.get("live_test") or {}
        if (not cfg["enabled"] or cfg["mode"] != "production" or not proof.get("passed") or
                proof.get("app_id") != cfg["app_id"] or proof.get("secret_revision") != cfg.get("secret_revision") or
                proof.get("target") == self.target(cfg)):
            raise ValueError("须先通过测试表验收，并启用不同的正式表")
        if not self.status()["last_success"]:
            raise ValueError("须先完成一次正式表成功同步，再安装每日任务")
        self.client(cfg)  # Verify that this Windows account can read the saved credential.
        return {"status": "success", "message": "每日任务安装前提已满足"}

    def _event(self, run_id, key, action, result, message=""):
        with self.connect() as conn:
            conn.execute("INSERT INTO events(run_id,app_key,action,result,message,created) VALUES(?,?,?,?,?,?)",
                         (run_id, key, action, result, message, now()))

    def _ack(self, target, key, record_id, fields):
        with self.connect() as conn:
            conn.execute("""INSERT INTO items(target,app_key,record_id,content_hash) VALUES(?,?,?,?)
              ON CONFLICT(target,app_key) DO UPDATE SET record_id=excluded.record_id,content_hash=excluded.content_hash,
              pending_token=NULL,pending_payload=NULL,pending_status=NULL,pending_at=NULL""",
                         (target, key, record_id, digest(fields)))

    @staticmethod
    def remote_index(records, source_id):
        indexed, seen_ids = {}, set()
        for record in records:
            rid, fields = record.get("record_id"), record.get("fields")
            if not rid or rid in seen_ids or not isinstance(fields, dict):
                raise ValueError("飞书记录结构或 ID 异常，本轮禁止写入")
            seen_ids.add(rid)
            key = text_value(fields.get("同步键")).strip()
            appno = text_value(fields.get("申请号")).strip()
            marker = text_value(fields.get("同步来源")).strip()
            if not key and not appno and not marker:
                if any(text_value(v).strip() for k, v in fields.items() if k in FIELDS and v is not None):
                    raise ValueError("明细表存在未标识的非空行，请先核对")
                continue
            normalized = normalize_application_no(key or appno)
            if not normalized or normalized in indexed:
                raise ValueError("飞书存在空申请号或重复申请号，本轮禁止写入")
            if key != normalized or normalize_application_no(appno) != normalized or marker != source_id:
                raise ValueError("明细表存在非本同步程序管理的记录，已停止，避免重复或误覆盖")
            indexed[normalized] = record
        return indexed

    @staticmethod
    def same_fields(remote, desired):
        for name, value in desired.items():
            actual = remote.get(name)
            if FIELDS[name] == 1:
                if text_value(actual) != str(value):
                    return False
            elif (actual if actual not in ("", []) else None) != value:
                return False
        return True

    def run(self, trigger="manual"):
        with ProcessLock(self.lock_path):
            return self._run_locked(trigger)

    def _run_locked(self, trigger):
        cfg = self.config()
        target = self.target(cfg)
        run_id, counts = uuid.uuid4().hex, {k: 0 for k in ("added", "updated", "removed", "unchanged", "failed", "recovered")}
        with self.connect() as conn:
            conn.execute("UPDATE runs SET status='interrupted',finished=?,message='进程中断，后续运行将核对远端结果' WHERE status='running'", (now(),))
            conn.execute("INSERT INTO runs(id,target,trigger,started,status) VALUES(?,?,?,?,'running')", (run_id, target, trigger, now()))
        status, message = "failed", ""
        try:
            if not cfg["enabled"]:
                raise ValueError("同步尚未启用")
            if cfg["mode"] == "production":
                proof = cfg.get("live_test") or {}
                if proof.get("app_id") != cfg["app_id"] or proof.get("secret_revision") != cfg.get("secret_revision") or not proof.get("passed"):
                    raise ValueError("此应用尚未通过飞书测试表验收，禁止连接正式表")
                if proof.get("target") == target:
                    raise ValueError("正式表不得与验收测试表相同")
            snapshot = source_snapshot(self.source, cfg["source_id"], cfg.get("scope_approvals"))
            if snapshot["pending"]:
                raise ValueError(f"存在 {len(snapshot['pending'])} 条集团范围待核对记录，本轮未写入")
            if not snapshot["total"]:
                raise ValueError("源库为空，本轮禁止写入")
            desired = snapshot["items"]
            client = self.client(cfg)
            self.check_schema(client)
            remote = self.remote_index(client.list_records(), cfg["source_id"])
            if remote and not desired:
                raise ValueError("集团范围为空，本轮禁止批量移除，请核对源库")
            for key, fields in desired.items():
                try:
                    with self.connect() as conn:
                        item = conn.execute("SELECT * FROM items WHERE target=? AND app_key=?", (target, key)).fetchone()
                    record = remote.get(key)
                    if record:
                        if item and item["record_id"] and item["record_id"] != record["record_id"]:
                            raise ValueError("飞书记录 ID 与本地映射冲突")
                        if item and item["pending_token"]:
                            counts["recovered"] += 1
                            self._event(run_id, key, "新增恢复", "成功", "通过同步键核对已写入记录")
                        if self.same_fields(record["fields"], fields):
                            counts["unchanged"] += 1
                        else:
                            client.update(record["record_id"], {**fields, "最后同步时间": now()})
                            counts["updated"] += 1
                            self._event(run_id, key, "修改", "成功")
                        self._ack(target, key, record["record_id"], fields)
                    else:
                        if item and item["record_id"]:
                            # A list read alone must not turn transient invisibility into a duplicate create.
                            raise ValueError("已映射的飞书记录暂不可见；停止新增，请核对远端记录或稍后重试")
                        if item and item["pending_token"] and item["pending_status"] != "rejected":
                            raise ValueError("此前新增结果待确认；远端尚未找到，本轮不重复新增，请稍后重试核对")
                        payload = {**fields, "最后同步时间": now()}
                        token = str(uuid.uuid4())
                        with self.connect() as conn:
                            conn.execute("""INSERT INTO items(target,app_key,pending_token,pending_payload,pending_status,pending_at)
                              VALUES(?,?,?,?,?,?) ON CONFLICT(target,app_key) DO UPDATE SET record_id=NULL,
                              pending_token=excluded.pending_token,pending_payload=excluded.pending_payload,
                              pending_status=excluded.pending_status,pending_at=excluded.pending_at""",
                                         (target, key, token, packed(payload), "prepared", now()))
                        try:
                            rid = client.create(payload, token)
                        except FeishuError as exc:
                            with self.connect() as conn:
                                conn.execute("UPDATE items SET pending_status=? WHERE target=? AND app_key=?",
                                             ("uncertain" if exc.uncertain else "rejected", target, key))
                            raise
                        self._ack(target, key, rid, fields)
                        counts["added"] += 1
                        self._event(run_id, key, "新增", "成功")
                except (FeishuError, ValueError) as exc:
                    counts["failed"] += 1
                    self._event(run_id, key, "同步", "失败", str(exc))
                with self.connect() as conn:
                    conn.execute("UPDATE runs SET counts=? WHERE id=?", (packed(counts), run_id))
            for key, record in remote.items():
                if key in desired:
                    continue
                try:
                    client.delete(record["record_id"])
                    with self.connect() as conn:
                        conn.execute("DELETE FROM items WHERE target=? AND app_key=?", (target, key))
                    counts["removed"] += 1
                    self._event(run_id, key, "退出范围", "成功", "仅移除飞书镜像，源记录保留")
                except FeishuError as exc:
                    counts["failed"] += 1
                    self._event(run_id, key, "退出范围", "失败", str(exc))
            # Resolve pending operations for records no longer in scope without ever replaying an old payload.
            with self.connect() as conn:
                items = conn.execute("SELECT app_key,pending_token FROM items WHERE target=?", (target,)).fetchall()
            for item in items:
                if item["app_key"] not in desired and item["app_key"] not in remote:
                    if item["pending_token"]:
                        counts["failed"] += 1
                        self._event(run_id, item["app_key"], "退出核对", "待确认", "此前新增结果未知，后续仅核对和移除，不重放旧数据")
                    else:
                        with self.connect() as conn:
                            conn.execute("DELETE FROM items WHERE target=? AND app_key=?", (target, item["app_key"]))
                        self._event(run_id, item["app_key"], "退出核对", "成功", "完整读取确认远端记录已移除")
            status = "partial" if counts["failed"] else "success"
            message = "有失败或待确认记录，请查看日志后重试" if counts["failed"] else "同步完成"
        except Exception as exc:
            # Do not log unknown exception text: it could include request credentials.
            message = str(exc) if isinstance(exc, (ValueError, FeishuError, sqlite3.Error)) else "同步异常中断，请查看本地运行环境"
            counts["failed"] += 1
            self._event(run_id, "", "运行", "失败", message)
        finally:
            with self.connect() as conn:
                conn.execute("UPDATE runs SET status=?,finished=?,counts=?,message=? WHERE id=?",
                             (status, now(), packed(counts), message, run_id))
        return {"run_id": run_id, "status": status, "counts": counts, "message": message}

    def start(self, action="sync"):
        if action not in {"sync", "acceptance"}:
            raise ValueError("未知操作")
        if self.thread and self.thread.is_alive():
            raise BusyError("同步正在执行")
        def work():
            try:
                if action == "acceptance":
                    from feishu_acceptance import live_acceptance
                    live_acceptance(self)
                else:
                    self.run()
            except BusyError:
                pass
        # The worker and scheduled CLI both acquire the same OS file lock.
        self.thread = threading.Thread(target=work, daemon=True, name="feishu-sync")
        self.thread.start()
        return {"message": "已提交，结果将在下方更新"}


def scheduled_window(value=None):
    value = value or datetime.now(CHINA)
    return value.hour == 11 and value.minute < 5


def main():
    parser = argparse.ArgumentParser(description="V3.1 飞书单向镜像")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--source-db", type=Path)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--acceptance", action="store_true")
    parser.add_argument("--task-ready", action="store_true")
    args = parser.parse_args()
    if args.scheduled and not scheduled_window():
        print(packed({"status": "skipped", "message": "不在 11:00 触发窗口，不补跑"}))
        return 0
    try:
        service = SyncService(args.data_dir, args.source_db)
        if args.task_ready:
            result = service.task_ready()
        elif args.preview:
            result = service.preview_source()
        elif args.acceptance:
            from feishu_acceptance import live_acceptance
            result = live_acceptance(service)
        else:
            if args.scheduled and service.config()["mode"] != "production":
                raise ValueError("定时任务仅允许已验收的正式表")
            result = service.run("scheduled" if args.scheduled else "manual")
        print(packed(result))
        return 0 if result.get("status") in (None, "success") else 1
    except (BusyError, ValueError, FeishuError) as exc:
        print(packed({"status": "failed", "message": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
