from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import BusinessIntentProfileRecord
from app.schemas import BusinessIntentCreate, BusinessIntentRead, BusinessIntentUpdate

router = APIRouter(prefix="/api/business-intents", tags=["business-intents"])


@router.get("")
def list_business_intents(
    q: str | None = None,
    enabled: bool | None = None,
    db: Session = Depends(get_db),
) -> dict:
    query = select(BusinessIntentProfileRecord)
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(
                BusinessIntentProfileRecord.id.like(like),
                BusinessIntentProfileRecord.intent.like(like),
                BusinessIntentProfileRecord.domain.like(like),
                BusinessIntentProfileRecord.sub_intent.like(like),
                BusinessIntentProfileRecord.candidate_skill.like(like),
            )
        )
    if enabled is not None:
        query = query.where(BusinessIntentProfileRecord.enabled == enabled)
    records = list(db.scalars(query.order_by(BusinessIntentProfileRecord.id.asc())))
    return {
        "items": [BusinessIntentRead.model_validate(record).model_dump(mode="json") for record in records],
        "total": len(records),
    }


@router.post("", response_model=BusinessIntentRead, status_code=status.HTTP_201_CREATED)
def create_business_intent(
    payload: BusinessIntentCreate,
    db: Session = Depends(get_db),
) -> BusinessIntentProfileRecord:
    if db.get(BusinessIntentProfileRecord, payload.id) is not None:
        raise HTTPException(status_code=409, detail="业务意图 ID 已存在。")
    record = BusinessIntentProfileRecord(**payload.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/{intent_id}", response_model=BusinessIntentRead)
def get_business_intent(
    intent_id: str,
    db: Session = Depends(get_db),
) -> BusinessIntentProfileRecord:
    return _get_business_intent_or_404(db, intent_id)


@router.put("/{intent_id}", response_model=BusinessIntentRead)
def update_business_intent(
    intent_id: str,
    payload: BusinessIntentUpdate,
    db: Session = Depends(get_db),
) -> BusinessIntentProfileRecord:
    record = _get_business_intent_or_404(db, intent_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(record, field, value)
    db.commit()
    db.refresh(record)
    return record


@router.delete("/{intent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_business_intent(
    intent_id: str,
    db: Session = Depends(get_db),
) -> Response:
    record = _get_business_intent_or_404(db, intent_id)
    db.delete(record)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _get_business_intent_or_404(
    db: Session,
    intent_id: str,
) -> BusinessIntentProfileRecord:
    record = db.get(BusinessIntentProfileRecord, intent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="业务意图不存在。")
    return record
