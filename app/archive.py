from __future__ import annotations

import base64
import hashlib
import hmac
import html
import io
import json
import mimetypes
import os
import re
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_query import analyze_material_document, load_settings as load_ai_settings, protect_secret, unprotect_secret
from core import MATERIAL_CATEGORIES, classify_material, json_text, normalize_application_no, safe_file_name, sha256
from database import PatentDatabase, publication_no_from_raw, demo_category_from_raw


ARCHIVE_JOB_STATUSES = ("待处理", "识别中", "待确认", "已归档", "失败", "已忽略")
AUTO_PDF_EXTENSIONS = {".pdf"}
LEGACY_EXTENSIONS = {".doc", ".docx", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
MAX_ZIP_FILES = 5000
MAX_ZIP_MEMBER_SIZE = 500 * 1024 * 1024
MAX_ZIP_TOTAL_SIZE = 2 * 1024 * 1024 * 1024

JURISDICTION_ALIASES = {
    "BR": ("巴西", "BRAZIL", "BRASIL"),
    "JP": ("日本", "JAPAN", "JPO"),
    "CN": ("中国", "CHINA", "CNIPA"),
    "PCT": ("世界知识产权组织", "WIPO", "PCT", "WO"),
    "US": ("美国", "UNITED STATES", "USPTO"),
    "EP": ("欧洲专利局", "EPO", "EUROPEAN PATENT"),
    "GB": ("英国", "UNITED KINGDOM", "UKIPO"),
    "ES": ("西班牙", "SPAIN"),
    "IN": ("印度", "INDIA"),
    "KR": ("韩国", "KOREA", "KIPO"),
    "CA": ("加拿大", "CANADA"),
    "AU": ("澳大利亚", "AUSTRALIA"),
}


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _loads(value: Any, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def extract_document_date(text: str) -> str:
    text = str(text or "")
    labelled = re.search(
        r"(?:发文日|发文日期|通知日|通知书日期|出文日|签发日|日期)\s*[：:]?\s*"
        r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?",
        text,
        re.I,
    )
    match = labelled or re.search(r"(?<!\d)(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?", text)
    if not match:
        compact = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", text)
        match = compact
    if not match:
        return ""
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def standard_material_name(patent: dict, category: str, document_date: str = "", extension: str = ".pdf") -> str:
    application_no = str(patent.get("application_no") or "未识别申请号").replace("/", "-").replace("\\", "-")
    demo_category = demo_category_from_raw(patent.get("raw_json")) or "未分类"
    publication_no = publication_no_from_raw(patent.get("raw_json"))
    parts = [application_no, demo_category]
    if publication_no:
        parts.append(publication_no.replace("/", "-").replace("\\", "-"))
    parts.append(category)
    if document_date:
        parts.append(document_date.replace("-", ""))
    extension = extension.lower() if re.fullmatch(r"\.[a-z0-9]{1,8}", extension.lower()) else ".pdf"
    return safe_file_name("-".join(parts) + extension)


def patent_jurisdiction_code(patent: dict) -> str:
    """Return the filing jurisdiction represented by one master-table row."""
    application_no = re.sub(r"[^A-Z0-9]", "", str(patent.get("application_no") or "").upper())
    if application_no.startswith("PCT") or application_no.startswith("WO"):
        return "PCT"
    prefix = re.match(r"[A-Z]{2}", application_no)
    if prefix:
        return prefix.group(0)
    country = str(patent.get("country") or "").upper()
    for code, aliases in JURISDICTION_ALIASES.items():
        if any(alias.upper() in country for alias in aliases):
            return code
    return ""


def filename_jurisdiction_hint(filename: str) -> str:
    """Read an explicit destination office from a filename, not from family references in the PDF."""
    upper = Path(filename).stem.upper()
    for code, aliases in JURISDICTION_ALIASES.items():
        if any(len(alias) > 3 and alias.upper() in upper for alias in aliases):
            return code
    for code in JURISDICTION_ALIASES:
        if re.search(rf"(?:^|[^A-Z0-9]){re.escape(code)}(?:[^A-Z0-9]|$)", upper):
            return code
    # Matter references often append the destination office: FPME22140112BR-...
    for code in JURISDICTION_ALIASES:
        if re.search(rf"\d{re.escape(code)}(?=[^A-Z0-9]|$)", upper):
            return code
    return ""


class ArchiveSettings:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / "archive_settings.json"

    def load(self, include_secrets: bool = False) -> dict:
        raw = _loads(self.path.read_text("utf-8") if self.path.is_file() else "{}", {})
        default_watch = self.data_dir / "自动归档" / "待归档"
        settings = {
            "watch_enabled": bool(raw.get("watch_enabled", True)),
            "watch_folder": str(raw.get("watch_folder") or default_watch),
            "stable_seconds": max(3, min(int(raw.get("stable_seconds") or 5), 120)),
            "mineru_enabled": bool(raw.get("mineru_enabled", bool(raw.get("mineru_token")))),
            "mineru_model": str(raw.get("mineru_model") or "vlm"),
            "mineru_language": str(raw.get("mineru_language") or "ch"),
            "mineru_configured": bool(raw.get("mineru_token")),
            "tencent_enabled": bool(raw.get("tencent_enabled", False)),
            "tencent_region": str(raw.get("tencent_region") or "ap-guangzhou"),
            "tencent_configured": bool(raw.get("tencent_secret_id") and raw.get("tencent_secret_key")),
        }
        if include_secrets:
            for public, stored in (("mineru_token", "mineru_token"),
                                   ("tencent_secret_id", "tencent_secret_id"),
                                   ("tencent_secret_key", "tencent_secret_key")):
                if raw.get(stored):
                    try:
                        settings[public] = unprotect_secret(str(raw[stored]))
                    except Exception as exc:
                        provider = "MinerU Token" if public == "mineru_token" else "腾讯云密钥"
                        raise RuntimeError(f"本机保存的{provider}无法读取，请重新保存") from exc
        return settings

    def public(self) -> dict:
        settings = self.load()
        settings["mineru_token_masked"] = "已安全保存" if settings["mineru_configured"] else "未配置"
        settings["tencent_secret_id_masked"] = "已安全保存" if settings["tencent_configured"] else "未配置"
        settings["tencent_secret_key_masked"] = "已安全保存" if settings["tencent_configured"] else "未配置"
        watch = Path(settings["watch_folder"]).expanduser().resolve()
        settings["watch_folder"] = str(watch)
        settings["processed_folder"] = str(watch.parent / "已归档")
        return settings

    def save(self, payload: dict) -> dict:
        current = _loads(self.path.read_text("utf-8") if self.path.is_file() else "{}", {})
        if "watch_enabled" in payload:
            current["watch_enabled"] = bool(payload.get("watch_enabled"))
        if str(payload.get("watch_folder") or "").strip():
            watch = Path(str(payload["watch_folder"])).expanduser()
            if not watch.is_absolute():
                raise ValueError("监控文件夹必须使用完整路径")
            watch = watch.resolve()
            watch.mkdir(parents=True, exist_ok=True)
            (watch.parent / "已归档").mkdir(parents=True, exist_ok=True)
            current["watch_folder"] = str(watch)
        if "stable_seconds" in payload:
            current["stable_seconds"] = max(3, min(int(payload.get("stable_seconds") or 5), 120))
        if "mineru_enabled" in payload:
            current["mineru_enabled"] = bool(payload.get("mineru_enabled"))
        model = str(payload.get("mineru_model") or current.get("mineru_model") or "vlm").strip()
        if model not in {"vlm", "pipeline"}:
            raise ValueError("MinerU 模型只能使用 vlm 或 pipeline")
        current["mineru_model"] = model
        language = str(payload.get("mineru_language") or current.get("mineru_language") or "ch").strip()
        if not re.fullmatch(r"[A-Za-z-]{2,12}", language):
            raise ValueError("MinerU 语言格式不正确")
        current["mineru_language"] = language
        if payload.get("clear_mineru"):
            current.pop("mineru_token", None)
            current["mineru_enabled"] = False
        elif str(payload.get("mineru_token") or "").strip():
            token = str(payload["mineru_token"]).strip()
            if len(token) < 20 or len(token) > 500:
                raise ValueError("MinerU Token 格式不正确")
            current["mineru_token"] = protect_secret(token)
        if "tencent_enabled" in payload:
            current["tencent_enabled"] = bool(payload.get("tencent_enabled"))
        region = str(payload.get("tencent_region") or current.get("tencent_region") or "ap-guangzhou").strip()
        if not re.fullmatch(r"[a-z]+-[a-z]+(?:-\d+)?", region):
            raise ValueError("腾讯云地域格式不正确")
        current["tencent_region"] = region
        if payload.get("clear_tencent"):
            current.pop("tencent_secret_id", None)
            current.pop("tencent_secret_key", None)
            current["tencent_enabled"] = False
        else:
            secret_id = str(payload.get("tencent_secret_id") or "").strip()
            secret_key = str(payload.get("tencent_secret_key") or "").strip()
            if bool(secret_id) != bool(secret_key):
                raise ValueError("腾讯云 SecretId 和 SecretKey 必须同时填写")
            if secret_id:
                current["tencent_secret_id"] = protect_secret(secret_id)
                current["tencent_secret_key"] = protect_secret(secret_key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), "utf-8")
        temporary.replace(self.path)
        return self.public()


class MinerUParser:
    create_url = "https://mineru.net/api/v4/file-urls/batch"
    result_url = "https://mineru.net/api/v4/extract-results/batch/{batch_id}"

    def __init__(self, settings: dict):
        self.token = str(settings.get("mineru_token") or "")
        self.model = str(settings.get("mineru_model") or "vlm")
        self.language = str(settings.get("mineru_language") or "ch")
        if not self.token:
            raise RuntimeError("尚未配置 MinerU Token")

    def _json(self, request: urllib.request.Request, timeout: int = 60) -> dict:
        result = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
                    result = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                if exc.code == 401:
                    raise RuntimeError("MinerU Token 无效，请重新填写") from exc
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise RuntimeError(f"MinerU 请求失败（HTTP {exc.code}）：{detail}") from exc
            except urllib.error.URLError as exc:
                if attempt == 2:
                    raise RuntimeError(f"无法连接 MinerU：{exc.reason}") from exc
            time.sleep(2 * (attempt + 1))
        if result is None:
            raise RuntimeError("MinerU 请求未返回结果")
        if int(result.get("code", -1)) != 0:
            raise RuntimeError("MinerU 返回错误：" + str(result.get("msg") or "未知错误"))
        return result

    def extract(self, path: Path, source_name: str, timeout_seconds: int = 240) -> tuple[str, dict]:
        data_id = uuid.uuid4().hex
        payload = {
            "files": [{"name": safe_file_name(source_name), "data_id": data_id, "is_ocr": True}],
            "model_version": self.model,
            "language": self.language,
            "enable_table": True,
            "enable_formula": False,
        }
        request = urllib.request.Request(
            self.create_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method="POST",
        )
        created = self._json(request)
        batch_id = str((created.get("data") or {}).get("batch_id") or "")
        urls = (created.get("data") or {}).get("file_urls") or []
        upload_url = str(urls[0].get("url") if urls and isinstance(urls[0], dict) else urls[0] if urls else "")
        if not batch_id or not upload_url:
            raise RuntimeError("MinerU 未返回文件上传地址")
        try:
            import requests
            response = None
            for attempt in range(3):
                try:
                    with path.open("rb") as handle:
                        response = requests.put(upload_url, data=handle, timeout=120)
                    if response.status_code in (200, 201, 204):
                        break
                    if response.status_code not in {429, 500, 502, 503, 504}:
                        break
                except requests.RequestException:
                    if attempt == 2:
                        raise
                time.sleep(2 * (attempt + 1))
            if response is None or response.status_code not in (200, 201, 204):
                status = response.status_code if response is not None else "无响应"
                detail = response.text[:300] if response is not None else ""
                raise RuntimeError(f"MinerU 文件上传失败（HTTP {status}）：{detail}")
        except ImportError as exc:
            raise RuntimeError("本机缺少 MinerU 文件上传组件") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"MinerU 文件上传失败：{exc}") from exc
        deadline = time.monotonic() + max(30, timeout_seconds)
        result_item = None
        while time.monotonic() < deadline:
            poll = urllib.request.Request(
                self.result_url.format(batch_id=batch_id),
                headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"},
                method="GET",
            )
            result = self._json(poll)
            items = (result.get("data") or {}).get("extract_result") or []
            result_item = next((item for item in items if str(item.get("data_id") or "") == data_id), None)
            if result_item is None and len(items) == 1:
                result_item = items[0]
            state = str((result_item or {}).get("state") or "")
            if state == "done":
                break
            if state == "failed":
                raise RuntimeError("MinerU 解析失败：" + str(result_item.get("err_msg") or "未知错误"))
            time.sleep(2)
        else:
            raise RuntimeError("MinerU 解析超时，请稍后重试")
        zip_url = str((result_item or {}).get("full_zip_url") or "")
        if not zip_url:
            raise RuntimeError("MinerU 解析完成但未返回结果文件")
        try:
            with urllib.request.urlopen(zip_url, timeout=120, context=ssl.create_default_context()) as response:
                archive_bytes = response.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"MinerU 结果下载失败：{exc.reason}") from exc
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as package:
                markdown_names = [name for name in package.namelist() if Path(name).name.lower() == "full.md"]
                if not markdown_names:
                    markdown_names = [name for name in package.namelist() if name.lower().endswith(".md")]
                if not markdown_names:
                    raise RuntimeError("MinerU 结果中没有 Markdown 正文")
                markdown = package.read(markdown_names[0]).decode("utf-8", "replace").strip()
        except zipfile.BadZipFile as exc:
            raise RuntimeError("MinerU 返回的结果压缩包损坏") from exc
        if not markdown:
            raise RuntimeError("MinerU 未识别到可用文字")
        return markdown, {"provider": "MinerU", "batch_id": batch_id, "model": self.model, "ocr": True}

    def test(self) -> dict:
        target = self._temporary_pdf_path()
        try:
            try:
                import fitz
                document = fitz.open()
                page = document.new_page(width=600, height=800)
                page.insert_text((72, 120), "MINERU OCR CONNECTION TEST 2026", fontsize=20)
                document.save(target)
                document.close()
            except ImportError as exc:
                raise RuntimeError("本机缺少生成 MinerU 测试 PDF 的组件") from exc
            text, metadata = self.extract(target, "MinerU连接测试.pdf", timeout_seconds=180)
            return {"ok": True, "message": "MinerU 连接及强制 OCR 测试成功", "recognized": text[:80], **metadata}
        finally:
            target.unlink(missing_ok=True)

    @staticmethod
    def _temporary_pdf_path() -> Path:
        import tempfile
        descriptor, name = tempfile.mkstemp(prefix="mineru-test-", suffix=".pdf")
        os.close(descriptor)
        return Path(name)


class TencentOCR:
    endpoint = "ocr.tencentcloudapi.com"
    service = "ocr"
    version = "2018-11-19"
    action = "GeneralBasicOCR"

    def __init__(self, settings: dict):
        self.secret_id = str(settings.get("tencent_secret_id") or "")
        self.secret_key = str(settings.get("tencent_secret_key") or "")
        self.region = str(settings.get("tencent_region") or "ap-guangzhou")
        if not self.secret_id or not self.secret_key:
            raise RuntimeError("尚未配置腾讯云 OCR 密钥")

    def _request(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{self.endpoint}\nx-tc-action:{self.action.lower()}\n"
        signed_headers = "content-type;host;x-tc-action"
        canonical_request = "\n".join((
            "POST", "/", "", canonical_headers, signed_headers,
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
        ))
        credential_scope = f"{date}/{self.service}/tc3_request"
        string_to_sign = "\n".join((
            "TC3-HMAC-SHA256", str(timestamp), credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ))
        secret_date = hmac.new(("TC3" + self.secret_key).encode("utf-8"), date.encode("utf-8"), hashlib.sha256).digest()
        secret_service = hmac.new(secret_date, self.service.encode("utf-8"), hashlib.sha256).digest()
        secret_signing = hmac.new(secret_service, b"tc3_request", hashlib.sha256).digest()
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            f"TC3-HMAC-SHA256 Credential={self.secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        request = urllib.request.Request(
            "https://" + self.endpoint,
            data=body.encode("utf-8"),
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json; charset=utf-8",
                "Host": self.endpoint,
                "X-TC-Action": self.action,
                "X-TC-Timestamp": str(timestamp),
                "X-TC-Version": self.version,
                "X-TC-Region": self.region,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60, context=ssl.create_default_context()) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"腾讯云 OCR 请求失败（HTTP {exc.code}）：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接腾讯云 OCR：{exc.reason}") from exc
        response = result.get("Response") or {}
        if response.get("Error"):
            error = response["Error"]
            raise RuntimeError(f"腾讯云 OCR 返回错误：{error.get('Code')} {error.get('Message')}")
        return response

    def recognize_page(self, path: Path, page_number: int) -> tuple[str, list[dict]]:
        raw_limit = 7 * 1024 * 1024
        if path.stat().st_size <= raw_limit:
            payload = {
                "ImageBase64": base64.b64encode(path.read_bytes()).decode("ascii"),
                "LanguageType": "auto", "IsPdf": True, "PdfPageNumber": page_number,
            }
        else:
            try:
                import fitz
            except ImportError as exc:
                raise RuntimeError("PDF 超过腾讯云单次限制，且本机缺少 PDF 页面渲染组件") from exc
            document = fitz.open(path)
            try:
                page = document.load_page(page_number - 1)
                image = page.get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False).tobytes("jpeg")
            finally:
                document.close()
            payload = {"ImageBase64": base64.b64encode(image).decode("ascii"), "LanguageType": "auto"}
        response = self._request(payload)
        detections = []
        lines = []
        for item in response.get("TextDetections") or []:
            text = str(item.get("DetectedText") or "").strip()
            if text:
                lines.append(text)
                detections.append({"text": text, "confidence": item.get("Confidence"), "page": page_number})
        return "\n".join(lines), detections

    def test(self) -> dict:
        try:
            import fitz
            document = fitz.open()
            page = document.new_page(width=600, height=800)
            page.insert_text((72, 120), "OCR CONNECTION TEST 2026", fontsize=24)
            image = page.get_pixmap(alpha=False).tobytes("png")
            document.close()
        except ImportError:
            image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        response = self._request({"ImageBase64": base64.b64encode(image).decode("ascii"), "LanguageType": "auto"})
        return {"ok": True, "message": "腾讯云 OCR 连接成功", "request_id": response.get("RequestId", "")}


def extract_pdf_text(path: Path) -> tuple[list[str], int]:
    try:
        import fitz
    except ImportError:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("本机缺少 PDF 文字提取组件，请安装 PyMuPDF 或 pypdf") from exc
        reader = PdfReader(str(path))
        return [(page.extract_text() or "") for page in reader.pages], len(reader.pages)
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise RuntimeError(f"PDF 无法读取：{exc}") from exc
    try:
        if document.needs_pass:
            raise RuntimeError("PDF 已加密，无法自动识别")
        return [page.get_text("text") or "" for page in document], document.page_count
    finally:
        document.close()


class ArchiveService:
    def __init__(self, data_dir: Path, db: PatentDatabase):
        self.data_dir = data_dir
        self.db = db
        self.settings_store = ArchiveSettings(data_dir)
        self.root = data_dir / "archive"
        self.originals = self.root / "originals"
        self.extracted = self.root / "extracted"
        self.pending = self.root / "pending"
        self.audits = self.root / "audits"
        for folder in (self.root, self.originals, self.extracted, self.pending, self.audits):
            folder.mkdir(parents=True, exist_ok=True)
        public = self.settings_store.public()
        Path(public["watch_folder"]).mkdir(parents=True, exist_ok=True)
        Path(public["processed_folder"]).mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._queue_lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._watcher: threading.Thread | None = None
        self._stability: dict[str, tuple[int, int, float]] = {}
        with self.db.connect() as conn:
            conn.execute("UPDATE archive_jobs SET status='待处理',stage='等待恢复',updated_at=? WHERE status='识别中'", (now_text(),))

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="archive-worker", daemon=True)
        self._watcher = threading.Thread(target=self._watch_loop, name="archive-watcher", daemon=True)
        self._worker.start()
        self._watcher.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def public_settings(self) -> dict:
        return self.settings_store.public()

    def save_settings(self, payload: dict) -> dict:
        result = self.settings_store.save(payload)
        self._stability.clear()
        self._wake.set()
        return result

    def test_tencent(self) -> dict:
        settings = self.settings_store.load(include_secrets=True)
        return TencentOCR(settings).test()

    def test_mineru(self) -> dict:
        settings = self.settings_store.load(include_secrets=True)
        return MinerUParser(settings).test()

    @staticmethod
    def _fingerprint(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    def enqueue(self, path: Path, source_name: str = "", intake_source: str = "upload") -> int | None:
        path = path.resolve()
        if not path.is_file():
            return None
        fingerprint = self._fingerprint(path)
        created = now_text()
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM archive_jobs WHERE source_path=? AND fingerprint=?",
                (str(path), fingerprint),
            ).fetchone()
            if existing:
                return int(existing[0])
            cursor = conn.execute("""INSERT INTO archive_jobs(
                fingerprint,source_path,source_name,intake_source,status,stage,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (fingerprint, str(path), safe_file_name(source_name or path.name), intake_source,
                 "待处理", "等待识别", created, created))
            job_id = int(cursor.lastrowid)
        self._wake.set()
        return job_id

    def enqueue_zip(self, path: Path, intake_source: str = "upload") -> list[int]:
        batch = self.pending / ("zip-" + uuid.uuid4().hex)
        batch.mkdir(parents=True, exist_ok=True)
        jobs: list[int] = []
        extracted: list[tuple[Path, str]] = []
        total_size = 0
        try:
            with zipfile.ZipFile(path) as package:
                members = [member for member in package.infolist() if not member.is_dir()]
                if len(members) > MAX_ZIP_FILES:
                    raise ValueError(f"压缩包内文件超过 {MAX_ZIP_FILES} 个")
                for index, member in enumerate(members):
                    name = safe_file_name(Path(member.filename.replace("\\", "/")).name)
                    if not name or name.startswith(".") or "__MACOSX/" in member.filename or name.lower() in {"thumbs.db", "desktop.ini"}:
                        continue
                    if member.flag_bits & 0x1:
                        continue
                    if member.file_size > MAX_ZIP_MEMBER_SIZE:
                        continue
                    total_size += member.file_size
                    if total_size > MAX_ZIP_TOTAL_SIZE:
                        raise ValueError("压缩包解压后超过 2GB")
                    if Path(name).suffix.lower() not in AUTO_PDF_EXTENSIONS | LEGACY_EXTENSIONS:
                        continue
                    # Keep the original name in SQLite/UI but use a compact internal path.
                    # This avoids Windows MAX_PATH failures for long legal document names.
                    suffix = Path(name).suffix.lower()
                    target = batch / f"{index:04d}{suffix}"
                    with package.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    extracted.append((target, name))
            # Expose the batch to the worker only after every member was extracted.
            with self._queue_lock:
                for target, name in extracted:
                    job_id = self.enqueue(target, name, intake_source)
                    if job_id is not None:
                        jobs.append(job_id)
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            shutil.rmtree(batch, ignore_errors=True)
            raise ValueError(f"压缩包无法读取：{exc}") from exc
        if not jobs:
            shutil.rmtree(batch, ignore_errors=True)
            raise ValueError("压缩包中没有可处理的 PDF、Word 或图片材料")
        return jobs

    def intake_paths(self, paths: list[Path], intake_source: str = "upload") -> dict:
        job_ids, errors = [], []
        for path in paths:
            try:
                if path.suffix.lower() == ".zip":
                    new_jobs = self.enqueue_zip(path, intake_source)
                    job_ids.extend(new_jobs)
                    if new_jobs and intake_source == "upload" and path.is_file():
                        self._move_intake_to_processed(path)
                else:
                    job_id = self.enqueue(path, path.name, intake_source)
                    if job_id is not None:
                        job_ids.append(job_id)
            except Exception as exc:
                errors.append({"filename": path.name, "message": str(exc)})
        return {"job_ids": job_ids, "queued": len(job_ids), "errors": errors}

    def _worker_loop(self):
        while not self._stop.is_set():
            job_id = self._claim_next_job()
            if job_id is None:
                self._wake.wait(2.0)
                self._wake.clear()
                continue
            try:
                self._process_job(job_id)
            except Exception as exc:
                with self.db.connect() as conn:
                    conn.execute("""UPDATE archive_jobs SET status='失败',stage='处理失败',error_message=?,
                        retry_count=retry_count+1,updated_at=? WHERE id=?""", (str(exc)[:1000], now_text(), job_id))

    def _claim_next_job(self) -> int | None:
        with self._queue_lock:
            with self.db.connect() as conn:
                row = conn.execute("SELECT id FROM archive_jobs WHERE status='待处理' ORDER BY id LIMIT 1").fetchone()
                if not row:
                    return None
                job_id = int(row[0])
                conn.execute("UPDATE archive_jobs SET status='识别中',stage='读取文件',updated_at=? WHERE id=? AND status='待处理'", (now_text(), job_id))
                return job_id

    def _watch_loop(self):
        while not self._stop.wait(2.0):
            try:
                settings = self.settings_store.load()
                if not settings["watch_enabled"]:
                    self._stability.clear()
                    continue
                folder = Path(settings["watch_folder"]).resolve()
                folder.mkdir(parents=True, exist_ok=True)
                valid = AUTO_PDF_EXTENSIONS | LEGACY_EXTENSIONS | {".zip"}
                current_paths = set()
                for path in folder.iterdir():
                    if not path.is_file() or path.suffix.lower() not in valid:
                        continue
                    key = str(path.resolve())
                    current_paths.add(key)
                    stat = path.stat()
                    previous = self._stability.get(key)
                    marker = (stat.st_size, stat.st_mtime_ns)
                    if previous is None or previous[:2] != marker:
                        self._stability[key] = (stat.st_size, stat.st_mtime_ns, time.monotonic())
                        continue
                    if time.monotonic() - previous[2] < settings["stable_seconds"]:
                        continue
                    if path.suffix.lower() == ".zip":
                        result = self.intake_paths([path], "watch")
                        if result["queued"]:
                            self._move_intake_to_processed(path)
                    else:
                        self.enqueue(path, path.name, "watch")
                for key in list(self._stability):
                    if key not in current_paths:
                        self._stability.pop(key, None)
            except Exception:
                continue

    def _match_identifiers(self, text: str) -> tuple[list[dict], bool, list[dict]]:
        normalized = re.sub(r"[^A-Z0-9]", "", str(text or "").upper())
        index = self.db.material_identifier_index()
        patents: dict[int, dict] = {}
        matches = []
        conflict = False
        for identifier, records in index.items():
            if identifier not in normalized:
                continue
            if len(records) > 1:
                conflict = True
            for patent_id, patent in records.items():
                patents[patent_id] = patent
                matches.append({"patent_id": patent_id, "identifier": identifier, "ambiguous": len(records) > 1})
        best: dict[int, dict] = {}
        for match in matches:
            current = best.get(match["patent_id"])
            if current is None or len(match["identifier"]) > len(current["identifier"]):
                best[match["patent_id"]] = match
        output = []
        for patent_id, patent in patents.items():
            output.append({**patent, "matched_identifier": best[patent_id]["identifier"]})
        return sorted(output, key=lambda item: item["id"]), conflict, matches

    def _patent_from_acceptance_notice(self, analysis: dict, source_name: str, recognized_text: str) -> dict | None:
        """Create one provisional master-table row only from high-confidence acceptance evidence."""
        if str(analysis.get("category") or "") != "受理通知书":
            return None
        if str(analysis.get("category_confidence") or "").lower() != "high":
            return None
        if str(analysis.get("record_confidence") or "").lower() != "high":
            return None
        numbers: dict[str, str] = {}
        for value in analysis.get("application_numbers") or []:
            original = str(value or "").strip()
            normalized = normalize_application_no(original)
            if len(normalized) >= 8:
                numbers.setdefault(normalized, original)
        if len(numbers) != 1:
            return None
        evidence_text = normalize_application_no(source_name + "\n" + recognized_text)
        normalized_no, original_no = next(iter(numbers.items()))
        if normalized_no not in evidence_text:
            return None
        filing_date = str(analysis.get("filing_date") or "")
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", filing_date):
            return None
        title_cn = str(analysis.get("patent_title_cn") or "").strip()
        title_original = str(analysis.get("patent_title_original") or analysis.get("patent_title") or "").strip()
        if not title_cn and not title_original:
            return None
        applicants = [str(value).strip() for value in (analysis.get("applicants") or []) if str(value).strip()]
        inventors = [str(value).strip() for value in (analysis.get("inventors") or []) if str(value).strip()]
        if not applicants and not inventors:
            return None
        jurisdiction = str(analysis.get("primary_jurisdiction") or "").upper()
        if jurisdiction and not normalized_no.startswith(jurisdiction) and jurisdiction != "PCT":
            application_no = jurisdiction + original_no
        else:
            application_no = original_no
        country = (JURISDICTION_ALIASES.get(jurisdiction) or (jurisdiction,))[0]
        patent_type = str(analysis.get("patent_type") or "申请")
        record = {
            "申请号": application_no,
            "标题(译)(简体中文)": title_cn,
            "标题": title_original or title_cn,
            "法律状态/事件": "申请 | 已受理",
            "[标]当前申请(专利权)人": " | ".join(applicants),
            "[标]原始申请(专利权)人": " | ".join(applicants),
            "发明人": " | ".join(inventors),
            "申请日": filing_date,
            "受理局": country,
            "专利类型": patent_type,
            "业务分类": "未分类",
            "记录来源": "受理通知书自动新增",
            "资料完整性": "待后续总表核对",
            "来源文件": source_name,
        }
        patent = self.db.add_from_acceptance_notice(record, source_name)
        patent["matched_identifier"] = normalized_no
        return patent

    @staticmethod
    def _primary_jurisdiction_matches(patents: list[dict], source_name: str,
                                      analysis: dict | None = None) -> tuple[list[dict], str, list[dict]]:
        """Prefer the office this document belongs to over referenced family members."""
        if len(patents) <= 1:
            return patents, "", []
        hint = filename_jurisdiction_hint(source_name)
        if not hint and analysis:
            candidate = str(analysis.get("primary_jurisdiction") or "").strip().upper()
            if candidate in JURISDICTION_ALIASES:
                hint = candidate
        if not hint:
            return patents, "", []
        primary = [patent for patent in patents if patent_jurisdiction_code(patent) == hint]
        if not primary:
            return patents, hint, []
        excluded = [patent for patent in patents if patent_jurisdiction_code(patent) != hint]
        return primary, hint, excluded

    def _source_document(self, source: Path, source_name: str, digest: str, page_count: int) -> tuple[int, Path]:
        extension = source.suffix.lower() or ".bin"
        target_dir = self.originals / digest[:2]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{digest}{extension}"
        if not target.exists():
            shutil.copy2(source, target)
        with self.db.connect() as conn:
            row = conn.execute("SELECT id,stored_path FROM source_documents WHERE sha256=?", (digest,)).fetchone()
            if row:
                return int(row[0]), self.data_dir / str(row[1])
            cursor = conn.execute("""INSERT INTO source_documents(
                sha256,original_name,stored_path,content_type,file_size,page_count,created_at
                ) VALUES(?,?,?,?,?,?,?)""", (
                    digest, source_name, str(target.relative_to(self.data_dir)),
                    mimetypes.guess_type(source_name)[0] or "application/octet-stream",
                    source.stat().st_size, page_count, now_text(),
                ))
            return int(cursor.lastrowid), target

    def _process_job(self, job_id: int):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM archive_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return
        job = dict(row)
        source = Path(job["source_path"])
        if not source.is_file():
            raise RuntimeError("待归档原文件不存在")
        suffix = source.suffix.lower()
        digest = sha256(source)
        page_texts: list[str] = []
        page_count = 0
        ocr_evidence: list[dict] = []
        extraction_error = ""
        extraction_metadata: dict = {}
        mineru_required_failed = False
        if suffix == ".pdf":
            page_texts, page_count = extract_pdf_text(source)
            settings = self.settings_store.load()
            if settings.get("mineru_enabled") and settings.get("mineru_configured"):
                try:
                    secret_settings = self.settings_store.load(include_secrets=True)
                    mineru_text, extraction_metadata = MinerUParser(secret_settings).extract(source, job["source_name"])
                    page_texts = [mineru_text]
                except Exception as exc:
                    mineru_required_failed = True
                    extraction_error = str(exc)
            elif settings.get("mineru_enabled"):
                mineru_required_failed = True
                extraction_error = "已启用 MinerU，但尚未配置 Token"
            if (not settings.get("mineru_enabled") or mineru_required_failed) \
                    and settings.get("tencent_enabled") and settings.get("tencent_configured"):
                settings = self.settings_store.load(include_secrets=True)
                ocr = TencentOCR(settings)
                for index, text in enumerate(page_texts):
                    if len(re.sub(r"\s+", "", text)) >= 80:
                        continue
                    try:
                        ocr_text, detections = ocr.recognize_page(source, index + 1)
                        if ocr_text:
                            page_texts[index] = ocr_text
                            ocr_evidence.extend(detections)
                            extraction_metadata = {"provider": "腾讯云 OCR 备用", "ocr": True}
                            mineru_required_failed = False
                    except Exception as exc:
                        extraction_error = extraction_error + "；腾讯云备用失败：" + str(exc)
        else:
            extraction_error = "Word 和图片暂不参与全自动识别，请人工确认"
        source_document_id, immutable_source = self._source_document(source, job["source_name"], digest, page_count)
        extraction_path = self.extracted / f"{digest}.json"
        extraction_path.write_text(json.dumps({
            "source_name": job["source_name"], "page_count": page_count, "pages": page_texts,
            "ocr_evidence": ocr_evidence, "extraction": extraction_metadata,
        }, ensure_ascii=False), "utf-8")
        combined = "\n".join(f"[第{index + 1}页]\n{text}" for index, text in enumerate(page_texts))
        exact_patents, identifier_conflict, identifier_evidence = self._match_identifiers(job["source_name"] + "\n" + combined)
        filename_patents, filename_identifier_conflict, filename_identifier_evidence = self._match_identifiers(job["source_name"])
        country_prefixed_filename_patents = [
            item for item in filename_patents
            if re.match(r"^[A-Z]{2,3}\d", str(item.get("matched_identifier") or ""))
        ]
        if country_prefixed_filename_patents:
            filename_patents = country_prefixed_filename_patents
            filename_ids_for_evidence = {int(item["id"]) for item in filename_patents}
            filename_identifier_evidence = [
                item for item in filename_identifier_evidence
                if int(item.get("patent_id") or 0) in filename_ids_for_evidence
                and re.match(r"^[A-Z]{2,3}\d", str(item.get("identifier") or ""))
            ]
            filename_identifier_conflict = any(bool(item.get("ambiguous")) for item in filename_identifier_evidence)
        category, category_explicit = classify_material(job["source_name"] + "\n" + combined)
        document_date = extract_document_date(job["source_name"] + "\n" + combined)
        candidates = [] if exact_patents else self.db.patent_candidates(combined, job["source_name"])
        analysis, analysis_error = {}, ""
        try:
            if load_ai_settings(self.data_dir).get("configured"):
                analysis = analyze_material_document(self.data_dir, job["source_name"], combined, candidates)
        except Exception as exc:
            analysis_error = str(exc)
        filename_ids = {int(item["id"]) for item in filename_patents}
        excluded_family_patents = []
        if exact_patents and filename_ids:
            selected = [item for item in exact_patents if int(item["id"]) in filename_ids]
            excluded_family_patents = [item for item in exact_patents if int(item["id"]) not in filename_ids]
            exact_patents = selected or exact_patents
            primary_jurisdiction = "文件名强编号"
            identifier_conflict = filename_identifier_conflict
            identifier_evidence.extend(filename_identifier_evidence)
        else:
            exact_patents, primary_jurisdiction, excluded_family_patents = self._primary_jurisdiction_matches(
                exact_patents, job["source_name"], analysis,
            )
        if primary_jurisdiction and excluded_family_patents:
            selected_ids = {int(item["id"]) for item in exact_patents}
            if primary_jurisdiction == "文件名强编号":
                # Family identifiers in the OCR body may legitimately be shared by
                # several rows. Once a unique country-prefixed filename identifier
                # selected the filing, only that strong identifier can block auto
                # archiving; shared body references are evidence, not a conflict.
                identifier_conflict = any(
                    bool(item.get("ambiguous"))
                    and int(item.get("patent_id") or 0) in selected_ids
                    and re.match(r"^[A-Z]{2,3}\d", str(item.get("identifier") or ""))
                    for item in filename_identifier_evidence
                )
            else:
                identifier_conflict = any(
                    bool(item.get("ambiguous")) and int(item.get("patent_id") or 0) in selected_ids
                    for item in identifier_evidence
                )
            identifier_evidence.append({
                "primary_jurisdiction": primary_jurisdiction,
                "excluded_family_patent_ids": [int(item["id"]) for item in excluded_family_patents],
                "reason": "文件名强编号或材料主办国家优先；其他国家编号作为同族引用保留在证据中",
            })
        if analysis.get("category") in MATERIAL_CATEGORIES:
            proposed = str(analysis["category"])
            confidence = str(analysis.get("category_confidence") or "").lower()
            if confidence == "high":
                category, category_explicit = proposed, True
            elif not category_explicit:
                category = proposed
        acceptance_auto_added = False
        if not exact_patents and category == "受理通知书" and not mineru_required_failed and not analysis_error:
            try:
                acceptance_patent = self._patent_from_acceptance_notice(
                    {**analysis, "category": category}, job["source_name"], combined,
                )
            except Exception as exc:
                analysis_error = "受理通知书未自动新增：" + str(exc)
                acceptance_patent = None
            if acceptance_patent:
                exact_patents = [acceptance_patent]
                acceptance_auto_added = bool(acceptance_patent.pop("was_added", False))
                primary_jurisdiction = "受理通知书自动新增"
                identifier_conflict = False
                identifier_evidence.append({
                    "patent_id": int(acceptance_patent["id"]),
                    "identifier": acceptance_patent.get("matched_identifier") or "",
                    "ambiguous": False,
                    "reason": "MinerU 与 DeepSeek 从受理通知书提取唯一强编号后自动新增",
                })
        document_date = str(analysis.get("document_date") or document_date or "")
        summary = str(analysis.get("summary") or "")
        if acceptance_auto_added and exact_patents:
            summary = f"已自动加入总表：{exact_patents[0]['application_no']}。" + summary
        actions = analysis.get("action_items") or []
        evidence = analysis.get("evidence") or []
        match_method = ("受理通知书自动新增" if acceptance_auto_added else
                        "申请号/公开号精确匹配" if exact_patents else
                        "AI候选待确认" if analysis.get("candidate_patent_ids") else "未匹配")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM archive_job_patents WHERE job_id=?", (job_id,))
            for patent in exact_patents:
                conn.execute("""INSERT INTO archive_job_patents(job_id,patent_id,selected,matched_identifier,reason)
                    VALUES(?,?,?,?,?)""", (
                        job_id, patent["id"], 1, patent["matched_identifier"],
                        "受理通知书识别后自动加入总表" if acceptance_auto_added else
                        f"材料主办国家为 {primary_jurisdiction}，同族编号不归档" if primary_jurisdiction else "材料中出现唯一强编号",
                    ))
            if not exact_patents:
                selected_candidates = set(analysis.get("candidate_patent_ids") or [])
                candidate_map = {int(item["id"]): item for item in candidates}
                for patent_id in selected_candidates:
                    if patent_id in candidate_map:
                        item = candidate_map[patent_id]
                        conn.execute("""INSERT INTO archive_job_patents(job_id,patent_id,selected,matched_identifier,reason)
                            VALUES(?,?,?,?,?)""", (job_id, patent_id, 0, "", analysis.get("candidate_reason") or item.get("reason") or "AI候选"))
            error_parts = [part for part in (extraction_error, analysis_error) if part]
            status = "待确认"
            stage = "等待人工确认"
            auto_archive = bool(exact_patents and category_explicit and not identifier_conflict
                                and suffix == ".pdf" and not mineru_required_failed)
            conn.execute("""UPDATE archive_jobs SET source_sha256=?,source_document_id=?,category=?,document_date=?,
                summary=?,action_items_json=?,evidence_json=?,match_method=?,match_json=?,analysis_json=?,
                error_message=?,auto_archive=?,status=?,stage=?,updated_at=? WHERE id=?""", (
                    digest, source_document_id, category, document_date, summary, json_text(actions), json_text(evidence),
                    match_method, json_text(identifier_evidence), json_text({**analysis, "text_path": str(extraction_path.relative_to(self.data_dir))}),
                    "；".join(error_parts), int(auto_archive), status, stage, now_text(), job_id,
                ))
        if auto_archive:
            try:
                self.archive_job(job_id, confirmed=False)
            except FileExistsError as exc:
                with self.db.connect() as conn:
                    conn.execute("UPDATE archive_jobs SET status='待确认',stage='存在同名异内容冲突',error_message=?,updated_at=? WHERE id=?",
                                 (str(exc), now_text(), job_id))

    def _job_with_patents(self, job_id: int) -> tuple[dict, list[dict]]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM archive_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("归档任务不存在")
            job = dict(row)
            patents = [dict(item) for item in conn.execute("""SELECT p.*,j.selected,j.matched_identifier,j.reason
                FROM archive_job_patents j JOIN patents p ON p.id=j.patent_id
                WHERE j.job_id=? ORDER BY p.id""", (job_id,))]
        return job, patents

    def archive_job(self, job_id: int, patent_ids: list[int] | None = None, category: str = "",
                    document_date: str = "", confirmed: bool = True) -> dict:
        job, suggested = self._job_with_patents(job_id)
        if patent_ids is None:
            patents = [item for item in suggested if item.get("selected")]
        else:
            clean_ids = []
            for value in patent_ids:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
                if value not in clean_ids:
                    clean_ids.append(value)
            with self.db.connect() as conn:
                patents = []
                for patent_id in clean_ids:
                    row = conn.execute("SELECT * FROM patents WHERE id=?", (patent_id,)).fetchone()
                    if row:
                        patents.append(dict(row))
        if not patents:
            raise ValueError("请至少选择一件专利")
        category = category or job.get("category") or "其他材料"
        if category not in MATERIAL_CATEGORIES:
            raise ValueError("材料类型不在允许范围内")
        document_date = document_date or job.get("document_date") or ""
        if document_date and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", document_date):
            raise ValueError("发文日期格式不正确")
        digest = str(job.get("source_sha256") or "")
        source_document_id = job.get("source_document_id")
        if not digest or not source_document_id:
            raise ValueError("材料尚未完成识别")
        with self.db.connect() as conn:
            source_row = conn.execute("SELECT * FROM source_documents WHERE id=?", (source_document_id,)).fetchone()
        if not source_row:
            raise ValueError("归档原件记录不存在")
        immutable_source = self.data_dir / source_row["stored_path"]
        if not immutable_source.is_file():
            raise ValueError("归档原件文件不存在")
        planned = []
        for patent in patents:
            name = standard_material_name(patent, category, document_date, immutable_source.suffix)
            target_dir = self.data_dir / "materials" / patent["app_key"] / category
            target = target_dir / name
            if target.exists() and sha256(target) != digest:
                raise FileExistsError(f"{name} 已存在但内容不同，未覆盖")
            planned.append((patent, name, target))
        archived = skipped = 0
        for patent, name, target in planned:
            if self.db.material_hash_exists(int(patent["id"]), category, digest):
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(immutable_source, target)
            self.db.add_material(
                int(patent["id"]), category, name, str(target.relative_to(self.data_dir)), digest,
                patent["application_no"], source_document_id=int(source_document_id), standard_name=name,
                material_date=document_date, summary=job.get("summary") or "",
                action_items=_loads(job.get("action_items_json"), []), match_method=job.get("match_method") or "人工确认",
                evidence=_loads(job.get("evidence_json"), []), review_status="已确认" if confirmed else "自动归档",
            )
            archived += 1
        with self.db.connect() as conn:
            conn.execute("DELETE FROM archive_job_patents WHERE job_id=?", (job_id,))
            for patent in patents:
                conn.execute("INSERT INTO archive_job_patents(job_id,patent_id,selected,matched_identifier,reason) VALUES(?,?,?,?,?)",
                             (job_id, patent["id"], 1, "", "人工确认" if confirmed else "强编号自动归档"))
            conn.execute("""UPDATE archive_jobs SET status='已归档',stage='归档完成',category=?,document_date=?,
                error_message='',updated_at=? WHERE id=?""", (category, document_date, now_text(), job_id))
            conn.execute("INSERT INTO operations(created_at,operation,target,result,message) VALUES(?,?,?,?,?)",
                         (now_text(), "V3自动归档", job["source_name"], "成功", f"新增 {archived}，跳过重复 {skipped}"))
        source_path = Path(job["source_path"])
        if job.get("intake_source") in {"watch", "upload"} and source_path.is_file():
            self._move_intake_to_processed(source_path)
        return {"archived": archived, "skipped": skipped, "job_id": job_id}

    def _move_intake_to_processed(self, path: Path):
        settings = self.settings_store.public()
        processed = Path(settings["processed_folder"])
        processed.mkdir(parents=True, exist_ok=True)
        target = processed / safe_file_name(path.name)
        number = 2
        while target.exists():
            if sha256(target) == sha256(path):
                path.unlink(missing_ok=True)
                return
            target = processed / f"{path.stem}-{number}{path.suffix}"
            number += 1
        shutil.move(str(path), target)

    def list_jobs(self, status: str = "", limit: int = 300) -> dict:
        clauses, params = [], []
        if status:
            clauses.append("j.status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT j.* FROM archive_jobs j" + where + " ORDER BY j.id DESC LIMIT ?", (*params, min(max(limit, 1), 1000))
            )]
            for job in rows:
                job["patents"] = [dict(item) for item in conn.execute("""SELECT p.id,p.application_no,p.title_cn,p.title_original,
                    p.country,jp.selected,jp.matched_identifier,jp.reason FROM archive_job_patents jp
                    JOIN patents p ON p.id=jp.patent_id WHERE jp.job_id=? ORDER BY p.id""", (job["id"],))]
                job["action_items"] = _loads(job.get("action_items_json"), [])
                job["evidence"] = _loads(job.get("evidence_json"), [])
                job.pop("analysis_json", None)
        counts = {name: 0 for name in ARCHIVE_JOB_STATUSES}
        with self.db.connect() as conn:
            for row in conn.execute("SELECT status,COUNT(*) FROM archive_jobs GROUP BY status"):
                counts[str(row[0])] = int(row[1])
        return {"items": rows, "counts": counts}

    def confirm(self, job_id: int, payload: dict) -> dict:
        return self.archive_job(
            job_id,
            patent_ids=payload.get("patent_ids") or [],
            category=str(payload.get("category") or ""),
            document_date=str(payload.get("document_date") or ""),
            confirmed=True,
        )

    @staticmethod
    def _clean_job_ids(values: Any) -> list[int]:
        job_ids = []
        for value in values or []:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in job_ids:
                job_ids.append(value)
        if not job_ids:
            raise ValueError("请至少选择一个归档任务")
        if len(job_ids) > 100:
            raise ValueError("单次最多处理 100 个归档任务")
        return job_ids

    def batch_confirm(self, payload: dict) -> dict:
        """Confirm reviewed jobs using their selected candidates and extracted metadata."""
        job_ids = self._clean_job_ids(payload.get("job_ids"))
        category_override = str(payload.get("category") or "").strip()
        if category_override and category_override not in MATERIAL_CATEGORIES:
            raise ValueError("批量材料类型不在允许范围内")
        archived = skipped = 0
        completed = []
        errors = []
        for job_id in job_ids:
            try:
                job, patents = self._job_with_patents(job_id)
                if job.get("status") != "待确认":
                    raise ValueError("任务不是待确认状态")
                patent_ids = [int(item["id"]) for item in patents if item.get("selected")]
                result = self.archive_job(
                    job_id, patent_ids=patent_ids,
                    category=category_override or str(job.get("category") or ""),
                    document_date=str(job.get("document_date") or ""), confirmed=True,
                )
                archived += int(result.get("archived") or 0)
                skipped += int(result.get("skipped") or 0)
                completed.append(job_id)
            except (ValueError, FileExistsError, OSError) as exc:
                errors.append({"job_id": job_id, "error": str(exc)})
        return {"completed": completed, "archived": archived, "skipped": skipped, "errors": errors}

    def batch_ignore(self, payload: dict) -> dict:
        job_ids = self._clean_job_ids(payload.get("job_ids"))
        ignored = []
        with self.db.connect() as conn:
            for job_id in job_ids:
                cursor = conn.execute(
                    "UPDATE archive_jobs SET status='已忽略',stage='人工批量忽略',updated_at=? "
                    "WHERE id=? AND status IN ('待确认','失败')", (now_text(), job_id),
                )
                if cursor.rowcount:
                    ignored.append(job_id)
        return {"ignored": ignored, "skipped": len(job_ids) - len(ignored)}

    def retry(self, job_id: int) -> dict:
        with self.db.connect() as conn:
            row = conn.execute("SELECT source_path FROM archive_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("归档任务不存在")
            if not Path(row[0]).is_file():
                raise ValueError("原入口文件已不存在，无法重试")
            conn.execute("UPDATE archive_jobs SET status='待处理',stage='等待重试',error_message='',updated_at=? WHERE id=?",
                         (now_text(), job_id))
        self._wake.set()
        return {"ok": True, "job_id": job_id}

    def retry_pending_review(self) -> dict:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id,source_path FROM archive_jobs WHERE status='待确认'").fetchall()
            retry_ids = [int(row[0]) for row in rows if Path(str(row[1])).is_file()]
            for job_id in retry_ids:
                conn.execute("""UPDATE archive_jobs SET status='待处理',stage='使用 MinerU + DeepSeek 重新识别',
                    error_message='',updated_at=? WHERE id=?""", (now_text(), job_id))
        if retry_ids:
            self._wake.set()
        return {"requeued": len(retry_ids), "job_ids": retry_ids}

    def ignore(self, job_id: int) -> dict:
        with self.db.connect() as conn:
            cursor = conn.execute("UPDATE archive_jobs SET status='已忽略',stage='人工忽略',updated_at=? WHERE id=?",
                                  (now_text(), job_id))
            if not cursor.rowcount:
                raise ValueError("归档任务不存在")
        return {"ok": True, "job_id": job_id}

    def delete_material(self, material_id: int) -> dict:
        """Remove one patent-material relation and move its visible copy to a recoverable trash folder."""
        with self.db.connect() as conn:
            row = conn.execute("""SELECT m.*,p.application_no FROM materials m
                JOIN patents p ON p.id=m.patent_id WHERE m.id=?""", (material_id,)).fetchone()
            if not row:
                raise ValueError("材料记录不存在或已经删除")
            item = dict(row)
            remaining = conn.execute(
                "SELECT COUNT(*) FROM materials WHERE stored_path=? AND id<>?",
                (item["stored_path"], material_id),
            ).fetchone()[0]
        source = (self.data_dir / str(item["stored_path"])).resolve()
        materials_root = (self.data_dir / "materials").resolve()
        if source != materials_root and materials_root not in source.parents:
            raise ValueError("材料路径不在允许删除的目录中")
        moved_to = None
        if not remaining and source.is_file():
            trash = self.data_dir / "回收站" / "材料" / datetime.now().strftime("%Y%m%d")
            trash.mkdir(parents=True, exist_ok=True)
            moved_to = trash / f"{material_id}-{safe_file_name(item['original_name'])}"
            number = 2
            while moved_to.exists():
                moved_to = trash / f"{material_id}-{number}-{safe_file_name(item['original_name'])}"
                number += 1
            shutil.move(str(source), moved_to)
        try:
            with self.db.connect() as conn:
                cursor = conn.execute("DELETE FROM materials WHERE id=?", (material_id,))
                if not cursor.rowcount:
                    raise ValueError("材料记录不存在或已经删除")
                conn.execute("INSERT INTO operations(created_at,operation,application_no,target,result,message) VALUES(?,?,?,?,?,?)",
                             (now_text(), "删除材料", item["application_no"], item["original_name"], "成功",
                              "材料副本已移入回收站" if moved_to else "删除材料关联；共用文件仍保留"))
        except Exception:
            if moved_to and moved_to.is_file() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(moved_to), source)
            raise
        return {
            "deleted": True, "material_id": material_id,
            "recoverable": bool(moved_to),
            "message": "已移入本机回收站" if moved_to else "材料关联已删除，共用文件仍保留",
        }

    def notify_patent_index_updated(self) -> dict:
        """Stable hook for the future automatic master-table updater."""
        with self.db.connect() as conn:
            cursor = conn.execute("""UPDATE archive_jobs SET status='待处理',stage='总表已更新，重新匹配',updated_at=?
                WHERE status IN ('待确认','失败') AND (match_method='未匹配' OR match_method='AI候选待确认')""", (now_text(),))
        if cursor.rowcount:
            self._wake.set()
        return {"requeued": cursor.rowcount}

    def audit_existing(self) -> dict:
        materials_root = (self.data_dir / "materials").resolve()
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute("""SELECT m.*,p.app_key,p.application_no,p.raw_json,
                p.title_cn,p.title_original FROM materials m JOIN patents p ON p.id=m.patent_id ORDER BY m.id""")]
        referenced = set()
        items = []
        hashes: dict[str, list[int]] = {}
        for row in rows:
            path = (self.data_dir / row["stored_path"]).resolve()
            referenced.add(str(path).lower())
            exists = path.is_file() and (path == self.data_dir.resolve() or self.data_dir.resolve() in path.parents)
            actual_hash = sha256(path) if exists else ""
            if actual_hash:
                hashes.setdefault(actual_hash, []).append(int(row["id"]))
            standard_name = standard_material_name(row, row["category"], row.get("material_date") or "")
            issues = []
            if not exists:
                issues.append("文件缺失")
            elif row.get("sha256") and row["sha256"] != actual_hash:
                issues.append("数据库哈希与文件不一致")
            if row["category"] not in MATERIAL_CATEGORIES:
                issues.append("分类不在V3分类表")
            if path.name != standard_name:
                issues.append("文件名待规范")
            items.append({
                "material_id": row["id"], "patent_id": row["patent_id"], "application_no": row["application_no"],
                "category": row["category"], "stored_path": row["stored_path"], "exists": exists,
                "actual_sha256": actual_hash, "standard_name": standard_name, "issues": issues,
            })
        duplicate_ids = {material_id for ids in hashes.values() if len(ids) > 1 for material_id in ids}
        for item in items:
            if item["material_id"] in duplicate_ids:
                item["issues"].append("内容重复")
        orphans = []
        if materials_root.is_dir():
            for path in materials_root.rglob("*"):
                if path.is_file() and str(path.resolve()).lower() not in referenced:
                    orphans.append(str(path.relative_to(self.data_dir)))
        token = uuid.uuid4().hex
        report = {
            "token": token, "created_at": now_text(),
            "summary": {
                "database_records": len(rows), "files_present": sum(item["exists"] for item in items),
                "files_missing": sum(not item["exists"] for item in items), "unique_hashes": len(hashes),
                "duplicate_groups": sum(len(ids) > 1 for ids in hashes.values()),
                "duplicate_records": len(duplicate_ids), "orphan_files": len(orphans),
                "rename_suggestions": sum("文件名待规范" in item["issues"] for item in items),
            },
            "items": items, "orphans": orphans, "applied": False,
        }
        target = self.audits / f"{token}.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        summary = report["summary"]
        rows_html = "".join(
            "<tr>"
            f"<td>{item['material_id']}</td><td>{html.escape(str(item['application_no']))}</td>"
            f"<td>{html.escape(str(item['category']))}</td><td>{html.escape(str(item['stored_path']))}</td>"
            f"<td>{html.escape(str(item['standard_name']))}</td>"
            f"<td class={'bad' if item['issues'] else 'ok'}>{html.escape('；'.join(item['issues']) or '正常')}</td>"
            "</tr>" for item in items
        )
        orphan_html = "".join(f"<li>{html.escape(value)}</li>" for value in orphans) or "<li>无</li>"
        report_html = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>V3现有材料审计预览</title>
<style>body{{font-family:'Microsoft YaHei UI',sans-serif;margin:32px;color:#17231e}}h1{{margin-bottom:6px}}p{{color:#68766f}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:24px 0}}.card{{border:1px solid #dce3de;border-radius:10px;padding:14px}}.card b{{display:block;font-size:24px;margin-top:5px}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border:1px solid #dce3de;padding:8px;text-align:left;vertical-align:top}}th{{background:#f3f6f3;position:sticky;top:0}}.bad{{color:#a23b3b}}.ok{{color:#1f6b4f}}code{{word-break:break-all}}</style></head><body>
<h1>专利查询系统 V3｜现有材料审计预览</h1><p>生成时间：{html.escape(report['created_at'])}。本报告只读生成，尚未执行迁移。</p>
<div class='cards'><div class='card'>数据库记录<b>{summary['database_records']}</b></div><div class='card'>实际存在<b>{summary['files_present']}</b></div><div class='card'>唯一内容<b>{summary['unique_hashes']}</b></div><div class='card'>重复组<b>{summary['duplicate_groups']}</b></div><div class='card'>缺失文件<b>{summary['files_missing']}</b></div><div class='card'>孤立文件<b>{summary['orphan_files']}</b></div><div class='card'>建议规范命名<b>{summary['rename_suggestions']}</b></div><div class='card'>重复记录<b>{summary['duplicate_records']}</b></div></div>
<h2>逐项检查</h2><table><thead><tr><th>ID</th><th>申请号</th><th>分类</th><th>现路径</th><th>建议标准名称</th><th>问题</th></tr></thead><tbody>{rows_html}</tbody></table>
<h2>数据库外的孤立文件</h2><ul>{orphan_html}</ul></body></html>"""
        (self.audits / f"{token}.html").write_text(report_html, "utf-8")
        report["report_url"] = f"/api/archive/audit/{token}/report"
        return report

    def audit_report_path(self, token: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            raise ValueError("审计预览编号不正确")
        path = self.audits / f"{token}.html"
        if not path.is_file():
            raise ValueError("审计详细报告不存在，请重新生成")
        return path

    def apply_audit(self, token: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            raise ValueError("审计预览编号不正确")
        path = self.audits / f"{token}.json"
        if not path.is_file():
            raise ValueError("审计预览已失效，请重新生成")
        report = json.loads(path.read_text("utf-8"))
        if report.get("applied"):
            raise ValueError("该审计预览已经执行过")
        migrated = skipped = conflicts = 0
        with self.db.connect() as conn:
            patent_rows = {int(row["id"]): dict(row) for row in conn.execute("SELECT * FROM patents")}
        for item in report.get("items") or []:
            if not item.get("exists"):
                skipped += 1
                continue
            source = self.data_dir / item["stored_path"]
            digest = item["actual_sha256"]
            patent = patent_rows.get(int(item["patent_id"]))
            if not patent:
                skipped += 1
                continue
            source_document_id, immutable = self._source_document(source, source.name, digest, 0)
            category = item["category"] if item["category"] in MATERIAL_CATEGORIES else "其他材料"
            name = standard_material_name(patent, category, "")
            target = self.data_dir / "materials" / patent["app_key"] / category / name
            if target.exists() and sha256(target) != digest:
                conflicts += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(immutable, target)
            with self.db.connect() as conn:
                conn.execute("""UPDATE materials SET source_document_id=?,standard_name=?,stored_path=?,match_method=?,
                    review_status=?,evidence_json=COALESCE(NULLIF(evidence_json,''),'[]'),
                    action_items_json=COALESCE(NULLIF(action_items_json,''),'[]') WHERE id=?""", (
                    source_document_id, name, str(target.relative_to(self.data_dir)), "V2.1材料迁移", "迁移确认", item["material_id"],
                ))
            migrated += 1
        for orphan in report.get("orphans") or []:
            source = self.data_dir / orphan
            if source.is_file():
                self.enqueue(source, source.name, "audit")
        report["applied"] = True
        report["applied_at"] = now_text()
        report["apply_result"] = {"migrated": migrated, "skipped": skipped, "conflicts": conflicts, "orphan_jobs": len(report.get("orphans") or [])}
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        with self.db.connect() as conn:
            conn.execute("INSERT INTO operations(created_at,operation,result,message) VALUES(?,?,?,?)",
                         (now_text(), "V3材料迁移", "成功", f"迁移 {migrated}，跳过 {skipped}，冲突 {conflicts}"))
        return report["apply_result"]
