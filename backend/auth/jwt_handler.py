"""
JWT认证处理器 — 用户登录、Token签发、权限校验

支持的认证方式:
  1. JWT Bearer Token — 前端用户登录后获取，有效期可配
  2. API Key — 程序化调用，绑定数据库/表权限（骨架，生产扩展）

用户角色:
  - admin: 全部数据库+全部表
  - analyst: 指定数据库+全部表
  - viewer: 指定数据库+指定表（只读）
"""
import time
import hashlib
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 生产环境应该存数据库，Demo用内存字典
_users_db: dict[str, dict] = {}
_api_keys_db: dict[str, dict] = {}
_sessions: dict[str, dict] = {}


@dataclass
class User:
    """认证后的用户信息"""
    user_id: str
    username: str
    role: str  # admin / analyst / viewer
    permissions: dict = field(default_factory=dict)
    # permissions 示例: {"databases": ["sales_db"], "tables": ["orders", "products"]}


# ═══════════════════════════════════════════════════════════
# 用户管理
# ═══════════════════════════════════════════════════════════

def create_user(username: str, password: str, role: str = "viewer",
                permissions: dict = None) -> dict:
    """创建用户（Demo用，生产应接LDAP/OAuth）"""
    user_id = hashlib.md5(username.encode()).hexdigest()[:8]

    if username in _users_db:
        return {"error": "用户名已存在"}

    from config import JWT_SECRET
    pw_hash = hashlib.sha256(f"{password}{JWT_SECRET}".encode()).hexdigest()

    _users_db[username] = {
        "user_id": user_id,
        "username": username,
        "password_hash": pw_hash,
        "role": role,
        "permissions": permissions or {"databases": ["*"], "tables": ["*"]},
        "created_at": time.time(),
    }

    logger.info(f"[Auth] User created: {username} (role={role})")
    return {"user_id": user_id, "username": username, "role": role}


def authenticate(username: str, password: str) -> Optional[User]:
    """验证用户名密码，返回User对象或None"""
    user = _users_db.get(username)
    if not user:
        return None

    from config import JWT_SECRET
    pw_hash = hashlib.sha256(f"{password}{JWT_SECRET}".encode()).hexdigest()

    if pw_hash != user["password_hash"]:
        return None

    return User(
        user_id=user["user_id"],
        username=user["username"],
        role=user["role"],
        permissions=user["permissions"],
    )


# ═══════════════════════════════════════════════════════════
# Token管理
# ═══════════════════════════════════════════════════════════

def create_session_token(user: User) -> str:
    """创建会话Token（简化版，生产应改用JWT库）"""
    from config import JWT_SECRET, JWT_EXPIRE_HOURS

    token_id = hashlib.md5(f"{user.user_id}{time.time()}".encode()).hexdigest()
    token = f"sk-{token_id}"

    _sessions[token] = {
        "user_id": user.user_id,
        "username": user.username,
        "role": user.role,
        "permissions": user.permissions,
        "created_at": time.time(),
        "expires_at": time.time() + JWT_EXPIRE_HOURS * 3600,
    }

    return token


def validate_token(token: str) -> Optional[User]:
    """验证Token，返回User或None"""
    session = _sessions.get(token)
    if not session:
        return None

    # 检查过期
    if time.time() > session.get("expires_at", 0):
        del _sessions[token]
        return None

    return User(
        user_id=session["user_id"],
        username=session["username"],
        role=session["role"],
        permissions=session["permissions"],
    )


def revoke_token(token: str):
    """注销Token"""
    _sessions.pop(token, None)


# ═══════════════════════════════════════════════════════════
# API Key管理（程序化调用）
# ═══════════════════════════════════════════════════════════

def create_api_key(user_id: str, permissions: dict = None) -> str:
    """创建API Key，绑定到用户和权限"""
    key = f"ak-{hashlib.md5(f'{user_id}{time.time()}'.encode()).hexdigest()[:16]}"
    _api_keys_db[key] = {
        "user_id": user_id,
        "permissions": permissions or {"databases": ["*"], "tables": ["*"]},
        "created_at": time.time(),
    }
    return key


def validate_api_key(api_key: str) -> Optional[dict]:
    """验证API Key，返回权限信息"""
    return _api_keys_db.get(api_key)


# ═══════════════════════════════════════════════════════════
# 权限检查
# ═══════════════════════════════════════════════════════════

def check_table_permission(user: User, table_name: str) -> bool:
    """检查用户是否有权访问某张表"""
    if user.role == "admin":
        return True

    allowed_tables = user.permissions.get("tables", [])
    if "*" in allowed_tables:
        return True

    return table_name in allowed_tables


def init_default_users():
    """创建默认用户（Demo用）"""
    if "admin" not in _users_db:
        create_user("admin", "admin123", "admin")
        create_user("analyst", "analyst123", "analyst",
                     permissions={"databases": ["*"], "tables": ["*"]})
        create_user("viewer", "viewer123", "viewer",
                     permissions={"databases": ["sales_db"], "tables": ["orders", "products", "order_items"]})
        logger.info("[Auth] Default users created: admin/analyst/viewer (password same as username + 123)")
