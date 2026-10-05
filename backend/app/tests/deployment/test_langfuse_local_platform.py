import importlib.util
import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[4]
COMPOSE_PATH = PROJECT_ROOT / "langfuse" / "compose.yaml"
INIT_PATH = PROJECT_ROOT / "langfuse" / "init.py"


def _load_init_module():
    spec = importlib.util.spec_from_file_location("langfuse_local_init", INIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_langfuse_compose_pins_images_and_limits_host_ports():
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["langfuse-web"]["image"] == "docker.langfuse.com/langfuse/langfuse:4.16.0"
    assert (
        services["langfuse-worker"]["image"]
        == "docker.langfuse.com/langfuse/langfuse-worker:4.16.0"
    )
    for name in ("langfuse-web", "langfuse-worker"):
        service = services[name]
        assert service.get("environment", {}).get("TELEMETRY_ENABLED") == "false"
        assert service["environment"]["NEXTAUTH_URL"] == "http://localhost:3000"
    assert (
        services["langfuse-web"]["environment"]["LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT"]
        == "http://localhost:9090"
    )

    expected_ports = {
        "langfuse-web": ["127.0.0.1:3000:3000"],
        "minio": ["127.0.0.1:9090:9000"],
    }
    for name, service in services.items():
        assert service.get("ports", []) == expected_ports.get(name, [])

    for name in ("langfuse-web", "langfuse-worker", "postgres", "clickhouse", "redis", "minio"):
        assert services[name].get("healthcheck")
    assert (
        services["langfuse-web"]["environment"]["LANGFUSE_INIT_PROJECT_ID"]
        == "bsagent-content-local"
    )
    for volume in (
        "langfuse_postgres_data",
        "langfuse_clickhouse_data",
        "langfuse_clickhouse_logs",
        "langfuse_minio_data",
        "langfuse_redis_data",
    ):
        assert volume in compose["volumes"]


def test_langfuse_root_gitignore_excludes_pinned_data_directory():
    assert (
        "langfuse-4.16.0/" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    )


def test_readme_describes_only_the_local_platform_and_its_endpoints():
    readme = (PROJECT_ROOT / "langfuse" / "README.md").read_text(encoding="utf-8")
    assert "http://localhost:3000" in readme
    assert "127.0.0.1:9090" in readme
    assert "Cloud EU" not in readme
    assert "下一步工作" not in readme
    assert "本目录未实现" not in readme


def test_init_fills_missing_values_atomically_and_keeps_secrets_private(
    tmp_path, monkeypatch, capsys
):
    module = _load_init_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    env_path = tmp_path / ".env"
    existing_secret = "keep-this-existing-secret"
    env_path.write_text(
        f"POSTGRES_PASSWORD={existing_secret}\nCUSTOM_VALUE=preserve-me\n", encoding="utf-8"
    )
    env_path.chmod(0o644)

    replacements = []
    original_replace = module.os.replace

    def observe_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", observe_replace)
    module.main()

    output = capsys.readouterr().out
    contents = env_path.read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in contents.splitlines())
    assert values["POSTGRES_PASSWORD"] == existing_secret
    assert values["CUSTOM_VALUE"] == "preserve-me"
    assert {
        "COMPOSE_PROJECT_NAME",
        "NEXTAUTH_URL",
        "POSTGRES_PASSWORD",
        "CLICKHOUSE_PASSWORD",
        "REDIS_AUTH",
        "MINIO_ROOT_PASSWORD",
        "NEXTAUTH_SECRET",
        "SALT",
        "ENCRYPTION_KEY",
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
        "LANGFUSE_INIT_PROJECT_SECRET_KEY",
        "LANGFUSE_INIT_USER_EMAIL",
        "LANGFUSE_INIT_USER_PASSWORD",
    } <= values.keys()
    assert values["NEXTAUTH_URL"] == "http://localhost:3000"
    assert replacements and replacements[0][1] == env_path
    assert os.stat(env_path).st_mode & 0o777 == 0o600
    assert existing_secret not in output
    assert all(
        value not in output
        for key, value in values.items()
        if "PASSWORD" in key or "SECRET" in key or key == "SALT"
    )
    assert not list(tmp_path.glob(".env.*"))


def test_init_is_idempotent_when_environment_is_complete(tmp_path, monkeypatch, capsys):
    module = _load_init_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    env_path = tmp_path / ".env"
    module.main()
    first = env_path.read_bytes()
    module.main()
    assert env_path.read_bytes() == first
    assert env_path.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert "已补齐" in output and "配置完整" in output
    assert "sk-lf-" not in output
