"""Feishu Bitable transport; credentials never appear in errors or public settings."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


class FeishuError(RuntimeError):
    def __init__(self, message, *, uncertain=False, retryable=False):
        super().__init__(message)
        self.uncertain = uncertain
        self.retryable = retryable


class CredentialStore:
    """Reuse the existing patent_tool Windows Credential Manager approach."""
    def __init__(self, app_id):
        self.target = "DEMOPatentSync:" + app_id

    def _api(self):
        if os.name != "nt":
            raise RuntimeError("飞书密钥仅支持 Windows 凭据管理器")
        class Credential(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.POINTER(ctypes.POINTER(Credential))]
        api.CredReadW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        return api, Credential

    def save(self, secret):
        api, kind = self._api()
        blob = secret.encode("utf-16-le")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = kind(Type=1, TargetName=self.target, CredentialBlobSize=len(blob),
                    CredentialBlob=buf, Persist=2, UserName="Feishu application")
        if not api.CredWriteW(ctypes.byref(cred), 0):
            raise RuntimeError("Windows 凭据保存失败，请核对运行账户")

    def load(self):
        api, kind = self._api()
        ptr = ctypes.POINTER(kind)()
        if not api.CredReadW(self.target, 1, 0, ctypes.byref(ptr)):
            if ctypes.get_last_error() == 1168:
                return ""
            raise RuntimeError("Windows 凭据读取失败，请使用保存密钥的账户运行")
        try:
            return ctypes.string_at(ptr.contents.CredentialBlob,
                                    ptr.contents.CredentialBlobSize).decode("utf-16-le")
        finally:
            api.CredFree(ptr)


class FeishuClient:
    # Same endpoints and token flow as patent_tool/feishu.py, with strict pagination,
    # explicit acknowledgement and safe retry classification added.
    BASE = "https://open.feishu.cn/open-apis"
    TRANSIENT = {99991400, 1254290, 1254291, 1255001, 1255002, 1255040}
    TOKEN_ERRORS = {99991661, 99991663, 99991664, 99991668}

    def __init__(self, config, secret, *, opener=None, sleep=time.sleep):
        self.config, self.secret = config, secret
        self.opener, self.sleep = opener or urllib.request.urlopen, sleep
        self.token, self.expires = "", 0

    def _once(self, method, path, body=None, auth=True):
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            if not self.token or time.monotonic() >= self.expires:
                data = self._request("POST", "/auth/v3/tenant_access_token/internal",
                                     {"app_id": self.config["app_id"], "app_secret": self.secret}, auth=False)
                token = data.get("tenant_access_token")
                if not isinstance(token, str) or not token:
                    raise FeishuError("飞书未返回有效应用令牌")
                self.token = token
                self.expires = time.monotonic() + max(1, int(data.get("expire", 7200)) - 120)
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.BASE + path, data=payload, headers=headers, method=method)
        try:
            with self.opener(req, timeout=25) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            # HTTP 5xx/408 can occur after a committed write. Never blindly replay.
            if status in (401, 403, 404, 429):
                if status == 401:
                    self.token = ""
                raise FeishuError(f"飞书 HTTP {status}，请检查权限、表格或调用频率",
                                  retryable=status in (401, 429)) from None
            raise FeishuError(f"飞书 HTTP {status}", uncertain=method != "GET",
                              retryable=method == "GET" and status >= 500) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise FeishuError("飞书连接中断或超时", uncertain=method != "GET",
                              retryable=method == "GET") from None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise FeishuError("飞书响应无法解析", uncertain=method != "GET") from None
        if not isinstance(data, dict) or not isinstance(data.get("code"), int):
            raise FeishuError("飞书响应缺少结果码", uncertain=method != "GET")
        code = data["code"]
        if code:
            if code in self.TOKEN_ERRORS:
                self.token = ""
            # Only explicit rejection codes are safely retryable for writes.
            safe_rejection = code in self.TOKEN_ERRORS or code in {99991400, 1254290, 1254291}
            raise FeishuError(f"飞书接口返回错误码 {code}",
                              uncertain=method != "GET" and code in {1255001, 1255002, 1255040},
                              retryable=safe_rejection or (method == "GET" and code in self.TRANSIENT))
        return data

    def _request(self, method, path, body=None, auth=True):
        for attempt in range(3):
            try:
                return self._once(method, path, body, auth)
            except FeishuError as exc:
                if not exc.retryable or exc.uncertain or attempt == 2:
                    raise
                self.sleep(1 + 2 * attempt)

    def _path(self):
        q = urllib.parse.quote
        return f"/bitable/v1/apps/{q(self.config['app_token'], safe='')}/tables/{q(self.config['table_id'], safe='')}"

    def _list(self, suffix):
        result, seen, page = [], set(), ""
        while True:
            query = {"page_size": 100}
            if page:
                query["page_token"] = page
            data = self._request("GET", self._path() + suffix + "?" + urllib.parse.urlencode(query)).get("data")
            if not isinstance(data, dict) or not isinstance(data.get("has_more"), bool):
                raise FeishuError("飞书分页结果不完整，本轮禁止写入")
            items = data.get("items")
            # Feishu omits `items` for an empty table: {has_more: false, total: 0}.
            # Accept only this explicit FIRST-page empty result. A missing array on
            # later pages, a null array, or an inconsistent total must still fail.
            if ("items" not in data and data["has_more"] is False
                    and type(data.get("total")) is int and data["total"] == 0
                    and not page and not result):
                items = []
            if not isinstance(items, list):
                raise FeishuError("飞书分页结果不完整，本轮禁止写入")
            result.extend(items)
            if not data["has_more"]:
                return result
            page = data.get("page_token")
            if not isinstance(page, str) or not page or page in seen:
                raise FeishuError("飞书分页游标异常，本轮禁止写入")
            seen.add(page)

    def list_fields(self):
        return self._list("/fields")

    def list_records(self):
        return self._list("/records")

    def add_field(self, name, kind):
        return self._request("POST", self._path() + "/fields", {"field_name": name, "type": kind})

    def create(self, fields, client_token):
        query = urllib.parse.urlencode({"client_token": client_token})
        data = self._request("POST", self._path() + "/records?" + query, {"fields": fields}).get("data", {})
        record = data.get("record")
        if not isinstance(record, dict) or not record.get("record_id"):
            raise FeishuError("飞书新增未返回记录 ID，等待核对", uncertain=True)
        return record["record_id"]

    def update(self, record_id, fields):
        data = self._request("PUT", self._path() + "/records/" + urllib.parse.quote(record_id, safe=""),
                             {"fields": fields}).get("data", {})
        if not isinstance(data.get("record"), dict) or data["record"].get("record_id") != record_id:
            raise FeishuError("飞书修改未返回有效确认", uncertain=True)

    def delete(self, record_id):
        data = self._request("DELETE", self._path() + "/records/" + urllib.parse.quote(record_id, safe="")).get("data", {})
        if data.get("record_id") != record_id or data.get("deleted") is not True:
            raise FeishuError("飞书移除未返回有效确认", uncertain=True)
