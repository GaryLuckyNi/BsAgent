import logging
from collections.abc import Callable


logger = logging.getLogger(__name__)


def close_resources(*closers: Callable[[], None]) -> None:
    first_error: Exception | None = None
    for closer in closers:
        try:
            closer()
        except Exception as error:
            logger.exception("关闭应用资源失败: %s", closer)
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error
