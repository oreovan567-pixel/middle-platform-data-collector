"""平台1: lida.qimingdaren.com 爬虫（异步版）
采集: 整体使用率、整体集备、级部集备、学部集备

数据通过 Metabase 嵌入式仪表板（iframe）加载，需要进入 iframe 提取。
流程: 登录 → 切换学校端 → 选择学校 → 导航到使用概览 → 从 iframe 提取数据
"""
from __future__ import annotations
from datetime import date

from playwright.async_api import Page, Frame

from config.config_loader import get_credentials
from scrapers.base import BaseScraper
from scrapers.retry import with_retry


SELECTORS = {
    # 登录页 (Element UI)
    "login_username": 'input.el-input__inner:visible',
    "login_password": 'input[type="password"]:visible',
    "login_button": 'button.el-button--primary:visible',

    # 模式切换
    "header_switch": '.header-switch',
    "school_mode_text": '学校端',

    # 学校选择器（右上角 el-dropdown）
    "school_dropdown": '.school-select .el-dropdown',
}

# 使用概览页面 URL
USAGE_OVERVIEW_URL = "https://lida.qimingdaren.com/#/data/CollectivePreparationStats/UseageOverview"


class LidaScraper(BaseScraper):
    PLATFORM_NAME = "lida"

    def __init__(self, browser_manager):
        super().__init__(browser_manager)
        self._creds = get_credentials("lida")
        self._logged_in = False

    @with_retry(max_attempts=2, backoff_base=3.0)
    async def login(self):
        """登录 lida 平台"""
        page = await self._get_page()
        self.logger.info("正在登录 lida 平台...")

        await page.goto(self._creds["url"], wait_until="domcontentloaded",
                        timeout=60000)
        await self._wait_network_idle(page)

        # 如果不在登录页，检查是否需要切换学校端
        if "/login" not in page.url:
            if await self._is_school_mode(page):
                self.logger.info("已处于登录状态且为学校端")
                self._logged_in = True
                return

        # 填写登录表单
        if "/login" in page.url:
            await page.locator(SELECTORS["login_username"]).first.fill(
                self._creds["username"]
            )
            await page.locator(SELECTORS["login_password"]).first.fill(
                self._creds["password"]
            )
            await page.locator(SELECTORS["login_button"]).first.click()
            await page.wait_for_timeout(2000)
            await self._wait_network_idle(page, timeout=20000)

        # 切换到学校端
        await self._switch_to_school_mode(page)
        self._logged_in = True
        self.logger.info("lida 平台登录成功（学校端）")

    async def _is_school_mode(self, page: Page) -> bool:
        """检查是否已在学校端"""
        try:
            hs = await page.query_selector(SELECTORS["header_switch"])
            if hs:
                text = (await hs.text_content() or "").strip()
                return "学校" in text
        except Exception:
            pass
        return False

    async def _switch_to_school_mode(self, page: Page):
        """从集团端切换到学校端（优化：cookie 探测 + 快速 UI 点击）"""
        try:
            hs = await page.query_selector(SELECTORS["header_switch"])
            if not hs:
                return

            mode_text = (await hs.text_content() or "").strip()
            if "学校" in mode_text:
                self.logger.info("已在学校端模式")
                return

            self.logger.info("当前模式: %s，切换到学校端...", mode_text)

            # cookie 探测: 看看有没有模式相关的 cookie（首次运行后可从日志分析）
            try:
                ctx = page.context
                cookies = await ctx.cookies()
                mode_cookies = [c for c in cookies
                               if any(k in c['name'].lower()
                                      for k in ('mode', 'scope', 'role', 'platform', 'type', 'view'))]
                if mode_cookies:
                    self.logger.info("模式相关 cookies: %s",
                                   [(c['name'], c['value']) for c in mode_cookies])
                # 也打印所有 lida 域名的 cookie 名称，帮助分析
                lida_cookies = [c['name'] for c in cookies
                               if 'qimingdaren' in c.get('domain', '')]
                self.logger.info("Lida cookies (%d个): %s", len(lida_cookies), lida_cookies)
            except Exception as ce:
                self.logger.warning("cookie 探测失败: %s", ce)

            # UI 点击切换
            await hs.click()
            await page.wait_for_timeout(300)
            await page.locator(f'text={SELECTORS["school_mode_text"]}').first.click()
            await page.wait_for_timeout(1000)
            await self._wait_network_idle(page, timeout=15000)

            # 验证切换结果
            if await self._is_school_mode(page):
                self.logger.info("已切换到学校端")
            else:
                self.logger.warning("切换后仍未在学校端，当前页面: %s", page.url[:80])
        except Exception as e:
            self.logger.warning("切换学校端失败: %s", e)

    async def cleanup_between_schools(self):
        """学校间轻量清理：导航回工作台，保持登录状态和上下文不关闭。
        
        Lida 平台通过右上角下拉框切换学校，无需重新登录。
        只需导航回工作台页面，确保下次 _select_school 从干净状态开始。
        """
        if self._page and not self._page.is_closed():
            try:
                await self._page.goto(
                    self._creds.get("url", "https://lida.qimingdaren.com/#/workbench"),
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                await self._page.wait_for_timeout(2000)
                self.logger.info("Lida 已导航回工作台，准备下一个学校")
            except Exception as e:
                self.logger.warning("Lida 导航回工作台失败: %s", e)

    async def _select_school(self, page: Page, school_keyword: str):
        """在右上角学校下拉框中选择对应学校（精确匹配优先）

        school_keyword: config 中 lida_name 的值，用于在下拉菜单中精确匹配学校名称。
        新增学校时应填写立达系统中显示的完整学校名称。
        """
        self.logger.info("选择学校（精确匹配）: %s", school_keyword)
        try:
            # 1. 检查当前是否已选中包含关键字的学校
            current_text = await page.evaluate("""
                () => {
                    const el = document.querySelector('.school-select');
                    return el ? el.textContent.trim() : '';
                }
            """)
            if current_text and current_text == school_keyword:
                self.logger.info("学校已选中（精确匹配）: %s", current_text)
                return True

            # 2. 用 JS 触发 Element UI 下拉菜单（mouseenter 事件）
            await page.evaluate("""
                () => {
                    const trigger = document.querySelector('.school-select .el-tooltip__trigger')
                        || document.querySelector('.school-select .el-dropdown');
                    if (trigger) {
                        trigger.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
                    }
                }
            """)
            await page.wait_for_timeout(1000)

            # 3. 用 JS 遍历所有下拉菜单项，精确匹配（兜底子串匹配）
            clicked = await page.evaluate("""
                (keyword) => {
                    const items = document.querySelectorAll('.el-dropdown-menu__item');
                    // 第一轮：精确匹配
                    for (const item of items) {
                        const text = item.textContent.trim();
                        if (text === keyword) {
                            item.click();
                            return {matched: true, text: text, method: 'exact'};
                        }
                    }
                    // 第二轮：子串匹配（兜底）
                    for (const item of items) {
                        const text = item.textContent.trim();
                        if (text.includes(keyword)) {
                            item.click();
                            return {matched: true, text: text, method: 'includes'};
                        }
                    }
                    // 记录所有可见的菜单项文本，用于调试
                    const allTexts = Array.from(items).map(i => i.textContent.trim());
                    return {matched: false, allTexts: allTexts};
                }
            """, school_keyword)

            if clicked.get("matched"):
                self.logger.info("学校已选择: %s", clicked.get("text", ""))
                await page.wait_for_timeout(3000)
                await self._wait_network_idle(page)
                return True
            else:
                self.logger.warning(
                    "未找到匹配 '%s' 的学校，可用选项: %s",
                    school_keyword, clicked.get("allTexts", []),
                )
                # 兜底：尝试 Playwright locator 子串匹配
                try:
                    await page.locator(
                        f'.el-dropdown-menu__item:has-text("{school_keyword}")'
                    ).first.click(force=True, timeout=3000)
                    self.logger.info("兜底点击成功")
                    await page.wait_for_timeout(3000)
                    return True
                except Exception:
                    self.logger.warning("兜底点击也失败")
                    return False

        except Exception as e:
            self.logger.warning("选择学校失败 [%s]: %s", school_keyword, e)
            return False

    def _find_metabase_frame(self, page: Page) -> Frame | None:
        """从页面 frames 中找到 Metabase iframe"""
        for frame in page.frames:
            if "metabase" in frame.url:
                return frame
        return None

    async def _wait_for_iframe_data(self, frame: Frame, timeout: int = 30000):
        """等待 iframe 内数据加载完成（智能等待百分比数值出现）"""
        try:
            await frame.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

        # 智能等待：轮询检查直到 iframe 中出现 % 数值（最多等 20 秒）
        for _ in range(20):
            has_data = await frame.evaluate("""
                () => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null, false
                    );
                    let node;
                    while (node = walker.nextNode()) {
                        const text = node.textContent.trim();
                        if (text.includes('%') && text.length < 12 && text.length > 0) {
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if has_data:
                self.logger.info("iframe 数据已加载（检测到百分比数值）")
                await frame.page.wait_for_timeout(1000)  # 额外等待渲染稳定
                return
            await frame.page.wait_for_timeout(1000)

        self.logger.warning("iframe 数据加载超时，未检测到百分比数值，继续执行")

    async def _extract_card_data(self, frame: Frame) -> list[dict]:
        """从 Metabase iframe 中提取所有指标卡片数据

        DOM 结构（从调试分析得出）:
        - 百分比值在 SPAN 元素中（无 class）
        - 向上追溯: SPAN → SPAN.ygoQK → DIV → DIV.emotion → DIV → DIV(卡片行) → DIV → react-grid-layout item
        - react-grid-layout 的子元素是每个卡片容器，包含标题和数值

        返回: [{"title": "...", "values": ["使用率%", "访问次数", "平均访问次数"]}]
        """
        cards = await frame.evaluate(r"""
            () => {
                const results = [];
                const seenTitles = new Set();

                // 找到所有包含 % 的文本节点
                const pctSpans = [];
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null, false
                );
                let node;
                while (node = walker.nextNode()) {
                    const text = node.textContent.trim();
                    if (text.includes('%') && text.length < 12 && text.length > 0 && node.parentElement) {
                        if (node.parentElement.tagName === 'SPAN') {
                            pctSpans.push(node.parentElement);
                        }
                    }
                }

                // 已知指标标签（用于从卡片文本中剔除，留下标题）
                const knownLabels = ['使用率', '平均访问次数', '访问次数', '客卡扫描数量', '容器卡扫描数量'];

                // 对每个 % SPAN，向上追溯到 react-grid-layout 的直接子元素（卡片容器）
                for (const pctSpan of pctSpans) {
                    let cardEl = pctSpan;
                    for (let i = 0; i < 10; i++) {
                        if (!cardEl.parentElement) break;
                        cardEl = cardEl.parentElement;
                        if (cardEl.parentElement &&
                            cardEl.parentElement.classList.contains('react-grid-layout')) {
                            break;
                        }
                    }

                    // 收集卡片内所有短文本节点
                    const metricWalker = document.createTreeWalker(
                        cardEl, NodeFilter.SHOW_TEXT, null, false
                    );
                    const textNodes = [];
                    let mNode;
                    while (mNode = metricWalker.nextNode()) {
                        const t = mNode.textContent.trim();
                        if (t.length > 0 && t.length < 25) {
                            textNodes.push(t);
                        }
                    }

                    // 提取标题：从文本节点中找到不符合"标签"或"数值"模式的文本
                    let title = '';
                    for (const t of textNodes) {
                        // 跳过已知标签
                        if (knownLabels.some(l => t === l || t.includes(l))) continue;
                        // 跳过百分比值
                        if (t.includes('%')) continue;
                        // 跳过纯数字（含逗号分隔）
                        if (/^[\d,]+(\.\d+)?$/.test(t)) continue;
                        // 跳过 "..." 或单个字符
                        if (t.length < 2) continue;
                        // 这应该就是标题
                        title = t;
                        break;
                    }

                    // 去重
                    if (seenTitles.has(title)) continue;
                    seenTitles.add(title);

                    // 提取标签-数值对
                    const metrics = {};
                    const extraLabels = ['集备', '组卷', '个备', '使用率', '访问次数', '平均访问次数'];
                    for (let i = 0; i < textNodes.length; i++) {
                        const t = textNodes[i];
                        if (t.includes('使用率') && i + 1 < textNodes.length) {
                            metrics['使用率'] = textNodes[i + 1];
                        } else if (t === '访问次数' && i + 1 < textNodes.length) {
                            metrics['访问次数'] = textNodes[i + 1];
                        } else if (t.includes('平均访问次数') && i + 1 < textNodes.length) {
                            metrics['平均访问次数'] = textNodes[i + 1];
                        }
                    }
                    // 通用提取: 捕获其他标签-数值对（如"集备"、"组卷"等）
                    for (let i = 0; i < textNodes.length; i++) {
                        const t = textNodes[i];
                        if (t.includes('%') || /^[\d,]+(\.\d+)?$/.test(t)) continue;
                        if (t.length < 2 || t.length > 20) continue;
                        if (t === title) continue;
                        if (extraLabels.some(l => t === l || t.includes(l))) {
                            if (!metrics[t] && i + 1 < textNodes.length) {
                                const nextVal = textNodes[i + 1];
                                if (nextVal.includes('%') || /^[\d,]+(\.\d+)?$/.test(nextVal)) {
                                    metrics[t] = nextVal;
                                }
                            }
                        }
                    }
                    // 兜底: 如果metrics为空但有百分比值，取第一个%值
                    if (Object.keys(metrics).length === 0) {
                        for (const t of textNodes) {
                            if (t.includes('%') && t.length < 12) {
                                metrics['使用率'] = t;
                                break;
                            }
                        }
                    }

                    results.push({ title, metrics });
                }

                return results;
            }
        """)
        return cards

    async def _extract_usage_overview(self, frame: Frame) -> dict:
        """从使用概览页面提取关键数据

        返回: {
            "overall_usage_rate": "平台总体使用率",
            "overall_jibei_rate": "集备使用率",
            "overall_jibei_visits": "集备访问次数",
        }
        """
        cards = await self._extract_card_data(frame)

        result = {
            "overall_usage_rate": "",
            "overall_jibei_rate": "",
            "overall_jibei_visits": "",
        }

        self.logger.info("提取到 %d 个数据卡片", len(cards))
        for card in cards:
            title = card.get("title", "")
            metrics = card.get("metrics", {})
            self.logger.info("  卡片: title='%s', metrics=%s", title[:30], metrics)

            # 平台总体数据 → 整体使用率
            if "总体" in title or "平台总" in title:
                result["overall_usage_rate"] = metrics.get("使用率", "")
                self.logger.info("平台总体: %s", metrics)

            # 集备访问数据 → 整体集备
            if "集备" in title and "个备" not in title:
                result["overall_jibei_rate"] = metrics.get("使用率", "")
                result["overall_jibei_visits"] = metrics.get("访问次数", "")
                self.logger.info("集备: %s", metrics)

        return result

    async def _set_iframe_filter(self, frame: Frame, filter_name: str, filter_value: str):
        """在 iframe 内设置筛选条件（学段/年级/学科）

        Metabase 筛选器是 Mantine UI 按钮，点击后弹出选项面板。
        选择值后必须点击弹出面板中的 "Add filter" 按钮才能生效。
        """
        self.logger.info("设置 iframe 筛选: %s = %s", filter_name, filter_value)
        try:
            # 1. 点击筛选按钮（如"学段"/"年级"按钮）
            filter_btn = frame.locator(f'button:has-text("{filter_name}")').first
            await filter_btn.click(timeout=5000)
            await frame.page.wait_for_timeout(500)

            # 2. 等待弹出面板（role="dialog"）出现
            popup = frame.locator('[role="dialog"]').first
            await popup.wait_for(state="visible", timeout=5000)
            self.logger.info("筛选面板已打开")

            # 3. 在弹出面板内选择目标值
            #    选项是 LI 元素，文本内容为目标值
            option = popup.locator(f'li:has-text("{filter_value}")').first
            await option.click(timeout=5000)
            await frame.page.wait_for_timeout(300)
            self.logger.info("已选择值: %s", filter_value)

            # 4. 点击弹出面板内的 "Add filter" 或 "Update filter" 按钮
            confirm_clicked = False
            for btn_text in ["Add filter", "Update filter"]:
                try:
                    btn = popup.locator(f'button:has-text("{btn_text}")').first
                    if await btn.count() > 0:
                        await btn.click(timeout=3000)
                        self.logger.info("已点击 '%s'", btn_text)
                        confirm_clicked = True
                        break
                except Exception:
                    continue
            if not confirm_clicked:
                # 兜底: 通过 JS 查找弹出面板内的确认按钮
                js_clicked = await frame.evaluate(r"""() => {
                    const dialog = document.querySelector('[role="dialog"]');
                    if (!dialog) return false;
                    const buttons = dialog.querySelectorAll('button');
                    for (const b of buttons) {
                        const t = b.textContent.trim().toLowerCase();
                        if (t.includes('add filter') || t.includes('update filter')) {
                            b.click();
                            return b.textContent.trim();
                        }
                    }
                    return false;
                }""")
                if js_clicked:
                    self.logger.info("JS兜底点击确认按钮: %s", js_clicked)
                else:
                    self.logger.warning("未找到 Add filter / Update filter 按钮")

            # 5. 等待数据刷新（优化：缩短固定等待，依赖 networkidle）
            await frame.page.wait_for_timeout(1500)
            try:
                await frame.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await frame.page.wait_for_timeout(2000)

            self.logger.info("筛选设置完成: %s = %s", filter_name, filter_value)
        except Exception as e:
            self.logger.warning("设置筛选失败 [%s=%s]: %s", filter_name, filter_value, e)

    async def _read_current_iframe_dates(self, frame: Frame) -> tuple:
        """从 iframe 日期筛选按钮中读取当前设置的日期值

        返回: (start_date_str, end_date_str)，如 ("2026-05-01", "2026-05-31")
        无法读取时返回 ("", "")
        """
        try:
            dates = await frame.evaluate(r"""() => {
                // 月份映射：英文缩写 → 数字
                const MONTHS = {
                    'Jan': '01', 'Feb': '02', 'Mar': '03', 'Apr': '04',
                    'May': '05', 'Jun': '06', 'Jul': '07', 'Aug': '08',
                    'Sep': '09', 'Oct': '10', 'Nov': '11', 'Dec': '12',
                    'January': '01', 'February': '02', 'March': '03', 'April': '04',
                    'June': '06', 'July': '07', 'August': '08',
                    'September': '09', 'October': '10', 'November': '11', 'December': '12'
                };

                function parseDate(text) {
                    // 格式1: YYYY-MM-DD
                    const m1 = text.match(/(\d{4})-(\d{2})-(\d{2})/);
                    if (m1) return m1[0];

                    // 格式2: "Month D, YYYY" 如 "May 1, 2026" 或 "May 31, 2026"
                    const m2 = text.match(/(\w+)\s+(\d{1,2}),?\s+(\d{4})/);
                    if (m2) {
                        const month = MONTHS[m2[1]];
                        if (month) {
                            const day = m2[2].padStart(2, '0');
                            return m2[3] + '-' + month + '-' + day;
                        }
                    }

                    // 格式3: "YYYY年MM月DD日" 或 "YYYY年M月D日"
                    const m3 = text.match(/(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日/);
                    if (m3) {
                        return m3[1] + '-' + m3[2].padStart(2, '0') + '-' + m3[3].padStart(2, '0');
                    }

                    return '';
                }

                const buttons = document.querySelectorAll('button');
                let startDate = '', endDate = '';

                for (const btn of buttons) {
                    const ariaLabel = btn.getAttribute('aria-label') || '';
                    const text = btn.textContent.trim();
                    const combined = ariaLabel + ' ' + text;

                    // 通过 aria-label 或 text 判断是起始还是结束
                    const isStart = ariaLabel.includes('起始') || ariaLabel.toLowerCase().includes('start')
                        || text.includes('起始');
                    const isEnd = ariaLabel.includes('结束') || ariaLabel.toLowerCase().includes('end')
                        || text.includes('结束');

                    if (!isStart && !isEnd) continue;

                    const parsed = parseDate(combined);
                    if (!parsed) continue;

                    if (isStart && !startDate) startDate = parsed;
                    if (isEnd && !endDate) endDate = parsed;
                }

                return {startDate, endDate};
            }""")
            start = dates.get("startDate", "")
            end = dates.get("endDate", "")
            self.logger.info("读取到 iframe 当前日期: %s ~ %s", start, end)
            return (start, end)
        except Exception as e:
            self.logger.warning("读取当前日期筛选值失败: %s", e)
            return ("", "")

    async def _set_iframe_date_filter(self, frame: Frame, start_date: date, end_date: date,
                                       current_start: str = "", current_end: str = ""):
        """在 Metabase iframe 中设置日期范围筛选（智能模式：只修改不一致的日期）

        Metabase 有两个独立的日期筛选按钮:
        - "起始日期: <当前值>" (aria-label='起始日期')
        - "结束日期: <当前值>" (aria-label='结束日期')

        每个按钮点击后弹出面板，修改日期后点 "Update filter" 确认。
        current_start/current_end: 已读取的当前日期值（YYYY-MM-DD），用于跳过不需要修改的日期。
        """
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")

        need_start = (current_start != start_str)
        need_end = (current_end != end_str)

        if not need_start and not need_end:
            self.logger.info("日期已匹配 (%s ~ %s)，无需修改", start_str, end_str)
            return

        self.logger.info("修改日期: 起始(%s→%s)%s, 结束(%s→%s)%s",
                        current_start, start_str, " [改]" if need_start else " [跳过]",
                        current_end, end_str, " [改]" if need_end else " [跳过]")

        # 只修改需要变更的日期
        if need_start:
            await self._set_single_date_filter(frame, "起始日期", start_str)
        if need_end:
            await self._set_single_date_filter(frame, "结束日期", end_str)

        self.logger.info("日期筛选设置完成: %s ~ %s", start_str, end_str)

    async def _set_single_date_filter(self, frame: Frame, aria_label: str, date_value: str):
        """设置单个日期筛选（起始日期或结束日期）

        aria_label: 按钮的 aria-label，如 "起始日期" 或 "结束日期"
        date_value: 要设置的日期值，如 "2026-05-25"
        """
        self.logger.info("设置 %s = %s", aria_label, date_value)

        # 1. 通过 aria-label 定位日期按钮并点击
        btn = frame.locator(f'button[aria-label="{aria_label}"]').first
        if await btn.count() == 0:
            # 回退: 用文本匹配
            btn = frame.locator(f'button:has-text("{aria_label}")').first
        await btn.click(timeout=5000)
        self.logger.info("已点击 '%s' 按钮", aria_label)
        await frame.page.wait_for_timeout(1500)

        # 2. 诊断: 列出弹出后页面上所有可见的 input 和 button
        try:
            diag = await frame.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll('input:visible'));
                const buttons = Array.from(document.querySelectorAll('button:visible'));
                return {
                    inputs: inputs.map(i => ({
                        type: i.type, name: i.name, placeholder: i.placeholder,
                        value: i.value, cls: i.className.substring(0, 60)
                    })),
                    buttons: buttons.map(b => ({
                        text: b.textContent.trim().substring(0, 60),
                        aria: b.getAttribute('aria-label') || '',
                        cls: b.className.substring(0, 60)
                    }))
                };
            }""")
            self.logger.info("弹出后诊断 - %d 个可见 input, %d 个可见 button:",
                           len(diag['inputs']), len(diag['buttons']))
            for i, inp in enumerate(diag['inputs']):
                self.logger.info("  input[%d] type=%s name='%s' ph='%s' val='%s'",
                               i, inp['type'], inp['name'], inp['placeholder'], inp['value'])
            for i, b in enumerate(diag['buttons']):
                self.logger.info("  button[%d] text='%s' aria='%s'",
                               i, b['text'], b['aria'])
        except Exception as e:
            self.logger.warning("诊断失败: %s", e)

        # 3. 找到日期输入框并填写
        date_filled = False

        # 策略1: input[type="date"]
        try:
            date_inputs = frame.locator('input[type="date"]:visible')
            di_count = await date_inputs.count()
            if di_count > 0:
                await date_inputs.first.click()
                await date_inputs.first.fill(date_value)
                date_filled = True
                self.logger.info("通过 input[type=date] 填写成功")
        except Exception as e:
            self.logger.info("input[type=date] 策略失败: %s", e)

        # 策略2: 通过 JS 找到所有 input，筛选出日期类型的
        if not date_filled:
            try:
                filled = await frame.evaluate("""(dateValue) => {
                    const inputs = Array.from(document.querySelectorAll('input'));
                    const visible = inputs.filter(i => {
                        const rect = i.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    });
                    // 找到看起来像日期输入的 input
                    for (const inp of visible) {
                        const type = inp.type || 'text';
                        const ph = (inp.placeholder || '').toLowerCase();
                        const name = (inp.name || '').toLowerCase();
                        if (type === 'date' || type === 'datetime-local'
                            || ph.includes('date') || ph.includes('日期')
                            || name.includes('date') || name.includes('start') || name.includes('end')) {
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            setter.call(inp, dateValue);
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                            return true;
                        }
                    }
                    // 如果只有一个可见的文本 input，也试试
                    const textInputs = visible.filter(i => i.type === 'text' || !i.type);
                    if (textInputs.length === 1) {
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        setter.call(textInputs[0], dateValue);
                        textInputs[0].dispatchEvent(new Event('input', {bubbles: true}));
                        textInputs[0].dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                    return false;
                }""", date_value)
                if filled:
                    date_filled = True
                    self.logger.info("通过 JS 填写日期成功")
            except Exception as e:
                self.logger.info("JS 填写策略失败: %s", e)

        # 策略3: 用 Playwright 找到任何可见的 input 直接填写
        if not date_filled:
            try:
                all_inputs = frame.locator('input:visible')
                ai_count = await all_inputs.count()
                self.logger.info("尝试在所有 %d 个可见 input 中找日期输入", ai_count)
                for i in range(ai_count):
                    inp = all_inputs.nth(i)
                    inp_type = await inp.get_attribute('type') or 'text'
                    if inp_type in ('date', 'datetime-local', 'text'):
                        await inp.click()
                        await inp.fill(date_value)
                        date_filled = True
                        self.logger.info("通过遍历 visible input 填写成功 (index=%d)", i)
                        break
            except Exception as e:
                self.logger.info("遍历 input 策略失败: %s", e)

        if not date_filled:
            self.logger.warning("未能填写日期值: %s", date_value)

        await frame.page.wait_for_timeout(500)

        # 4. 点击 "Update filter" 按钮（使用多种策略）
        update_clicked = False

        # 策略1: Playwright locator 匹配按钮文本
        update_keywords = [
            "Update filter", "update filter", "Update",
            "更新筛选", "确定", "确认", "应用",
        ]
        for kw in update_keywords:
            try:
                btn = frame.locator(f'button:visible:has-text("{kw}")').first
                if await btn.count() > 0:
                    await btn.click(timeout=3000)
                    update_clicked = True
                    self.logger.info("点击了 '%s' 按钮 (Playwright)", kw)
                    break
            except Exception:
                continue

        # 策略2: JS 查找所有可见按钮，匹配文本
        if not update_clicked:
            try:
                clicked_text = await frame.evaluate("""() => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const visible = buttons.filter(b => {
                        const rect = b.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    });
                    const keywords = ['Update filter', 'update filter', 'Update',
                                    '更新筛选', '确定', '确认', '应用'];
                    for (const btn of visible) {
                        const text = btn.textContent.trim();
                        if (keywords.some(kw => text.toLowerCase().includes(kw.toLowerCase()))) {
                            btn.click();
                            return text;
                        }
                    }
                    // 回退: 找到弹出面板中的按钮（排除取消按钮）
                    for (const btn of visible) {
                        const text = btn.textContent.trim().toLowerCase();
                        if (text && !text.includes('cancel') && !text.includes('取消')
                            && text !== '×' && text !== 'x') {
                            // 检查是否在弹出面板内
                            let parent = btn.parentElement;
                            for (let i = 0; i < 5; i++) {
                                if (!parent) break;
                                const role = parent.getAttribute('role');
                                const cls = parent.className || '';
                                if (role === 'dialog' || cls.includes('popover')
                                    || cls.includes('popup') || cls.includes('filter')
                                    || cls.includes('dropdown')) {
                                    btn.click();
                                    return btn.textContent.trim();
                                }
                                parent = parent.parentElement;
                            }
                        }
                    }
                    return '';
                }""")
                if clicked_text:
                    update_clicked = True
                    self.logger.info("点击了 '%s' 按钮 (JS)", clicked_text)
            except Exception as e:
                self.logger.info("JS 查找按钮失败: %s", e)

        if not update_clicked:
            self.logger.warning("未找到并点击 Update filter 按钮!")

        # 5. 等待数据刷新（优化：缩短固定等待）
        await frame.page.wait_for_timeout(1500)
        try:
            await frame.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await frame.page.wait_for_timeout(1500)
        self.logger.info("%s 设置完成 (update_clicked=%s)", aria_label, update_clicked)

    @with_retry(max_attempts=2, backoff_base=2.0)
    async def scrape(self, school: dict, date_range: tuple) -> dict:
        """
        采集 lida 平台数据

        返回: {
            "overall_usage_rate": "整体使用率",
            "overall_jibei": "整体集备数",
            "grade_jibei": "级部集备数",
            "department_jibei": "学部集备数",
        }
        """
        if not self._logged_in:
            await self.login()

        page = await self._get_page()
        start_date, end_date = date_range

        result = {
            "overall_usage_rate": "",
            "overall_jibei": "",
            "grade_jibei": "",
            "department_jibei": "",
        }

        try:
            # 1. 先在右上角选择对应学校（可能在工作台页面）
            lida_name = school.get("lida_name", school["name"])
            if not await self._select_school(page, lida_name):
                self.logger.error("学校 '%s' 在 Lida 平台中未找到，跳过采集", lida_name)
                return None

            # 2. 导航到使用概览页面（_wait_for_iframe_data 会等数据就绪，这里只需等 iframe 出现）
            self.logger.info("导航到使用概览页面...")
            await page.goto(USAGE_OVERVIEW_URL, wait_until="domcontentloaded",
                           timeout=30000)
            await page.wait_for_timeout(500)
            await self._wait_network_idle(page, timeout=2000)

            # 3. 找到 Metabase iframe（如果首次找不到，短暂等待后重试）
            frame = self._find_metabase_frame(page)
            if not frame:
                self.logger.info("首次未找到 Metabase iframe，等待 3 秒后重试...")
                await page.wait_for_timeout(3000)
                frame = self._find_metabase_frame(page)
            if not frame:
                self.logger.error("未找到 Metabase iframe")
                return result

            self.logger.info("找到 Metabase iframe: %s", frame.url[:60])

            # 4. 等待 iframe 数据加载
            await self._wait_for_iframe_data(frame, timeout=30000)

            # 4.5 智能日期筛选（先比较，不一致才修改）
            current_start, current_end = await self._read_current_iframe_dates(frame)
            target_start = start_date.strftime("%Y-%m-%d")
            target_end = end_date.strftime("%Y-%m-%d")
            if current_start == target_start and current_end == target_end:
                self.logger.info("日期筛选已匹配 (%s ~ %s)，跳过修改", current_start, current_end)
            else:
                self.logger.info("日期不匹配: 当前(%s ~ %s) vs 目标(%s ~ %s)，需要修改",
                               current_start, current_end, target_start, target_end)
                await self._set_iframe_date_filter(frame, start_date, end_date,
                                                   current_start, current_end)

            # 5. Phase 1: 仅时间筛选 → 提取整体使用率和整体集备
            self.logger.info("Phase 1: 提取整体数据（仅时间筛选）...")
            overview = await self._extract_usage_overview(frame)
            result["overall_usage_rate"] = overview["overall_usage_rate"]
            result["overall_jibei"] = overview.get("overall_jibei_rate",
                                                    overview.get("overall_jibei_visits", ""))
            self.logger.info("整体数据: 使用率=%s, 集备=%s",
                           result["overall_usage_rate"], result["overall_jibei"])

            # 6. Phase 2: 加年级筛选 → 提取级部集备
            nianji = school.get("nianji")
            if nianji:
                self.logger.info("Phase 2: 筛选年级=%s，采集级部集备...", nianji)
                await self._set_iframe_filter(frame, "年级", nianji)
                grade_overview = await self._extract_usage_overview(frame)
                result["grade_jibei"] = grade_overview.get(
                    "overall_jibei_rate",
                    grade_overview.get("overall_jibei_visits", "")
                )
                self.logger.info("级部集备: %s", result["grade_jibei"])

            # 7. Phase 3: 加学段筛选 → 提取学部集备
            xueduan = school.get("xueduan")
            if xueduan:
                self.logger.info("Phase 3: 筛选学段=%s，采集学部集备...", xueduan)
                await self._set_iframe_filter(frame, "学段", xueduan)
                dept_overview = await self._extract_usage_overview(frame)
                result["department_jibei"] = dept_overview.get(
                    "overall_jibei_rate",
                    dept_overview.get("overall_jibei_visits", "")
                )
                self.logger.info("学部集备: %s", result["department_jibei"])

            self.logger.info("lida 数据采集完成: %s", result)

        except Exception as e:
            self.logger.error("lida 数据采集失败: %s", e, exc_info=True)
            raise

        return result

    async def _clear_iframe_filter(self, frame: Frame, filter_name: str):
        """清除 iframe 中已有的筛选条件（点击筛选按钮上的 X 移除）"""
        self.logger.info("清除 iframe 筛选: %s", filter_name)
        try:
            # Metabase 筛选按钮上通常有一个 X 按钮用于移除
            cleared = await frame.evaluate(r"""(filterName) => {
                // 找到所有筛选按钮
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = btn.textContent.trim();
                    // 筛选按钮显示已选值时通常包含 filterName 或值文本
                    // 查找旁边的清除按钮 (X icon)
                    if (text.includes(filterName) || text.includes('学段')) {
                        const parent = btn.parentElement;
                        if (parent) {
                            const clearBtn = parent.querySelector('[aria-label*="close"], [aria-label*="remove"], svg[class*="close"]');
                            if (clearBtn) { clearBtn.click(); return true; }
                        }
                        // 尝试再次点击打开，然后选"全部"或清除
                        btn.click();
                        return 'clicked';
                    }
                }
                return false;
            }""", filter_name)

            if cleared == 'clicked':
                await frame.page.wait_for_timeout(1000)
                # 弹出面板中尝试选择全部或清除
                try:
                    select_all = frame.locator('[role="dialog"] li:first-child').first
                    await select_all.click(timeout=3000)
                    await frame.page.wait_for_timeout(500)
                    add_btn = frame.locator('[role="dialog"] button:has-text("Add filter")').first
                    await add_btn.click(timeout=3000)
                    await frame.page.wait_for_timeout(2000)
                except Exception:
                    pass
            elif cleared:
                await frame.page.wait_for_timeout(2000)

            self.logger.info("清除筛选完成: %s (result=%s)", filter_name, cleared)
        except Exception as e:
            self.logger.warning("清除筛选失败 [%s]: %s", filter_name, e)

    async def _extract_segment_data(self, frame: Frame) -> dict:
        """从当前 iframe 状态提取平台使用率、集备、组卷数据

        返回: {
            "usage_rate": "使用率",
            "jibei": "集备数据",
            "zujuan": "组卷数据",
        }
        """
        cards = await self._extract_card_data(frame)
        result = {"usage_rate": "", "jibei": "", "zujuan": ""}

        for card in cards:
            title = card.get("title", "")
            metrics = card.get("metrics", {})
            self.logger.info("  月度卡片: title='%s', metrics=%s", title[:30], metrics)

            # 平台总体使用率
            if "总体" in title or "平台总" in title:
                result["usage_rate"] = metrics.get("使用率", "")

            # 集备相关
            if "集备" in title and "个备" not in title:
                val = metrics.get("使用率", "") or metrics.get("访问次数", "")
                result["jibei"] = val

            # 组卷相关
            if "组卷" in title:
                val = metrics.get("使用率", "") or metrics.get("访问次数", "")
                result["zujuan"] = val

        return result

    @with_retry(max_attempts=2, backoff_base=2.0)
    async def scrape_monthly(self, school: dict, date_range: tuple) -> dict:
        """
        采集 lida 平台月度数据

        返回: {
            "overall_usage_rate": "整体使用率",
            "overall_jibei": "整体集备",
            "platform_usage": "整体平台使用率",
            "platform_usage_hs": "高中平台使用率",
            "platform_usage_ms": "初中平台使用率",
            "platform_usage_ps": "小学平台使用率",
            "jibei_hs": "高中集备",
            "jibei_ms": "初中集备",
            "jibei_ps": "小学集备",
            "zujuan": "整体组卷",
            "zujuan_hs": "高中组卷",
            "zujuan_ms": "初中组卷",
            "zujuan_ps": "小学组卷",
        }
        """
        if not self._logged_in:
            await self.login()

        page = await self._get_page()
        start_date, end_date = date_range

        result = {
            "overall_usage_rate": "",
            "overall_jibei": "",
            "platform_usage": "",
            "platform_usage_hs": "",
            "platform_usage_ms": "",
            "platform_usage_ps": "",
            "jibei_hs": "",
            "jibei_ms": "",
            "jibei_ps": "",
            "zujuan": "",
            "zujuan_hs": "",
            "zujuan_ms": "",
            "zujuan_ps": "",
        }

        try:
            # 1. 选择学校
            lida_name = school.get("lida_name", school["name"])
            if not await self._select_school(page, lida_name):
                self.logger.error("学校 '%s' 在 Lida 平台中未找到，跳过月表采集", lida_name)
                return None

            # 2. 导航到使用概览页面（_wait_for_iframe_data 会等数据就绪，这里只需等 iframe 出现）
            self.logger.info("导航到使用概览页面（月度模式）...")
            await page.goto(USAGE_OVERVIEW_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(500)
            await self._wait_network_idle(page, timeout=2000)

            # 3. 找到 Metabase iframe（首次找不到则等待重试）
            frame = self._find_metabase_frame(page)
            if not frame:
                self.logger.info("首次未找到 Metabase iframe，等待 3 秒后重试...")
                await page.wait_for_timeout(3000)
                frame = self._find_metabase_frame(page)
            if not frame:
                self.logger.error("未找到 Metabase iframe")
                return result

            # 4. 等待数据加载 + 智能日期筛选（先比较，不一致才修改）
            await self._wait_for_iframe_data(frame, timeout=30000)
            current_start, current_end = await self._read_current_iframe_dates(frame)
            target_start = start_date.strftime("%Y-%m-%d")
            target_end = end_date.strftime("%Y-%m-%d")
            if current_start == target_start and current_end == target_end:
                self.logger.info("日期筛选已匹配 (%s ~ %s)，跳过修改", current_start, current_end)
            else:
                self.logger.info("日期不匹配: 当前(%s ~ %s) vs 目标(%s ~ %s)，需要修改",
                               current_start, current_end, target_start, target_end)
                await self._set_iframe_date_filter(frame, start_date, end_date,
                                                   current_start, current_end)

            # 5. Phase 1: 整体数据（无学段筛选）
            self.logger.info("月度 Phase 1: 提取整体数据...")
            overall_data = await self._extract_segment_data(frame)
            result["overall_usage_rate"] = overall_data["usage_rate"]
            result["overall_jibei"] = overall_data["jibei"]
            result["platform_usage"] = overall_data["usage_rate"]
            result["zujuan"] = overall_data["zujuan"]
            self.logger.info("整体: usage=%s, jibei=%s, zujuan=%s",
                           result["platform_usage"], result["overall_jibei"], result["zujuan"])

            # 6. Phase 2: 按学段采集（高中、初中、小学）
            segments = [
                ("高中", "platform_usage_hs", "jibei_hs", "zujuan_hs"),
                ("初中", "platform_usage_ms", "jibei_ms", "zujuan_ms"),
                ("小学", "platform_usage_ps", "jibei_ps", "zujuan_ps"),
            ]

            for segment_name, usage_key, jibei_key, zujuan_key in segments:
                self.logger.info("月度 Phase: 切换学段=%s...", segment_name)
                # 直接切换学段筛选（已有筛选时用 Update filter 更新）
                await self._set_iframe_filter(frame, "学段", segment_name)
                # _set_iframe_filter 已等待数据刷新，直接提取
                seg_data = await self._extract_segment_data(frame)
                result[usage_key] = seg_data["usage_rate"]
                result[jibei_key] = seg_data["jibei"]
                result[zujuan_key] = seg_data["zujuan"]
                self.logger.info("%s: usage=%s, jibei=%s, zujuan=%s",
                               segment_name, result[usage_key], result[jibei_key], result[zujuan_key])

            self.logger.info("lida 月度数据采集完成: %s", result)

        except Exception as e:
            self.logger.error("lida 月度数据采集失败: %s", e, exc_info=True)
            raise

        return result
