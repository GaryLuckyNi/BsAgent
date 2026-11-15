from collections.abc import Iterable

from app.intent.schemas import BusinessIntentProfile


class BusinessIntentCatalog:
    def __init__(self, profiles: Iterable[BusinessIntentProfile]) -> None:
        self._profiles = tuple(profile.model_copy(deep=True) for profile in profiles)

    def all_profiles(self) -> list[BusinessIntentProfile]:
        return [profile.model_copy(deep=True) for profile in self._profiles]

    def allows(self, *, intent_type: str, domain: str, subdomain: str) -> bool:
        return any(
            profile.intent_type == intent_type
            and profile.domain == domain
            and profile.subdomain == subdomain
            for profile in self._profiles
        )

    def prompt_taxonomy(self) -> list[dict]:
        return [
            {
                "id": profile.id,
                "intent_type": profile.intent_type,
                "domain": profile.domain,
                "subdomain": profile.subdomain,
                "description": getattr(profile, "description", None),
                "trigger_keywords": list(profile.trigger_keywords),
                "trigger_examples": list(profile.trigger_examples),
            }
            for profile in self._profiles
        ]
