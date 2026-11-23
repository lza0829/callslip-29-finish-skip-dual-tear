from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel, Field
from app.db import db
from app.services import windows as win_svc

router = APIRouter(tags=["业务种类"])


class ActiveBody(BaseModel):
    启用: bool = Field(..., alias="启用")

    class Config:
        populate_by_name = True


@router.get("/业务种类")
def list_kinds():
    with db() as conn:
        return win_svc.list_kinds(conn)


@router.post("/业务种类/{kind_id}/启停")
def set_active(kind_id: int, body: ActiveBody):
    with db() as conn:
        win_svc.set_kind_active(conn, kind_id, body.启用)
        kinds = win_svc.list_kinds(conn)
        return next(k for k in kinds if k["id"] == kind_id)
