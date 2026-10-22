"""生成本地凭证；已有配置不覆盖，终端不输出密钥。"""

import os
import secrets
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def write_private(path, content):
    fd, temporary_path = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def main():
    env = ROOT / ".env"
    values = {
        "COMPOSE_PROJECT_NAME": "bsagent-langfuse-local",
        "NEXTAUTH_URL": "http://localhost:3000",
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "CLICKHOUSE_PASSWORD": secrets.token_hex(24),
        "REDIS_AUTH": secrets.token_hex(24),
        "MINIO_ROOT_PASSWORD": secrets.token_hex(24),
        "NEXTAUTH_SECRET": secrets.token_hex(32),
        "SALT": secrets.token_hex(32),
        "ENCRYPTION_KEY": secrets.token_hex(32),
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": "pk-lf-" + secrets.token_hex(16),
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": "sk-lf-" + secrets.token_hex(32),
        "LANGFUSE_INIT_USER_EMAIL": "admin@bsagent.local",
        "LANGFUSE_INIT_USER_PASSWORD": secrets.token_urlsafe(24),
    }
    original = env.read_text(encoding="utf-8") if env.exists() else ""
    present = {
        line.split("=", 1)[0]
        for line in original.splitlines()
        if line and not line.lstrip().startswith("#") and "=" in line
    }
    missing = {key: value for key, value in values.items() if key not in present}
    if missing:
        separator = "" if not original or original.endswith("\n") else "\n"
        content = (
            original + separator + "".join(f"{key}={value}\n" for key, value in missing.items())
        )
        write_private(env, content)
        print("已补齐本地 .env 缺失配置（权限 600）；原有配置保持不变。")
    elif env.exists():
        os.chmod(env, 0o600)
        print("本地 .env 配置完整，原有配置保持不变（权限 600）。")
    else:
        write_private(env, "")


if __name__ == "__main__":
    main()
