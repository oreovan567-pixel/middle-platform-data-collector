"""Playwright 浏览器生命周期管理（异步版）"""
from __future__ import annotations
import logging
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from config.config_loader import get_browser_config

logger = logging.getLogger(__name__)


class BrowserManager:
    """管理 Playwright Browser 实例的创建与销毁"""

    def __init__(self):
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self) -> Browser:
        """启动浏览器"""
        if self._browser is not None:
            return self._browser

        cfg = get_browser_config()
        logger.info(
            "启动浏览器: headless=%s, slow_mo=%sms",
            cfg["headless"], cfg["slow_mo"],
        )

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=cfg["headless"],
            slow_mo=cfg["slow_mo"],
            args=["--disable-gpu", "--no-sandbox"],
        )
        return self._browser

    async def new_context(self, **kwargs) -> BrowserContext:
        """创建新的浏览器上下文"""
        await self.start()
        cfg = get_browser_config()
        ctx_kwargs = {"bypass_csp": True}
        if cfg["headless"]:
            # 无头模式: 设置标准视口，确保页面正常渲染
            ctx_kwargs["viewport"] = {"width": 1920, "height": 1080}
        else:
            # 有头模式: 视口跟随窗口大小
            ctx_kwargs["no_viewport"] = True
        ctx_kwargs.update(kwargs)
        ctx = await self._browser.new_context(**ctx_kwargs)
        # 清除缓存和存储，避免 Grafana 旧数据干扰
        try:
            await ctx.clear_cookies()
        except Exception:
            pass
        ctx.set_default_timeout(cfg["default_timeout"])
        return ctx

    async def new_page(self, **kwargs) -> Page:
        """创建新页面（自动创建context）"""
        ctx = await self.new_context(**kwargs)
        return await ctx.new_page()

    async def stop(self):
        """关闭浏览器"""
        if self._browser:
            logger.info("关闭浏览器")
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    @property
    def is_running(self) -> bool:
        return self._browser is not None and self._browser.is_connected()
