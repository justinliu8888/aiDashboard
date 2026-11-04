"""反馈收件箱的**维护者鉴权**。

边界（20261104 确认稿「存储与权限」）：

* 维护者凭据**在部署环境安全配置**，不写前端、不提交仓库、不与 Demo 共用。
* 维护者登录用**独立 cookie**（``fb_admin``），**不动** Demo 的 Basic Auth
  ``Authorization``，也不扩张 CORS。
* 没配置维护者凭据 → **安全关闭**管理登录（503），不是「放行」。
* 密码存**有盐的成熟哈希**（PBKDF2-HMAC-SHA256），不存明文。
* 远端 HTTPS 用 ``Secure``；本机 loopback 明确用非 Secure。

生成密码哈希（**在本机跑，密码不进聊天、不进 git**）::

    cd backend
    python -m app.services.feedback_auth --generate-hash
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from typing import Any

from starlette.requests import Request

logger = logging.getLogger(__name__)


#: 维护者会话 cookie 名（与 Demo 的 Basic Auth 完全独立）。
ADMIN_COOKIE_NAME = "fb_admin"

#: 匿名浏览器身份 cookie 名（只用来防重复，**不是**管理权限）。
VISITOR_COOKIE_NAME = "fb_vid"

#: PBKDF2 参数。
_PBKDF2_ALGORITHM = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 240_000
_PBKDF2_SALT_BYTES = 16

#: 登录失败限流（按用户名 + 客户端地址）。
_LOGIN_FAIL_LIMIT = 5
_LOGIN_FAIL_WINDOW_SECONDS = 600


# ---------- 密码哈希 ----------

def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """算一个有盐的密码哈希。

    格式：``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``。
    """
    salt = os.urandom(_PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return "$".join(
        [
            _PBKDF2_ALGORITHM,
            str(iterations),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    """核对密码。**任何解析失败都返回 False**（不抛异常）。"""
    try:
        algorithm, iterations_text, salt_b64, hash_b64 = (encoded or "").split("$")
        if algorithm != _PBKDF2_ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(digest, expected)


# ---------- 维护者会话 ----------

def sign_admin_token(username: str, secret: str, *, ttl_seconds: int) -> str:
    """签发维护者会话令牌。

    载荷：``{"u": 用户名, "exp": 过期时间戳, "n": 随机数}``。
    签名：``HMAC-SHA256(secret, payload_b64)``。
    """
    payload = {
        "u": username,
        "exp": int(time.time()) + int(ttl_seconds),
        "n": secrets.token_hex(8),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(
        secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{body}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"


def verify_admin_token(token: str, secret: str, *, expected_username: str) -> str | None:
    """校验维护者会话令牌。有效返回用户名，否则返回 ``None``。"""
    if not token or not secret:
        return None
    try:
        body, signature = token.split(".", 1)
        expected_sig = hmac.new(
            secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).digest()
        given_sig = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected_sig, given_sig):
            return None
        padding = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + padding).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        username = str(payload.get("u") or "")
        if not username or not hmac.compare_digest(
            username.encode("utf-8"), (expected_username or "").encode("utf-8")
        ):
            return None
        return username
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


# ---------- 登录失败限流 ----------

class _LoginThrottle:
    """按「用户名 + 客户端地址」记失败次数。进程内限流。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _key(self, username: str, client: str) -> str:
        return f"{username}\x00{client}"

    def is_blocked(
        self, username: str, client: str, *, limit: int = _LOGIN_FAIL_LIMIT
    ) -> bool:
        key = self._key(username, client)
        now = time.time()
        with self._lock:
            stamps = [
                value
                for value in self._failures.get(key, [])
                if now - value < _LOGIN_FAIL_WINDOW_SECONDS
            ]
            self._failures[key] = stamps
            return len(stamps) >= limit

    def record_failure(self, username: str, client: str) -> None:
        key = self._key(username, client)
        now = time.time()
        with self._lock:
            stamps = [
                value
                for value in self._failures.get(key, [])
                if now - value < _LOGIN_FAIL_WINDOW_SECONDS
            ]
            stamps.append(now)
            self._failures[key] = stamps

    def clear(self, username: str, client: str) -> None:
        with self._lock:
            self._failures.pop(self._key(username, client), None)


login_throttle = _LoginThrottle()


# ---------- 同源写请求保护 ----------

def request_origin_ok(request: Request) -> bool:
    """同源写请求保护。

    浏览器发起的写请求带 ``Origin``；它与本站不一致 → 拒绝（403）。
    **没有** ``Origin``（非浏览器客户端、测试）→ 放行。
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host")
    if not host:
        return False
    try:
        from urllib.parse import urlparse

        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.netloc and parsed.netloc != host:
        return False
    return True


# ---------- cookie ----------

def is_loopback_host(host: str | None) -> bool:
    """本机 loopback 主机？本机明确用**非 Secure** cookie。"""
    if not host:
        return False
    name = host.split(":")[0].strip("[]")
    return name in {"127.0.0.1", "localhost", "::1"}


def request_is_secure(request: Request) -> bool:
    """这次请求是不是走 HTTPS？

    * 直接 HTTPS → 是；
    * 反代终止 TLS 并回传 ``X-Forwarded-Proto: https`` → 是；
    * 本机 loopback 的 http → 否（明确用非 Secure，便于本地调试）。

    只有 HTTPS 才发 ``Secure`` cookie：明文连接上浏览器本来就不会带它，
    硬发等于把维护者会话变成「登录不进去」。
    """
    scheme = str(request.url.scheme or "").lower()
    if scheme == "https":
        return True
    forwarded = str(request.headers.get("x-forwarded-proto") or "").lower()
    if forwarded == "https":
        return True
    return False


def cookie_secure_flag(request: Request) -> bool:
    """维护者 / 访客 cookie 的 ``Secure`` 标志。"""
    if request_is_secure(request):
        return True
    return not is_loopback_host(request.headers.get("host"))


def new_visitor_id() -> str:
    """匿名浏览器身份：独立随机串。**不是**管理凭据。"""
    return f"fbv_{secrets.token_urlsafe(24)}"


def set_admin_cookie(
    response: Any, token: str, *, request: Request, ttl_seconds: int
) -> None:
    """写维护者会话 cookie（HttpOnly + 短期 + SameSite）。"""
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        max_age=int(ttl_seconds),
        httponly=True,
        samesite="lax",
        secure=cookie_secure_flag(request),
        path="/",
    )


def clear_admin_cookie(response: Any) -> None:
    """清除维护者会话。**不注销 Demo、不重置报表。**"""
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/")


def read_admin_username(request: Request, settings: Any) -> str | None:
    """读维护者身份。没配置 / 没登录 / 过期都返回 ``None``。"""
    username = (settings.feedback_admin_username or "").strip()
    secret = (settings.feedback_session_secret or "").strip()
    if not username or not secret:
        return None
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    if not token:
        return None
    return verify_admin_token(
        token, secret, expected_username=username
    )


def admin_configured(settings: Any) -> bool:
    """维护者登录是否**已配置**。没配置 → 管理入口安全关闭（503）。"""
    return bool(
        (settings.feedback_admin_username or "").strip()
        and (settings.feedback_admin_password_hash or "").strip()
        and (settings.feedback_session_secret or "").strip()
    )


def _main() -> int:  # pragma: no cover — 命令行工具
    import argparse

    parser = argparse.ArgumentParser(
        description="生成维护者密码哈希（本地执行，密码不进聊天、不进 git）"
    )
    parser.add_argument(
        "--generate-hash",
        action="store_true",
        help="交互式读取密码并打印哈希（推荐，避免密码进 shell 历史）",
    )
    parser.add_argument(
        "--stdin-password",
        action="store_true",
        help="从标准输入读密码（用于脚本化；仍不进命令行参数）",
    )
    args = parser.parse_args()

    if args.stdin_password:
        import sys

        password = sys.stdin.readline().rstrip("\n")
    else:
        import getpass

        password = getpass.getpass("维护者密码（不会回显）: ")
    if not password:
        print("密码为空，未生成。", flush=True)
        return 1
    print(hash_password(password), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
