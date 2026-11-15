import argparse
import json
from typing import Any

from app.storage.database import SessionLocal
from app.vision.policy import SqlAlchemyTenantImagePolicyProvider


def set_tenant_image_policy(
    *,
    session: Any,
    provider: Any,
    tenant_id: str,
    allow_external_vision: bool,
) -> None:
    normalized_tenant_id = tenant_id.strip()
    if not normalized_tenant_id:
        raise ValueError("tenant_id 不能为空。")
    provider.upsert_policy(
        tenant_id=normalized_tenant_id,
        allow_external_vision=allow_external_vision,
    )
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="配置租户图片外部 Vision 策略。")
    parser.add_argument("--tenant-id", required=True)
    decision = parser.add_mutually_exclusive_group(required=True)
    decision.add_argument("--allow-external-vision", action="store_true")
    decision.add_argument("--deny-external-vision", action="store_true")
    arguments = parser.parse_args()

    allowed = bool(arguments.allow_external_vision)
    with SessionLocal() as session:
        set_tenant_image_policy(
            session=session,
            provider=SqlAlchemyTenantImagePolicyProvider(session),
            tenant_id=arguments.tenant_id,
            allow_external_vision=allowed,
        )
    print(
        json.dumps(
            {
                "tenant_id": arguments.tenant_id.strip(),
                "allow_external_vision": allowed,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
