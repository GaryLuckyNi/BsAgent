from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    scopes: tuple[str, ...]
    token_id: str | None = None
    roles: tuple[str, ...] = ()

    @property
    def permissions(self) -> tuple[str, ...]:
        return self.scopes
