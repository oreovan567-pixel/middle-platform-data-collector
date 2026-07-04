"""主站 HTTP API 直连采集器

流程:
  1. POST /cloud/login/pc                    大账号登录，获取 cloud token
  2. GET  /cloud/platform/school-ops/list    获取所有学校列表 (含 accountId)
  3. PUT  /cloud/platform/onekey-login/login 一键登录学校，获取 school token
  4. GET  ks.qimingdaren.com/api/exam/optimization  查询作业数据

方案B: ks cookie 通过浏览器一次性获取（ks?token=xxx 自动登录），
      后续数据查询走 API，浏览器只负责登录那一步。

API 来源: 浏览器 DevTools 抓包确认
"""
from __future__ import annotations
import asyncio
import logging
from datetime import date
from typing import Any

import aiohttp

from config.config_loader import get_credentials
from scrapers.browser_manager import BrowserManager

logger = logging.getLogger(__name__)

# API 基础 URL
CLOUD_BASE = "https://api-cloud.qimingdaren.com"
EXAM_BASE = "https://ks.qimingdaren.com"

# 作业类型的 examTypeId (moduleType=2)  只选手阅作业=8
HOMEWORK_EXAM_TYPE_IDS = "8"


class ApiMainSiteScraper:
    """纯 HTTP 方式的主站采集器"""

    def __init__(self):
        self._creds = get_credentials("main_site")
        self._session: aiohttp.ClientSession | None = None
        self._cloud_token: str = ""
        self._school_list: list[dict] = []  # 学校列表 (school-ops/list)
        self._school_tokens: dict[str, str] = {}  # school_name -> token
        self._browser_manager: BrowserManager | None = None
        self._ks_cookies: dict[str, str] = {}  # school_name -> jzt_token cookie
        self._shared_context = None  # 与 main_site 共享的浏览器 context
        self._shared_ctx_logged_in = False  # 共享 context 中是否已完成 Cloud 登录

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        self._shared_context = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── 步骤 1: 登录 ──

    async def _login(self) -> bool:
        """登录大账号获取 cloud token"""
        if self._cloud_token:
            logger.debug("[API] 使用缓存 cloud token")
            return True

        session = await self._get_session()
        url = f"{CLOUD_BASE}/cloud/login/pc"
        payload = {
            "account": self._creds.get("username", ""),
            "password": self._creds.get("password", ""),
            "type": 1,
        }

        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 200:
                        token = self._extract_token(data)
                        if token:
                            self._cloud_token = token
                            logger.info("[API] 主站登录成功, token=%s...", token[:20])
                            return True
                        logger.warning("[API] 登录成功但未找到 token, data keys: %s",
                                       list(data.get("data", {}).keys()) if isinstance(data.get("data"), dict) else type(data.get("data")))
        except Exception as e:
            logger.warning("[API] 主站登录失败: %s", e)

        return False

    def _extract_token(self, data: dict) -> str:
        """从响应中提取 token"""
        inner = data.get("data", {})
        if isinstance(inner, dict):
            for key in ("token", "accessToken", "access_token", "jwt"):
                if inner.get(key):
                    return str(inner[key])
        for key in ("token", "accessToken"):
            if data.get(key):
                return str(data[key])
        return ""

    def _cloud_headers(self) -> dict[str, str]:
        """cloud API 请求头 (注意: 不使用 Bearer，直接用原始 token)"""
        headers = {"Content-Type": "application/json"}
        if self._cloud_token:
            headers["Authorization"] = self._cloud_token
            headers["token"] = self._cloud_token
        return headers

    # ── 步骤 2: 获取学校列表 ──

    async def _fetch_school_list(self) -> list[dict]:
        """获取所有学校列表 (school-ops/list)，使用 page 参数分页获取全部

        注意：该 API 只接受 page 参数（不接受 pageNum/pageSize/size），
        每页固定返回 20 条，通过 total_pages 判断总页数。
        """
        if self._school_list:
            return self._school_list

        if not self._cloud_token and not await self._login():
            return []

        session = await self._get_session()
        url = f"{CLOUD_BASE}/cloud/platform/school-ops/list"
        all_items = []
        page = 1
        total_pages = 1  # 初始值，首次请求后更新

        while page <= total_pages:
            params = {"page": page}
            try:
                async with session.get(url, headers=self._cloud_headers(), params=params) as resp:
                    if resp.status != 200:
                        logger.warning("[API] 学校列表请求返回 %d (page=%d)", resp.status, page)
                        break
                    data = await resp.json()
                    if data.get("code") != 200:
                        logger.warning("[API] 学校列表返回异常 code=%s (page=%d)", data.get("code"), page)
                        break
                    inner = data.get("data", {})
                    if not isinstance(inner, dict):
                        logger.warning("[API] 学校列表 data 不是 dict: %s", type(inner))
                        break
                    items = inner.get("list", [])
                    total = inner.get("total", 0)
                    total_pages = inner.get("total_pages", 1)
                    all_items.extend(items)
                    logger.info("[API] 学校列表第%d/%d页: 获取 %d 条 (累计 %d/%d)",
                                page, total_pages, len(items), len(all_items), total)
                    if not items:
                        break
                    page += 1
            except Exception as e:
                logger.warning("[API] 获取学校列表失败(第%d页): %s", page, e)
                break

        if all_items:
            self._school_list = all_items
            logger.info("[API] 共获取到 %d 个学校", len(all_items))
        return all_items

    def _find_school(self, school_name: str) -> dict | None:
        """在学校列表中查找匹配的学校（精确匹配优先）

        匹配策略：
        1. 精确全名匹配（优先）
        2. 子串匹配（兜底，仅当精确匹配失败时）
        """
        # 1. 精确全名匹配
        for item in self._school_list:
            name = (
                item.get("schoolName", "")
                or item.get("companyName", "")
                or item.get("userName", "")
                or item.get("name", "")
            )
            if not name:
                continue
            if school_name == name:
                return item

        # 2. 子串匹配（兜底）
        for item in self._school_list:
            name = (
                item.get("schoolName", "")
                or item.get("companyName", "")
                or item.get("userName", "")
                or item.get("name", "")
            )
            if not name:
                continue
            if school_name in name or name in school_name:
                logger.info("[API] 子串匹配(兜底): '%s' -> '%s'", school_name, name)
                return item

        # 调试日志：打印所有学校名称以便排查
        all_names = []
        for item in self._school_list:
            n = (item.get("schoolName", "") or item.get("companyName", "")
                 or item.get("userName", "") or item.get("name", ""))
            if n:
                all_names.append(n)
        logger.warning("[API] 未匹配到学校 '%s'，学校列表(%d个): %s", school_name, len(all_names), all_names)
        return None

    # ── 步骤 3: 一键登录学校 ──

    async def _switch_to_school(self, school_name: str) -> str:
        """一键登录目标学校，返回学校 token"""
        if school_name in self._school_tokens:
            logger.info("[API] 使用缓存 school token: %s", school_name)
            return self._school_tokens[school_name]

        logger.info("[API] _switch_to_school: 开始查找 '%s' (学校列表=%d个)", school_name, len(self._school_list))
        if not self._school_list:
            logger.info("[API] _switch_to_school: 学校列表为空，正在获取...")
            await self._fetch_school_list()
            logger.info("[API] _switch_to_school: 获取后学校列表=%d个", len(self._school_list))

        school_info = self._find_school(school_name)
        if not school_info:
            logger.warning("[API] 未在学校列表中找到: %s", school_name)
            return ""

        account_id = school_info.get("accountId")
        if not account_id:
            logger.warning("[API] 学校缺少 accountId: %s", school_name)
            return ""

        session = await self._get_session()
        # 防止 cookie jar 串号：每次请求前清空 session 残留 cookie
        session.cookie_jar.clear()
        url = f"{CLOUD_BASE}/cloud/platform/onekey-login/login"

        # 尝试一键登录，如果 token 过期(400004)则重新登录后重试
        for attempt in range(2):
            try:
                async with session.put(
                    url,
                    json={"accountId": account_id},
                    headers=self._cloud_headers(),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        resp_code = data.get("code")
                        if resp_code == 200:
                            token = data.get("data", {}).get("token", "")
                            user_name = data.get("data", {}).get("user_name", "")
                            if token:
                                self._school_tokens[school_name] = token
                                logger.info("[API] 一键登录 %s 成功 (%s), token=%s...",
                                            school_name, user_name, token[:20])
                                return token
                            logger.warning("[API] 一键登录 %s: 响应无 token, data=%s", school_name, data.get("data"))
                        elif resp_code == 400004:
                            # token 过期，清除缓存重新登录
                            logger.warning("[API] 一键登录 %s: cloud token 过期(400004)，重新登录...", school_name)
                            self._cloud_token = ""
                            self._school_tokens.clear()
                            self._shared_ctx_logged_in = False  # 浏览器 Cloud 会话也失效了
                            if attempt == 0 and await self._login():
                                logger.info("[API] 重新登录成功，重试一键登录 %s", school_name)
                                continue  # 重试
                            else:
                                logger.warning("[API] 重新登录失败或已达重试次数")
                                break
                        else:
                            logger.warning("[API] 一键登录 %s: code=%s, msg=%s", school_name, resp_code, data.get("msg", ""))
                    else:
                        logger.warning("[API] 一键登录 %s: HTTP %d", school_name, resp.status)
            except Exception as e:
                logger.warning("[API] 一键登录异常: %s", e)
            break  # 非 400004 错误不重试

        logger.warning("[API] 一键登录 %s: 所有路径均失败", school_name)
        return ""


    # ── 步骤 3.5: 通过浏览器获取 ks cookie ──

    def set_shared_context(self, ctx):
        """设置与 main_site 浏览器共享的浏览器 context。
        
        共享 context 的好处：
        1. Cloud 只登录一次，API 和浏览器共用同一会话
        2. API 建立的 ks 会话（cookie），浏览器也能直接用
        3. 避免独立 context 登录 Cloud 杀死浏览器会话的问题
        """
        self._shared_context = ctx
        self._shared_ctx_logged_in = False

    async def _ensure_cloud_login_in_ctx(self, ctx):
        """确保共享 context 中已完成 Cloud 浏览器登录。

        API 的 _login() 使用 HTTP 请求，不会在浏览器 context 中建立会话。
        必须在 context 中打开页面并执行浏览器端登录，运维页面才能正常渲染。
        只在首次调用时执行，后续学校复用已有会话。
        """
        if self._shared_ctx_logged_in:
            return True

        page = await ctx.new_page()
        try:
            logger.info("[API] 共享 context: 执行 Cloud 浏览器登录...")
            await page.goto(
                self._creds.get("url", "https://www.qimingdaren.com/platform/login"),
                wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)

            if "/login" in page.url:
                inputs = page.locator('input.el-input__inner:visible')
                count = await inputs.count()
                if count >= 2:
                    await inputs.nth(0).fill(self._creds.get("username", ""))
                    await inputs.nth(1).fill(self._creds.get("password", ""))
                    await page.locator('button:has-text("登录")').first.click()
                    for _ in range(30):
                        await page.wait_for_timeout(500)
                        if "/login" not in page.url:
                            break

            if "/login" not in page.url:
                self._shared_ctx_logged_in = True
                logger.info("[API] 共享 context: Cloud 浏览器登录成功: %s", page.url)
                return True
            else:
                logger.warning("[API] 共享 context: Cloud 浏览器登录失败，仍在登录页")
                return False
        except Exception as e:
            logger.error("[API] 共享 context: Cloud 浏览器登录异常: %s", e)
            return False
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _get_ks_cookies(self, school_name: str, school_token: str) -> str:
        """
        在共享 context 中通过 UI 流程获取 ks cookie。

        流程: 打开新标签 → 导航运维 → 搜索学校 → 一键登录(确认弹窗) → ks 会话建立 → 提取 cookie → 关闭标签
        
        关键：必须通过"一键登录"按钮触发服务端 SSO，直接导航 ks?token=xxx 时 JS 不执行。
        Cloud 登录由共享 context 的初始登录完成，此处不再重复登录。
        """
        if school_name in self._ks_cookies and self._ks_cookies[school_name]:
            return self._ks_cookies[school_name]

        # 优先使用共享 context，降级到独立 context
        ctx = self._shared_context
        is_shared = ctx is not None
        
        if not ctx:
            if not self._browser_manager:
                logger.warning("[API] 无共享 context 且无 BrowserManager，无法获取 ks cookie")
                return ""
            try:
                if not self._browser_manager._browser:
                    await self._browser_manager.start()
                ctx = await self._browser_manager._browser.new_context(
                    no_viewport=True, bypass_csp=True)
            except Exception as e:
                logger.error("[API] 创建独立 context 失败: %s", e)
                return ""
        
        _captcha_retried = False  # 验证码重试标记
        try:
            # 在 context 中打开新标签页（不影响其他页面）
            page = await ctx.new_page()
            opened_pages = [page]  # 跟踪需要关闭的页面
            try:
                logger.info("[API] 获取 ks cookie (%s): %s",
                           "共享ctx" if is_shared else "独立ctx", school_name)

                # 0. 共享 context 必须先完成 Cloud 浏览器登录
                if is_shared and not self._shared_ctx_logged_in:
                    if not await self._ensure_cloud_login_in_ctx(ctx):
                        logger.warning("[API] 共享 context Cloud 登录失败，继续尝试...")

                # 1. 导航到学校运维页面（共享 context 应该已登录 Cloud）
                await page.goto("https://operation.qimingdaren.com/#/account/school",
                               wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)
                
                # 如果被重定向到登录页或非运维域名（Cloud 会话失效），需要重新登录
                need_login = ("/login" in page.url or "redirect" in page.url
                              or "operation" not in page.url)
                if need_login:
                    logger.info("[API] 运维页面需要登录 (URL=%s)，执行 Cloud 登录", page.url)
                    self._shared_ctx_logged_in = False
                    if not await self._ensure_cloud_login_in_ctx(ctx):
                        # _ensure_cloud_login_in_ctx 失败，手动尝试
                        await page.goto(self._creds.get("url",
                            "https://www.qimingdaren.com/platform/login"),
                            wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(1500)
                        if "/login" in page.url:
                            inputs = page.locator('input.el-input__inner:visible')
                            count = await inputs.count()
                            if count >= 2:
                                await inputs.nth(0).fill(self._creds.get("username", ""))
                                await inputs.nth(1).fill(self._creds.get("password", ""))
                                await page.locator('button:has-text("登录")').first.click()
                                for _ in range(30):
                                    await page.wait_for_timeout(500)
                                    if "/login" not in page.url:
                                        break
                        logger.info("[API] Cloud 登录完成: %s", page.url)
                    # 登录后重新导航到运维
                    await page.goto("https://operation.qimingdaren.com/#/account/school",
                                   wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(2000)

                logger.info("[API] 学校运维页面: %s", page.url)

                # 2. 搜索学校
                search_input = page.locator('input.el-input__inner:visible').first
                try:
                    await search_input.wait_for(state="visible", timeout=10000)
                except Exception:
                    logger.warning("[API] 搜索框未出现, URL: %s", page.url)
                    return ""
                await search_input.click()
                await search_input.fill(school_name)
                await page.wait_for_timeout(300)
                try:
                    await page.locator('button:has-text("搜索")').first.click(timeout=3000)
                except Exception:
                    await page.keyboard.press("Enter")
                # 等待搜索结果完全渲染（SPA 表格 + 一键登录按钮）
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
                logger.info("[API] 搜索完成: %s", school_name)

                # 2.5 检测 Cloud 安全验证（验证码 cookie），如果触发则重置并重试
                pre_cookies = await ctx.cookies()
                has_captcha = any(c["name"] == "HxxAI_Captcha_Key" for c in pre_cookies)
                if has_captcha and not _captcha_retried:
                    _captcha_retried = True
                    logger.warning("[API] 检测到 Cloud 安全验证 (HxxAI_Captcha_Key)，重置并重试...")
                    self._shared_ctx_logged_in = False
                    # 清除 context 中所有 cookie，避免残留
                    await ctx.clear_cookies()
                    # 关闭当前标签页
                    for p in opened_pages:
                        try:
                            if not p.is_closed():
                                await p.close()
                        except Exception:
                            pass
                    opened_pages.clear()
                    # 等待一小段时间让安全验证冷却
                    import asyncio as _asyncio
                    await _asyncio.sleep(2)
                    # 打开新标签页
                    page = await ctx.new_page()
                    opened_pages.append(page)
                    # 重新执行 Cloud 浏览器登录
                    if not await self._ensure_cloud_login_in_ctx(ctx):
                        logger.warning("[API] 重试 Cloud 登录失败")
                        return ""
                    # 重新导航到运维
                    await page.goto("https://operation.qimingdaren.com/#/account/school",
                                   wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(2000)
                    logger.info("[API] 重试: 学校运维页面: %s", page.url)
                    # 重新搜索
                    search_input2 = page.locator('input.el-input__inner:visible').first
                    try:
                        await search_input2.wait_for(state="visible", timeout=10000)
                    except Exception:
                        logger.warning("[API] 重试: 搜索框未出现")
                        return ""
                    await search_input2.click()
                    await search_input2.fill(school_name)
                    await page.wait_for_timeout(300)
                    try:
                        await page.locator('button:has-text("搜索")').first.click(timeout=3000)
                    except Exception:
                        await page.keyboard.press("Enter")
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1500)
                    logger.info("[API] 重试: 搜索完成: %s", school_name)

                # 2.9 捕获基线 cookie（onekey-login 前），用于后续差异提取
                _pre_cookies = await ctx.cookies()
                _pre_cookie_keys = set()
                for _c in _pre_cookies:
                    _pre_cookie_keys.add((_c["name"], _c.get("domain", "")))
                logger.info("[API] 基线 cookie: %d 个", len(_pre_cookies))

                # 3. 点击"一键登录" + 确认弹窗（关键：触发服务端 SSO 建立 ks 会话）
                onekey_ok = False
                try:
                    await page.locator('text=一键登录').first.click(timeout=12000)
                    await page.wait_for_timeout(1000)

                    msgbox = page.locator('.el-message-box__wrapper')
                    await msgbox.wait_for(state="visible", timeout=5000)
                    confirm_btn = msgbox.locator('.el-button--primary')
                    await confirm_btn.wait_for(state="visible", timeout=3000)

                    # 点击确认（可能打开新标签页或同页面导航）
                    try:
                        async with page.context.expect_page(timeout=5000) as new_page_info:
                            await confirm_btn.click()
                        new_page = await new_page_info.value
                        await new_page.wait_for_load_state("domcontentloaded")
                        await new_page.wait_for_timeout(500)
                        opened_pages.append(new_page)
                        logger.info("[API] 一键登录新标签页: %s", new_page.url)
                        page = new_page
                    except Exception:
                        await page.wait_for_timeout(500)
                        logger.info("[API] 一键登录同页面导航: %s", page.url)
                    onekey_ok = True

                except Exception as e:
                    logger.warning("[API] 一键登录UI流程失败: %s", e)
                    # 兜底：school_token 注入 + 直接导航
                    if school_token:
                        await ctx.add_cookies([{
                            "name": "token",
                            "value": school_token,
                            "domain": ".qimingdaren.com",
                            "path": "/",
                        }])
                        await page.goto(
                            f"https://ks.qimingdaren.com/?token={school_token}",
                            wait_until="domcontentloaded", timeout=15000)
                        for _ in range(10):
                            await page.wait_for_timeout(500)
                            if "/workbench" in page.url:
                                break

                # 4. 如果不在 ks 域名，导航到 ks workbench
                if "ks.qimingdaren.com" not in page.url:
                    await page.goto("https://ks.qimingdaren.com/workbench",
                                   wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(1000)
                    logger.info("[API] 导航到 ks workbench: %s", page.url)

                # 5. 尝试导航到考试管理（触发 ks 完整会话建立）
                try:
                    await page.locator('text=考试管理').first.click(timeout=8000)
                    await page.wait_for_timeout(1000)
                    logger.info("[API] 考试管理页面: %s", page.url)
                except Exception:
                    logger.info("[API] 考试管理按钮未找到，继续提取 cookie")

                # 6. 提取 cookie（只取 onekey-login 后新增/变化的，避免前校残留污染）
                all_cookies = await ctx.cookies()
                logger.info("[API] ks 共 %d 个 cookie (基线 %d 个)", len(all_cookies), len(_pre_cookies))

                # 构建基线 cookie 值映射，用于检测变化
                _pre_cookie_map = {}
                for _c in _pre_cookies:
                    _pre_cookie_map[(_c["name"], _c.get("domain", ""))] = _c.get("value", "")

                ks_cookie_parts = []
                for cookie in all_cookies:
                    name = cookie["name"]
                    domain = cookie.get("domain", "")
                    value = cookie.get("value", "")
                    if not value:
                        continue
                    key = (name, domain)
                    # 只保留新增或值变化的 cookie
                    if key not in _pre_cookie_map or _pre_cookie_map[key] != value:
                        ks_cookie_parts.append(name + "=" + value)

                # 如果差异提取结果为空（异常情况），回退到全部 cookie
                if not ks_cookie_parts:
                    logger.warning("[API] 差异提取为空，回退全部 cookie")
                    for cookie in all_cookies:
                        value = cookie.get("value", "")
                        if value:
                            ks_cookie_parts.append(cookie["name"] + "=" + value)

                logger.info("[API] 提取新增/变化 cookie: %d 个, names: %s",
                           len(ks_cookie_parts),
                           [p.split("=")[0] for p in ks_cookie_parts])

                # 7. 浏览器内验证 ks API
                if school_token:
                    try:
                        fetch_result = await page.evaluate(r"""async () => {
                            try {
                                const resp = await fetch('/api/exam/optimization?type=0&pageNumber=1&pageSize=5&examTypeId=12,13,14,15,16,8&classType=0&h=1', {
                                    credentials: 'include',
                                    headers: {'x-requested-with': 'XMLHttpRequest'}
                                });
                                const json = await resp.json();
                                return {status: resp.status, code: json.code, msg: json.msg};
                            } catch(e) { return {error: e.message}; }
                        }""")
                        if fetch_result.get("code") == 200:
                            logger.info("[API] ✅ 浏览器内 ks API 认证成功！")
                        elif fetch_result.get("code") == 401:
                            logger.warning("[API] ❌ 浏览器内 ks API 也 401")
                            # Bearer token 兜底
                            bearer_result = await page.evaluate(r"""async (token) => {
                                try {
                                    const resp = await fetch('/api/exam/optimization?type=0&pageNumber=1&pageSize=5&examTypeId=12,13,14,15,16,8&classType=0&h=1', {
                                        credentials: 'include',
                                        headers: {
                                            'x-requested-with': 'XMLHttpRequest',
                                            'Authorization': 'Bearer ' + token
                                        }
                                    });
                                    const json = await resp.json();
                                    return {status: resp.status, code: json.code, msg: json.msg};
                                } catch(e) { return {error: e.message}; }
                            }""", school_token)
                            if bearer_result.get("code") == 200:
                                logger.info("[API] ✅ Bearer token 认证成功！")
                                self._ks_cookies[school_name] = f"Bearer:{school_token}"
                                return self._ks_cookies[school_name]
                    except Exception:
                        pass

                if ks_cookie_parts:
                    cookie_str = "; ".join(ks_cookie_parts)
                    self._ks_cookies[school_name] = cookie_str
                    logger.info("[API] ks cookie (全部 %d 个): %s",
                                len(ks_cookie_parts),
                                ", ".join([p.split("=")[0] for p in ks_cookie_parts]))
                    return cookie_str

                # 最后兜底：school_token
                if school_token:
                    logger.info("[API] 未获取到有效 cookie，使用 school_token 兜底")
                    fallback = f"token={school_token}"
                    self._ks_cookies[school_name] = fallback
                    return fallback

                logger.warning("[API] 未找到可用 cookie")
                return ""
            finally:
                # 关闭本次打开的标签页（保留共享 context 中其他页面）
                for p in opened_pages:
                    try:
                        if not p.is_closed():
                            await p.close()
                    except Exception:
                        pass
        except Exception as e:
            logger.error("[API] 获取 ks cookie 异常: %s", e)
            if not is_shared:
                try:
                    await ctx.close()
                except Exception:
                    pass
            return ""

    def set_browser_manager(self, bm: BrowserManager):
        """设置 BrowserManager（用于获取 ks cookie）"""
        self._browser_manager = bm
    # ── 步骤 4: 查询作业 ──

    async def _query_homework(
        self, school_name: str, school_token: str, start_date: date, end_date: date,
    ) -> dict:
        """
        查询作业数据。

        返回: {
            "total_count": 总条数 (totalRow),
            "class_count": 按班级累加数 (月表用),
        }
        """
        session = await self._get_session()
        # 防止 cookie jar 串号：每次查询前清空 session 残留 cookie
        session.cookie_jar.clear()
        url = f"{EXAM_BASE}/api/exam/optimization"

        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "x-requested-with": "XMLHttpRequest",
            "Referer": f"{EXAM_BASE}/exam/optimization",
            "Origin": EXAM_BASE,
        }
        # 方案B: 使用浏览器获取的 ks cookie
        ks_cookie = self._ks_cookies.get(school_name, "")
        if not ks_cookie:
            # 尝试获取 ks cookie
            ks_cookie = await self._get_ks_cookies(school_name, school_token)
        
        if ks_cookie:
            # 处理 Bearer token 格式（"Bearer:xxx"）
            if ks_cookie.startswith("Bearer:"):
                bearer_token = ks_cookie[7:]
                headers["Authorization"] = f"Bearer {bearer_token}"
                logger.info("[API] 使用 Bearer token 查询: %s", school_name)
            # cookie 可能是 "name=value" 格式（如 web_msid=xxx）或纯值（如 jzt_token）
            elif "=" in ks_cookie:
                headers["Cookie"] = ks_cookie
                cookie_name = ks_cookie.split("=")[0]
                logger.info("[API] 使用 %s cookie 查询: %s", cookie_name, school_name)
            else:
                headers["Cookie"] = f"jzt_token={ks_cookie}"
                logger.info("[API] 使用 jzt_token cookie 查询: %s", school_name)
        elif school_token:
            headers["Cookie"] = f"token={school_token}"
            logger.warning("[API] 无 ks cookie，降级使用 school_token: %s", school_name)

        params = {
            "type": "0",
            "pageNumber": "1",
            "pageSize": "100",
            "examTypeId": HOMEWORK_EXAM_TYPE_IDS,
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "classType": "0",
            "sysGradeId": "",
            "sysCourseIds": "",
            "examStatus": "",
            "examName": "",
            "h": "1",
        }

        result = {"total_count": 0, "class_count": 0, "_auth_ok": False}
        resp_data = None  # 存储有效的 API 响应数据

        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    resp_code = data.get("code")
                    if resp_code == 200:
                        result["_auth_ok"] = True
                        resp_data = data
                    elif resp_code == 401:
                        # 会话失效：清除缓存 cookie，尝试重新获取
                        logger.warning("[API] 作业查询 401 (会话失效): %s", school_name)
                        logger.info("[API-诊断] 401 时发送的 Cookie header: %s",
                                   headers.get("Cookie", "无")[:150])
                        logger.info("[API-诊断] 401 响应体: code=%s msg=%s",
                                   data.get("code"), data.get("msg", ""))
                        if school_name in self._ks_cookies:
                            del self._ks_cookies[school_name]
                        # 用新 school_token 重新获取 cookie 并重试
                        new_cookie = await self._get_ks_cookies(school_name, school_token)
                        if new_cookie:
                            if "=" in new_cookie:
                                headers["Cookie"] = new_cookie
                            else:
                                headers["Cookie"] = f"jzt_token={new_cookie}"
                            logger.info("[API] 重新获取 cookie 后重试: %s", school_name)
                            async with session.get(url, params=params, headers=headers) as resp_retry:
                                if resp_retry.status == 200:
                                    retry_data = await resp_retry.json()
                                    if retry_data.get("code") == 200:
                                        result["_auth_ok"] = True
                                        resp_data = retry_data
                                    elif retry_data.get("code") == 401:
                                        logger.warning("[API] 重试后仍 401，尝试 Bearer token: %s", school_name)
                                        # ── Bearer token 最终兜底 ──
                                        if school_token:
                                            bearer_headers = dict(headers)
                                            bearer_headers["Authorization"] = f"Bearer {school_token}"
                                            bearer_headers.pop("Cookie", None)
                                            async with session.get(url, params=params, headers=bearer_headers) as resp_bearer:
                                                if resp_bearer.status == 200:
                                                    bd = await resp_bearer.json()
                                                    if bd.get("code") == 200:
                                                        result["_auth_ok"] = True
                                                        resp_data = bd
                                                        logger.info("[API] ✅ Bearer token 认证成功: %s", school_name)
                                                    else:
                                                        logger.warning("[API] Bearer token code=%s: %s", bd.get("code"), bd.get("msg",""))
                                                else:
                                                    logger.warning("[API] Bearer HTTP %d", resp_bearer.status)
                                    else:
                                        logger.warning("[API] 重试后 code=%s: %s", retry_data.get("code"), retry_data.get("msg", ""))
                                else:
                                    logger.warning("[API] 重试 HTTP %d", resp_retry.status)
                        else:
                            logger.warning("[API] 无法重新获取 cookie: %s", school_name)
                    else:
                        logger.warning("[API] 作业查询 code=%s: %s",
                                       data.get("code"), data.get("msg", ""))
                else:
                    text = await resp.text()
                    logger.warning("[API] 作业查询 HTTP %d: %s", resp.status, text[:300])
        except Exception as e:
            logger.error("[API] 作业查询异常: %s", e)

        # 统一处理有效数据
        if resp_data:
            page_list = resp_data.get("data", {}).get("pageList", {})
            total_row = page_list.get("totalRow", 0)
            result["total_count"] = total_row
            logger.info("[API] 作业总条数: %d", total_row)

            items = page_list.get("list", [])
            class_count = 0
            for item in items:
                classes = item.get("examClass", [])
                class_count += len(classes) if classes else 1
            result["class_count"] = class_count

            # 多页翻页累加
            total_page = page_list.get("totalPage", 1)
            if total_page > 1:
                for page_num in range(2, total_page + 1):
                    params["pageNumber"] = str(page_num)
                    try:
                        async with session.get(url, params=params, headers=headers) as resp2:
                            if resp2.status == 200:
                                data2 = await resp2.json()
                                items2 = data2.get("data", {}).get("pageList", {}).get("list", [])
                                for item in items2:
                                    classes = item.get("examClass", [])
                                    class_count += len(classes) if classes else 1
                    except Exception as e:
                        logger.warning("[API] 翻页 %d 失败: %s", page_num, e)
                        break
                result["class_count"] = class_count
                logger.info("[API] 班级累加: %d (共 %d 页)", class_count, total_page)

        return result

    # ── 周表采集 ──

    async def scrape(self, school: dict, date_range: tuple) -> dict:
        """
        周表采集：作业次数（总条数 totalRow）

        返回: {
            "homework_count": "作业场次数",
        }
        """
        start_date, end_date = date_range
        school_name = school.get("main_site_name", school["name"])
        logger.info("[API] 主站周表采集: %s (%s ~ %s)", school_name, start_date, end_date)

        result = {"homework_count": ""}

        try:
            if not await self._login():
                logger.warning("[API] 周表采集: 登录失败 %s", school_name)
                return result

            school_token = await self._switch_to_school(school_name)
            if not school_token:
                logger.warning("[API] 周表采集: 切换学校失败 %s", school_name)
                return result

            hw = await self._query_homework(school_name, school_token, start_date, end_date)
        except Exception as e:
            logger.error("[API] 周表采集异常 %s: %s", school_name, e, exc_info=True)
            return result

        # 只在认证通过时标记成功
        if hw.get("_auth_ok"):
            result["_api_success"] = True
            # 与月表一致：优先使用班级累加数，兜底用总条数
            if hw["class_count"] > 0:
                result["homework_count"] = str(hw["class_count"])
                logger.info("[API] %s 作业次数(班级累加): %s", school_name, result["homework_count"])
            else:
                result["homework_count"] = str(hw["total_count"])
                logger.info("[API] %s 作业次数(兜底totalRow): %s", school_name, result["homework_count"])
        else:
            logger.warning("[API] %s 作业查询认证失败，不标记成功", school_name)

        return result

    # ── 月表采集 ──

    async def scrape_monthly(self, school: dict, date_range: tuple) -> dict:
        """
        月表采集：作业次数（按班级数累加，与浏览器版一致）

        返回: {
            "homework_count": "作业次数（班级累加）",
        }
        """
        start_date, end_date = date_range
        school_name = school.get("main_site_name", school["name"])
        logger.info("[API] 主站月表采集: %s (%s ~ %s)", school_name, start_date, end_date)

        result = {"homework_count": ""}

        try:
            if not await self._login():
                logger.warning("[API] 月表采集: 登录失败 %s", school_name)
                return result

            school_token = await self._switch_to_school(school_name)
            if not school_token:
                logger.warning("[API] 月表采集: 切换学校失败 %s", school_name)
                return result

            hw = await self._query_homework(school_name, school_token, start_date, end_date)
        except Exception as e:
            logger.error("[API] 月表采集异常 %s: %s", school_name, e, exc_info=True)
            return result

        # 只在认证通过时标记成功
        if hw.get("_auth_ok"):
            result["_api_success"] = True
            if hw["class_count"] > 0:
                result["homework_count"] = str(hw["class_count"])
                logger.info("[API] %s 作业次数(班级累加): %s", school_name, result["homework_count"])
            else:
                result["homework_count"] = str(hw["total_count"])
                logger.info("[API] %s 作业次数(兜底): %s", school_name, result["homework_count"])
        else:
            logger.warning("[API] %s 作业查询认证失败，不标记成功", school_name)

        return result

    @property
    def is_available(self) -> bool:
        return bool(self._cloud_token)
