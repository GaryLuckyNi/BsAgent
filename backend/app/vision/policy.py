from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.storage.models import TenantImagePolicyRecord
from app.vision.coordinator import TenantImagePolicy


class SqlAlchemyTenantImagePolicyProvider:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_policy(self, *, tenant_id: str) -> TenantImagePolicy:
        record = self._session.get(TenantImagePolicyRecord, tenant_id)
        return TenantImagePolicy(
            allow_external_vision=(
                record.allow_external_vision if record is not None else False
            )
        )

    def upsert_policy(
        self,
        *,
        tenant_id: str,
        allow_external_vision: bool,
    ) -> TenantImagePolicyRecord:
        record = self._session.get(TenantImagePolicyRecord, tenant_id)
        if record is None:
            record = TenantImagePolicyRecord(
                tenant_id=tenant_id,
                allow_external_vision=allow_external_vision,
            )
            self._session.add(record)
        else:
            record.allow_external_vision = allow_external_vision
            record.updated_at = datetime.now(UTC)
        self._session.flush()
        return record
