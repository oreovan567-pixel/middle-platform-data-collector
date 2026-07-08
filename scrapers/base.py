"""爬虫抽象基类（异步版）"""
from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from datetime import date

from playwright.async_api import Page, BrowserContext

from scrapers.browser_manager import BrowserManager


class BaseScraper(ABC):
    """所有平台爬虫的基类"""

    PLATFORM_NAME: str = "base"

    def __init__(self, browser_manager: BrowserManager):
        self.bm = browser_manager
        self.logger = logging.getLogger(f"scraper.{self.PLATFORM_NAME}")
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._is_shared_context: bool = False  # 标记 context 是否为外部共享的

    async def _get_page(self) -> Page:
        """获取当前页面，如果不存在则创建。

        如果已有 context（例如共享 context），在其中创建新页面而不是创建新 context。
        """
        if self._page is None or self._page.is_closed():
            if self._context is None or self._context.is_closed():
                # 没有 context 或 context 已关闭，创建新的
                self._context = await self.bm.new_context()
                self._is_shared_context = False
            self._page = await self._context.new_page()
        return self._page

    @abstractmethod
    async def login(self):
        """执行登录流程"""
        ...

    @abstractmethod
    async def scrape(self, school: dict, date_range: tuple[date, date]) -> dict:
        """
        采集指定学校在指定日期范围的数据。

        Args:
            school: 学校配置字典（来自 config.yaml 的 schools 列表项）
            date_range: (start_date, end_date) 日期范围元组

        Returns:
            dict，包含采集到的数据字段
        """
        ...

    async def close(self):
        """清理资源。如果是共享 context，只清理页面不关闭 context。"""
        if self._context:
            if not self._is_shared_context:
                try:
                    await self._context.close()
                except Exception:
                    pass
            self._context = None
            self._page = None
            self._is_shared_context = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── 通用辅助方法 ──

    async def _safe_goto(self, page: Page, url: str, wait_selector: str = "", timeout: int = 0):
        """导航到URL并等待目标元素出现"""
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout or None)
        if wait_selector:
            await page.wait_for_selector(wait_selector, timeout=timeout or None)

    async def _wait_network_idle(self, page: Page, timeout: int = 15000):
        """等待网络空闲（SPA数据加载完成）"""
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            self.logger.warning("等待网络空闲超时，继续执行")

    async def _safe_text(self, page: Page, selector: str, default: str = "") -> str:
        """安全获取元素文本"""
        try:
            el = await page.query_selector(selector)
            if el:
                return (await el.text_content() or "").strip()
        except Exception as e:
            self.logger.warning("获取文本失败 [%s]: %s", selector, e)
        return default

    async def _click_and_wait(self, page: Page, selector: str, wait_selector: str = "", timeout: int = 5000):
        """点击元素并等待"""
        await page.click(selector, timeout=timeout)
        if wait_selector:
            await page.wait_for_selector(wait_selector, timeout=timeout)
