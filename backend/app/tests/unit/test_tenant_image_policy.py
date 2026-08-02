from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.storage.models import Base
from app.vision.policy import SqlAlchemyTenantImagePolicyProvider


def test_tenant_image_policy_defaults_to_deny_and_is_tenant_scoped():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        provider = SqlAlchemyTenantImagePolicyProvider(session)

        assert provider.get_policy(
            tenant_id="tenant-1"
        ).allow_external_vision is False

        provider.upsert_policy(
            tenant_id="tenant-1",
            allow_external_vision=True,
        )
        session.commit()

        assert provider.get_policy(
            tenant_id="tenant-1"
        ).allow_external_vision is True
        assert provider.get_policy(
            tenant_id="tenant-2"
        ).allow_external_vision is False
