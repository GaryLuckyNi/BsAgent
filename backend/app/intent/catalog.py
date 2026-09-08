import json
from collections.abc import Iterable
from pathlib import Path

from app.intent.schemas import BusinessIntentProfile


class BusinessIntentCatalog:
    def __init__(self, profiles: Iterable[BusinessIntentProfile]) -> None:
        self._profiles = tuple(profiles)

    @classmethod
    def load_default(cls) -> "BusinessIntentCatalog":
        return cls.load_from_path(Path(__file__).with_name("business_intents.json"))

    @classmethod
    def load_from_path(cls, path: Path) -> "BusinessIntentCatalog":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            profiles=[
                BusinessIntentProfile.model_validate(item)
                for item in payload.get("profiles", [])
            ]
        )

    def all_profiles(self) -> tuple[BusinessIntentProfile, ...]:
        return self._profiles

    def match_by_keywords(self, message: str) -> BusinessIntentProfile | None:
        normalized = message.lower()
        scored_profiles = [
            (self._score_profile(profile, normalized), profile)
            for profile in self._profiles
        ]
        scored_profiles = [
            (score, profile) for score, profile in scored_profiles if score > 0
        ]
        if not scored_profiles:
            return None
        return max(scored_profiles, key=lambda item: item[0])[1]

    def _score_profile(self, profile: BusinessIntentProfile, message: str) -> int:
        score = sum(1 for keyword in profile.trigger_keywords if keyword.lower() in message)
        score += sum(2 for example in profile.trigger_examples if example.lower() in message)
        return score
