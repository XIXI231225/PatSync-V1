"""Credential-safe HTTP and Streamable HTTP MCP clients for patent sources."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from feishu_client import CredentialStore


class SourceError(RuntimeError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class SourceCredentials(CredentialStore):
    def __init__(self, name):
        self.target = "DEMOPatentSource:" + name

    def load(self):
        # Offline pages do not need Windows credentials. Saving remains Windows-only.
        if os.name != 'nt':
            return ''
        return super().load()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_endpoint(value):
    u = urllib.parse.urlsplit(str(value))
    if (u.username or u.password or u.query or u.fragment or not u.hostname or
            (u.scheme != "https" and not (u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("接口须使用 HTTPS，本机 MCP 可使用 HTTP；密钥请单独保存")
    return value


class Http:
    def __init__(self, secret="", *, opener=None):
        self.secret = secret
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open

    def request(self, url, body=None, *, headers=None, auth=True):
        values = {"Content-Type": "application/json", **(headers or {})}
        if auth and self.secret:
            values["Authorization"] = "Bearer " + self.secret
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=payload, headers=values)
        try:
            with self.opener(req, timeout=45) as response:
                raw = response.read(64 * 1024 * 1024 + 1)
                response_headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            raise SourceError(f"数据源 HTTP {exc.code}，请检查权限、额度或接口配置",
                              uncertain=body is not None and exc.code not in {400, 401, 403, 404, 405, 415, 422, 429}) from None
        except (OSError, TimeoutError, urllib.error.URLError):
            # Never expose exception strings: they may contain a signed URL or token.
            raise SourceError("数据源连接中断或超时；请核对已有任务后再继续", uncertain=body is not None) from None
        if len(raw) > 64 * 1024 * 1024:
            raise SourceError("数据源响应超过 64MB，未导入")
        return raw, response_headers

    def json(self, url, body=None):
        raw, _ = self.request(url, body)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SourceError("数据源响应不是有效 JSON", uncertain=body is not None) from None
        if (not isinstance(result, dict) or result.get("status") is not True
                or type(result.get("error_code")) is not int or result.get("error_code") != 0):
            code = result.get("error_code") if isinstance(result, dict) else None
            code = code if isinstance(code, int) else "未知"
            known_rejection = isinstance(result, dict) and result.get("status") is False and type(result.get("error_code")) is int
            raise SourceError(f"智慧芽接口未成功，错误码 {code}", uncertain=body is not None and not known_rejection)
        return result.get("data")


class Mcp:
    def __init__(self, endpoint, secret, *, http=None):
        self.endpoint = validate_endpoint(endpoint)
        self.http = http or Http(secret)
        self.session = ""
        self.version = "2025-11-25"
        self.sequence = 0

    def rpc(self, method, params, *, notification=False):
        self.sequence += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = self.sequence
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": self.version}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        raw, response_headers = self.http.request(self.endpoint, body, headers=headers)
        for name, value in response_headers.items():
            if name.lower() == "mcp-session-id":
                self.session = value
        if notification:
            return None
        try:
            if raw.lstrip().startswith((b"event:", b"data:", b":")):
                candidates = []
                for block in raw.decode().replace("\r\n", "\n").split("\n\n"):
                    payload = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
                    if payload:
                        candidates.append(json.loads(payload))
                result = next(d for d in candidates if d.get("id") == self.sequence)
            else:
                result = json.loads(raw)
            if result.get("id") != self.sequence or "error" in result or "result" not in result:
                raise ValueError()
        except (ValueError, TypeError, AttributeError, StopIteration, UnicodeError):
            raise SourceError("MCP 响应失败或格式不完整") from None
        return result["result"]

    def initialize(self):
        result = self.rpc("initialize", {"protocolVersion": self.version, "capabilities": {},
                                       "clientInfo": {"name": "demo-patent-source", "version": "1.0"}})
        self.version = result.get("protocolVersion", self.version)
        self.rpc("notifications/initialized", {}, notification=True)
        return self

    def tools(self):
        tools, cursor = [], None
        seen = set()
        while True:
            result = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result.get("tools"), list):
                raise SourceError("MCP 工具清单不完整")
            tools.extend(result["tools"])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if cursor in seen:
                raise SourceError("MCP 工具分页游标重复")
            seen.add(cursor)

    def call(self, name, arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise SourceError("MCP 工具执行失败")
        value = result.get("structuredContent")
        if value is None:
            texts = [b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"]
            try:
                value = json.loads("\n".join(texts))
            except (ValueError, TypeError):
                raise SourceError("MCP 工具须返回结构化 JSON") from None
        if not isinstance(value, (dict, list)):
            raise SourceError("MCP 工具数据类型异常")
        if isinstance(value, dict) and (value.get("success") is False or value.get("status") is False or value.get("error_code", 0) != 0):
            raise SourceError("MCP 数据接口未成功")
        return value


class PatsnapApi:
    BASE = "https://connect.zhihuiya.com"
    FIELDS = "PN,APNO,TTL,ANC,IN,APDT,PBDT,ISDT,PATENT_TYPE,COUNTRY"

    def __init__(self, secret, *, http=None):
        self.http = http or Http(secret)

    def create(self, query, expected):
        return self.http.json(self.BASE + "/patent-search/export/V2", {
            "query_text": query, "fields": self.FIELDS, "export_type": "json", "rows": expected,
            "collapse_type": "APNO", "collapse_by": "PBD", "collapse_order": "LATEST", "lang": "cn"})

    def task(self, task_id):
        return self.http.json(self.BASE + "/patent-search/task?" + urllib.parse.urlencode({"task_id": task_id}))

    def download(self, url):
        u = urllib.parse.urlsplit(str(url))
        host = u.hostname or ""
        if (not any(host.endswith("." + domain) for domain in ("zhihuiya.com", "patsnap.com"))
                or u.username or u.password or u.port not in (None, 80, 443) or u.scheme not in {"http", "https"}):
            raise SourceError("导出地址不属于智慧芽下载域名")
        # The API may return HTTP links; keep signed path/query and use TLS.
        url = urllib.parse.urlunsplit(("https", host, u.path, u.query, ""))
        raw, _ = self.http.request(url, auth=False)
        try:
            rows = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SourceError("导出文件不是有效 JSON") from None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise SourceError("导出记录格式异常")
        return rows, raw
