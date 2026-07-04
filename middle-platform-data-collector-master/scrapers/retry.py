"""通用重试装饰器（指数退避，支持同步和异步函数）"""
import asyncio
import inspect
import logging
import time
from functools import wraps

logger = logging.getLogger(__name__)

DEFAULT_RETRYABLE = (TimeoutError, ConnectionError, OSError)


def with_retry(
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    retryable_errors: tuple = DEFAULT_RETRYABLE,
    on_retry=None,
):
    """
    重试装饰器，支持指数退避。自动检测同步/异步函数。

    Args:
        max_attempts: 最大尝试次数（含首次）
        backoff_base: 退避基数（秒），等待时间 = backoff_base ^ (attempt-1)
        retryable_errors: 可重试的异常类型
        on_retry: 重试回调 fn(attempt, exception)
    """

    def decorator(func):
        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                last_error = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        return await func(*args, **kwargs)
                    except retryable_errors as e:
                        last_error = e
                        if attempt == max_attempts:
                            logger.error(
                                "[%s] 第 %d/%d 次尝试失败，不再重试: %s",
                                func.__name__, attempt, max_attempts, e,
                            )
                            raise
                        wait = backoff_base ** (attempt - 1)
                        logger.warning(
                            "[%s] 第 %d/%d 次尝试失败: %s，%0.1f秒后重试...",
                            func.__name__, attempt, max_attempts, e, wait,
                        )
                        if on_retry:
                            on_retry(attempt, e)
                        await asyncio.sleep(wait)
                raise last_error  # type: ignore
            return async_wrapper
        else:
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                last_error = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        return func(*args, **kwargs)
                    except retryable_errors as e:
                        last_error = e
                        if attempt == max_attempts:
                            logger.error(
                                "[%s] 第 %d/%d 次尝试失败，不再重试: %s",
                                func.__name__, attempt, max_attempts, e,
                            )
                            raise
                        wait = backoff_base ** (attempt - 1)
                        logger.warning(
                            "[%s] 第 %d/%d 次尝试失败: %s，%0.1f秒后重试...",
                            func.__name__, attempt, max_attempts, e, wait,
                        )
                        if on_retry:
                            on_retry(attempt, e)
                        time.sleep(wait)
                raise last_error  # type: ignore
            return sync_wrapper

    return decorator
