from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SkillProfileRecord
from app.schemas import (
    SkillCreate,
    SkillEvidenceContract,
    SkillRead,
    SkillUpdate,
    validate_skill_evidence_contract_alignment,
)

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
    db.commit()
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
    try:
        evidence_contract = SkillEvidenceContract.model_validate(
            changes.get("evidence_contract", record.evidence_contract)
        )
        validate_skill_evidence_contract_alignment(
            allowed_tools=changes.get("allowed_tools", record.allowed_tools),
            evidence_contract=evidence_contract,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Skill 市场数据工具与证据契约配置不一致：{exc}",
        ) from None

    for field, value in changes.items():
        setattr(record, field, value)
    db.commit()
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
