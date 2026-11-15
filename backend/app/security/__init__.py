from app.security.principal import Principal

__all__ = ["MockRbacProvider", "Principal", "RbacProvider"]


def __getattr__(name):
    # 纯安全策略可在 Settings 校验期间导入，不提前构造依赖配置的 RBAC。
    if name in {"MockRbacProvider", "RbacProvider"}:
        from app.security import rbac

        return getattr(rbac, name)
    raise AttributeError(name)
