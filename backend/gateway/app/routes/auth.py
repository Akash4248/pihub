from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.auth_service import authenticate_user, register_user


router = APIRouter(tags=["Auth"])


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="student")
    grade: int = Field(default=9, ge=0, le=12)


class LoginRequest(BaseModel):
    email: str
    password: str


_SELF_REGISTERABLE_ROLES = {"student", "teacher", "parent"}


@router.post("/auth/register")
async def auth_register(payload: RegisterRequest) -> dict[str, Any]:
    """Create a user account and return a JWT. Admin roles cannot be self-assigned."""
    if payload.role not in _SELF_REGISTERABLE_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role for self-registration.")
    return await asyncio.to_thread(
        register_user, payload.email, payload.password, payload.full_name, payload.role, payload.grade
    )


@router.post("/auth/login")
async def auth_login(payload: LoginRequest) -> dict[str, Any]:
    """Exchange credentials for a JWT."""
    return await asyncio.to_thread(authenticate_user, payload.email, payload.password)
