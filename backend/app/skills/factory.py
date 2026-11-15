import httpx

from app.core.config import settings
from app.security.admin_service_auth import build_admin_service_auth
from app.skills.admin_provider import AdminSkillDefinitionProvider
from app.skills.registry import SkillRegistry
from app.skills.registry_client import CachedSkillDefinitionProvider


def build_default_skill_registry() -> SkillRegistry:
    if not settings.skill_registry_base_url:
        return SkillRegistry()
    client = httpx.Client(
        auth=build_admin_service_auth(settings),
        follow_redirects=False,
        base_url=settings.skill_registry_base_url,
        timeout=5.0,
    )
    provider = CachedSkillDefinitionProvider(
        AdminSkillDefinitionProvider(client=client),
        ttl_seconds=settings.skill_definition_cache_ttl_seconds,
    )
    return SkillRegistry(definition_provider=provider)
