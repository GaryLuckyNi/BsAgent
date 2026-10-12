from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SkillProfileRecord
from app.schemas import SkillCreate, SkillRead, SkillUpdate

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("")
def list_skills(
    q: str | None = None,
    enabled: bool | None = None,
    db: Session = Depends(get_db),
) -> dict:
    query = select(SkillProfileRecord)
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(
                SkillProfileRecord.name.like(like),
                SkillProfileRecord.version.like(like),
                SkillProfileRecord.summary.like(like),
            )
        )
    if enabled is not None:
        query = query.where(SkillProfileRecord.enabled == enabled)
    records = list(db.scalars(query.order_by(SkillProfileRecord.name.asc())))
    return {
        "items": [SkillRead.model_validate(record).model_dump(mode="json") for record in records],
        "total": len(records),
    }


@router.post("", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
def create_skill(
    payload: SkillCreate,
    db: Session = Depends(get_db),
) -> SkillProfileRecord:
    if db.get(SkillProfileRecord, payload.name) is not None:
        raise HTTPException(status_code=409, detail="Skill 名称已存在。")
    record = SkillProfileRecord(**payload.model_dump())
    db.add(record)
    _commit_skill_change(db)
    db.refresh(record)
    return record


@router.get("/{skill_name}", response_model=SkillRead)
def get_skill(
    skill_name: str,
    db: Session = Depends(get_db),
) -> SkillProfileRecord:
    return _get_skill_or_404(db, skill_name)


@router.put("/{skill_name}", response_model=SkillRead)
def update_skill(
    skill_name: str,
    payload: SkillUpdate,
    db: Session = Depends(get_db),
) -> SkillProfileRecord:
    record = _get_skill_or_404(db, skill_name)
    changes = payload.model_dump(exclude_unset=True)
    target_name = changes.get("name")
    if (
        target_name is not None
        and target_name != skill_name
        and db.get(SkillProfileRecord, target_name) is not None
    ):
        raise HTTPException(status_code=409, detail="Skill 名称已存在。")
    for field, value in changes.items():
        setattr(record, field, value)
    _commit_skill_change(db)
    db.refresh(record)
    return record


@router.delete("/{skill_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(
    skill_name: str,
    db: Session = Depends(get_db),
) -> Response:
    record = _get_skill_or_404(db, skill_name)
    db.delete(record)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _get_skill_or_404(db: Session, skill_name: str) -> SkillProfileRecord:
    record = db.get(SkillProfileRecord, skill_name)
    if record is None:
        raise HTTPException(status_code=404, detail="Skill 不存在。")
    return record


def _commit_skill_change(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Skill 名称已存在。") from None
