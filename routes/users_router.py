"""User administration over HTTP — create, role, disable, delete, reset.

Exists because the API could not do this at all. POST /CRUD/register/ and
POST /CRUD/users/ accept a name, an email and a password and nothing else:
there is no role field on UserCreate and no endpoint anywhere that writes
role_id, so every account ever made over HTTP landed with no role, which
reads as no permissions. Creating an admin required SSH and a script.

Everything here requires the admin role. That is not decoration — an
endpoint that assigns the admin role on an API where nothing checks the
caller is a public admin-maker, so these routes and the enforcement in
main.py are the same change and must not be separated.

The guard that matters most is LAST ADMIN. Deleting, disabling or demoting
the only admin who can actually log in locks everyone out of every protected
route, and the only way back is a shell on the server. Four operations can
cause it and all four check first.
"""

import logging
import secrets
from datetime import datetime
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

import models_pgdb.models as models
from helpers.auth_deps import CurrentUser, get_db, require_admin
from helpers.pwd import MIN_PASSWORD_LENGTH, Hasher, password_problem

logger = logging.getLogger(__name__)

# Every route inherits the admin requirement from the router, so a new one
# added later cannot be accidentally public.
router = APIRouter(prefix="/users", tags=["Users"],
                   dependencies=[Depends(require_admin())])

db_dependency = Annotated[Session, Depends(get_db)]


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: Optional[str] = None
    has_password: bool
    disabled: bool
    created_date: Optional[datetime] = None


class CreateUserRequest(BaseModel):
    email: EmailStr
    role: str = Field(..., description="admin, trader or user")
    name: Optional[str] = None
    # Optional on purpose. Omit it and the account exists but cannot log in
    # until its owner sets a password, which is the same reasoning
    # scripts/add_user.py is built on: a password chosen by someone other than
    # its owner has to be transmitted, and every way of doing that is worse.
    password: Optional[str] = Field(None, min_length=MIN_PASSWORD_LENGTH)


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    disabled: Optional[bool] = None
    name: Optional[str] = None


class ChangeRoleResult(BaseModel):
    id: int
    email: str
    role: Optional[str] = None
    disabled: bool


class TempPasswordResult(BaseModel):
    id: int
    email: str
    temporary_password: str
    note: str


def _as_out(u: models.User) -> UserOut:
    return UserOut(
        id=u.id, name=u.name, email=u.email,
        role=u.role.role if u.role is not None else None,
        has_password=u.password_hash is not None,
        disabled=bool(u.disabled), created_date=u.created_date,
    )


def _role_or_400(db: Session, role_name: str) -> models.Role:
    role = db.query(models.Role).filter(models.Role.role == role_name.strip().lower()).first()
    if role is None:
        have = ", ".join(r.role for r in db.query(models.Role).all())
        raise HTTPException(status_code=400, detail=f"No such role. Available: {have}")
    return role


def _user_or_404(db: Session, user_id: int) -> models.User:
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _live_admin_ids(db: Session) -> set:
    """Admins who could actually log in right now.

    A disabled admin or one with no password cannot authenticate, so neither
    counts toward the last-admin guard. Counting them would let the guard pass
    while leaving nobody able to get in.
    """
    admin = db.query(models.Role).filter(models.Role.role == "admin").first()
    if admin is None:
        return set()
    rows = (
        db.query(models.User)
        .filter(models.User.role_id == admin.id,
                models.User.disabled.is_(False),
                models.User.password_hash.isnot(None))
        .all()
    )
    return {u.id for u in rows}


def _guard_last_admin(db: Session, target: models.User, action: str) -> None:
    live = _live_admin_ids(db)
    if target.id in live and len(live) == 1:
        raise HTTPException(
            status_code=409,
            detail=(f"Refusing to {action} the only admin who can log in. "
                    f"Give another account the admin role and a password first, "
                    f"or this locks everyone out of every protected route."),
        )


@router.get("", response_model=List[UserOut])
async def list_users(db: db_dependency):
    return [_as_out(u) for u in db.query(models.User).order_by(models.User.id).all()]


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: int, db: db_dependency):
    return _as_out(_user_or_404(db, user_id))


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUserRequest, db: db_dependency, caller: CurrentUser):
    # Lowercased because login compares the email exactly; without this,
    # Someone@example.com and someone@example.com are two accounts and only
    # one of them can ever log in.
    email = str(body.email).strip().lower()
    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    role = _role_or_400(db, body.role)
    hashed = None
    if body.password is not None:
        problem = password_problem(body.password)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        hashed = Hasher.get_password_hash(body.password)

    user = models.User(name=(body.name or email.split("@")[0]), email=email,
                       role_id=role.id, password_hash=hashed)
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("User %s created %s (id=%s) with role %s, password %s.",
                caller.email, email, user.id, role.role,
                "set" if hashed else "not set")
    return _as_out(user)


@router.patch("/{user_id}", response_model=ChangeRoleResult)
async def update_user(user_id: int, body: UpdateUserRequest,
                      db: db_dependency, caller: CurrentUser):
    user = _user_or_404(db, user_id)

    if body.role is not None:
        role = _role_or_400(db, body.role)
        if role.role != "admin":
            _guard_last_admin(db, user, "demote")
        user.role_id = role.id
    if body.disabled is not None:
        if body.disabled:
            _guard_last_admin(db, user, "disable")
        user.disabled = body.disabled
    if body.name is not None:
        user.name = body.name

    db.commit()
    db.refresh(user)
    logger.info("User %s updated %s (id=%s): role=%s disabled=%s.",
                caller.email, user.email, user.id,
                user.role.role if user.role else None, user.disabled)
    return ChangeRoleResult(id=user.id, email=user.email,
                            role=user.role.role if user.role else None,
                            disabled=bool(user.disabled))


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, db: db_dependency, caller: CurrentUser):
    user = _user_or_404(db, user_id)
    # Separate from the last-admin guard and checked first: deleting yourself
    # is almost always a slip, and the message should say so rather than
    # reporting a lockout that may not apply.
    if user.id == caller.id:
        raise HTTPException(status_code=409,
                            detail="Refusing to delete the account you are signed in as.")
    _guard_last_admin(db, user, "delete")
    db.delete(user)
    db.commit()
    logger.info("User %s deleted %s (id=%s).", caller.email, user.email, user.id)


@router.post("/{user_id}/reset-password", response_model=TempPasswordResult)
async def reset_password(user_id: int, db: db_dependency, caller: CurrentUser):
    """Issue a new random password for someone who has forgotten theirs.

    This is the recovery path, and it is admin-driven because there is no
    other one available: nothing in this codebase sends email, so a reset LINK
    cannot be delivered. An admin runs this and passes the result to its owner
    over a channel they already trust.

    The password is generated here rather than accepted from the caller, so an
    admin cannot set a password they have chosen and then use it themselves.
    It is returned exactly once and only the hash is stored — lose it and run
    this again.
    """
    user = _user_or_404(db, user_id)
    temp = secrets.token_urlsafe(15)
    user.password_hash = Hasher.get_password_hash(temp)
    db.commit()
    logger.info("User %s reset the password for %s (id=%s).",
                caller.email, user.email, user.id)
    return TempPasswordResult(
        id=user.id, email=user.email, temporary_password=temp,
        note=("Give this to its owner over a channel you trust, and have them "
              "change it with POST /auth/change-password. It is shown once."),
    )
