from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.get("/pending")
def list_pending_approvals() -> dict[str, list[Any]]:
    return {"items": []}
