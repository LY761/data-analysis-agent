"""
认证中间件 + 限流器 + 熔断器

速率限制:
  - 每用户每分钟最多30次查询
  - 超过限制返回429
  - 超限用户进入冷却期

熔断器:
  - 连续LLM调用失败5次 → 熔断60秒
  - 熔断期间所有请求走缓存/规则兜底
  - 半开状态：放行1个请求探测，成功则关闭熔断
"""
import time
import logging
from collections import defaultdict
from contextvars import ContextVar
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# 当前请求的用户上下文（线程安全）
current_user_ctx: ContextVar[dict] = ContextVar("current_user", default=None)


# ═══════════════════════════════════════════════════════════
# 速率限制器
# ═══════════════════════════════════════════════════════════

class RateLimiter:
    """
    滑动窗口速率限制器。

    规则:
      - 每用户每分钟最多30次请求
      - 超过限制后冷却60秒
      - 被限流期间所有请求返回429
    """

    def __init__(self, max_requests: int = 30, window_seconds: int = 60, cooldown_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        # {user_id: [timestamp1, timestamp2, ...]}
        self._windows: dict[str, list[float]] = defaultdict(list)
        # {user_id: cooldown_end_timestamp}
        self._cooldowns: dict[str, float] = {}

    def check(self, user_id: str) -> tuple[bool, str]:
        """
        检查是否允许请求。
        返回: (allowed: bool, reason: str)
        """
        now = time.time()

        # 冷却期检查
        cooldown_end = self._cooldowns.get(user_id, 0)
        if now < cooldown_end:
            remaining = int(cooldown_end - now)
            return False, f"请求过于频繁，请{remaining}秒后重试"

        # 清理过期记录
        cutoff = now - self.window_seconds
        self._windows[user_id] = [t for t in self._windows[user_id] if t > cutoff]

        # 判断是否超限
        if len(self._windows[user_id]) >= self.max_requests:
            # 触发冷却
            self._cooldowns[user_id] = now + self.cooldown_seconds
            logger.warning(f"[RateLimit] User {user_id} exceeded {self.max_requests} req/min, "
                         f"cooldown {self.cooldown_seconds}s")
            return False, f"请求过于频繁（{self.max_requests}次/分钟），请{self.cooldown_seconds}秒后重试"

        # 记录本次请求
        self._windows[user_id].append(now)
        return True, "OK"

    def get_remaining(self, user_id: str) -> int:
        """查询剩余可用次数"""
        cutoff = time.time() - self.window_seconds
        self._windows[user_id] = [t for t in self._windows[user_id] if t > cutoff]
        return max(0, self.max_requests - len(self._windows[user_id]))


# ═══════════════════════════════════════════════════════════
# 熔断器
# ═══════════════════════════════════════════════════════════

class CircuitBreaker:
    """
    LLM调用熔断器。

    状态流转:
      CLOSED（正常）→ 连续失败5次 → OPEN（熔断，60秒）
      OPEN → 60秒后 → HALF_OPEN（放行1个探测请求）
      HALF_OPEN → 成功 → CLOSED  |  失败 → OPEN（继续熔断）
    """

    CLOSED = "closed"        # 正常
    OPEN = "open"            # 熔断，拒绝请求
    HALF_OPEN = "half_open"  # 半开，探测恢复

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failure_count: int = 0
        self._last_failure_time: float = 0
        self._state: str = self.CLOSED
        self._opened_at: float = 0

    @property
    def state(self) -> str:
        return self._state

    def before_call(self) -> bool:
        """调用前检查：是否允许本次调用"""
        now = time.time()

        if self._state == self.OPEN:
            # 检查是否到了恢复时间
            if now - self._opened_at >= self.recovery_timeout:
                self._state = self.HALF_OPEN
                logger.info("[CircuitBreaker] OPEN → HALF_OPEN（尝试恢复）")
                return True  # 放行一个探测请求
            else:
                logger.warning("[CircuitBreaker] OPEN，拒绝请求（熔断中）")
                return False

        return True  # CLOSED或HALF_OPEN状态允许调用

    def on_success(self):
        """调用成功后重置"""
        if self._state == self.HALF_OPEN:
            logger.info("[CircuitBreaker] HALF_OPEN → CLOSED（探测成功，熔断恢复）")
        self._failure_count = 0
        self._state = self.CLOSED

    def on_failure(self):
        """调用失败后记录"""
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._failure_count >= self.failure_threshold:
            self._state = self.OPEN
            self._opened_at = time.time()
            logger.error(f"[CircuitBreaker] CLOSED → OPEN（连续{self._failure_count}次失败，"
                        f"熔断{self.recovery_timeout}秒）")

    def get_status(self) -> dict:
        """返回熔断器状态（供健康检查接口使用）"""
        return {
            "state": self._state,
            "failure_count": self._failure_count,
            "threshold": self.failure_threshold,
        }


# ═══════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════

rate_limiter = RateLimiter()
circuit_breaker = CircuitBreaker()


# ═══════════════════════════════════════════════════════════
# FastAPI 认证中间件
# ═══════════════════════════════════════════════════════════

class AuthMiddleware(BaseHTTPMiddleware):
    """
    认证+限流中间件。

    流程:
      1. 从请求头提取 token 或 api_key
      2. 验证身份（JWT / API Key）
      3. 检查速率限制
      4. 注入用户上下文到 contextvar
      5. 放行请求
    """

    # 不需要认证的路径
    PUBLIC_PATHS = [
        "/api/login",
        "/api/register",
        "/api/health",
        "/docs",
        "/openapi.json",
        "/",
    ]

    async def dispatch(self, request: Request, call_next):
        from config import AUTH_ENABLED

        # Demo模式：跳过认证，所有请求直接放行
        if not AUTH_ENABLED:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in self.PUBLIC_PATHS):
            return await call_next(request)

        # 提取Token
        token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

        api_key = request.headers.get("X-API-Key", "")

        # 验证
        user = None
        if token:
            from auth.jwt_handler import validate_token
            user = validate_token(token)
        elif api_key:
            from auth.jwt_handler import validate_api_key
            key_info = validate_api_key(api_key)
            if key_info:
                from auth.jwt_handler import User
                user = User(
                    user_id=key_info["user_id"],
                    username=f"api:{api_key[:8]}",
                    role="api",
                    permissions=key_info.get("permissions", {}),
                )

        if not user:
            return JSONResponse(
                status_code=401,
                content={"error": "未登录或Token已过期", "code": "UNAUTHORIZED"},
            )

        # 速率限制
        allowed, reason = rate_limiter.check(user.user_id)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"error": reason, "code": "RATE_LIMITED",
                         "remaining": rate_limiter.get_remaining(user.user_id)},
            )

        # 注入用户上下文
        current_user_ctx.set({
            "user_id": user.user_id,
            "username": user.username,
            "role": user.role,
            "permissions": user.permissions,
        })

        response = await call_next(request)
        return response
