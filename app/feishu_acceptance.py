"""Live acceptance against an explicitly configured TEST table, never production."""
from __future__ import annotations

from datetime import datetime
import sqlite3
import uuid

from core import normalize_application_no, DEMO_GROUP_APPLICANTS
from database import PatentDatabase
from feishu_client import FeishuError
from feishu_sync import SyncService, ProcessLock, FIELDS, packed, now


class LostResponse:
    """One real committed create, followed by a deliberately lost acknowledgement."""
    def __init__(self, client):
        self.client, self.fired = client, False

    def __getattr__(self, name):
        return getattr(self.client, name)

    def create(self, fields, client_token):
        rid = self.client.create(fields, client_token)
        if not self.fired:
            self.fired = True
            raise FeishuError("测试：飞书新增成功后模拟响应丢失", uncertain=True)
        return rid


def live_acceptance(service):
    with ProcessLock(service.lock_path):
        cfg = service.config()
        run_id, target = uuid.uuid4().hex, service.target(cfg)
        with service.connect() as conn:
            conn.execute("UPDATE runs SET status='interrupted',finished=?,message='前次执行中断' WHERE status='running'", (now(),))
            conn.execute("INSERT INTO runs(id,target,trigger,started,status) VALUES(?,?,'acceptance',?,'running')", (run_id, target, now()))
        checks, success, message = [], False, ""
        try:
            if cfg["mode"] != "test":
                raise ValueError("真实验收只允许连接测试表")
            client = service.client(cfg)
            service.check_schema(client)
            test_dir = service.folder / "acceptance"
            child = SyncService(test_dir, client_factory=service.client_factory,
                                credential_factory=service.credential_factory)
            child_cfg = child.config()
            child_cfg.update({k: cfg[k] for k in ("app_id", "app_token", "table_id", "secret_revision")})
            child_cfg.update(enabled=True, mode="test", scope_approvals={})
            child._write_config(child_cfg)
            child.remote_index(client.list_records(), child_cfg["source_id"])
            fields = {v["field_name"]: v["type"] for v in client.list_fields()}
            note_name = "人工验收备注"
            if note_name not in fields:
                client.add_field(note_name, 1)
            elif fields[note_name] != 1:
                raise ValueError("人工验收备注应为文本字段")
            db = PatentDatabase(test_dir / "patents.db")
            # Only the dedicated synthetic database is reset. No production data is copied.
            with db.connect() as conn:
                conn.execute("DELETE FROM patents")
            def add(number, title):
                db.execute_import(db.build_import_preview([{
                    "__row__": 2, "申请号": number, "标题": title,
                    "[标]当前申请(专利权)人": (DEMO_GROUP_APPLICANTS or ['测试企业'])[0],
                    "法律状态/事件": "申请公开", "申请日": "2026-10-19", "受理局": "中国"}]))
            numbers = ["CN209910190001.X", "CN209910190002.8", "CN209910190003.6"]
            keys = [normalize_application_no(v) for v in numbers]
            def verify_run(label):
                result = child.run("acceptance-step")
                if result["status"] != "success":
                    raise ValueError(label + "未通过：" + result["message"])
                checks.append(label)
                service._event(run_id, "", "测试验收", "通过", label)
                return result
            def remote():
                return child.remote_index(client.list_records(), child_cfg["source_id"])
            add(numbers[0], "同步验收样例一（虚构）")
            add(numbers[1], "同步验收样例二（虚构）")
            verify_run("新增")
            rows = remote()
            if set(rows) != set(keys[:2]):
                raise ValueError("新增后的记录集合不正确")
            rid = rows[keys[0]]["record_id"]
            client.update(rid, {note_name: "人工内容必须保留"})
            with db.connect() as conn:
                conn.execute("UPDATE patents SET title_cn='同步验收样例一（已修改）',updated_at=? WHERE app_key=?", (now(), keys[0]))
            verify_run("修改")
            rows = remote()
            if rows[keys[0]]["fields"].get(note_name) != "人工内容必须保留":
                raise ValueError("人工字段保护未通过")
            checks.append("人工字段保护")
            repeated = verify_run("重复同步")
            if any(repeated["counts"][k] for k in ("added", "updated", "removed")):
                raise ValueError("重复同步仍产生写入")
            add(numbers[2], "同步验收样例三（虚构，恢复测试）")
            real_factory = child.client_factory
            fault = LostResponse(client)
            child.client_factory = lambda _: fault
            lost = child.run("acceptance-lost-response")
            child.client_factory = real_factory
            if lost["status"] != "partial":
                raise ValueError("响应丢失场景未正确记录失败")
            recovery = verify_run("响应丢失恢复")
            if recovery["counts"]["recovered"] != 1 or set(remote()) != set(keys):
                raise ValueError("恢复后存在重复或漏记录")
            with db.connect() as conn:
                conn.execute("UPDATE patents SET applicant_current='外部验收测试公司' WHERE app_key=?", (keys[1],))
            verify_run("退出范围")
            if keys[1] in remote() or not db.patent_by_key(keys[1]):
                raise ValueError("退出范围未做到仅移除飞书镜像")
            with db.connect() as conn:
                conn.execute("UPDATE patents SET applicant_current=? WHERE app_key=?", ((DEMO_GROUP_APPLICANTS or ['测试企业'])[0], keys[1]))
            verify_run("重新纳入")
            if set(remote()) != set(keys):
                raise ValueError("重新纳入后存在重复或漏记录")
            for row in remote().values():
                client.delete(row["record_id"])
            if remote():
                raise ValueError("测试记录未全部清理")
            # Remote cleanup also clears delivery mappings so a repeated acceptance is safe.
            with child.connect() as conn:
                conn.execute("DELETE FROM items WHERE target=?", (target,))
            cfg["live_test"] = {"passed": True, "at": now(), "target": target,
                                "app_id": cfg["app_id"], "secret_revision": cfg["secret_revision"], "checks": checks}
            service._write_config(cfg)
            success, message = True, "飞书测试表验收通过，虚构测试记录已清理"
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ValueError, FeishuError, sqlite3.Error)) else "测试验收中断，请查看本地环境后重试"
            cfg["live_test"] = None
            service._write_config(cfg)
            service._event(run_id, "", "测试验收", "失败", message)
        finally:
            with service.connect() as conn:
                conn.execute("UPDATE runs SET status=?,finished=?,counts=?,message=? WHERE id=?",
                             ("success" if success else "failed", now(), packed({"checks_passed": len(checks)}), message, run_id))
        return {"status": "success" if success else "failed", "checks": checks, "message": message}
