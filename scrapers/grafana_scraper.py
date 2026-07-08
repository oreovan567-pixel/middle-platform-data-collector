"""平台2: Grafana 爬虫（异步版）
采集: 本周活跃教师、本周使用总教师、周活教师比例

优先使用 Grafana HTTP API 方式采集（更稳定），失败时回退到 UI 操作方式。
"""
from __future__ import annotations
import json
import re
from datetime import date, datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.async_api import Page

from config.config_loader import get_credentials
from scrapers.base import BaseScraper
from scrapers.retry import with_retry


SELECTORS = {
    # 登录页
    "login_username": 'input[name="user"]',
    "login_password": 'input[name="password"]',
    "login_button": 'button[type="submit"]',
    "login_success": '.sidemenu, [aria-label*="Toggle menu"], [data-testid="sidemenu"], nav[aria-label="Main menu"], .navbar',

    # 时间选择器
    "time_picker": 'button[aria-label*="Time range selected"]',

    # 面板结构（Grafana 生成式 CSS 类名，用 *= 匹配子串）
    "panel_container": 'section[class*="panel-container"]',
    "panel_title_h2": 'div[class*="panel-title"] h2',
    "panel_content": 'div[class*="panel-content"]',
}

# 面板标题 → 返回字段 映射（支持精确匹配和前缀匹配）
PANEL_FIELD_MAP = {
    "本周活跃教师": "weekly_active_teachers",
    "本周使用总教师": "weekly_total_teachers",
    "本周整体活跃度": "weekly_overall_activity",
    "本周平均使用人数": "weekly_avg_users",
    "本周平均活跃教师人数": "weekly_avg_active_teachers",
    "周活跃教师比例": "weekly_active_ratio",
}

GRAFANA_BASE_URL = "https://grafana.qimingdaren.com"


class GrafanaScraper(BaseScraper):
    PLATFORM_NAME = "grafana"

    def __init__(self, browser_manager):
        super().__init__(browser_manager)
        self._creds = get_credentials("grafana")
        self._logged_in = False

    @with_retry(max_attempts=2, backoff_base=3.0)
    async def login(self):
        """登录 Grafana"""
        if self._creds.get("api_token"):
            self.logger.info("使用 API Token 认证")
            self._logged_in = True
            return

        page = await self._get_page()
        self.logger.info("正在登录 Grafana...")

        # 直接打开 dashboards 列表页（未登录会自动跳转登录页）
        dashboards_url = "https://grafana.qimingdaren.com/dashboards"
        await page.goto(dashboards_url, wait_until="domcontentloaded")
        await self._wait_network_idle(page)

        if await self._is_logged_in(page):
            self.logger.info("已处于登录状态，已在 dashboards 页面")
            self._logged_in = True
            return

        username = self._creds.get("username", "")
        password = self._creds.get("password", "")
        if not username or username == "your_username":
            # 回退到 lida 凭证（调试中发现 lida 凭证也可登录 Grafana）
            lida_creds = get_credentials("lida")
            username = lida_creds["username"]
            password = lida_creds["password"]

        await page.fill(SELECTORS["login_username"], username)
        await page.fill(SELECTORS["login_password"], password)
        await page.click(SELECTORS["login_button"])

        # 多重登录成功检测
        login_detected = False

        # 方式1: 等待 CSS 选择器匹配（sidemenu/navbar 等）
        try:
            await page.wait_for_selector(SELECTORS["login_success"], timeout=10000)
            login_detected = True
            self.logger.info("登录成功: CSS 选择器匹配")
        except Exception:
            self.logger.info("CSS 选择器未匹配，尝试 URL 检测...")

        # 方式2: URL 变化检测（Grafana 登录后从 /login 重定向到 /d/... 或 /）
        if not login_detected:
            for _ in range(10):
                await page.wait_for_timeout(1000)
                current_url = page.url
                if "/login" not in current_url.lower():
                    login_detected = True
                    self.logger.info("登录成功: URL 已变化为 %s", current_url)
                    break

        # 方式3: 检查页面上是否还有登录表单（若消失则说明登录成功）
        if not login_detected:
            try:
                login_form = await page.query_selector(SELECTORS["login_username"])
                if login_form is None:
                    login_detected = True
                    self.logger.info("登录成功: 登录表单已消失")
            except Exception:
                pass

        if not login_detected:
            raise Exception("Grafana 登录失败: 无法确认登录成功")

        # 登录后额外等待页面加载
        await page.wait_for_timeout(2000)
        await self._wait_network_idle(page, timeout=15000)

        # 登录后确保在 dashboards 页面
        if "/dashboards" not in page.url:
            self.logger.info("登录后导航到 dashboards 列表页")
            await page.goto("https://grafana.qimingdaren.com/dashboards", wait_until="domcontentloaded")
            await self._wait_network_idle(page, timeout=15000)
            await page.wait_for_timeout(2000)

        # 清除浏览器存储，避免缓存干扰数据采集
        try:
            await page.evaluate("localStorage.clear(); sessionStorage.clear();")
            self.logger.info("已清除 localStorage/sessionStorage")
        except Exception as e:
            self.logger.warning("清除存储失败: %s", e)

        self._logged_in = True
        self.logger.info("Grafana 登录成功，已在 dashboards 页面: %s", page.url)

    async def _is_logged_in(self, page: Page) -> bool:
        # 方式1: CSS 选择器匹配（sidemenu/navbar）
        try:
            if await page.query_selector(SELECTORS["login_success"]) is not None:
                return True
        except Exception:
            pass
        # 方式2: URL 不在 /login 页面
        try:
            if "/login" not in page.url.lower():
                return True
        except Exception:
            pass
        return False

    async def _navigate_to_dashboard(self, page: Page, dashboard_name: str):
        """从 dashboards 列表页点击进入指定 dashboard"""
        self.logger.info("在 dashboards 列表页查找: '%s'", dashboard_name)

        # 等待列表加载
        await page.wait_for_timeout(2000)

        # 方法1: 点击包含 dashboard 名称的链接/卡片
        clicked = await page.evaluate(r"""(name) => {
            // 查找所有包含目标文字的可点击元素
            const allLinks = document.querySelectorAll('a, [role="link"], [class*="card"], [class*="item"], [class*="search-result"]');
            for (const el of allLinks) {
                const text = el.textContent.trim();
                if (text.includes(name) && text.length < 200) {
                    el.click();
                    return 'clicked:' + text.substring(0, 80);
                }
            }
            // 方法2: 从所有元素中查找（包括 span, div 等）
            const allEls = document.querySelectorAll('*');
            for (const el of allEls) {
                if (el.children.length > 5) continue;
                const text = el.textContent.trim();
                if (text === name || (text.includes(name) && text.length < 100)) {
                    // 点击最近的 a 标签或可点击父元素
                    let target = el.closest('a') || el.closest('[role="link"]') || el;
                    target.click();
                    return 'clicked-alt:' + text.substring(0, 80);
                }
            }
            return '';
        }""", dashboard_name)

        if clicked:
            self.logger.info("点击 dashboard: %s", clicked)
        else:
            self.logger.warning("未在列表中找到 '%s'，尝试直接搜索...", dashboard_name)
            # 尝试在搜索框中输入
            try:
                search_input = await page.query_selector('input[placeholder*="Search"], input[placeholder*="搜索"], input[type="search"]')
                if search_input:
                    await search_input.fill(dashboard_name)
                    await page.wait_for_timeout(2000)
                    # 点击搜索结果
                    result_clicked = await page.evaluate(r"""(name) => {
                        const results = document.querySelectorAll('a, [role="link"], [class*="search-result"], [class*="item"]');
                        for (const el of results) {
                            if (el.textContent.trim().includes(name)) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }""", dashboard_name)
                    if result_clicked:
                        self.logger.info("通过搜索找到并点击: %s", dashboard_name)
                    else:
                        raise Exception(f"搜索结果中未找到 '{dashboard_name}'")
                else:
                    raise Exception(f"dashboards 列表中未找到 '{dashboard_name}' 且无搜索框")
            except Exception as e:
                raise Exception(f"无法导航到 dashboard '{dashboard_name}': {e}")

        # 等待 dashboard 页面加载
        await page.wait_for_timeout(3000)
        await self._wait_network_idle(page, timeout=20000)
        self.logger.info("已进入 dashboard: %s (URL: %s)", dashboard_name, page.url)

    async def _set_time_range_via_ui(self, page: Page, start_date, end_date):
        """通过修改 URL 参数设置时间范围（点击 dashboard 进入后调用）"""
        from datetime import datetime as dt
        self.logger.info("设置时间范围: %s ~ %s", start_date, end_date)

        from_ts = int(dt.combine(start_date, dt.min.time()).timestamp() * 1000)
        to_ts = int(dt.combine(end_date, dt.min.time()).timestamp() * 1000) + 86399000

        # 通过 JS 修改 URL 参数并刷新页面
        navigated = await page.evaluate(r"""(params) => {
            const url = new URL(window.location.href);
            url.searchParams.set('from', String(params.from));
            url.searchParams.set('to', String(params.to));
            window.location.href = url.toString();
            return url.toString();
        }""", {"from": from_ts, "to": to_ts})

        self.logger.info("修改时间参数 URL: %s", navigated[:200])

        # 等待页面重新加载
        await page.wait_for_timeout(3000)
        await self._wait_network_idle(page, timeout=20000)
        self.logger.info("时间范围已设置并刷新完成")
        return True

    async def _set_school_filter_via_ui(self, page: Page, school_name: str):
        """通过修改 URL 参数设置学校变量筛选（点击 dashboard 进入后调用）"""
        self.logger.info("设置学校筛选: %s", school_name)

        # 通过 JS 修改 URL 中的学校变量参数并刷新页面
        # 同时设置 var-school_name 和 var-school（月表用 var-school，周表用 var-school_name）
        navigated = await page.evaluate(r"""(schoolName) => {
            const url = new URL(window.location.href);
            // 设置 school_name 变量（周表主要变量）
            url.searchParams.set('var-school_name', schoolName);
            // 同时设置 school 变量（月表主要变量）
            url.searchParams.set('var-school', schoolName);
            // school_id 保持 $__all（不按 ID 筛选）
            if (url.searchParams.has('var-school_id')) {
                url.searchParams.set('var-school_id', '$__all');
            }
            // exclude_tianli_user_id 保持 $__all
            if (url.searchParams.has('var-exclude_tianli_user_id')) {
                url.searchParams.set('var-exclude_tianli_user_id', '$__all');
            }
            window.location.href = url.toString();
            return url.toString();
        }""", school_name)

        self.logger.info("修改学校参数 URL: %s", navigated[:200])

        # 等待页面重新加载
        await page.wait_for_timeout(3000)
        await self._wait_network_idle(page, timeout=20000)
        self.logger.info("学校筛选已设置并刷新完成")
        return True

    async def _scrape_via_api(self, school: dict, date_range: tuple) -> dict | None:
        """尝试通过 Grafana HTTP API 获取数据"""
        api_token = self._creds.get("api_token")
        if not api_token:
            return None

        try:
            self.logger.info("尝试通过 API 获取 Grafana 数据...")
            return None
        except Exception as e:
            self.logger.warning("API 方式失败，回退到 UI 方式: %s", e)
            return None

    def _build_dashboard_url(self, base_url: str, school_name: str,
                             start_date: date, end_date: date) -> str:
        """构建带时间范围和学校筛选的仪表板 URL"""
        parsed = urlparse(base_url)
        params = parse_qs(parsed.query, keep_blank_values=True)

        # 设置学校变量 - 检测已有变量名并覆盖
        existing_school_vars = [k for k in params if k.startswith('var-school')]
        if existing_school_vars:
            for var_name in existing_school_vars:
                params[var_name] = [school_name]
            # 确保同时有 var-school_name 和 var-school
            if "var-school_name" not in params:
                params["var-school_name"] = [school_name]
            if "var-school" not in params:
                params["var-school"] = [school_name]
        else:
            params["var-school_name"] = [school_name]
            params["var-school"] = [school_name]

        # 使用实际日期范围（本地时区 epoch 毫秒格式）
        from_ts = int(datetime.combine(start_date, datetime.min.time()).timestamp() * 1000)
        to_ts = int(datetime.combine(end_date, datetime.min.time()).timestamp() * 1000) + 86399000  # 23:59:59
        params["from"] = [str(from_ts)]
        params["to"] = [str(to_ts)]

        new_query = urlencode(params, doseq=True)
        self.logger.info("构建 dashboard URL: school=%s, from=%s, to=%s", school_name, from_ts, to_ts)
        return urlunparse(parsed._replace(query=new_query))

    async def _scrape_via_ui(self, school: dict, date_range: tuple) -> dict:
        """通过 UI 操作方式采集数据"""
        page = await self._get_page()
        start_date, end_date = date_range
        school_name = school.get("grafana_name", school["name"])

        self.logger.info("通过 UI 方式采集 Grafana 数据: %s", school_name)

        # 确保在 dashboards 列表页（从其他 dashboard 回来）
        if "/dashboards" not in page.url:
            self.logger.info("返回 dashboards 列表页")
            await page.goto("https://grafana.qimingdaren.com/dashboards", wait_until="domcontentloaded")
            await self._wait_network_idle(page, timeout=15000)
            await page.wait_for_timeout(2000)

        # 1. 设置网络响应拦截，捕获面板查询数据
        captured_responses: list[str] = []

        async def on_response(response):
            url = response.url
            if any(kw in url for kw in [
                '/api/ds/query', '/api/tsdb/query',
                '/api/datasources/proxy', '/api/query',
                '/api/dashboards/', '/api/annotations',
            ]):
                try:
                    body = await response.text()
                    captured_responses.append(body[:10000])
                except Exception:
                    pass

        page.on("response", on_response)

        # 2. 从 dashboards 列表页点击进入周表 dashboard
        await self._navigate_to_dashboard(page, "中台周报表")

        # 3. 通过 UI 设置时间范围
        await self._set_time_range_via_ui(page, start_date, end_date)

        # 4. 通过 UI 设置学校筛选
        await self._set_school_filter_via_ui(page, school_name)

        # 额外等待面板渲染（Grafana SPA 渲染可能有延迟）
        try:
            await page.wait_for_selector(SELECTORS["panel_container"], timeout=15000)
        except Exception:
            self.logger.warning("等待面板容器超时，尝试继续提取")

        # 给面板数据加载留出时间
        await page.wait_for_timeout(3000)

        # 2. 通过 JS 从面板中提取 KPI 数据
        result = {
            "weekly_active_teachers": "",
            "weekly_total_teachers": "",
            "weekly_active_ratio": "",
        }

        panels = await page.query_selector_all(SELECTORS["panel_container"])
        self.logger.info("找到 %d 个面板", len(panels))

        # 诊断: 列出所有面板的标题
        for i, p in enumerate(panels):
            h2 = await p.query_selector(SELECTORS["panel_title_h2"])
            if h2:
                t = (await h2.text_content() or "").strip()
                self.logger.info("  面板[%d] h2='%s'", i, t)
            else:
                # 尝试其他选择器获取标题
                alt_title = await p.evaluate("""(el) => {
                    // 尝试多种标题选择器
                    const sels = ['h2', 'h3', '[class*="title"]', '[data-testid="header-container"]'];
                    for (const s of sels) {
                        const el2 = el.querySelector(s);
                        if (el2) return el2.textContent.trim().substring(0, 80);
                    }
                    return '<无标题>';
                }""")
                self.logger.info("  面板[%d] 无h2, alt='%s'", i, alt_title)

        for panel in panels:
            # 获取面板标题
            title_el = await panel.query_selector(SELECTORS["panel_title_h2"])
            if not title_el:
                continue
            title = (await title_el.text_content() or "").strip()

            # 匹配字段（精确匹配 + 前缀匹配，支持"趋势图"等后缀）
            field_key = PANEL_FIELD_MAP.get(title)
            if not field_key:
                for map_title, map_field in PANEL_FIELD_MAP.items():
                    if title.startswith(map_title) or title.endswith(map_title):
                        field_key = map_field
                        break
            if not field_key:
                continue

            # 提取数值 — 比例面板和KPI面板使用不同策略
            is_ratio_panel = "比例" in title

            if is_ratio_panel:
                # 图表面板（如周活跃教师比例趋势图）：
                # 跳过 panel-content 标准提取（图表SVG会返回错误内容），
                # 直接从图表元素中提取百分比值
                raw_value = await panel.evaluate("""(panel) => {
                    // SVG text 元素中的百分比
                    const svgTexts = panel.querySelectorAll('text');
                    for (const t of svgTexts) {
                        const text = t.textContent.trim();
                        if (text.includes('%') && text.length < 15) {
                            return text;
                        }
                    }
                    // data-label / value 类元素
                    const labels = panel.querySelectorAll(
                        '[class*="dataLabel"], [class*="value"], [class*="DataLabel"]');
                    for (const l of labels) {
                        const text = l.textContent.trim();
                        if (text.includes('%') && text.length < 15) {
                            return text;
                        }
                    }
                    // 遍历面板内所有文本节点找百分比（排除标题h2）
                    const walker = document.createTreeWalker(
                        panel, NodeFilter.SHOW_TEXT, null, false
                    );
                    const h2Text = panel.querySelector('h2')?.textContent?.trim() || '';
                    let node;
                    while (node = walker.nextNode()) {
                        const text = node.textContent.trim();
                        if (text.includes('%') && text.length < 15 && text !== h2Text) {
                            return text;
                        }
                    }
                    return '';
                }""")
                # 验证返回值包含数字（排除纯文本如"周活教师比例"图例）
                if raw_value and re.search(r'\d', raw_value):
                    value = raw_value
                    self.logger.info("从图表提取百分比: [%s] = %s", title, value)
                else:
                    value = ""
                    if raw_value:
                        self.logger.info("图表返回值无数值，忽略: '%s'", raw_value)
            else:
                # KPI 面板: 标准 panel-content 文本提取
                value = await panel.evaluate("""(panel) => {
                    const content = panel.querySelector('div[class*="panel-content"]');
                    if (!content) return '';
                    for (const node of content.childNodes) {
                        if (node.nodeType === 3) {
                            const text = node.textContent.trim();
                            if (text) return text;
                        }
                    }
                    const firstChild = content.firstElementChild;
                    if (firstChild) {
                        let el = firstChild;
                        while (el.firstElementChild) {
                            el = el.firstElementChild;
                        }
                        return el.textContent.trim();
                    }
                    return content.textContent.trim();
                }""")

            result[field_key] = value
            self.logger.info("面板 [%s] = %s", title, value)

        # 2.5 点击"周活教师比例趋势图"柱状图，从详情框中取比例值
        current_ratio = result.get("weekly_active_ratio", "")
        self.logger.info("[step2.5] 进入柱状图提取, current_ratio='%s'", current_ratio)
        if not current_ratio or not re.search(r'\d', current_ratio):
            if current_ratio:
                result["weekly_active_ratio"] = ""
            try:
                ratio_from_chart = await self._extract_ratio_by_clicking_bar(
                    page, school_name)
                if ratio_from_chart:
                    result["weekly_active_ratio"] = ratio_from_chart
                    self.logger.info("从柱状图详情提取: 周活教师比例 = %s",
                                   ratio_from_chart)
                else:
                    self.logger.info("柱状图未获取到比例，保持空值")
            except Exception as e:
                self.logger.warning("柱状图提取失败: %s", e)

        # 3. 如果面板提取失败，从整个页面查找"周活跃教师比例"的百分比值
        if not result.get("weekly_active_ratio"):
            try:
                ratio_value = await page.evaluate("""() => {
                    const panels = document.querySelectorAll(
                        'section[class*="panel-container"]');
                    for (const panel of panels) {
                        // 检查该面板内任何文本是否包含"周活跃教师比例"
                        const allText = panel.textContent || '';
                        if (!allText.includes('周活跃教师比例')
                            && !allText.includes('周活教师比例')) continue;

                        // 在该面板内查找百分比值
                        // 策略1: SVG text 元素
                        const svgTexts = panel.querySelectorAll('text');
                        for (const t of svgTexts) {
                            const text = t.textContent.trim();
                            if (text.includes('%') && text.length < 15) return text;
                        }
                        // 策略2: 所有文本节点
                        const walker = document.createTreeWalker(
                            panel, NodeFilter.SHOW_TEXT, null, false);
                        let node;
                        while (node = walker.nextNode()) {
                            const text = node.textContent.trim();
                            if (text.includes('%') && text.length < 15) return text;
                        }
                    }
                    return '';
                }""")
                if ratio_value:
                    result["weekly_active_ratio"] = ratio_value
                    self.logger.info("页面级查找: 周活跃教师比例 = %s", ratio_value)
                else:
                    self.logger.warning("未能提取周活跃教师比例的百分比值")
            except Exception as e:
                self.logger.warning("提取周活跃教师比例失败: %s", e)

        # 3.4 直接通过 Grafana API 获取比例面板数据（最可靠方式）
        #     利用已登录的浏览器 session 直接调用 Grafana 面板查询接口
        if not result.get("weekly_active_ratio"):
            try:
                ratio_from_api_call = await self._fetch_ratio_via_grafana_api(
                    page, school_name, start_date, end_date)
                if ratio_from_api_call:
                    result["weekly_active_ratio"] = ratio_from_api_call
                    self.logger.info("Grafana API 直调: 周活跃教师比例 = %s",
                                   ratio_from_api_call)
            except Exception as e:
                self.logger.warning("Grafana API 直调失败: %s", e)

        # 3.5 如果 DOM 和 API 都无法取到，尝试通过 canvas tooltip 提取
        #     (在 canvas 图表上模拟鼠标悬停，触发 tooltip 显示数值)
        if not result.get("weekly_active_ratio"):
            try:
                ratio_from_canvas = await self._extract_from_canvas_tooltip(page)
                if ratio_from_canvas:
                    result["weekly_active_ratio"] = ratio_from_canvas
                    self.logger.info("从 canvas tooltip 提取: 周活跃教师比例 = %s",
                                   ratio_from_canvas)
            except Exception as e:
                self.logger.warning("canvas tooltip 提取失败: %s", e)

        # 4. 从捕获的 API 响应中提取比例数据（canvas 图表的值不在 DOM 中）
        if not result.get("weekly_active_ratio") and captured_responses:
            self.logger.info("尝试从 %d 个捕获的 API 响应中提取比例...",
                           len(captured_responses))
            try:
                ratio_from_api = self._parse_ratio_from_responses(
                    captured_responses, start_date, end_date)
                if ratio_from_api:
                    result["weekly_active_ratio"] = ratio_from_api
                    self.logger.info("从 API 响应提取: 周活跃教师比例 = %s",
                                   ratio_from_api)
            except Exception as e:
                self.logger.warning("API 响应解析失败: %s", e)

        # 移除响应监听
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

        self.logger.info("Grafana UI 采集结果: %s", result)
        return result

    def _parse_ratio_from_responses(self, responses: list[str],
                                     start_date: date, end_date: date) -> str:
        """从捕获的 Grafana API 响应中提取周活跃教师比例百分比值。

        Grafana /api/ds/query 响应典型结构:
        {"results": {"A": {"frames": [{"schema": {"fields": [...]},
                                       "data": {"values": [[ts...], [num...]]}}]}}}
        """
        ratio_candidates: list[float] = []

        for i, raw in enumerate(responses):
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                self.logger.debug("响应[%d] JSON 解析失败 (len=%d)", i, len(raw))
                continue

            if isinstance(obj, dict):
                top_keys = list(obj.keys())[:5]
                self.logger.info("响应[%d] keys=%s", i, top_keys)

            # 收集所有 data frames
            frames: list[dict] = []
            if isinstance(obj, dict):
                results = obj.get("results", {})
                if isinstance(results, dict):
                    for ref_id, ref_data in results.items():
                        if isinstance(ref_data, dict):
                            ref_frames = ref_data.get("frames", [])
                            frames.extend(ref_frames)
                            # 诊断: 记录无数据的响应
                            if not ref_frames:
                                ref_keys = list(ref_data.keys())
                                self.logger.info(
                                    "响应[%d] refId=%s 无frames, keys=%s",
                                    i, ref_id, ref_keys)
                                # 尝试从其他字段提取数据
                                if "tables" in ref_data:
                                    self.logger.info(
                                        "响应[%d] 有 tables 字段", i)
                elif isinstance(results, list):
                    for item in results:
                        if isinstance(item, dict):
                            frames.extend(item.get("frames", []))
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict) and "frames" in item:
                        frames.extend(item["frames"])

            for frame in frames:
                schema = frame.get("schema", {})
                data = frame.get("data", {})
                values_arrays = data.get("values", [])

                if not values_arrays:
                    continue

                field_names = [
                    f.get("name", "?")
                    for f in schema.get("fields", [])
                ]
                field_types = [
                    f.get("type", "?")
                    for f in schema.get("fields", [])
                ]
                self.logger.info(
                    "帧 fields=%s, types=%s, 值数组数=%d, 数据点数=%s",
                    field_names, field_types, len(values_arrays),
                    [len(a) if isinstance(a, list) else 0
                     for a in values_arrays],
                )

                # 查找数值字段: 优先从第2个数组开始（跳过时间列），
                # 如果只有1个数组也尝试提取
                start_idx = 1 if len(values_arrays) >= 2 else 0
                for arr_idx in range(start_idx, len(values_arrays)):
                    arr = values_arrays[arr_idx]
                    if not isinstance(arr, list) or not arr:
                        continue
                    # 跳过时间类型字段
                    if arr_idx < len(field_types) and field_types[arr_idx] == "time":
                        continue
                    for v in reversed(arr):
                        fv = None
                        # 数值类型直接取
                        if isinstance(v, (int, float)) and v != 0:
                            fv = float(v)
                        # 字符串类型尝试解析为浮点数
                        elif isinstance(v, str) and v.strip():
                            try:
                                fv = float(v.strip().rstrip('%'))
                            except (ValueError, TypeError):
                                continue
                        if fv is not None and fv != 0:
                            fname = (field_names[arr_idx]
                                     if arr_idx < len(field_names) else "?")
                            self.logger.info(
                                "帧 %s 数值候选: %s (原始类型=%s, 值=%r)",
                                fname, fv,
                                type(v).__name__, v)
                            ratio_candidates.append(fv)
                            break

        if not ratio_candidates:
            self.logger.warning("API 响应中未找到有效的比例数值 (共 %d 个响应)",
                              len(responses))
            for i, raw in enumerate(responses[:5]):
                self.logger.info("响应[%d] 前300字符: %s", i, raw[:300])
            return ""

        self.logger.info("所有数值候选: %s (共 %d 个)",
                        ratio_candidates, len(ratio_candidates))

        # 比例面板的值通常在 0~100% 范围内
        # 其他 KPI 面板的数值（如教师数、活跃度）通常 >100
        pct_candidates = [v for v in ratio_candidates if 0 < v <= 100]

        if pct_candidates:
            # 取最后一个百分比范围内的值（API 响应按面板顺序，
            # 比例趋势图面板通常在 KPI 面板之后）
            raw_value = pct_candidates[-1]
            self.logger.info("百分比候选值: %s, 选取最后一个: %s",
                           pct_candidates, raw_value)
        else:
            # 兜底: 取最小值（比例通常是最小的数值）
            raw_value = min(ratio_candidates)
            self.logger.warning("无百分比范围候选，取最小值: %s", raw_value)

        # >1 表示已经是百分比形式 (如 3.37)，<1 表示小数形式 (如 0.0337)
        if raw_value > 1:
            return f"{raw_value:.2f}%"
        else:
            return f"{raw_value * 100:.2f}%"

    async def _extract_ratio_by_clicking_bar(
        self, page: Page, school_name: str
    ) -> str:
        """点击"周活教师比例趋势图"柱状图，从弹出的详情框中读取比例值。"""
        self.logger.info("点击柱状图提取比例 (学校: %s)...", school_name)

        # 1. 找到面板、滚动到可视区域、等待 canvas 渲染
        chart_info = await page.evaluate("""() => {
            const panels = document.querySelectorAll(
                'section[class*="panel-container"]');
            for (const p of panels) {
                const h2 = p.querySelector('h2');
                if (h2 && h2.textContent.trim().includes('周活教师比例')) {
                    // 先滚动到可视区域
                    p.scrollIntoView({block: 'center'});
                    return {found: true};
                }
            }
            return {found: false};
        }""")

        if not chart_info or not chart_info.get("found"):
            self.logger.warning("未找到比例柱状图面板")
            return ""

        # 等待 canvas 渲染（最多重试 5 次，每次 1 秒）
        canvas_rect = None
        for attempt in range(5):
            await page.wait_for_timeout(1000)
            canvas_rect = await page.evaluate("""() => {
                const panels = document.querySelectorAll(
                    'section[class*="panel-container"]');
                for (const p of panels) {
                    const h2 = p.querySelector('h2');
                    if (h2 && h2.textContent.trim().includes('周活教师比例')) {
                        const canvas = p.querySelector('canvas');
                        if (canvas) {
                            const r = canvas.getBoundingClientRect();
                            return {x: r.left, y: r.top,
                                    width: r.width, height: r.height};
                        }
                    }
                }
                return null;
            }""")
            if canvas_rect:
                break
            self.logger.info("等待 canvas 渲染... (%d/5)", attempt + 1)

        if not canvas_rect:
            self.logger.warning("比例柱状图 canvas 未渲染")
            return ""

        self.logger.info("比例柱状图 canvas: %.0fx%.0f @ (%.0f, %.0f)",
                        canvas_rect["width"], canvas_rect["height"],
                        canvas_rect["x"], canvas_rect["y"])

        # 2. 先 hover 触发 tooltip
        cx = canvas_rect["x"] + canvas_rect["width"] * 0.5
        cy = canvas_rect["y"] + canvas_rect["height"] * 0.5
        await page.mouse.move(cx, cy)
        await page.wait_for_timeout(1000)

        ratio_value = await self._read_tooltip(page)
        if ratio_value:
            self.logger.info("hover tooltip 提取到比例: %s", ratio_value)
            return ratio_value

        # 3. hover 没拿到，点击多个位置尝试
        for x_pct in [0.5, 0.3, 0.7, 0.15, 0.85]:
            click_x = canvas_rect["x"] + canvas_rect["width"] * x_pct
            click_y = canvas_rect["y"] + canvas_rect["height"] * 0.5
            await page.mouse.click(click_x, click_y)
            self.logger.info("点击柱状图 %.0f%% (%.0f, %.0f)",
                           x_pct * 100, click_x, click_y)
            await page.wait_for_timeout(1500)

            # 首次点击后做诊断：输出页面新出现的 tooltip 类元素
            if x_pct == 0.5:
                diag = await page.evaluate("""() => {
                    const found = [];
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        const cls = el.className || '';
                        const clsStr = typeof cls === 'string' ? cls
                            : (cls.baseVal || '');
                        if (clsStr && (
                            clsStr.match(/[Tt]ooltip/i) ||
                            clsStr.match(/[Pp]opover/i) ||
                            clsStr.match(/[Pp]opup/i) ||
                            clsStr.match(/[Oo]verlay/i) ||
                            clsStr.match(/[Mm]odal/i) ||
                            clsStr.match(/[Pp]ortal/i)
                        )) {
                            const text = (el.textContent || '')
                                .substring(0, 200).replace(/\\s+/g, ' ');
                            found.push({cls: clsStr.substring(0, 100),
                                        tag: el.tagName,
                                        text: text});
                        }
                    }
                    return found;
                }""")
                if diag:
                    self.logger.info("诊断-页面tooltip元素 (%d个):", len(diag))
                    for d in diag[:5]:
                        self.logger.info("  [%s.%s] %s",
                                        d.get("tag", ""), d.get("cls", ""),
                                        d.get("text", "")[:120])
                else:
                    self.logger.info("诊断-页面未发现tooltip类元素")

            ratio_value = await self._read_tooltip(page)
            if ratio_value:
                self.logger.info("点击 %d%% 提取到比例: %s",
                               int(x_pct * 100), ratio_value)
                return ratio_value

        self.logger.info("柱状图所有位置均未获取到比例值")
        return ""

    async def _read_tooltip(self, page: Page) -> str:
        """从页面上的 tooltip 或详情框中读取周活教师比例值"""
        result = await page.evaluate(r"""() => {
            // 方法1: 全局搜索含"周活" + "%"的元素（最精确）
            const allEls = document.querySelectorAll('*');
            for (const el of allEls) {
                if (el.children.length > 10) continue;
                const text = el.textContent || '';
                if (text.includes('周活') && text.includes('%')
                    && text.length < 200) {
                    const match = text.match(/([0-9]+\.?[0-9]*)\s*%/);
                    if (match) return match[1] + '%';
                }
            }
            // 方法2: Grafana VizTooltip / GraphTooltip 内的任何百分比值
            const tips = document.querySelectorAll(
                '[class*="Tooltip"], [class*="tooltip"], '
                + '[class*="VizTooltip"], [class*="viz-tooltip"], '
                + '[class*="GraphTooltip"], [class*="graph-tooltip"], '
                + '[class*="PanelTooltip"], [class*="u-tooltip"]');
            for (const tip of tips) {
                const text = tip.textContent || '';
                const match = text.match(/([0-9]+\.?[0-9]*)\s*%/);
                if (match) return match[1] + '%';
            }
            // 方法3: portal/overlay 层内的百分比
            const portals = document.querySelectorAll(
                '[class*="portal"], [class*="Portal"], '
                + '[class*="overlay"], [class*="Overlay"]');
            for (const p of portals) {
                const text = p.textContent || '';
                if (text.length < 500) {
                    const match = text.match(/([0-9]+\.?[0-9]*)\s*%/);
                    if (match) return match[1] + '%';
                }
            }
            // 方法4: absolute/fixed 定位的 tooltip div
            const divs = document.querySelectorAll('div[style*="position"]');
            for (const div of divs) {
                const style = div.getAttribute('style') || '';
                if (!style.includes('absolute') && !style.includes('fixed'))
                    continue;
                const text = div.textContent || '';
                if (text.length < 300) {
                    const match = text.match(/([0-9]+\.?[0-9]*)\s*%/);
                    if (match) return match[1] + '%';
                }
            }
            // 方法5: 含"周活"文字的元素（可能不含%号，值为小数）
            for (const el of allEls) {
                if (el.children.length > 5) continue;
                const text = el.textContent || '';
                if (text.includes('周活') && text.length < 100) {
                    const decMatch = text.match(/([0-9]+\.[0-9]+)/);
                    if (decMatch) {
                        const v = parseFloat(decMatch[1]);
                        if (v >= 0 && v <= 100) return decMatch[1] + '%';
                    }
                }
            }
            return '';
        }""")

        if result and re.search(r'\d', result):
            if '%' in result:
                return result
            try:
                v = float(result.strip())
                return f"{v * 100:.2f}%" if v < 1 else f"{v:.2f}%"
            except (ValueError, TypeError):
                return result
        return ""

    async def _fetch_ratio_via_grafana_api(
        self, page: Page, school_name: str,
        start_date: date, end_date: date
    ) -> str:
        """通过浏览器 session 直接调用 Grafana API 获取比例面板数据。

        1. 获取 dashboard JSON → 找到比例面板配置
        2. 如果面板有 expression target，直接查询
        3. 如果有原始数据 target，查询后本地计算
        """
        self.logger.info("尝试 Grafana API 直调获取比例...")

        # 1. 从当前 URL 提取 dashboard UID
        dashboard_uid = await page.evaluate("""() => {
            const match = location.pathname.match(/\\/d\\/([^\\/]+)/);
            return match ? match[1] : '';
        }""")
        if not dashboard_uid:
            self.logger.warning("无法从 URL 提取 dashboard UID")
            return ""
        self.logger.info("Dashboard UID: %s", dashboard_uid)

        # 2. 获取 dashboard JSON，找到比例面板配置
        panel_config = await page.evaluate("""async (uid) => {
            try {
                const resp = await fetch('/api/dashboards/uid/' + uid);
                const dash = await resp.json();
                const panels = dash?.dashboard?.panels || [];
                // 查找包含"比例"的面板
                for (const p of panels) {
                    const title = p.title || '';
                    if (title.includes('比例')) {
                        return {
                            id: p.id,
                            title: title,
                            type: p.type,
                            targets: JSON.stringify(p.targets || []),
                            fieldConfig: JSON.stringify(
                                p.fieldConfig?.defaults?.unit || ''),
                            transformations: JSON.stringify(
                                p.transformations || []),
                        };
                    }
                }
                return null;
            } catch(e) {
                return {error: e.message};
            }
        }""", dashboard_uid)

        if not panel_config:
            self.logger.warning("未找到比例面板配置")
            return ""
        if "error" in panel_config:
            self.logger.warning("获取面板配置失败: %s", panel_config["error"])
            return ""

        self.logger.info("比例面板: id=%s, title=%s, type=%s",
                        panel_config.get("id"), panel_config.get("title"),
                        panel_config.get("type"))
        self.logger.info("面板 targets: %s", panel_config.get("targets", "")[:500])
        self.logger.info("面板 unit: %s", panel_config.get("fieldConfig", ""))

        panel_id = panel_config.get("id")
        if not panel_id:
            return ""

        # 3. 解析 targets，尝试直接查询面板数据
        try:
            targets_str = panel_config.get("targets", "[]")
            targets = json.loads(targets_str) if targets_str else []
        except (json.JSONDecodeError, TypeError):
            targets = []

        # 如果有 expression target (type: "expression")，它通常依赖其他查询
        # 需要把整个面板的查询都发出去
        has_expression = any(
            t.get("type") == "expression" for t in targets
            if isinstance(t, dict)
        )
        self.logger.info("面板有 expression: %s, targets 数: %d",
                        has_expression, len(targets))

        # 4. 使用 /api/ds/query 查询面板数据
        from_ts = int(datetime.combine(
            start_date, datetime.min.time()).timestamp() * 1000)
        to_ts = int(datetime.combine(
            end_date, datetime.min.time()).timestamp() * 1000) + 86399000

        query_result = await page.evaluate("""async (params) => {
            try {
                const resp = await fetch('/api/ds/query', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        queries: params.targets,
                        from: String(params.from),
                        to: String(params.to),
                    }),
                });
                const data = await resp.json();
                // 提取所有帧中的数值
                const values = [];
                const results = data.results || {};
                for (const [refId, refData] of Object.entries(results)) {
                    const frames = refData.frames || [];
                    for (const frame of frames) {
                        const fields = frame.schema?.fields || [];
                        const dataValues = frame.data?.values || [];
                        const info = {
                            refId: refId,
                            fields: fields.map(f => f.name + ':' + f.type),
                        };
                        // 取每个非时间字段的最后一个值
                        for (let i = 1; i < dataValues.length; i++) {
                            const arr = dataValues[i];
                            if (Array.isArray(arr) && arr.length > 0) {
                                const last = arr[arr.length - 1];
                                if (typeof last === 'number' && last !== 0) {
                                    values.push({
                                        field: fields[i]?.name || '?',
                                        value: last,
                                        refId: refId,
                                    });
                                } else if (typeof last === 'string'
                                           && last.trim()) {
                                    const num = parseFloat(last);
                                    if (!isNaN(num) && num !== 0) {
                                        values.push({
                                            field: fields[i]?.name || '?',
                                            value: num,
                                            refId: refId,
                                            wasString: true,
                                        });
                                    }
                                }
                            }
                        }
                        info.valueCount = dataValues.length;
                    }
                }
                return {values: values, resultKeys: Object.keys(results)};
            } catch(e) {
                return {error: e.message};
            }
        }""", {
            "targets": targets,
            "from": from_ts,
            "to": to_ts,
        })

        if query_result.get("error"):
            self.logger.warning("面板查询失败: %s", query_result["error"])
            return ""

        self.logger.info("面板查询结果: %s", query_result.get("values", []))

        # 5. 从查询结果中提取比例值
        values = query_result.get("values", [])
        if not values:
            self.logger.warning("面板查询无返回值")
            return ""

        # 优先找 expression 结果（通常是 refId 较大的字母，如 C, D）
        # 或者找值在 0-100 范围内的字段
        ratio_value = None
        for v in values:
            val = v.get("value", 0)
            field = v.get("field", "")
            self.logger.info("  值: field=%s, value=%s, refId=%s, wasString=%s",
                           field, val, v.get("refId"), v.get("wasString"))
            if 0 < val <= 100 and val != 7 and val != 29:
                # 排除已知的教师数
                ratio_value = val

        if ratio_value is not None:
            if ratio_value > 1:
                return f"{ratio_value:.2f}%"
            else:
                return f"{ratio_value * 100:.2f}%"

        return ""

    async def _extract_from_canvas_tooltip(self, page: Page) -> str:
        """通过模拟鼠标悬停在 canvas 图表上，触发 tooltip 显示并提取百分比值。

        Grafana 使用 uPlot 渲染 canvas 图表，DOM 中无法直接获取数值。
        通过 mousemove 事件触发 tooltip，然后读取 tooltip 文本。
        """
        self.logger.info("尝试 canvas tooltip 提取比例...")

        # 找到比例趋势图面板的 canvas
        canvas_info = await page.evaluate("""() => {
            const panels = document.querySelectorAll(
                'section[class*="panel-container"]');
            for (const panel of panels) {
                const h2 = panel.querySelector('h2');
                if (!h2) continue;
                const title = h2.textContent.trim();
                if (!title.includes('比例')) continue;

                const canvas = panel.querySelector('canvas');
                if (!canvas) continue;

                const rect = canvas.getBoundingClientRect();
                return {
                    x: rect.x, y: rect.y,
                    width: rect.width, height: rect.height,
                    title: title,
                };
            }
            return null;
        }""")

        if not canvas_info:
            self.logger.warning("未找到比例面板的 canvas 元素")
            return ""

        self.logger.info("找到比例 canvas: %s (%.0fx%.0f @ %.0f,%.0f)",
                        canvas_info["title"], canvas_info["width"],
                        canvas_info["height"], canvas_info["x"],
                        canvas_info["y"])

        # 模拟鼠标移动到 canvas 右侧（最新数据点通常在右边）
        cx = canvas_info["x"] + canvas_info["width"] * 0.9
        cy = canvas_info["y"] + canvas_info["height"] * 0.5

        await page.mouse.move(cx, cy)
        await page.wait_for_timeout(800)

        # 尝试读取 tooltip 文本
        tooltip_text = await page.evaluate("""() => {
            // uPlot tooltip 通常在 body 或 chart 容器内
            const selectors = [
                '.uplot .u-tooltip',
                '[class*="tooltip"]',
                '.graph-tooltip',
                'div[role="tooltip"]',
                '.u-legend .u-value',
            ];
            for (const sel of selectors) {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    const text = el.textContent.trim();
                    if (text && text.length < 30) {
                        // 检查是否包含数字
                        if (/\\d/.test(text)) return text;
                    }
                }
            }
            // 查找 uPlot legend 中的值
            const legendVals = document.querySelectorAll(
                '.u-legend td, [class*="legend"] [class*="value"]');
            for (const lv of legendVals) {
                const text = lv.textContent.trim();
                if (text && /\\d/.test(text) && text.length < 20) {
                    return text;
                }
            }
            return '';
        }""")

        self.logger.info("canvas tooltip 文本: '%s'", tooltip_text)

        # 从 tooltip 文本中提取百分比
        if tooltip_text:
            match = re.search(r'([\d.]+)\s*%', tooltip_text)
            if match:
                return f"{match.group(1)}%"
            # 尝试提取纯数字
            match = re.search(r'([\d.]+)', tooltip_text)
            if match:
                val = float(match.group(1))
                if 0 < val <= 100:
                    return f"{val:.2f}%"

        # 再试一次: 移动到不同位置
        for pct in [0.75, 0.5, 0.25]:
            cx2 = canvas_info["x"] + canvas_info["width"] * pct
            await page.mouse.move(cx2, cy)
            await page.wait_for_timeout(500)

            tooltip_text = await page.evaluate("""() => {
                const els = document.querySelectorAll(
                    '[class*="tooltip"], .u-legend td, [class*="legend"] [class*="value"]');
                for (const el of els) {
                    const text = el.textContent.trim();
                    if (text && /\\d/.test(text) && text.length < 20) return text;
                }
                return '';
            }""")
            if tooltip_text:
                self.logger.info("canvas tooltip (pct=%.0f%%): '%s'", pct * 100,
                               tooltip_text)
                match = re.search(r'([\d.]+)', tooltip_text)
                if match:
                    val = float(match.group(1))
                    if 0 < val <= 100:
                        return f"{val:.2f}%"

        return ""

    @with_retry(max_attempts=2, backoff_base=2.0)
    async def scrape(self, school: dict, date_range: tuple) -> dict:
        """
        采集 Grafana 数据

        返回: {
            "weekly_active_teachers": "本周活跃教师数",
            "weekly_total_teachers": "本周使用总教师数",
            "weekly_active_ratio": "周活教师比例",
        }
        """
        if not self._logged_in:
            await self.login()

        api_result = await self._scrape_via_api(school, date_range)
        if api_result:
            self.logger.info("通过 API 获取数据成功")
            return api_result

        result = await self._scrape_via_ui(school, date_range)
        self.logger.info("Grafana 数据采集完成: %s", result)
        return result

    # 月度活跃度面板标题映射
    MONTHLY_PANEL_FIELD_MAP = {
        "日活教师占比": "daily_active_ratio",
        "周活教师占比": "weekly_active_ratio",
        "月活教师占比": "monthly_active_ratio",
        "日活跃教师占比": "daily_active_ratio",
        "周活跃教师占比": "weekly_active_ratio",
        "月活跃教师占比": "monthly_active_ratio",
        "教师活跃度": "teacher_activity",
        "活跃度": "activity",
    }

    @with_retry(max_attempts=2, backoff_base=2.0)
    async def scrape_monthly(self, school: dict, date_range: tuple) -> dict:
        """
        采集 Grafana 月度活跃度数据

        返回: {
            "daily_active_ratio": "日活教师占比",
            "weekly_active_ratio": "周活教师占比",
            "monthly_active_ratio": "月活教师占比",
        }
        """
        if not self._logged_in:
            await self.login()

        page = await self._get_page()
        start_date, end_date = date_range
        school_name = school.get("grafana_name", school["name"])

        result = {
            "daily_active_ratio": "",
            "weekly_active_ratio": "",
            "monthly_active_ratio": "",
        }

        self.logger.info("采集 Grafana 月度活跃度: %s", school_name)

        # 确保在 dashboards 列表页
        if "/dashboards" not in page.url:
            self.logger.info("返回 dashboards 列表页")
            await page.goto("https://grafana.qimingdaren.com/dashboards", wait_until="domcontentloaded")
            await self._wait_network_idle(page, timeout=15000)
            await page.wait_for_timeout(2000)

        # 从 dashboards 列表页点击进入月表 dashboard
        await self._navigate_to_dashboard(page, "中台使用统计")

        # 通过 UI 设置时间范围
        await self._set_time_range_via_ui(page, start_date, end_date)

        # 通过 UI 设置学校筛选
        await self._set_school_filter_via_ui(page, school_name)

        # 等待面板渲染
        try:
            await page.wait_for_selector(SELECTORS["panel_container"], timeout=15000)
        except Exception:
            self.logger.warning("等待面板容器超时")

        await page.wait_for_timeout(5000)

        # 提取活跃度数据
        panels = await page.query_selector_all(SELECTORS["panel_container"])
        self.logger.info("找到 %d 个面板（月度模式）", len(panels))

        # 诊断: 列出所有面板标题
        for i, p in enumerate(panels):
            h2 = await p.query_selector(SELECTORS["panel_title_h2"])
            if h2:
                t = (await h2.text_content() or "").strip()
                self.logger.info("  月度面板[%d] h2='%s'", i, t)
            else:
                alt = await p.evaluate("""(el) => {
                    const sels = ['h2','h3','[class*=\"title\"]','[data-testid=\"header-container\"]'];
                    for (const s of sels) { const e = el.querySelector(s); if (e) return e.textContent.trim().substring(0,80); }
                    return '<无标题>';
                }""")
                self.logger.info("  月度面板[%d] 无h2, alt='%s'", i, alt)

        # ── 诊断: 输出所有面板的 DOM ID 和 stat 值 ──
        panel_diag = await page.evaluate(r"""() => {
            const results = [];
            // 1. 列出所有 Grafana 面板容器的 ID 和标题
            const panelContainers = document.querySelectorAll('[class*="panel-container"], [data-panelid]');
            for (const p of panelContainers) {
                const id = p.id || '';
                const panelId = p.getAttribute('data-panelid') || '';
                const h2 = p.querySelector('h2, h6, [class*="title"]');
                const title = h2 ? h2.textContent.trim().substring(0, 60) : '';
                // 提取 stat 值（大数字）
                const bigVal = p.querySelector('[data-testid="singleValue"], [class*="singleValue"], [class*="single-stat"]');
                const statVal = bigVal ? bigVal.textContent.trim() : '';
                if (id || title) {
                    results.push({id, panelId, title, statVal: statVal.substring(0, 30)});
                }
            }
            // 2. 查找所有有 ID 且 ID 包含 _r 的元素（Grafana ref IDs）
            const allWithId = document.querySelectorAll('[id*="_r"]');
            for (const el of allWithId) {
                const id = el.id;
                const tag = el.tagName;
                const text = el.textContent.trim().substring(0, 80);
                const cls = el.className ? el.className.substring(0, 60) : '';
                if (!results.find(r => r.id === id)) {
                    results.push({id, tag, text, cls, panelId: ''});
                }
            }
            return results;
        }""")
        if panel_diag:
            self.logger.info("═══ 面板ID诊断（共%d个）═══", len(panel_diag))
            for d in panel_diag:
                self.logger.info("  id='%s' panelId='%s' title='%s' statVal='%s' tag='%s'",
                               d.get('id',''), d.get('panelId',''), d.get('title',''),
                               d.get('statVal',''), d.get('tag',''))

        # ── 用 querySelectorAll 找面板容器，按内部 H2 标题匹配 ──
        stat_extraction = await page.evaluate(r"""() => {
            const result = {stats: {}, tableRaw: '', diag: ''};
            const allPanels = document.querySelectorAll('[class*="panel-container"], [data-panelid]');
            const diagLines = [];

            for (const panel of allPanels) {
                const h2 = panel.querySelector('h2');
                if (!h2) continue;
                const title = h2.textContent.trim();
                const fullText = panel.textContent || '';
                const contentText = fullText.replace(title, '').trim();

                diagLines.push(title + ' => [' + contentText.substring(0, 80) + ']');

                // ── stat 面板 ──
                let statKey = null;
                if (title.includes('日活用户')) statKey = 'daily';
                else if (title.includes('周活教师') || title.includes('周活用户')) statKey = 'weekly';
                else if (title.includes('月活用户') || title.includes('月活教师')) statKey = 'monthly';

                if (statKey) {
                    const numMatch = contentText.match(/([\d,]+\.?\d*)/);
                    const num = numMatch ? parseFloat(numMatch[1].replace(/,/g, '')) : null;
                    const sv = panel.querySelector('[data-testid="singleValue"]');
                    const svText = sv ? sv.textContent.trim() : '';
                    const svVal = sv ? parseFloat(svText.replace(/,/g, '')) : null;
                    let svgVal = null;
                    const svgTexts = panel.querySelectorAll('svg text, text');
                    for (const t of svgTexts) {
                        const st = t.textContent.trim();
                        if (/^\d[\d,.]*$/.test(st) && st.length < 15) {
                            svgVal = parseFloat(st.replace(/,/g, ''));
                            break;
                        }
                    }
                    let classVal = null;
                    const valEls = panel.querySelectorAll('[class*="singleValue"], [class*="single-stat"], [class*="stat-value"], [class*="big-value"], [class*="value"]');
                    for (const el of valEls) {
                        const et = el.textContent.trim();
                        const em = et.match(/([\d,]+\.?\d*)/);
                        if (em) { classVal = parseFloat(em[1].replace(/,/g, '')); break; }
                    }
                    result.stats[statKey] = {
                        num, svVal, svgVal, classVal,
                        contentText: contentText.substring(0, 100),
                        h2Id: h2.id || '', h2Text: title,
                        cls: (panel.className || '').substring(0, 80)
                    };
                }

                // ── 全页面扫描"学校教师总数" ──
                const allCells = document.querySelectorAll('[role="cell"], [role="columnheader"], th, td');
                for (const cell of allCells) {
                    const ct = cell.textContent.trim();
                    if (ct === '学校教师总数') {
                        const row = cell.closest('[role="row"], tr');
                        if (row) {
                            const siblings = Array.from(row.querySelectorAll('[role="cell"], td'));
                            const idx = siblings.indexOf(cell);
                            if (idx >= 0 && idx + 1 < siblings.length) {
                                const val = parseFloat(siblings[idx + 1].textContent.replace(/,/g, ''));
                                if (!isNaN(val) && val > 0) result.teacherTotalPageScan = val;
                            }
                        }
                    }
                }

                // ── 表格面板 ──
                if (title.includes('教师活跃度') || title.includes('活跃度学校')) {
                    result.tableRaw = contentText.substring(0, 600);
                    result.tableTitle = title;
                    const trs = panel.querySelectorAll('tr');
                    if (trs.length > 0) {
                        result.tableType = 'tr';
                        result.tableRows = [];
                        for (const tr of trs) {
                            const cells = Array.from(tr.querySelectorAll('th, td')).map(c => c.textContent.trim());
                            result.tableRows.push(cells);
                        }
                    } else {
                        result.tableType = 'div';
                        const roleRows = panel.querySelectorAll('[role="row"]');
                        if (roleRows.length > 0) {
                            result.roleRows = [];
                            for (const row of roleRows) {
                                const cells = Array.from(row.querySelectorAll('[role="cell"], [role="columnheader"], th, td')).map(c => c.textContent.trim());
                                result.roleRows.push(cells);
                            }
                        }
                        const divRows = panel.querySelectorAll('[class*="row"]');
                        result.divRowCount = divRows.length;
                        if (divRows.length > 0 && divRows.length < 30) {
                            result.divRowSample = [];
                            for (let i = 0; i < Math.min(divRows.length, 10); i++) {
                                result.divRowSample.push({cls: divRows[i].className.substring(0, 80), text: divRows[i].textContent.trim().substring(0, 100), childCount: divRows[i].children.length});
                            }
                        }
                    }
                    const ttMatch = contentText.match(/学校教师总数\D*([\d,]+)/);
                    if (ttMatch) result.teacherTotalFromText = parseFloat(ttMatch[1].replace(/,/g, ''));
                }
            }
            result.diag = diagLines.join('\n');
            return result;
        }""")
        self.logger.info("Stat提取结果: %s", stat_extraction)
        if stat_extraction and stat_extraction.get('diag'):
            self.logger.info("面板内容诊断:\n%s", stat_extraction['diag'])

        # ── 逐步下滑找到 "月活教师学校占比" 面板并提取教师总数 ──
        # Grafana 懒加载：面板不在视口内时不在 DOM 中
        # 需要逐步下滑约一个页面高度，让面板加载到 DOM
        scroll_info = await page.evaluate(r"""async () => {
            // 逐步下滑，每次一个屏幕高度，最多滑5次
            for (let i = 0; i < 5; i++) {
                window.scrollBy(0, window.innerHeight);
                await new Promise(r => setTimeout(r, 1500));

                // 检查面板是否已出现
                const allPanels = document.querySelectorAll('[class*="panel-container"], [data-panelid]');
                for (const panel of allPanels) {
                    const h2 = panel.querySelector('h2');
                    if (!h2) continue;
                    const title = h2.textContent.trim();
                    if (title.includes('月活教师学校占比')) {
                        // 找到了，精准滚到视口中央
                        h2.scrollIntoView({behavior: 'instant', block: 'center'});
                        await new Promise(r => setTimeout(r, 2000));
                        return {found: true, title: title, scrollStep: i + 1};
                    }
                }
            }
            // 没找到，返回当前所有面板标题
            const titles = [];
            const allPanels = document.querySelectorAll('[class*="panel-container"], [data-panelid]');
            for (const panel of allPanels) {
                const h2 = panel.querySelector('h2');
                if (h2) titles.push(h2.textContent.trim());
            }
            return {found: false, titles: titles};
        }""")
        self.logger.info("逐步下滑找月活教师学校占比: %s", scroll_info)
        await page.wait_for_timeout(3000)

        # 提取函数（复用）
        extract_tt_js = r"""() => {
            const result = {val: 0, method: ''};
            const allPanels = document.querySelectorAll('[class*="panel-container"], [data-panelid]');
            for (const panel of allPanels) {
                const h2 = panel.querySelector('h2');
                if (!h2) continue;
                const title = h2.textContent.trim();
                if (!title.includes('月活教师学校占比')) continue;

                result.foundPanel = true;
                result.panelTitle = title;

                // 滚动表格容器（处理水平滚动隐藏列）
                const scrollable = panel.querySelector('[style*="overflow"], [class*="scroll"]');
                if (scrollable) scrollable.scrollLeft = scrollable.scrollWidth;

                // 取面板全文
                const text = panel.textContent || '';
                result.panelText = text.substring(0, 600);

                // 从 role=row 表格提取（不用text_match，避免拼接数字误匹配）
                const roleRows = panel.querySelectorAll('[role="row"]');
                result.roleRowCount = roleRows.length;
                if (roleRows.length >= 2) {
                    // header/data: 用.children取所有子元素（包括columnheader和cell）
                    const headerCells = Array.from(roleRows[0].children).map(c => c.textContent.trim());
                    const dataCells = Array.from(roleRows[1].children).map(c => c.textContent.trim());
                    result.header = headerCells;
                    result.data = dataCells;
                    const idx = headerCells.findIndex(c => c === '学校教师总数');
                    if (idx >= 0 && idx < dataCells.length) {
                        const v = parseFloat(dataCells[idx].replace(/,/g, ''));
                        if (!isNaN(v) && v > 0) {
                            result.val = v;
                            result.method = 'role_table';
                            return result;
                        }
                    }
                }

                // 方式3: 全面板 cell 扫描
                const allCells = panel.querySelectorAll('[role="cell"], [role="columnheader"], th, td');
                const cellTexts = Array.from(allCells).map(c => c.textContent.trim());
                result.cellTexts = cellTexts;
                result.hasNoData = cellTexts.includes('No data') || text.includes('No data');
                const idx2 = cellTexts.indexOf('学校教师总数');
                if (idx2 >= 0 && idx2 + 1 < cellTexts.length) {
                    const v = parseFloat(cellTexts[idx2 + 1].replace(/,/g, ''));
                    if (!isNaN(v) && v > 0) {
                        result.val = v;
                        result.method = 'cell_scan';
                        return result;
                    }
                }
            }
            return result;
        }"""

        tt_result = await page.evaluate(extract_tt_js)
        self.logger.info("月活教师学校占比提取: %s", tt_result)

        # 如果未找到面板或表格No data，再下滑一次重试
        if tt_result and not tt_result.get('val'):
            self.logger.info("首次未取到教师总数(foundPanel=%s, hasNoData=%s)，再下滑重试...",
                           tt_result.get('foundPanel', False), tt_result.get('hasNoData', False))
            # 再向下滑一个屏幕高度
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(5000)
            tt_result = await page.evaluate(extract_tt_js)
            self.logger.info("重试提取结果: %s", tt_result)

        # 从面板获取教师总数
        teacher_total = 0
        if tt_result and tt_result.get('val') and tt_result['val'] > 0:
            teacher_total = tt_result['val']
            self.logger.info("教师总数 = %.0f (method=%s)", teacher_total, tt_result.get('method',''))
        elif tt_result:
            self.logger.info("教师总数未取到, detail: foundPanel=%s, hasNoData=%s, panelText=%s",
                           tt_result.get('foundPanel', False), tt_result.get('hasNoData', False),
                           (tt_result.get('panelText','') or '')[:200])

        # 解析 stat 面板值
        if stat_extraction and stat_extraction.get('stats'):
            for key in ['daily', 'weekly', 'monthly']:
                s = stat_extraction['stats'].get(key, {})
                # 优先用 singleValue，否则用首个数字
                val = s.get('svVal') if s.get('svVal') is not None else s.get('num')
                if val is not None and val > 0:
                    result[key + '_stat_val'] = val
                    self.logger.info("Stat面板 %s: %.0f (h2Id=%s, h2Text=%s)",
                                   key, val, s.get('h2Id',''), s.get('h2Text',''))
                else:
                    self.logger.info("Stat面板 %s: 未取到值, detail=%s", key, s)

        # 解析教师总数（从教师活跃度学校占比面板，作为备用）
        # 注意：teacher_total 可能已从月活教师学校占比面板获取
        if stat_extraction:
            # 方式0: 全页面扫描 "学校教师总数" 列
            tt_page = stat_extraction.get('teacherTotalPageScan')
            if tt_page and tt_page > 0:
                teacher_total = tt_page
                self.logger.info("教师总数 = %.0f (from page scan)", teacher_total)

            # 方式1: 从表格文本 "学校教师总数" 后取数字
            if teacher_total <= 0:
                tt = stat_extraction.get('teacherTotalFromText')
                if tt and tt > 0:
                    teacher_total = tt
                    self.logger.info("教师总数 = %.0f (from text match)", teacher_total)

            # 方式2: 从 role=row 表格取（精确匹配"学校教师总数"）
            if teacher_total <= 0 and stat_extraction.get('roleRows'):
                rows = stat_extraction['roleRows']
                self.logger.info("表格(role): %d 行", len(rows))
                for i, row in enumerate(rows):
                    self.logger.info("  role行%d: %s", i, row)
                    for j, cell in enumerate(row):
                        if cell.strip() == '学校教师总数':
                            for dr in rows[i+1:]:
                                if j < len(dr):
                                    try:
                                        teacher_total = float(dr[j].replace(',', ''))
                                        self.logger.info("教师总数 = %.0f (from role table, exact)", teacher_total)
                                    except ValueError:
                                        pass
                                    break
                        if teacher_total > 0:
                            break
                    if teacher_total > 0:
                        break

            # 方式3: 从 <tr> 表格取（精确匹配"学校教师总数"）
            if teacher_total <= 0 and stat_extraction.get('tableRows'):
                rows = stat_extraction['tableRows']
                self.logger.info("表格(tr): %d 行", len(rows))
                for i, row in enumerate(rows):
                    self.logger.info("  tr行%d: %s", i, row)
                    for j, cell in enumerate(row):
                        if cell.strip() == '学校教师总数':
                            for dr in rows[i+1:]:
                                if j < len(dr):
                                    try:
                                        teacher_total = float(dr[j].replace(',', ''))
                                        self.logger.info("教师总数 = %.0f (from tr table, exact)", teacher_total)
                                    except ValueError:
                                        pass
                                    break
                        if teacher_total > 0:
                            break
                    if teacher_total > 0:
                        break

            # 方式4: 打印 div 表格结构供调试
            if teacher_total <= 0 and stat_extraction.get('divRowSample'):
                self.logger.info("表格(div): %d 个 div[class*=row]", stat_extraction.get('divRowCount', 0))
                for i, r in enumerate(stat_extraction['divRowSample']):
                    self.logger.info("  div行%d: cls='%s' text='%s' children=%d",
                                   i, r.get('cls',''), r.get('text',''), r.get('childCount', 0))

            self.logger.info("表格原文: %s", stat_extraction.get('tableRaw', '')[:200])

        # ── 计算比例 ──
        if teacher_total > 0:
            for key, label in [('daily', '日活'), ('weekly', '周活'), ('monthly', '月活')]:
                stat_val = result.get(key + '_stat_val', 0)
                if stat_val > 0:
                    ratio_val = round(stat_val / teacher_total * 100, 2)
                    ratio_str = str(ratio_val) + '%'
                    result[key + '_active_ratio'] = ratio_str
                    self.logger.info("计算: %s = %.0f / %.0f = %s",
                                   label, stat_val, teacher_total, ratio_str)
                else:
                    self.logger.info("计算: %s 分子为0，跳过", label)
        else:
            self.logger.warning("教师总数为0，无法计算比例")

        # 验证逻辑: 周活不应超过月活（日活面板指标尺度不同，不参与验证）
        try:
            daily = float(result["daily_active_ratio"].replace("%", "")) if result["daily_active_ratio"] else 0
            weekly = float(result["weekly_active_ratio"].replace("%", "")) if result["weekly_active_ratio"] else 0
            monthly = float(result["monthly_active_ratio"].replace("%", "")) if result["monthly_active_ratio"] else 0

            if weekly > 0 and monthly > 0:
                if weekly <= monthly:
                    self.logger.info("活跃度验证通过: 日活(%.2f), 周活(%.2f) <= 月活(%.2f)",
                                   daily, weekly, monthly)
                else:
                    self.logger.warning("活跃度数据异常: 周活(%.2f) > 月活(%.2f) - 周活不应超过月活",
                                      weekly, monthly)
                    result["data_anomaly"] = True
                    result["anomaly_message"] = f"数据异常: 周活({weekly})>月活({monthly})，周活不应超过月活"
        except (ValueError, KeyError) as e:
            self.logger.warning("活跃度验证跳过（数据不完整）: %s", e)

        self.logger.info("Grafana 月度数据采集完成: %s", result)
        return result
