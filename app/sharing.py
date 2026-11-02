from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path


SESSION_SECONDS = 7 * 24 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
PASSWORD_ITERATIONS = 310_000


class ShareSecurity:
    """In-memory reader sessions backed by a locally stored password hash."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "share_settings.json"
        self.settings = self._load()
        self.sessions: dict[str, dict] = {}
        self.login_failures: dict[str, deque[float]] = defaultdict(deque)
        self.login_locks: dict[str, float] = {}
        self.ai_by_client: dict[str, deque[float]] = defaultdict(deque)
        self.ai_global: deque[float] = deque()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text("utf-8")) if self.path.is_file() else {}
        except (OSError, ValueError):
            raw = {}
        return {
            "enabled": bool(raw.get("enabled", False)),
            "salt": str(raw.get("salt") or ""),
            "password_hash": str(raw.get("password_hash") or ""),
            "iterations": int(raw.get("iterations") or PASSWORD_ITERATIONS),
            "ai_per_hour": max(1, min(int(raw.get("ai_per_hour") or 30), 1000)),
            "ai_per_day": max(1, min(int(raw.get("ai_per_day") or 300), 10000)),
        }

    @property
    def configured(self) -> bool:
        return bool(self.settings["salt"] and self.settings["password_hash"])

    def public_settings(self) -> dict:
        return {
            "enabled": self.settings["enabled"],
            "configured": self.configured,
            "ai_per_hour": self.settings["ai_per_hour"],
            "ai_per_day": self.settings["ai_per_day"],
        }

    def save_settings(self, payload: dict) -> dict:
        if "enabled" in payload:
            self.settings["enabled"] = bool(payload.get("enabled"))
        for key, maximum in (("ai_per_hour", 1000), ("ai_per_day", 10000)):
            if key in payload:
                value = int(payload[key])
                if value < 1 or value > maximum:
                    raise ValueError(f"{key} 超出允许范围")
                self.settings[key] = value
        password = str(payload.get("password") or "")
        if password:
            if len(password) < 8 or len(password) > 128:
                raise ValueError("共享密码须为 8 至 128 个字符")
            salt = secrets.token_bytes(16)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                         PASSWORD_ITERATIONS)
            self.settings.update(
                salt=salt.hex(), password_hash=digest.hex(), iterations=PASSWORD_ITERATIONS
            )
            self.sessions.clear()
        if self.settings["enabled"] and not self.configured:
            raise ValueError("启用内网共享前请先设置共享密码")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), "utf-8")
        temporary.replace(self.path)
        return self.public_settings()

    def _expected_hash(self, password: str) -> str:
        salt = bytes.fromhex(self.settings["salt"])
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, self.settings["iterations"]
        ).hex()

    def login(self, password: str, ip: str, now: float | None = None) -> tuple[str | None, str]:
        now = time.time() if now is None else now
        locked_until = self.login_locks.get(ip, 0)
        if locked_until > now:
            return None, "尝试次数过多，请 15 分钟后再试"
        if not self.settings["enabled"] or not self.configured:
            return None, "管理员尚未启用内网共享"
        failures = self.login_failures[ip]
        while failures and failures[0] <= now - LOGIN_WINDOW_SECONDS:
            failures.popleft()
        valid = hmac.compare_digest(self._expected_hash(password), self.settings["password_hash"])
        if not valid:
            failures.append(now)
            if len(failures) >= LOGIN_MAX_FAILURES:
                self.login_locks[ip] = now + LOGIN_WINDOW_SECONDS
                failures.clear()
                return None, "尝试次数过多，请 15 分钟后再试"
            return None, "共享密码不正确"
        failures.clear()
        self.login_locks.pop(ip, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {"ip": ip, "expires": now + SESSION_SECONDS}
        return token, "登录成功"

    def logout(self, token: str) -> None:
        self.sessions.pop(token, None)

    def is_reader(self, token: str, ip: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        session = self.sessions.get(token)
        if not session:
            return False
        if session["expires"] <= now:
            self.sessions.pop(token, None)
            return False
        if session["ip"] != ip:
            return False
        return True

    @staticmethod
    def _prune(queue: deque[float], cutoff: float) -> None:
        while queue and queue[0] <= cutoff:
            queue.popleft()

    def allow_ai(self, token: str, ip: str, now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else now
        client_key = f"{token}|{ip}"
        client = self.ai_by_client[client_key]
        self._prune(client, now - 60 * 60)
        self._prune(self.ai_global, now - 24 * 60 * 60)
        if len(client) >= self.settings["ai_per_hour"]:
            return False, "本小时 AI 查询次数已用完，请稍后再试；手动筛选仍可使用"
        if len(self.ai_global) >= self.settings["ai_per_day"]:
            return False, "今日 AI 查询总额度已用完；手动筛选仍可使用"
        client.append(now)
        self.ai_global.append(now)
        return True, ""
