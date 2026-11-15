"""运行服务读取启用配置；不暴露写入路由或浏览器管理入口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.business_intents import list_business_intents
from app.api.skills import get_skill, list_skills
from app.database import get_db
from app.security import runtime_access

router = APIRouter(prefix="/api/runtime", dependencies=[Depends(runtime_access)])
Database = Annotated[Session, Depends(get_db)]


@router.get("/business-intents")
def runtime_intents(db: Database):
    return list_business_intents(enabled=True, db=db)


@router.get("/skills")
def runtime_skills(db: Database):
    return list_skills(enabled=True, db=db)


@router.get("/skills/{name}")
def runtime_skill(name: str, db: Database):
    row = get_skill(skill_name=name, db=db)
    if not row.enabled:
        raise HTTPException(status_code=404, detail={"code": "skill_not_found"})
    return row
