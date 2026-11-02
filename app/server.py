from __future__ import annotations

import cgi
import http.cookies
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
import tempfile
import threading
import uuid
import webbrowser
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from openpyxl import load_workbook

from ai_query import apply_business_mappings, apply_question_mappings, parse_with_deepseek, public_settings, save_settings, test_deepseek_connection
from archive import ArchiveService
from core import COMPANY_NAME_ALIASES, DEMO_GROUP_ALIASES, DEMO_GROUP_APPLICANTS, normalize_application_no, parse_material_name, safe_file_name, sha256
from database import PatentDatabase
from export_excel import build_export_workbook
from sharing import SESSION_SECONDS, ShareSecurity
from business_mappings import explicit_business_filters, clean_mapped_keyword


ROOT = Path(__file__).resolve().parent
if not (ROOT / "CONTEST_DEMO.json").is_file():
    raise RuntimeError("缺少参赛版标记，拒绝启动")
PORT = int(os.getenv("DEMO_PATENT_PORT") or "18879")
SHARE_MODE = False  # Contest edition is local only.
DATA = ROOT / "data"  # Never inherit a production data path.
UPLOADS = DATA / "materials"
PENDING = DATA / "pending"
STATIC = ROOT / "static"
EXPORTS = ROOT / "导出结果"
for folder in (DATA, UPLOADS, PENDING, EXPORTS): folder.mkdir(parents=True, exist_ok=True)
DB = PatentDatabase(DATA / "patents.db")
SHARING = ShareSecurity(DATA)
ARCHIVE = ArchiveService(DATA, DB)
FEISHU_SYNC = None
FEISHU_INIT_LOCK = threading.Lock()
MASTER_UPDATE = None
MASTER_UPDATE_LOCK = threading.Lock()


def master_update_service():
    global MASTER_UPDATE
    with MASTER_UPDATE_LOCK:
        if MASTER_UPDATE is None:
            from master_update import MasterUpdateService
            MASTER_UPDATE = MasterUpdateService(DATA, DB)
    return MASTER_UPDATE


def feishu_service():
    # Lazy setup: a sync lock/configuration failure must never prevent patent query startup.
    global FEISHU_SYNC
    with FEISHU_INIT_LOCK:
        if FEISHU_SYNC is None:
            from feishu_sync import SyncService
            FEISHU_SYNC = SyncService(DATA, DB.path)
    return FEISHU_SYNC


def api_permission(role: str, method: str, path: str) -> bool:
    if role == "admin":
        return True
    if path in {"/api/session", "/api/share/login", "/api/share/logout"}:
        return True
    if role != "reader":
        return False
    if method == "GET":
        return path in {"/api/stats", "/api/patents"} or bool(
            re.fullmatch(r"/api/patents/\d+", path)
            or re.fullmatch(r"/api/materials/\d+/download", path)
        )
    if method == "POST":
        return path in {"/api/ask", "/api/export", "/api/materials/export"}
    return False


def local_access_addresses() -> list[str]:
    addresses = []
    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            parsed = ipaddress.ip_address(address)
            if parsed.version == 4 and not parsed.is_loopback and not parsed.is_link_local:
                addresses.append(f"http://{address}:{PORT}")
    except (OSError, ValueError):
        pass
    hostname = socket.gethostname().strip()
    if hostname:
        addresses.insert(0, f"http://{hostname}:{PORT}")
    return list(dict.fromkeys(addresses))
IMPORT_CACHE: dict[str, list[dict]] = {}
MATERIAL_CACHE: dict[str, list[dict]] = {}
MATERIAL_EXTENSIONS = {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
ZIP_MAX_FILES = 5000
ZIP_MAX_FILE_SIZE = 500 * 1024 * 1024
ZIP_MAX_TOTAL_SIZE = 2 * 1024 * 1024 * 1024


def extract_zip_materials(archive: Path, folder: Path, start_index: int = 0, archive_label: str = "") -> tuple[list[dict], list[dict], int]:
    """Safely expand one ZIP into the pending folder without trusting member paths."""
    candidates: list[dict] = []
    errors: list[dict] = []
    index = start_index
    total_size = 0
    archive_name = archive_label or archive.name
    try:
        with zipfile.ZipFile(archive) as package:
            members = [member for member in package.infolist() if not member.is_dir()]
            if len(members) > ZIP_MAX_FILES:
                return [], [{"filename": archive_name, "status": "错误", "message": f"压缩包内文件超过 {ZIP_MAX_FILES} 个，请拆分后导入"}], index
            for member in members:
                member_path = member.filename.replace("\\", "/")
                original = safe_file_name(Path(member_path).name)
                if not original or original.startswith(".") or "__MACOSX/" in member_path or original.lower() in {"thumbs.db", "desktop.ini"}:
                    continue
                suffix = Path(original).suffix.lower()
                if member.flag_bits & 0x1:
                    errors.append({"filename": original, "status": "错误", "message": f"来自 {archive_name}：文件已加密，无法读取"})
                    continue
                if member.file_size > ZIP_MAX_FILE_SIZE:
                    errors.append({"filename": original, "status": "错误", "message": f"来自 {archive_name}：单个文件超过 500MB"})
                    continue
                total_size += member.file_size
                if total_size > ZIP_MAX_TOTAL_SIZE:
                    errors.append({"filename": archive_name, "status": "错误", "message": "压缩包解压后超过 2GB，请拆分后导入"})
                    break
                if suffix not in MATERIAL_EXTENSIONS and suffix not in {".xlsx", ".xls", ".csv"}:
                    continue
                stored = folder / f"{index:04d}-{original}"
                index += 1
                with package.open(member) as source, stored.open("wb") as target:
                    shutil.copyfileobj(source, target)
                candidates.append({"original": original, "stored": stored, "archive": archive_name})
    except (zipfile.BadZipFile, OSError) as exc:
        errors.append({"filename": archive_name, "status": "错误", "message": f"压缩包无法读取：{exc}"})
    if not candidates and not errors:
        errors.append({"filename": archive_name, "status": "错误", "message": "压缩包中没有支持的专利材料文件"})
    return candidates, errors, index


def read_excel(path: Path, preferred="集团专利总表"):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if preferred in workbook.sheetnames:
            sheet = workbook[preferred]
        else:
            candidates = [name for name in workbook.sheetnames if name.startswith("集团专利总表")]
            sheet = workbook[candidates[0] if candidates else workbook.sheetnames[0]]
        iterator = sheet.iter_rows(values_only=True)
        headers = [str(x).strip() if x is not None else "" for x in next(iterator)]
        rows = []
        for row_no, values in enumerate(iterator, 2):
            if not any(v not in (None, "", "-", "—") for v in values): continue
            rows.append({"__row__": row_no, **{headers[i]: values[i] for i in range(min(len(headers), len(values))) if headers[i]}})
        return sheet.title, rows
    finally:
        workbook.close()


def smart_filters(question: str) -> tuple[dict, str]:
    q = question.strip(); filters = {}; removed = q
    if re.search(r"尚未公开|还未公开|未公开|还没公开|尚未公布", q):
        filters["unpublished_only"] = True
        removed = re.sub(r"尚未公开|还未公开|未公开|还没公开|尚未公布", "", removed)
    if re.search(r"海外|境外|国外|非中国", q):
        filters["overseas_only"] = True
        removed = re.sub(r"海外|境外|国外|非中国", "", removed)
    elif re.search(r"(?:中国|国内)(?:的)?(?:已授权|授权)?专利", q):
        filters["country"] = "中国"
        removed = re.sub(r"中国|国内", "", removed)
    status_map = {"已授权": "已授权", "授权": "已授权", "申请中": "申请中", "审查中": "申请中",
                  "已驳回": "已驳回", "驳回": "已驳回", "已撤回": "已撤回", "撤回": "已撤回",
                  "已失效": "已失效", "失效": "已失效"}
    for phrase, value in status_map.items():
        if phrase in q:
            filters["status"] = value; removed = removed.replace(phrase, ""); break
    range_match = re.search(r"(20\d{2})\s*年?\s*(?:至|到|—|-)\s*(20\d{2})\s*年?", q)
    if range_match:
        filters["application_from"] = range_match.group(1) + "-01-01"
        filters["application_to"] = range_match.group(2) + "-12-31"
        removed = removed.replace(range_match.group(0), "")
    since_match = re.search(r"(20\d{2})\s*年?(?:以来|以后|之后)", q)
    if since_match:
        filters["application_from"] = since_match.group(1) + "-01-01"; removed = removed.replace(since_match.group(0), "")
    single_year = re.search(r"(20\d{2})\s*年", q)
    if single_year and not range_match and not since_match:
        prefix = "grant" if "授权" in q else "application"
        filters[f"{prefix}_from"] = single_year.group(1) + "-01-01"
        filters[f"{prefix}_to"] = single_year.group(1) + "-12-31"
        removed = removed.replace(single_year.group(0), "")
    short_year = re.search(r"(?<!\d)(\d{2})\s*年", q)
    if short_year and not single_year and not range_match and not since_match:
        year = 2000 + int(short_year.group(1))
        prefix = "grant" if "授权" in q else "application"
        filters[f"{prefix}_from"] = f"{year}-01-01"
        filters[f"{prefix}_to"] = f"{year}-12-31"
        removed = removed.replace(short_year.group(0), "")
    normalized_q = re.sub(r"\s+", "", q).replace("(", "（").replace(")", "）")
    mentioned_alias = next((alias for alias in COMPANY_NAME_ALIASES if re.sub(r"\s+", "", alias) in normalized_q), "")
    mentioned_company = COMPANY_NAME_ALIASES.get(mentioned_alias, "") or next((name for name in DEMO_GROUP_APPLICANTS if re.sub(r"\s+", "", name) in normalized_q), "")
    if mentioned_company:
        scope_token = re.sub(r"\s+", "", mentioned_alias or mentioned_company)
        tail = normalized_q.split(scope_token, 1)[1]
        filters["applicant" if re.match(r".{0,20}(?:申请的|申请了|申请过的)专利", tail) else "rightsholder"] = mentioned_company
        spellings = (mentioned_alias, mentioned_company, mentioned_company.replace("（", "(").replace("）", ")"))
        for spelling in (item for item in spellings if item):
            removed = re.sub(r"\s*".join(re.escape(ch) for ch in spelling), "", removed)
    elif any(alias in q for alias in DEMO_GROUP_ALIASES):
        alias=next(alias for alias in sorted(DEMO_GROUP_ALIASES,key=len,reverse=True) if alias in q)
        filters["applicant" if re.search(re.escape(alias)+r".{0,20}(?:申请的|申请了|申请过的)专利",normalized_q) else "rightsholder"]="集团"
        removed=removed.replace(alias,"")
    current_year = __import__("datetime").datetime.now().year
    relative_year = None
    if any(word in q for word in ("今年", "本年", "本年度")):
        relative_year = current_year
        removed = re.sub(r"今年|本年度|本年", "", removed)
    elif "去年" in q:
        relative_year = current_year - 1
        removed = removed.replace("去年", "")
    if relative_year is not None:
        prefix = "grant" if "授权" in q else "application"
        filters[f"{prefix}_from"] = f"{relative_year}-01-01"
        filters[f"{prefix}_to"] = f"{relative_year}-12-31"
    patterns = (("applicant", r"(?:原始申请主体|申请主体)(?:为|是|包含)?[：:]?\s*([^，,。；;]+)"),
                ("rightsholder", r"(?:当前申请人|当前权利人|当前专利权人|专利权人)(?:为|是|包含)?[：:]?\s*([^，,。；;]+)"),
                ("inventor", r"发明人(?:为|是|包含)?[：:]?\s*([^，,。；;]+)"),
                ("title", r"(?:专利名称|名称|标题)(?:为|是|包含)?[：:]?\s*([^，,。；;]+)"))
    for key, pattern in patterns:
        match = re.search(pattern, q)
        if match:
            value = re.sub(r"(?:的)?(?:专利|申请)$", "", match.group(1)).strip()
            if value: filters[key] = value
            removed = removed.replace(match.group(0), "")
    participant = re.search(r"(?:^|以来|由|[，,\s])([\u4e00-\u9fff]{2,4})(?:参与|发明)(?:的)?", q)
    if participant and "inventor" not in filters:
        filters["inventor"] = participant.group(1)
        removed = re.sub(re.escape(participant.group(1)) + r"(?:参与|发明)(?:的)?", "", removed)
    removed = re.sub(r"仅需要|仅有|只有|只要|单独|独有|独立申请|独立拥有|不含共有|排除共有|不要共有|非共有|仅", "", removed)
    removed = re.sub(r"^(?:帮我|请)\s*", "", removed)
    removed = re.sub(r"(查找|查询|找到|看看|一下|有哪些|相关|对应|材料|证书|通知书|专利|清单|列表)", " ", removed)
    removed = re.sub(r"(申请|授权|公开|参与)(?:的)?", " ", removed)
    removed = re.sub(r"\s+", " ", removed).strip(" ，,。；;")
    if removed in {"的", "是", "为"}: removed = ""
    if removed: filters["keyword"] = removed
    return apply_business_mappings(filters, question), removed


def answer_question(question: str) -> dict:
    question = question.strip()
    if not question:
        raise ValueError("请输入要查询的问题")
    source = "deepseek"
    ai_error = ""
    residual = ""
    compact = re.sub(r"\s+", "", question)
    if re.fullmatch(r"[A-Za-z0-9./_\-．]+", compact) and sum(c.isdigit() for c in compact) >= 7:
        filters = {"keyword": compact, "interpretation": "按申请号或公开号检索（忽略编号分隔符）"}
        source = "identifier"
    else:
        try:
            filters = parse_with_deepseek(question, DATA)
        except Exception as exc:
            filters, residual = smart_filters(apply_question_mappings(question, DATA))
            source = "local"
            ai_error = str(exc)
        explicit = explicit_business_filters(question)
        if explicit:
            filters.update(explicit)
            if "statuses" in explicit:
                filters.pop("status", None)
            if filters.get("keyword"):
                filters["keyword"] = clean_mapped_keyword(filters["keyword"], question, explicit)
            filters["interpretation"] = "按业务映射查询"

    interpretation = str(filters.get("interpretation") or "").strip()
    if filters.get("needs_expiry_data"):
        answer = "已识别为“快到期”查询，但现有总表没有到期日或年费期限字段，无法可靠判断。请先在总表增加该字段。"
        items = []
    else:
        items = DB.search(filters)
        if filters.get("count_intent"):
            answer = f"共找到 {len(items)} 条符合条件的专利记录。"
        else:
            answer = f"找到 {len(items)} 条符合条件的专利记录。" if items else "没有找到符合条件的记录，可以减少条件后再试。"
        if interpretation:
            answer = f"已识别：{interpretation}。{answer}"
    if source == "local":
        if ai_error.startswith("尚未配置"):
            answer = "尚未配置 DeepSeek，已使用本地规则查询。" + answer
        else:
            answer = f"DeepSeek 暂不可用（{ai_error}），已使用本地规则查询。" + answer
    return {"items": items, "filters": filters, "residual": residual, "answer": answer, "source": source, "ai_error": ai_error}


def build_material_export(filters: dict, mode: str, target: Path,
                          db: PatentDatabase = DB, data_dir: Path = DATA) -> dict:
    modes = {"all": "全部已关联材料", "preferred": "证书优先，无证书则受理通知书", "certificate": "仅专利证书", "acceptance": "仅受理通知书"}
    if mode not in modes:
        raise ValueError("材料导出方式不正确")
    patents = db.search(filters, limit=100000)
    materials = db.materials_for_patents([int(item["id"]) for item in patents])
    grouped: dict[int, list[dict]] = {}
    for material in materials:
        grouped.setdefault(int(material["patent_id"]), []).append(material)
    exported_patents = exported_files = 0
    notes = [f"导出方式：{modes[mode]}", f"筛选结果：{len(patents)} 件专利", ""]
    used_names: set[str] = set()
    data_root = data_dir.resolve()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as package:
        for index, patent in enumerate(patents, 1):
            patent_materials = grouped.get(int(patent["id"]), [])
            certificates = [item for item in patent_materials if item["category"] == "专利证书"]
            acceptances = [item for item in patent_materials if item["category"] == "受理通知书"]
            selected = patent_materials if mode == "all" else certificates if mode == "certificate" else acceptances if mode == "acceptance" else (certificates or acceptances)
            if not selected:
                notes.append(f"未找到材料：{patent['application_no']}｜{patent.get('title_cn') or patent.get('title_original') or ''}")
                continue
            folder_name = safe_file_name(
                f"{index:03d}-{str(patent['application_no']).replace('/', '-')}-{patent.get('title_cn') or patent.get('title_original') or '未命名'}"
            )[:120]
            patent_file_count = 0
            for material in selected:
                source = (data_dir / material["stored_path"]).resolve()
                if (data_root not in source.parents and source != data_root) or not source.is_file():
                    notes.append(f"文件缺失：{patent['application_no']}｜{material['original_name']}")
                    continue
                original_name = safe_file_name(material["original_name"])
                archive_name = f"{folder_name}/{original_name}"
                suffix_number = 2
                while archive_name.lower() in used_names:
                    name_path = Path(original_name)
                    archive_name = f"{folder_name}/{name_path.stem}-{suffix_number}{name_path.suffix}"
                    suffix_number += 1
                used_names.add(archive_name.lower())
                package.write(source, archive_name)
                patent_file_count += 1
                exported_files += 1
            if patent_file_count:
                exported_patents += 1
        notes[2:2] = [f"成功导出：{exported_patents} 件专利，{exported_files} 个文件", ""]
        package.writestr("导出说明.txt", "\ufeff" + "\r\n".join(notes))
    return {"patents": len(patents), "exported_patents": exported_patents, "exported_files": exported_files,
            "missing_patents": len(patents) - exported_patents, "mode": mode}


class Handler(BaseHTTPRequestHandler):
    server_version = "DEMOPatent/1.0"

    def log_message(self, *_): pass

    def send_json(self, payload, status=200, headers: dict | None = None):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def client_ip(self) -> str:
        return str(self.client_address[0])

    def is_local(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_ip()).is_loopback
        except ValueError:
            return False

    def session_token(self) -> str:
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return cookie.get("demo_reader").value if cookie.get("demo_reader") else ""

    def role(self) -> str:
        if self.is_local():
            return "admin"
        token = self.session_token()
        if SHARE_MODE and token and SHARING.is_reader(token, self.client_ip()):
            return "reader"
        return "unauthenticated"

    def authorize_api(self, method: str, path: str) -> bool:
        role = self.role()
        if api_permission(role, method, path):
            return True
        status = 401 if role == "unauthenticated" else 403
        self.send_json({"error": "请先登录" if status == 401 else "只读用户无权执行此操作"}, status)
        return False

    def session_payload(self) -> dict:
        role = self.role()
        payload = {"role": role, "share_mode": SHARE_MODE}
        if role == "admin":
            payload.update(SHARING.public_settings(), addresses=local_access_addresses())
        elif role == "unauthenticated":
            payload.update(enabled=SHARING.settings["enabled"], configured=SHARING.configured)
        return payload

    def send_excel(self, body: bytes):
        filename = f"专利筛选结果-{datetime.now():%Y%m%d-%H%M%S}.xlsx"
        from urllib.parse import quote
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_download_path(self, path: Path, filename: str, content_type: str, delete_after: bool = False):
        from urllib.parse import quote
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    self.wfile.write(block)
        finally:
            if delete_after:
                path.unlink(missing_ok=True)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0")); return json.loads(self.rfile.read(length) or b"{}")

    def form(self):
        return cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", "")})

    def do_GET(self):
        try:
            parsed = urlparse(self.path); path = parsed.path; query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            if path.startswith("/api/") and not self.authorize_api("GET", path): return
            if path.startswith("/api/feishu/"): return self.feishu_api("GET", path)
            if path.startswith("/api/master-update/"): return self.master_update_api("GET", path)
            if path == "/api/session": return self.send_json(self.session_payload())
            if path == "/api/ask": return self.send_json({"error": "AI 查询只接受页面提交"}, 405)
            if path == "/api/share/settings":
                return self.send_json({**SHARING.public_settings(), "share_mode": SHARE_MODE,
                                       "addresses": local_access_addresses()})
            if path == "/api/contest":
                from contest_support import summary
                return self.send_json(summary())
            if path == "/api/stats": return self.send_json(DB.stats())
            if path == "/api/health": return self.send_json(DB.health_report())
            if path == "/api/import/family-query": return self.send_json(DB.family_query_info())
            if path == "/api/patents": return self.send_json({"items": DB.search(query), "filters": query})
            if path == "/api/export":
                filters = json.loads(query.get("filters", "{}"))
                if not isinstance(filters, dict): return self.send_json({"error": "筛选条件格式不正确"}, 400)
                return self.send_excel(build_export_workbook(DB.search(filters, limit=100000), filters))
            if path == "/api/ai/settings": return self.send_json(public_settings(DATA))
            if path == "/api/archive/settings": return self.send_json(ARCHIVE.public_settings())
            if path == "/api/archive/jobs":
                return self.send_json(ARCHIVE.list_jobs(query.get("status", ""), int(query.get("limit") or 300)))
            match = re.fullmatch(r"/api/archive/audit/([a-f0-9]{32})/report", path)
            if match:
                report = ARCHIVE.audit_report_path(match.group(1))
                return self.send_download_path(report, report.name, "text/html; charset=utf-8")
            match = re.fullmatch(r"/api/patents/(\d+)", path)
            if match:
                item = DB.patent_detail(int(match.group(1))); return self.send_json(item or {"error": "未找到"}, 200 if item else 404)
            if path == "/api/logs": return self.send_json({"items": DB.logs()})
            match = re.fullmatch(r"/api/materials/(\d+)/download", path)
            if match: return self.download_material(int(match.group(1)))
            if path.startswith("/api/"): return self.send_json({"error": "接口不存在"}, 404)
            return self.static_file(path)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_DELETE(self):
        try:
            path = urlparse(self.path).path
            if path.startswith("/api/") and not self.authorize_api("DELETE", path):
                return
            match = re.fullmatch(r"/api/materials/(\d+)", path)
            if match:
                return self.send_json(ARCHIVE.delete_material(int(match.group(1))))
            self.send_json({"error": "接口不存在"}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path.startswith("/api/") and not self.authorize_api("POST", path): return
            if path.startswith("/api/feishu/"): return self.feishu_api("POST", path)
            if path.startswith("/api/master-update/"): return self.master_update_api("POST", path)
            if path == "/api/bundle/import":
                return self.bundle_import()
            if path == "/api/share/login":
                if self.is_local(): return self.send_json({"role": "admin", "message": "本机已是管理员"})
                if not SHARE_MODE: return self.send_json({"error": "当前未使用共享模式启动"}, 403)
                token, message = SHARING.login(str(self.read_json().get("password") or ""), self.client_ip())
                if not token:
                    status = 429 if "次数过多" in message else 401
                    return self.send_json({"error": message}, status)
                cookie = f"demo_reader={token}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Strict"
                return self.send_json({"role": "reader", "message": message}, headers={"Set-Cookie": cookie})
            if path == "/api/share/logout":
                SHARING.logout(self.session_token())
                return self.send_json({"message": "已退出"}, headers={
                    "Set-Cookie": "demo_reader=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
                })
            if path == "/api/share/settings":
                return self.send_json({**SHARING.save_settings(self.read_json()), "share_mode": SHARE_MODE,
                                       "addresses": local_access_addresses()})
            if path == "/api/admin/shutdown":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self.send_json({"message": "正在关闭"})
            if path == "/api/ask":
                if self.role() == "reader":
                    allowed, message = SHARING.allow_ai(self.session_token(), self.client_ip())
                    if not allowed: return self.send_json({"error": message}, 429)
                return self.send_json(answer_question(self.read_json().get("q", "")))
            if path == "/api/materials/export":
                payload = self.read_json(); filters = payload.get("filters") or {}; mode = str(payload.get("mode") or "preferred")
                if not isinstance(filters, dict): return self.send_json({"error": "筛选条件格式不正确"}, 400)
                descriptor, temporary_name = tempfile.mkstemp(prefix="material-export-", suffix=".zip", dir=PENDING)
                os.close(descriptor); temporary = Path(temporary_name)
                try:
                    build_material_export(filters, mode, temporary)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
                filename = f"专利材料-{datetime.now():%Y%m%d-%H%M%S}.zip"
                return self.send_download_path(temporary, filename, "application/zip", delete_after=True)
            if self.path == "/api/export/save":
                filters = self.read_json().get("filters") or {}
                if not isinstance(filters, dict): return self.send_json({"error": "筛选条件格式不正确"}, 400)
                body = build_export_workbook(DB.search(filters, limit=100000), filters)
                base = f"专利筛选结果-{datetime.now():%Y%m%d-%H%M%S}"
                target = EXPORTS / f"{base}.xlsx"
                number = 2
                while target.exists():
                    target = EXPORTS / f"{base}-{number}.xlsx"
                    number += 1
                target.write_bytes(body)
                opened = False
                try:
                    if sys.platform == "win32":
                        os.startfile(str(EXPORTS))
                        opened = True
                except OSError:
                    pass
                return self.send_json({"filename": target.name, "path": str(target), "opened": opened})
            if self.path == "/api/export":
                filters = self.read_json().get("filters") or {}
                if not isinstance(filters, dict): return self.send_json({"error": "筛选条件格式不正确"}, 400)
                return self.send_excel(build_export_workbook(DB.search(filters, limit=100000), filters))
            if self.path == "/api/ai/settings": return self.send_json(save_settings(DATA, self.read_json()))
            if self.path == "/api/ai/test": return self.send_json(test_deepseek_connection(DATA))
            if path == "/api/archive/settings": return self.send_json(ARCHIVE.save_settings(self.read_json()))
            if path == "/api/archive/mineru/test": return self.send_json(ARCHIVE.test_mineru())
            if path == "/api/archive/tencent/test": return self.send_json(ARCHIVE.test_tencent())
            if path == "/api/archive/retry-pending": return self.send_json(ARCHIVE.retry_pending_review())
            if path == "/api/archive/intake": return self.archive_intake()
            if path == "/api/archive/jobs/batch-confirm": return self.send_json(ARCHIVE.batch_confirm(self.read_json()))
            if path == "/api/archive/jobs/batch-ignore": return self.send_json(ARCHIVE.batch_ignore(self.read_json()))
            match = re.fullmatch(r"/api/archive/jobs/(\d+)/(confirm|retry|ignore)", path)
            if match:
                job_id, action = int(match.group(1)), match.group(2)
                if action == "confirm": return self.send_json(ARCHIVE.confirm(job_id, self.read_json()))
                if action == "retry": return self.send_json(ARCHIVE.retry(job_id))
                return self.send_json(ARCHIVE.ignore(job_id))
            if path == "/api/archive/audit": return self.send_json(ARCHIVE.audit_existing())
            if path == "/api/archive/audit/apply":
                return self.send_json(ARCHIVE.apply_audit(str(self.read_json().get("token") or "")))
            if self.path == "/api/import/preview": return self.import_preview()
            if self.path == "/api/import/execute": return self.import_execute()
            if self.path == "/api/materials/preview": return self.material_preview()
            if self.path == "/api/materials/execute": return self.material_execute()
            self.send_json({"error": "接口不存在"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def master_update_api(self, method, path):
        try:
            host = urlparse("http://" + self.headers.get("Host", ""))
            allowed = self.is_local() and host.hostname in {"127.0.0.1", "localhost", "::1"}
            allowed = allowed and (host.port or 80) == self.server.server_port
            if not allowed:
                return self.send_json({"error": "总表自动获取仅允许本机管理员访问"}, 403)
            if method == "POST" and (self.headers.get("X-DEMO-Source") != "1" or
                    self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json" or
                    self.headers.get("Sec-Fetch-Site") == "cross-site"):
                return self.send_json({"error": "请从本机管理页面提交操作"}, 403)
            service = master_update_service()
            if method == "GET":
                if path == "/api/master-update/settings": return self.send_json(service.settings())
                if path == "/api/master-update/status": return self.send_json(service.status())
            else:
                payload = self.read_json()
                if not isinstance(payload, dict): raise ValueError("请求格式错误")
                if path == "/api/master-update/settings": return self.send_json(service.save_settings(payload))
                if path == "/api/master-update/start": return self.send_json(service.start(bool(payload.get("resume"))))
                if path == "/api/master-update/apply":
                    result = service.apply(payload.get("run_id"))
                    if not result.get("already_applied"):
                        try:
                            result["archive_recheck"] = ARCHIVE.notify_patent_index_updated()["requeued"]
                        except Exception:
                            result["warning"] = "总表已更新；材料重新匹配未完成，可在自动归档页重试"
                    return self.send_json(result)
            return self.send_json({"error": "接口不存在"}, 404)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception:
            return self.send_json({"error": "数据源配置或本地环境异常，请核对运行账户"}, 500)

    def feishu_api(self, method, path):
        from feishu_sync import BusyError
        from feishu_client import FeishuError
        # In addition to the existing server-side admin check, prevent cross-origin
        # writes and DNS rebinding against the local-admin convention.
        try:
            host = urlparse("http://" + self.headers.get("Host", ""))
            allowed = self.is_local() and host.hostname in {"127.0.0.1", "localhost", "::1"}
            allowed = allowed and (host.port or 80) == self.server.server_port
            if not allowed:
                return self.send_json({"error": "飞书同步仅允许本机管理员访问"}, 403)
            if method == "POST" and (self.headers.get("X-DEMO-Sync") != "1" or
                    self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json" or
                    self.headers.get("Sec-Fetch-Site") == "cross-site"):
                return self.send_json({"error": "请从本机管理页面提交同步操作"}, 403)
            service = feishu_service()
            if method == "GET":
                actions = {"/api/feishu/status": service.status, "/api/feishu/source": service.preview_source,
                           "/api/feishu/preview": service.preview_diff}
                action = actions.get(path)
                return self.send_json(action() if action else {"error": "接口不存在"}, 200 if action else 404)
            payload = self.read_json()
            if not isinstance(payload, dict):
                raise ValueError("请求格式不正确")
            if path == "/api/feishu/settings": return self.send_json(service.save_settings(payload))
            if path == "/api/feishu/test": return self.send_json(service.test_connection())
            if path == "/api/feishu/prepare": return self.send_json(service.prepare_fields())
            if path == "/api/feishu/run": return self.send_json(service.start())
            if path == "/api/feishu/acceptance": return self.send_json(service.start("acceptance"))
            return self.send_json({"error": "接口不存在"}, 404)
        except BusyError as exc:
            return self.send_json({"error": str(exc)}, 409)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        except FeishuError as exc:
            return self.send_json({"error": str(exc)}, 502)
        except Exception:
            return self.send_json({"error": "同步配置或本地环境异常，请检查运行账户和配置文件"}, 500)

    def bundle_import(self):
        if self.headers.get("Sec-Fetch-Site")=="cross-site":
            return self.send_json({"error":"拒绝跨站导入"},403)
        if int(self.headers.get('Content-Length','0'))>550*1024*1024:
            return self.send_json({'error':'数据包过大，请拆分处理'},400)
        form=self.form()
        if 'file' not in form or isinstance(form['file'],list):
            return self.send_json({'error':'请选择一个完整数据ZIP包'},400)
        part=form['file']
        if not (part.filename or '').lower().endswith('.zip'):
            return self.send_json({'error':'请选择数据ZIP包'},400)
        handle,path=tempfile.mkstemp(suffix='.zip',prefix='bundle-',dir=PENDING)
        os.close(handle)
        try:
            with open(path,'wb') as target:shutil.copyfileobj(part.file,target)
            from bundle_transfer import import_bundle
            result=import_bundle(Path(path),DB,DATA)
            return self.send_json(result)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return self.send_json({'error':str(exc)},400)
        finally:Path(path).unlink(missing_ok=True)

    def import_preview(self):
        form = self.form(); part = form["file"]
        token = uuid.uuid4().hex
        path = PENDING / f"{token}.xlsx"
        with path.open("wb") as handle: shutil.copyfileobj(part.file, handle)
        sheet, rows = read_excel(path); preview = DB.build_import_preview(rows); IMPORT_CACHE[token] = preview
        public = [{k: v for k, v in item.items() if k not in ("record", "merged_raw", "canonical", "status", "app_key")} for item in preview]
        counts = {key: sum(x["action"] == key for x in preview) for key in ("新增", "更新", "不变", "跳过")}
        self.send_json({"token": token, "sheet": sheet, "items": public, "counts": counts,
                        "quality": DB.import_quality(rows, preview)})

    def import_execute(self):
        token = self.read_json().get("token", ""); preview = IMPORT_CACHE.pop(token, None)
        if preview is None: return self.send_json({"error": "预览已失效，请重新生成"}, 400)
        result = DB.execute_import(preview)
        result["archive_recheck"] = ARCHIVE.notify_patent_index_updated()["requeued"]
        pending = PENDING / f"{token}.xlsx"
        if pending.exists(): pending.unlink()
        self.send_json(result)

    def archive_intake(self):
        form = self.form()
        if "files" not in form:
            return self.send_json({"error": "请选择 PDF 或 ZIP 材料"}, 400)
        parts = form["files"] if isinstance(form["files"], list) else [form["files"]]
        batch = ARCHIVE.pending / ("upload-" + uuid.uuid4().hex)
        batch.mkdir(parents=True, exist_ok=True)
        paths = []
        for index, part in enumerate(parts):
            original = safe_file_name(part.filename or f"file-{index}")
            suffix = Path(original).suffix.lower()
            if suffix not in MATERIAL_EXTENSIONS | {".zip"}:
                continue
            target = batch / f"{index:04d}-{original}"
            with target.open("wb") as handle:
                shutil.copyfileobj(part.file, handle)
            paths.append(target)
        if not paths:
            shutil.rmtree(batch, ignore_errors=True)
            return self.send_json({"error": "没有可处理的 PDF、ZIP、Word 或图片材料"}, 400)
        return self.send_json(ARCHIVE.intake_paths(paths, "upload"))

    def material_preview(self):
        form = self.form(); parts = form["files"] if isinstance(form["files"], list) else [form["files"]]
        token = uuid.uuid4().hex; folder = PENDING / token; folder.mkdir()
        items = []; candidates = []; identifier_index = DB.material_identifier_index(); file_index = 0
        for part in parts:
            original = safe_file_name(part.filename or f"file-{file_index}")
            stored = folder / f"upload-{file_index:04d}-{original}"
            with stored.open("wb") as handle: shutil.copyfileobj(part.file, handle)
            if Path(original).suffix.lower() == ".zip":
                expanded, errors, file_index = extract_zip_materials(stored, folder, file_index, original)
                candidates.extend(expanded)
                items.extend({"filename": error["filename"], "application_no": "", "category": "—",
                              "patent_title": "", "status": error["status"], "message": error["message"]} for error in errors)
            else:
                candidates.append({"original": original, "stored": stored, "archive": ""})
                file_index += 1
        seen_upload_names = set()
        for candidate in candidates:
            original = candidate["original"]; stored = candidate["stored"]; archive = candidate["archive"]
            if Path(original).suffix.lower() in {".xlsx", ".xls", ".csv"}:
                info = {"application_no": "", "app_key": "", "category": "—", "recognized": False,
                        "status": "错误", "message": "这是表格文件，请到“更新总表”页面导入"}
            else:
                info = parse_material_name(original)
            patent = None
            if info["status"] != "错误":
                patent, matched_identifier, ambiguous = DB.match_material_filename(original, identifier_index)
                if ambiguous:
                    info.update(status="冲突", message="文件名中的编号匹配到多条专利，请保留一个明确编号")
                elif not patent:
                    info.update(status="错误", message="文件名中没有匹配到申请号或公开号")
            if patent:
                source_note = f"来自压缩包 {archive}；" if archive else ""
                info.update(patent_id=patent["id"], app_key=patent["app_key"],
                            patent_title=patent["title_cn"] or patent["title_original"], application_no=patent["application_no"],
                            status="可上传" if info["recognized"] else "待确认",
                            message=source_note + ("通过文件名中的申请号或公开号匹配成功" if info["recognized"] else "编号匹配成功，将归入其他材料"))
                batch_key = (patent["id"], info["category"], original.lower())
                if batch_key in seen_upload_names or DB.material_name_exists(patent["id"], info["category"], original):
                    info.update(status="冲突", message="同一材料字段已有同名文件")
                seen_upload_names.add(batch_key)
            info.update(filename=original, temp_path=str(stored)); items.append(info)
        MATERIAL_CACHE[token] = items
        public = [{k: v for k, v in x.items() if k != "temp_path"} for x in items]
        self.send_json({"token": token, "items": public})

    def material_execute(self):
        token = self.read_json().get("token", ""); items = MATERIAL_CACHE.pop(token, None)
        if items is None: return self.send_json({"error": "预览已失效，请重新生成"}, 400)
        ok = skipped = 0
        for item in items:
            if item["status"] not in ("可上传", "待确认"):
                skipped += 1; continue
            temp = Path(item["temp_path"])
            target_dir = UPLOADS / item["app_key"] / item["category"]; target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / item["filename"]
            counter = 2
            while target.exists():
                target = target_dir / f"{Path(item['filename']).stem}-{counter}{Path(item['filename']).suffix}"; counter += 1
            shutil.move(str(temp), target)
            DB.add_material(item["patent_id"], item["category"], item["filename"], str(target.relative_to(DATA)), sha256(target), item["application_no"])
            ok += 1
        shutil.rmtree(PENDING / token, ignore_errors=True)
        self.send_json({"uploaded": ok, "skipped": skipped})

    def download_material(self, material_id):
        item = DB.material(material_id)
        if not item: return self.send_json({"error": "文件不存在"}, 404)
        path = DATA / item["stored_path"]
        if not path.is_file(): return self.send_json({"error": "本地文件已丢失"}, 404)
        body = path.read_bytes(); name = item["original_name"]
        self.send_response(200); self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + __import__("urllib.parse").parse.quote(name))
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def static_file(self, path):
        relative = "index.html" if path == "/" else unquote(path.lstrip("/"))
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents and target != STATIC.resolve(): return self.send_json({"error": "禁止访问"}, 403)
        if not target.is_file(): target = STATIC / "index.html"
        body = target.read_bytes(); self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


def main():
    with socket.socket() as probe:
        probe.settimeout(.25)
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            raise RuntimeError(f"参赛端口 {PORT} 已被占用，请设置 DEMO_PATENT_PORT 后重试")
    server = ThreadingHTTPServer(("0.0.0.0" if SHARE_MODE else "127.0.0.1", PORT), Handler)
    ARCHIVE.start()
    # Fixed contest version; restart explicitly after edits.
    if os.getenv("DEMO_PATENT_NO_BROWSER") != "1":
        threading.Timer(0.7, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    try:
        server.serve_forever()
    finally:
        ARCHIVE.stop()


if __name__ == "__main__": main()
