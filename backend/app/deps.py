from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import AuditLog, User, XhsAccount
from app.security import decode_token

_bearer = HTTPBearer(auto_error=False)


async def current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    try:
        payload = decode_token(cred.credentials)
        user = await db.get(User, int(payload["sub"]))
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效") from None
    if user is None or user.is_deleted or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不可用")
    return user


def require_role(*roles: str):
    async def checker(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "无权限")
        return user

    return checker


staff_user = require_role("operator", "admin")
admin_user = require_role("admin")


async def owned_account(account_id: int, user: User, db: AsyncSession) -> XhsAccount:
    account = await db.get(XhsAccount, account_id)
    if account is None or account.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
    return account


def audit(db: AsyncSession, actor: User | None, action: str, detail: str = "") -> None:
    db.add(AuditLog(actor_id=actor.id if actor else None, action=action, detail=detail))
