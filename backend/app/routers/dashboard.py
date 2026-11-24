from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services import dayseal

router = APIRouter(tags=["总览"])


@router.get("/总览")
def overview():
    with db() as conn:
        return dayseal.overview(conn)

