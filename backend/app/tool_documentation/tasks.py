from app.tool_documentation.service import get_documentation_service
from app.worker.celery_app import celery_app


@celery_app.task(name="bs_agent.tool_documentation.enrich", acks_late=True)
def enrich_tool_documentation(*, key):
    # 数据库租约负责重复投递、worker 崩溃后的重领与冷却。
    get_documentation_service().process(key)
