import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.tool_documentation.contracts import Documentation
from app.tool_documentation.metadata import description_gaps, manifest_key, public_manifest

logger = logging.getLogger(__name__)
_publisher = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-doc-publish")


class DocumentationService:
    def __init__(self, *, repository, publish, prewarm_limit=8):
        self.repository, self.publish, self.prewarm_limit = repository, publish, prewarm_limit

    def prepare(self, tools, config):
        if not config.public_documentation:
            return
        if not config.documentation_domains:
            for tool in tools:
                tool.documentation = {"status": "missing"}
            return
        scheduled = 0
        source = {key: getattr(config, key) for key in ("name", "config_revision", "deployment_revision")}
        for tool in tools:
            tool.documentation = {"status": "missing"}
            try:
                spec = tool.spec
                manifest = public_manifest(spec)
                key = manifest_key(manifest, source, config.documentation_domains)
                cached = self.repository.ready(key)
                if cached:
                    tool.documentation = Documentation.model_validate(cached).model_dump(mode="json")
                    continue
                if not any(description_gaps(spec).values()) or scheduled >= self.prewarm_limit:
                    continue
                if self.repository.reserve(key, manifest, config.documentation_domains):
                    scheduled += 1
                    try:
                        self.publish(key)
                    except Exception:  # noqa: BLE001 - 派发失败不得阻止目录或研究请求。
                        self.repository.fail(key)
                        logger.warning("tool_documentation_publish_failed key=%s", key[:12])
            except Exception as exc:  # noqa: BLE001 - 文档补充属于非阻断能力。
                logger.warning("tool_documentation_unavailable type=%s", type(exc).__name__)

    def process(self, key, *, enricher=None):
        job = self.repository.claim(key)
        if job is None:
            return
        try:
            result = asyncio.run(enricher.enrich(manifest=job["manifest"], domains=job["domains"])) if enricher else asyncio.run(
                _enrich(job))
            if result.get("status") == "missing":
                self.repository.fail(key, job["lease"])
            else:
                self.repository.finish(key, job["lease"], result)
        except Exception as exc:  # noqa: BLE001 - 有界失败冷却，避免确定性错误重复请求模型。
            self.repository.fail(key, job["lease"])
            logger.warning("tool_documentation_enrichment_failed type=%s key=%s", type(exc).__name__, key[:12])


async def _enrich(job):
    from app.core.config import settings
    from app.llm.factory import maybe_build_llm_client
    from app.search.deepseek_api import DeepSeekSearchApiClient
    from app.tool_documentation.enrichment import DocumentationEnricher

    endpoint = settings.web_search_base_url.rstrip("/").removesuffix("/v1")
    async with httpx.AsyncClient() as client:
        enricher = DocumentationEnricher(search=DeepSeekSearchApiClient(api_key=settings.deepseek_api_key,
            base_url=endpoint, http_client=client), model=maybe_build_llm_client(), client=client,
            fetch_endpoint=endpoint + "/v1/tools/fetch", api_key=settings.deepseek_api_key)
        return await enricher.enrich(manifest=job["manifest"], domains=job["domains"])


def get_documentation_service():
    from app.storage.database import SessionLocal
    from app.tool_documentation.repository import DocumentationRepository

    return DocumentationService(repository=DocumentationRepository(SessionLocal), publish=_publish)


def _publish(key):
    # 目录首次构建也不等待 broker 建连；已持久化的任务键保护重复派发。
    return _publisher.submit(_send, key)


def _send(key):
    from app.worker.celery_app import celery_app

    try:
        with celery_app.connection_for_write(connect_timeout=2) as connection:
            celery_app.send_task("bs_agent.tool_documentation.enrich", kwargs={"key": key},
                queue="tool-documentation", retry=False, connection=connection)
    except Exception:  # noqa: BLE001 - 后台派发失败进入持久化冷却，不影响目录。
        try:
            get_documentation_service().repository.fail(key)
        except Exception:  # noqa: BLE001 - DB 不可用时租约到期后仍可重新派发。
            logger.warning("tool_documentation_cooldown_unavailable key=%s", key[:12])
        logger.warning("tool_documentation_publish_failed key=%s", key[:12])
