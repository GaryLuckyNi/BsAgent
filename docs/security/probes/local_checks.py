"""只运行隔离测试和本地探针；不修改应用，不连接真实服务。"""

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BLOCKED_NETWORK = []


def network_guard(event, args):
    if event == "socket.getaddrinfo":
        BLOCKED_NETWORK.append(event)
        raise RuntimeError("红队本地探针禁止 DNS 查询")
    if (event in {"socket.connect", "socket.sendto"}
            and args[0].family in {socket.AF_INET, socket.AF_INET6}):
        BLOCKED_NETWORK.append(event)
        raise RuntimeError("红队本地探针禁止网络连接")


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def run_baseline(component, output):
    import pytest

    if component == "backend":
        names = [
            "jwt_provider", "security_dependencies", "tool_agent", "rag_search_tool",
            "image_security", "tool_redaction", "langfuse_content",
            "tool_execution_certainty", "memory_candidates", "memory_preferences",
            "context_compaction_summarizer",
        ]
        tests = [ROOT / f"backend/app/tests/unit/test_{name}.py" for name in names]
        config = ROOT / "pyproject.toml"
    else:
        tests = [
            ROOT / "admin/backend/tests/test_market_data_providers_api.py",
            ROOT / "admin/backend/tests/test_skills_api.py",
        ]
        config = ROOT / "admin/backend/pyproject.toml"
    code = pytest.main([
        "-c", str(config), "-q", "--disable-warnings", "--tb=short",
        "-o", "cache_dir=" + str(Path.cwd() / "pytest-cache"),
        "--junitxml=" + str(output), *map(str, tests),
    ])
    emit({"baseline_exit_code": int(code), "blocked_network_events": BLOCKED_NETWORK})
    return int(code)


async def rag_probe():
    import httpx
    from app.schemas.tools import ToolCallRequest
    from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
    from app.tools.rag_search import build_default_rag_search_registration
    from app.tools.registry import ToolRegistry

    captured = []
    marker = "RT_PRIVATE_B_LOCAL_CHECK_01"

    def handler(request):
        captured.append({
            "path": request.url.path,
            "payload": json.loads(request.content),
            "headers": dict(request.headers),
        })
        return httpx.Response(200, json={
            "query": {"standalone": "内部测试资料"},
            "contexts": [{
                "id": "doc-b#1", "text": marker,
                "source": {"sourcePath": "private-b.md", "title": "测试私有资料"},
                "score": {"final": 0.9}, "metadata": {"tenant_id": "rt-tenant-b"},
            }],
            "metadata": {"contextCount": 1, "durationMs": 1},
        })

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://rag.invalid",
    ) as client:
        tool, binding = build_default_rag_search_registration(base_url="http://rag.invalid")
        tool._client = client
        registry = ToolRegistry()
        registry.register(tool, execution_binding=binding)
        gateway = ToolExecutionGateway(registry)
        results = []
        for tenant, user, argument_tenant in [
            ("rt-tenant-b", "rt-user-b1", "rt-tenant-b"),
            ("rt-tenant-a", "rt-user-a1", "rt-tenant-a"),
            ("rt-tenant-a", "rt-user-a1", "rt-tenant-b"),
        ]:
            result = await gateway.execute(
                ToolCallRequest(
                    tool_name="rag_search", purpose="查询内部测试资料",
                    arguments={"query": "内部测试资料", "tenant_id": argument_tenant,
                               "user_id": user},
                ),
                trace_id="rt-local-rag",
                context=ToolExecutionContext(
                    tenant_id=tenant, user_id=user, session_id="rt-session",
                    allowed_tools=("rag_search",), permission_scopes=("rag:retrieve",),
                ),
            )
            results.append({
                "context_tenant": tenant, "argument_tenant": argument_tenant,
                "status": result.status,
                "private_b_marker_returned": marker in json.dumps(result.result),
                "error_code": result.error.code if result.error else None,
            })
    emit({
        "probe": "RT-15", "layer": "whitebox_fixture_and_mock_http",
        "results": results, "captured_requests": captured,
        "wire_payloads_equal": len(captured) == 3 and all(
            item["payload"] == captured[0]["payload"] for item in captured
        ),
        "limitation": "上游是故意返回 B 文档的 mock；不能据此断言真实 RAG ACL 已被突破。",
        "blocked_network_events": BLOCKED_NETWORK,
    })


def admin_probe():
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    original = "https://api.tushare.pro/mcp/token=RT_SECRET_LOCAL_CHECK_01"
    replacement = "https://api.tushare.pro/mcp/token=RT_SECRET_LOCAL_CHECK_02"
    with TestClient(app) as client:
        created = client.post("/api/market-data-providers", json={
            "id": "rt-provider", "provider": "tushare", "display_name": "合成红队数据源",
            "enabled": True, "active": True, "settings": {"mcp_server_url": original},
        })
        public = client.get("/api/market-data-providers/rt-provider")
        internal = client.get("/api/internal/market-data-providers/active/mcp-connection")
        fake_user = client.get(
            "/api/internal/market-data-providers/active/mcp-connection",
            headers={"Authorization": "Bearer rt-not-a-valid-token",
                     "X-Tenant-ID": "rt-tenant-a", "X-User-ID": "rt-user-a1"},
        )
        updated = client.put("/api/market-data-providers/rt-provider", json={
            "settings": {"mcp_server_url": replacement},
        })
        reread = client.get("/api/internal/market-data-providers/active/mcp-connection")
    emit({
        "probe": "RT-25", "layer": "in_process_http_and_temporary_sqlite",
        "requests_authenticated": False,
        "create_status": created.status_code, "public_status": public.status_code,
        "public_masks_secret": original not in public.text,
        "internal_status": internal.status_code,
        "internal_returns_full_synthetic_secret": original in internal.text,
        "invalid_token_status": fake_user.status_code,
        "invalid_token_returns_full_synthetic_secret": original in fake_user.text,
        "anonymous_update_status": updated.status_code,
        "anonymous_update_persisted": replacement in reread.text,
        "limitation": "直接调用应用，未经过部署网关；不证明生产外网或普通主体实际可达。",
        "blocked_network_events": BLOCKED_NETWORK,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["backend-baseline", "admin-baseline", "rag", "admin"])
    parser.add_argument("--junit", type=Path)
    args = parser.parse_args()
    junit = args.junit.resolve() if args.junit else None
    if args.mode.endswith("baseline") and junit is None:
        parser.error("基线测试必须指定 --junit")
    component = "admin" if args.mode.startswith("admin") else "backend"
    for name in list(os.environ):
        if name.startswith("BS_AGENT_"):
            del os.environ[name]
    os.environ.update({
        "BS_AGENT_APP_ENV": "test", "BS_AGENT_AUTH_PROVIDER": "mock",
        "BS_AGENT_OTEL_ENABLED": "false", "BS_AGENT_LANGFUSE_ENABLED": "false",
        "BS_AGENT_DATABASE_URL": "sqlite://", "PYTHONDONTWRITEBYTECODE": "1",
    })
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / ("admin/backend" if component == "admin" else "backend")))
    sys.addaudithook(network_guard)
    with tempfile.TemporaryDirectory(prefix="bsagent-redteam-") as temporary:
        os.chdir(temporary)
        os.environ["ADMIN_DATABASE_URL"] = "sqlite:///" + str(Path(temporary) / "admin.db")
        if args.mode.endswith("baseline"):
            return run_baseline(component, junit)
        if args.mode == "rag":
            asyncio.run(rag_probe())
        else:
            admin_probe()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
