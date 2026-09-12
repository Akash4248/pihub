import asyncio
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.security import HTTPAuthorizationCredentials
import jwt

from app.core.pagination import page_query, page_size_query, paginate
from app.classroom.models import CreateAssignmentRequest, CreateSessionRequest, SubmitAssignmentRequest
from app.classroom.services.classroom_service import ClassroomNotFoundError, ClassroomService, ClassroomValidationError
from shared.security import get_jwt_secret_key, security_bearer, verify_internal_api_key, JWT_ALGORITHM


router = APIRouter(tags=["Classroom Distribution"])
classroom_service = ClassroomService()


async def require_teacher(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    x_user_role: Optional[str] = Header(None, alias="x-user-role"),
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Service-Key"),
) -> dict[str, Any]:
    """Enforce teacher/admin auth via JWT or x-user-role headers; fallback to internal key in dev mode."""
    if credentials:
        try:
            payload = jwt.decode(credentials.credentials, get_jwt_secret_key(), algorithms=[JWT_ALGORITHM])
            role = payload.get("role") or payload.get("user_role") or "student"
            if role in {"teacher", "admin"}:
                return payload
            raise HTTPException(status_code=403, detail="Forbidden: Classroom write operations require teacher or admin role.")
        except jwt.PyJWTError:
            pass

    if x_user_role in {"teacher", "admin"}:
        return {"user_id": x_user_id or "teacher", "role": x_user_role}

    if os.getenv("ALLOW_ANONYMOUS_DEV", "false").lower() == "true":
        try:
            await verify_internal_api_key(x_internal_key=x_internal_key, credentials=credentials)
            return {"user_id": x_user_id or "dev_teacher", "role": "teacher"}
        except HTTPException:
            pass

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required: Missing or invalid teacher Bearer token or x-user-role header.",
    )


def _dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        data = model.model_dump(mode="json")
    elif hasattr(model, "dict"):
        data = jsonable_encoder(model.dict())
    else:
        data = jsonable_encoder(dict(model))

    if isinstance(data, dict):
        if "session_id" in data and "assignment_session" not in data:
            data["assignment_session"] = data["session_id"]
    return data


def _parse_model(model_type: Any, payload: dict[str, Any]) -> Any:
    if hasattr(model_type, "model_validate"):
        return model_type.model_validate(payload)
    return model_type.parse_obj(payload)


@router.post("/classroom/sessions", dependencies=[Depends(require_teacher)])
async def create_classroom_session(payload: dict[str, Any], response: Response) -> dict[str, Any]:
    try:
        request = _parse_model(CreateSessionRequest, payload)
        result = await asyncio.to_thread(classroom_service.create_session, request)
        response.headers["Warning"] = '299 - "ClassroomSession payload key is deprecated, use assignment_session"'
        return _dump(result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/classroom/sessions")
async def list_classroom_sessions(
    response: Response,
    page: int = page_query(),
    page_size: int = page_size_query(),
) -> dict[str, Any]:
    sessions = await asyncio.to_thread(classroom_service.list_sessions)
    paged = paginate(sessions, page, page_size)
    response.headers["Warning"] = '299 - "ClassroomSession payload key is deprecated, use assignment_session"'
    return {
        "sessions": [_dump(session) for session in paged["items"]],
        "page": page,
        "page_size": page_size,
        "total": paged["total"],
    }


@router.post("/classroom/sessions/{session_id}/assignments", dependencies=[Depends(require_teacher)])
async def create_classroom_assignment(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _parse_model(CreateAssignmentRequest, payload)
        result = await asyncio.to_thread(classroom_service.create_assignment, session_id, request)
        return _dump(result)
    except ClassroomNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ClassroomValidationError as exc:
        raise HTTPException(status_code=400, detail={"errors": exc.errors}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/classroom/sessions/{session_id}/assignments")
async def list_classroom_assignments(
    session_id: str,
    page: int = page_query(),
    page_size: int = page_size_query(),
) -> dict[str, Any]:
    try:
        assignments = await asyncio.to_thread(classroom_service.list_assignments, session_id)
    except ClassroomNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    paged = paginate(assignments, page, page_size)
    return {"assignments": [_dump(assignment) for assignment in paged["items"]], "page": page, "page_size": page_size, "total": paged["total"]}


@router.post("/classroom/assignments/{assignment_id}/submit")
async def submit_classroom_assignment(assignment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _parse_model(SubmitAssignmentRequest, payload)
        result = await asyncio.to_thread(classroom_service.submit_assignment, assignment_id, request)
        return _dump(result)
    except ClassroomNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ClassroomValidationError as exc:
        raise HTTPException(status_code=400, detail={"errors": exc.errors}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/classroom/assignments/{assignment_id}/submissions")
async def list_classroom_submissions(
    assignment_id: str,
    page: int = page_query(),
    page_size: int = page_size_query(),
) -> dict[str, Any]:
    try:
        submissions = await asyncio.to_thread(classroom_service.list_submissions, assignment_id)
    except ClassroomNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    paged = paginate(submissions, page, page_size)
    return {"submissions": [_dump(submission) for submission in paged["items"]], "page": page, "page_size": page_size, "total": paged["total"]}


@router.get("/classroom/analytics")
async def classroom_analytics() -> dict[str, Any]:
    result = await asyncio.to_thread(classroom_service.analytics)
    return _dump(result)

