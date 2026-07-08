"""平台3: qimingdaren.com 主站爬虫（异步版）
采集: 作业次数（考试管理中场景为"作业"的总场次）

流程: 登录 → 导航到学校运维 → 搜索学校 → 一键登录 → 考试阅卷系统 → 考试管理 → 筛选作业
"""
import re
from datetime import date

from playwright.async_api import Page

from config.config_loader import get_credentials
from scrapers.base import BaseScraper
from scrapers.retry import with_retry


# 运维平台 URL
OPS_BASE = "https://operation.qimingdaren.com"
SCHOOL_OPS_URL = f"{OPS_BASE}/#/account/school"


class MainSiteScraper(BaseScraper):
    PLATFORM_NAME = "main_site"

    def __init__(self, browser_manager):
        super().__init__(browser_manager)
        self._creds = get_credentials("main_site")
        self._logged_in = False

    async def close(self):
        """关闭页面并重置登录状态，下次采集会重新打开页面并登录"""
        self._logged_in = False
        await super().close()

    async def cleanup_between_schools(self):
        """学校间轻量清理：关闭考试相关标签页，确保 self._page 指向学校运维页面。
        
        关键：遍历所有页面找到学校运维页面并保留，其余关闭。
        如果找不到运维页面，降级为完全重置。
        """
        if not self._context:
            return
        
        try:
            pages = [p for p in self._context.pages if not p.is_closed()]
            
            # 找到学校运维页面（URL 含 "operation"）
            ops_page = None
            other_pages = []
            for p in pages:
                if "operation" in p.url:
                    ops_page = p
                else:
                    other_pages.append(p)
            
            # 关闭所有非运维页面
            for p in other_pages:
                try:
                    await p.close()
                except Exception:
                    pass
            
            if ops_page:
                self._page = ops_page
                self.logger.info("主站轻量清理完成，保留运维页面: %s", ops_page.url)
                return
            
            # 没有找到运维页面 → 用当前页面导航回去
            if self._page and not self._page.is_closed():
                await self._page.goto(SCHOOL_OPS_URL,
                                      wait_until="domcontentloaded", timeout=30000)
                await self._page.wait_for_timeout(1500)
                self.logger.info("主站轻量清理：导航回运维页面: %s", self._page.url)
                return
            
            # 页面全部不可用 → 完全重置
            self.logger.warning("无可用页面，需要重新登录")
            self._logged_in = False
            self._context = None
            self._page = None
        except Exception as e:
            self.logger.warning("轻量清理失败，降级为完全重置: %s", e)
            self._logged_in = False
            try:
                for p in self._context.pages:
                    if not p.is_closed():
                        try:
                            await p.close()
                        except Exception:
                            pass
                await self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None

    @with_retry(max_attempts=2, backoff_base=3.0)
    async def login(self):
        """登录主站"""
        page = await self._get_page()
        self.logger.info("正在登录主站...")

        await page.goto(self._creds["url"], wait_until="domcontentloaded")
        await self._wait_network_idle(page)

        # 检查是否已登录
        if "/login" not in page.url:
            self.logger.info("已处于登录状态")
            self._logged_in = True
            return

        # 填写登录表单 (Element UI)
        await page.locator('input.el-input__inner:visible').first.fill(
            self._creds["username"]
        )
        await page.locator('input[type="password"]:visible').first.fill(
            self._creds["password"]
        )
        await page.locator('button:has-text("登录")').first.click()

        # 等待登录完成（URL 不再包含 /login）
        for _ in range(30):
            await page.wait_for_timeout(500)
            if "/login" not in page.url:
                break

        self._logged_in = True
        self.logger.info("主站登录成功: %s", page.url)

    async def _navigate_to_school_ops(self, page: Page):
        """导航到学校运维页面

        先尝试直接跳转 URL，如果会话丢失则通过 UI 导航。
        对网络超时自动重试。
        """
        self.logger.info("导航到学校运维...")

        # 方式1：直接跳转（带重试，应对 net::ERR_CONNECTION_TIMED_OUT）
        last_error = None
        for attempt in range(3):
            try:
                await page.goto(
                    SCHOOL_OPS_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                last_error = None
                break
            except Exception as e:
                last_error = e
                err_msg = str(e)
                if "TIMED_OUT" in err_msg or "CONNECTION" in err_msg:
                    self.logger.warning(
                        "导航超时 (第%d次): %s", attempt + 1, err_msg[:200])
                    if attempt < 2:
                        wait_sec = (attempt + 1) * 5
                        self.logger.info("等待 %d 秒后重试...", wait_sec)
                        await page.wait_for_timeout(wait_sec * 1000)
                else:
                    raise
        if last_error:
            raise last_error

        await page.wait_for_timeout(1500)
        await self._wait_network_idle(page, timeout=15000)

        # 检查是否被重定向（登录页 或 意外回到首页 #/index）
        if "login" in page.url or "redirect" in page.url or "/#/index" in page.url:
            self.logger.info("页面被重定向到 %s，尝试通过 UI 导航...", page.url)
            # 回到主站首页
            await page.goto(self._creds["url"].replace("/platform/login", "/index"),
                           wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # 通过 UI 点击导航
            try:
                await page.locator('text=我的工作台').first.click(timeout=10000)
                await page.wait_for_timeout(3000)
            except Exception:
                pass

            # 可能需要在新页面操作
            if "operation" in page.url:
                await page.locator('text=账户管理').first.click(timeout=10000)
                await page.wait_for_timeout(1000)
                await page.locator('text=学校运维').first.click(timeout=10000)
                await page.wait_for_timeout(1500)

            # 如果仍然不在学校运维页面，重试一次直接导航
            if "operation" not in page.url and "/#/index" in page.url:
                self.logger.info("UI 导航未到达学校运维，重试直接导航...")
                await page.goto(SCHOOL_OPS_URL,
                               wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2000)
                await self._wait_network_idle(page, timeout=10000)

        await self._wait_network_idle(page, timeout=10000)
        self.logger.info("学校运维页面: %s", page.url)

    async def _search_school(self, page: Page, school_name: str):
        """在学校运维中搜索学校"""
        self.logger.info("搜索学校: %s", school_name)
        # 搜索框: el-input__inner，placeholder 含"账号"或"名称"
        search_input = page.locator('input.el-input__inner:visible').first
        # 等待搜索框出现（最多 15 秒），如果出现超时则重试导航
        try:
            await search_input.wait_for(state="visible", timeout=15000)
        except Exception:
            self.logger.warning("搜索框未出现，当前 URL: %s，尝试重新导航...", page.url)
            await page.goto(SCHOOL_OPS_URL,
                           wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
            await self._wait_network_idle(page, timeout=10000)
            search_input = page.locator('input.el-input__inner:visible').first
        await search_input.click()
        await search_input.fill(school_name)
        await page.wait_for_timeout(300)

        # 点击搜索按钮或按 Enter
        try:
            await page.locator('button:has-text("搜索")').first.click(timeout=3000)
        except Exception:
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(1500)
        await self._wait_network_idle(page, timeout=10000)
        self.logger.info("搜索完成")

    async def _one_click_login(self, page: Page) -> Page:
        """
        点击一键登录。会弹出确认弹窗，点确认后可能打开新标签页或同页面导航。
        返回活动页面（可能是新标签或当前页面）。
        """
        self.logger.info("点击一键登录...")

        # 1. 点击"一键登录"链接
        await page.locator('text=一键登录').first.click(timeout=5000)
        await page.wait_for_timeout(1000)

        # 2. 等待确认弹窗出现并点击"确认"
        try:
            # Element UI MessageBox 弹窗
            msgbox = page.locator('.el-message-box__wrapper')
            await msgbox.wait_for(state="visible", timeout=5000)
            self.logger.info("确认弹窗已出现")

            # 点击"确认"按钮 — 用 CSS class 定位（el-button--primary 在 MessageBox 中即确认按钮）
            confirm_btn = msgbox.locator('.el-button--primary')
            await confirm_btn.wait_for(state="visible", timeout=3000)

            # 点击确认后可能打开新标签页
            try:
                async with page.context.expect_page(timeout=5000) as new_page_info:
                    await confirm_btn.click()

                new_page = await new_page_info.value
                await new_page.wait_for_load_state("domcontentloaded")
                await new_page.wait_for_timeout(500)
                self.logger.info("新标签页打开: %s", new_page.url)
                return new_page
            except Exception:
                self.logger.info("未打开新标签页，同页面导航")

            # 同页面导航的情况
            await page.wait_for_timeout(500)
            await self._wait_network_idle(page, timeout=8000)
            self.logger.info("同页面导航后: %s", page.url)
            return page

        except Exception as e:
            self.logger.warning("确认弹窗未出现或处理失败: %s", e)
            # 兜底: 直接等待页面变化
            await page.wait_for_timeout(500)
            await self._wait_network_idle(page, timeout=8000)
            self.logger.info("兜底处理后: %s", page.url)
            return page

    async def _navigate_to_exam_management(self, page: Page):
        """导航到考试管理页面"""
        self.logger.info("导航到考试阅卷系统...")

        await page.wait_for_timeout(300)

        # 点击"考试阅卷系统"
        try:
            # 检查是否会打开新标签
            async with page.context.expect_page(timeout=5000) as new_page_info:
                await page.locator('text=考试阅卷系统').first.click(timeout=8000)
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("domcontentloaded")
            await new_page.wait_for_timeout(500)
            self.logger.info("考试阅卷系统在新标签页: %s", new_page.url)
            page = new_page
        except Exception:
            await page.wait_for_timeout(500)
            self.logger.info("考试阅卷系统同页面: %s", page.url)

        # 查找并点击"考试管理"
        try:
            await page.locator('text=考试管理').first.click(timeout=8000)
            await page.wait_for_timeout(500)
            await self._wait_network_idle(page, timeout=10000)
            self.logger.info("考试管理页面: %s", page.url)
        except Exception as e:
            self.logger.warning("点击'考试管理'失败: %s", e)

        return page

    async def _set_filters_and_query(self, page: Page, start_date: date, end_date: date):
        """设置筛选条件并查询"""
        self.logger.info("设置筛选条件: %s ~ %s", start_date, end_date)

        # 1. 通过 JavaScript 设置 Vue 模型日期值（WdatePicker 输入框是 readonly）
        #    使用 Vue 的响应式 API 确保变更被检测到
        try:
            result = await page.evaluate("""(dates) => {
                const info = {method: '', startDate: '', endDate: ''};
                if (typeof vmApp !== 'undefined' && vmApp.examSearch) {
                    // 方式1: 使用 Vue 实例的 $set 确保响应式更新
                    if (vmApp.$set) {
                        vmApp.$set(vmApp.examSearch, 'startDate', dates.start);
                        vmApp.$set(vmApp.examSearch, 'endDate', dates.end);
                        info.method = 'Vue.$set';
                    } else {
                        // 方式2: 通过 __ob__ 触发响应式通知
                        vmApp.examSearch.startDate = dates.start;
                        vmApp.examSearch.endDate = dates.end;
                        if (vmApp.examSearch.__ob__) {
                            vmApp.examSearch.__ob__.dep.notify();
                        }
                        info.method = 'direct + __ob__.notify';
                    }
                    info.startDate = vmApp.examSearch.startDate;
                    info.endDate = vmApp.examSearch.endDate;

                    // 同步更新 DOM 输入框的值并触发事件
                    const startInput = document.querySelector('input[name="startDate"]');
                    const endInput = document.querySelector('input[name="endDate"]');
                    if (startInput) {
                        startInput.value = dates.start;
                        startInput.dispatchEvent(new Event('input', {bubbles: true}));
                        startInput.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    if (endInput) {
                        endInput.value = dates.end;
                        endInput.dispatchEvent(new Event('input', {bubbles: true}));
                        endInput.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                }
                return info;
            }""", {"start": start_date.strftime("%Y-%m-%d"), "end": end_date.strftime("%Y-%m-%d")})
            self.logger.info("日期已通过 JS 设置: method=%s, startDate=%s, endDate=%s",
                           result.get('method', '?'), result.get('startDate', '?'), result.get('endDate', '?'))
        except Exception as e:
            self.logger.warning("JS 设置日期失败: %s", e)
            # 兜底：尝试通过 DOM 直接设置
            try:
                await page.evaluate("""(dates) => {
                    const startInput = document.querySelector('input[name="startDate"]');
                    const endInput = document.querySelector('input[name="endDate"]');
                    if (startInput) startInput.value = dates.start;
                    if (endInput) endInput.value = dates.end;
                }""", {"start": start_date.strftime("%Y-%m-%d"), "end": end_date.strftime("%Y-%m-%d")})
            except Exception as e2:
                self.logger.warning("兜底日期设置也失败: %s", e2)

        # 2. 点击场景筛选的"作业"按钮（SPAN.li 元素，在 DIV.search-btns.s4 内）
        try:
            hw_btn = page.locator('.search-btns.s4 span.li:has-text("作业")').first
            await hw_btn.click(timeout=5000)
            self.logger.info("已选择场景=作业")
        except Exception as e:
            self.logger.warning("选择场景'作业'失败: %s", e)
            # 兜底：通过 JS 点击
            try:
                await page.evaluate("""
                    const spans = document.querySelectorAll('.search-btns.s4 span.li');
                    for (const s of spans) {
                        if (s.textContent.trim() === '作业') { s.click(); break; }
                    }
                """)
                self.logger.info("已通过 JS 点击'作业'")
            except Exception:
                pass

        # 2.5 等待"分类"筛选栏出现，然后点击"手阅作业"
        await page.wait_for_timeout(1000)
        try:
            # 分类筛选栏在场景筛选栏之后，class 含 search_gx
            sy_btn = page.locator('.search-btns.s4.search_gx span.li:has-text("手阅作业")').first
            await sy_btn.click(timeout=5000)
            self.logger.info("已选择分类=手阅作业")
        except Exception as e:
            self.logger.warning("选择分类'手阅作业'失败: %s", e)
            # 兜底：通过 JS 点击
            try:
                await page.evaluate("""
                    const divs = document.querySelectorAll('.search-btns.s4.search_gx');
                    for (const div of divs) {
                        const spans = div.querySelectorAll('span.li');
                        for (const s of spans) {
                            if (s.textContent.trim() === '手阅作业') { s.click(); return; }
                        }
                    }
                """)
                self.logger.info("已通过 JS 点击'手阅作业'")
            except Exception:
                pass

        # 3. 点击搜索按钮
        try:
            search_btn = page.locator('button:visible:has-text("搜索")').first
            await search_btn.click(timeout=5000)
            self.logger.info("已点击搜索")
        except Exception as e:
            self.logger.warning("点击搜索失败: %s, 尝试 JS 触发", e)
            try:
                await page.evaluate("""
                    if (typeof vmApp !== 'undefined' && vmApp.searchExam) {
                        vmApp.searchExam();
                    }
                """)
                self.logger.info("已通过 JS 触发搜索")
            except Exception:
                pass

        await page.wait_for_timeout(3000)
        await self._wait_network_idle(page)

    async def _extract_homework_count(self, page: Page) -> str:
        """提取作业场次数"""
        # 诊断: 记录当前页面 URL
        self.logger.info("提取作业次数，当前 URL: %s", page.url)

        # 等待表格或结果区域加载
        try:
            await page.wait_for_selector(
                'table, .el-table, [class*="table"], [class*="list"], [class*="result"]',
                timeout=8000,
            )
        except Exception:
            self.logger.warning("等待表格加载超时，尝试继续提取")

        # 再给一点时间让数据刷新完成
        await page.wait_for_timeout(2000)

        # 方式1: 通过正则匹配 "共计xx场"
        body_text = ""
        try:
            body_text = await page.locator("body").text_content() or ""
            match = re.search(r"共计\s*(\d+)\s*场", body_text)
            if match:
                val = match.group(1)
                self.logger.info("方式1匹配: 共计%s场", val)
                return val
        except Exception as e:
            self.logger.warning("方式1失败: %s", e)

        # 方式2: 匹配 "共xx条" 分页信息（Element UI 分页组件）
        try:
            if not body_text:
                body_text = await page.locator("body").text_content() or ""
            match = re.search(r"共\s*(\d+)\s*[条场页]", body_text)
            if match:
                val = match.group(1)
                self.logger.info("方式2匹配: 共%s条", val)
                return val
        except Exception as e:
            self.logger.warning("方式2失败: %s", e)

        # 方式3: 查找 el-pagination 组件的 total 属性或文本
        try:
            pagination = await page.query_selector('.el-pagination, [class*="pagination"]')
            if pagination:
                p_text = await pagination.text_content() or ""
                self.logger.info("分页组件文本: '%s'", p_text[:200])
                # 尝试匹配"共 X 条"
                match = re.search(r"共\s*(\d+)\s*[条场页]", p_text)
                if match:
                    val = match.group(1)
                    self.logger.info("方式3a匹配: 分页共%s条", val)
                    return val
                # 尝试从 total 属性读取
                total = await pagination.get_attribute("data-total")
                if total and total.isdigit():
                    self.logger.info("方式3b匹配: data-total=%s", total)
                    return total
        except Exception as e:
            self.logger.warning("方式3失败: %s", e)

        # 方式4: 通过 Vue 实例读取 total（适用于 Element UI 分页绑定）
        try:
            vue_total = await page.evaluate("""() => {
                // 尝试从 vmApp 读取分页数据
                if (typeof vmApp !== 'undefined') {
                    // 常见分页属性名
                    const keys = ['total', 'pageTotal', 'count',
                                  'examTotal', 'listTotal', 'totalCount'];
                    for (const k of keys) {
                        if (vmApp[k] !== undefined && typeof vmApp[k] === 'number') {
                            return {source: 'vmApp.' + k, value: vmApp[k]};
                        }
                    }
                    // 搜索嵌套对象
                    for (const prop of Object.keys(vmApp)) {
                        const val = vmApp[prop];
                        if (val && typeof val === 'object') {
                            for (const k2 of Object.keys(val)) {
                                if (k2.toLowerCase().includes('total')
                                    || k2.toLowerCase().includes('count')) {
                                    if (typeof val[k2] === 'number') {
                                        return {source: 'vmApp.' + prop + '.' + k2,
                                                value: val[k2]};
                                    }
                                }
                            }
                        }
                    }
                }
                return null;
            }""")
            if vue_total and isinstance(vue_total, dict):
                val = str(vue_total.get("value", ""))
                self.logger.info("方式4匹配: Vue %s = %s",
                               vue_total.get("source", "?"), val)
                return val
        except Exception as e:
            self.logger.warning("方式4(Vue)失败: %s", e)

        # 方式5: 计算表格行数（排除表头行）
        try:
            row_count = await page.evaluate("""() => {
                // Element UI 表格
                const elRows = document.querySelectorAll(
                    '.el-table__body-wrapper .el-table__body tbody tr');
                if (elRows.length > 0) return elRows.length;
                // 标准表格
                const tables = document.querySelectorAll('table');
                for (const t of tables) {
                    const rows = t.querySelectorAll('tbody tr');
                    if (rows.length > 0) return rows.length;
                }
                return -1;
            }""")
            if row_count > 0:
                self.logger.info("方式5匹配: 表格行数 = %d", row_count)
                return str(row_count)
        except Exception as e:
            self.logger.warning("方式5(表格行数)失败: %s", e)

        # 诊断: 输出页面关键文本帮助排查
        try:
            if not body_text:
                body_text = await page.locator("body").text_content() or ""
            # 查找包含数字和"场"/"条"/"次"的片段
            snippets = re.findall(r'.{0,20}\d+\s*[场次条页].{0,20}', body_text)
            if snippets:
                self.logger.info("页面中包含数字的片段: %s", snippets[:10])
            else:
                # 输出页面文本的前 500 字符
                clean = re.sub(r'\s+', ' ', body_text).strip()
                self.logger.info("页面文本前500字: %s", clean[:500])
        except Exception as e:
            self.logger.warning("诊断输出失败: %s", e)

        self.logger.warning("所有方式均未匹配到作业次数")
        return ""

    @with_retry(max_attempts=2, backoff_base=2.0)
    async def scrape(self, school: dict, date_range: tuple) -> dict:
        """
        采集主站数据

        返回: {
            "homework_count": "作业次数（班级累加）",
        }
        """
        if not self._logged_in:
            await self.login()

        page = await self._get_page()
        start_date, end_date = date_range
        school_name = school.get("main_site_name", school["name"])

        result = {"homework_count": ""}
        new_page = None

        try:
            # 1. 导航到学校运维
            await self._navigate_to_school_ops(page)

            # 2. 搜索学校
            await self._search_school(page, school_name)

            # 3. 一键登录
            active_page = await self._one_click_login(page)
            if active_page != page:
                new_page = active_page

            # 4. 导航到考试管理
            exam_page = await self._navigate_to_exam_management(active_page)

            # 5. 设置筛选并查询
            await self._set_filters_and_query(exam_page, start_date, end_date)

            # 6. 提取作业场次
            result["homework_count"] = await self._extract_homework_class_count(exam_page)

            self.logger.info("主站数据采集完成: %s", result)

        except Exception as e:
            self.logger.error("主站数据采集失败: %s", e, exc_info=True)
            raise
        finally:
            # 关闭所有考试相关标签页，找到并保留学校运维页面
            if self._context:
                ops_page = None
                for p in self._context.pages:
                    if p.is_closed():
                        continue
                    if "operation" in p.url:
                        ops_page = p
                    else:
                        try:
                            await p.close()
                        except Exception:
                            pass
                if ops_page:
                    self._page = ops_page
                elif self._page and not self._page.is_closed():
                    # self._page 不是运维页面，尝试导航回去
                    try:
                        await self._page.goto(SCHOOL_OPS_URL,
                                              wait_until="domcontentloaded", timeout=30000)
                        await self._page.wait_for_timeout(1000)
                    except Exception:
                        pass

        return result


    async def _extract_single_page(self, page: Page, page_num: int) -> dict:
        """提取单页作业数据（翻页时使用）"""
        try:
            result = await page.evaluate(r"""() => {
                let total = 0;
                let cardCount = 0;
                const bodyText = document.body.innerText || '';
                const entries = bodyText.split(/(?=[「\[][^」\]]+[」\]])/).filter(p => /[「\[][^」\]]+[」\]]/.test(p));
                for (const entry of entries) {
                    const classMatch = entry.match(/班级[：:]\s*([\s\S]*?)(?=(?:科目|状态|时间[：:])|$)/);
                    if (classMatch) {
                        const classList = classMatch[1].trim();
                        const classes = classList.split(/[、，,

]+/).filter(s => {
                            const t = s.trim();
                            return t.length > 0 && t.includes('班');
                        });
                        total += classes.length || 1;
                    } else {
                        total += 1;
                    }
                    cardCount++;
                }
                return {total, cardCount};
            }""")
            self.logger.info("第%d页提取: total=%s, cards=%s",
                           page_num, result.get("total", 0), result.get("cardCount", 0))
            return result
        except Exception as e:
            self.logger.warning("第%d页提取失败: %s", page_num, e)
            return {}

    async def _handle_pagination(self, page: Page):
        """处理考试管理页面分页，尽量将所有作业显示在一页"""
        try:
            # 检测分页组件
            pagination_info = await page.evaluate(r"""() => {
                // Element UI 分页
                const pagination = document.querySelector('.el-pagination');
                if (!pagination) {
                    // 也检查其他常见分页样式
                    const pager = document.querySelector('[class*="pagination"], [class*="pager"], .ant-pagination');
                    if (!pager) return { hasPagination: false };
                }

                const el = pagination || document.querySelector('[class*="pagination"], [class*="pager"], .ant-pagination');
                // 获取总条数和总页数
                const totalEl = el.querySelector('.el-pagination__total') || el.querySelector('[class*="total"]');
                const totalText = totalEl ? totalEl.textContent : '';

                // 分别匹配 "共X条" 和 "共X页"
                let totalItems = 0, totalPages = 0;
                const itemsMatch = totalText.match(/共\s*(\d+)\s*条/);
                const pagesMatch = totalText.match(/共\s*(\d+)\s*页/);
                if (itemsMatch) totalItems = parseInt(itemsMatch[1]);
                if (pagesMatch) totalPages = parseInt(pagesMatch[1]);
                // 兜底: 如果都没匹配到，取文本中最大的数字
                if (!totalItems && !totalPages) {
                    const nums = totalText.match(/\d+/g);
                    if (nums) totalItems = Math.max(...nums.map(Number));
                }

                // 获取当前每页条数
                const sizeBtn = el.querySelector('.el-pagination__sizes .el-input input, [class*="sizes"] input');
                const currentSize = sizeBtn ? sizeBtn.value || sizeBtn.textContent : '';

                // 获取当前页码
                const activePage = el.querySelector('.el-pager .number.active, .el-pager li.active');
                const currentPage = activePage ? parseInt(activePage.textContent) || 1 : 1;

                // 检查下一页按钮
                const nextBtn = el.querySelector('.btn-next, [class*="next"]');
                const isNextDisabled = nextBtn ? nextBtn.disabled || nextBtn.classList.contains('disabled') : true;

                // 综合判断是否有下一页
                let hasNextPage = !isNextDisabled;
                if (!hasNextPage && totalPages > 0 && currentPage < totalPages) {
                    hasNextPage = true;
                }
                if (!hasNextPage && totalItems > 0 && currentSize) {
                    const pageSize = parseInt(currentSize) || 10;
                    if (totalItems > currentPage * pageSize) hasNextPage = true;
                }

                return {
                    hasPagination: true,
                    totalItems,
                    totalPages,
                    currentPage,
                    currentSize: currentSize.trim(),
                    hasNextPage,
                    paginationText: totalText.trim()
                };
            }""")

            if not pagination_info or not pagination_info.get("hasPagination"):
                self.logger.info("未检测到分页组件，跳过分页处理")
                return

            total_items = pagination_info.get("totalItems", 0)
            total_pages = pagination_info.get("totalPages", 0)
            current_page = pagination_info.get("currentPage", 1)
            current_size = pagination_info.get("currentSize", "")
            has_next = pagination_info.get("hasNextPage", False)
            pag_text = pagination_info.get("paginationText", "")

            self.logger.info("分页信息: 总条数=%d, 总页数=%d, 当前页=%d, 每页=%s, 有下一页=%s, 文本='%s'",
                           total_items, total_pages, current_page, current_size, has_next, pag_text)

            # 用总页数判断是否需要分页（比总条数更可靠）
            if total_pages > 1 or has_next:
                self.logger.info("检测到多页(%d页)，需要分页处理", total_pages)
            elif total_items <= 10 and total_pages <= 1:
                self.logger.info("数据量≤10条且仅1页，无需分页处理")
                return
            elif not has_next and total_pages <= 1:
                self.logger.info("单页数据，无需分页处理")
                return

            # 方案A: 尝试修改每页显示条数为最大值
            size_changed = await self._try_change_page_size(page)
            if size_changed:
                self.logger.info("已修改每页显示条数，等待数据重新加载...")
                await page.wait_for_timeout(3000)
                return

            # 方案B: 如果无法修改每页条数，后续在提取时翻页累加
            self.logger.info("无法修改每页条数，将使用翻页方式采集（总条数=%d）", total_items)
            # 标记需要翻页（通过页面变量传递）
            await page.evaluate("window.__needs_pagination = true;")

        except Exception as e:
            self.logger.warning("分页处理失败: %s", e)

    async def _try_change_page_size(self, page: Page) -> bool:
        """尝试将 Element UI 分页的每页条数改为最大值"""
        try:
            # 点击 sizes 下拉框
            sizes_input = page.locator('.el-pagination__sizes .el-input input, .el-pagination .el-select .el-input input')
            if await sizes_input.count() == 0:
                self.logger.info("未找到每页条数选择器")
                return False

            await sizes_input.first.click()
            await page.wait_for_timeout(500)

            # 在下拉列表中找最大的选项（通常是"100条/页"或"50条/页"）
            max_size = await page.evaluate(r"""() => {
                const items = document.querySelectorAll('.el-select-dropdown__item, .el-select-dropdown__list li');
                let maxVal = 0;
                let maxText = '';
                for (const item of items) {
                    const text = item.textContent.trim();
                    const match = text.match(/(\d+)/);
                    if (match) {
                        const val = parseInt(match[1]);
                        if (val > maxVal) {
                            maxVal = val;
                            maxText = text;
                        }
                    }
                }
                return { maxVal, maxText };
            }""")

            if max_size and max_size.get("maxVal", 0) > 0:
                max_text = max_size["maxText"]
                self.logger.info("选择最大每页条数: %s", max_text)
                # 点击该选项
                option = page.locator(f'.el-select-dropdown__item:has-text("{max_text}"), .el-select-dropdown__list li:has-text("{max_text}")')
                if await option.count() > 0:
                    await option.first.click()
                    await page.wait_for_timeout(2000)
                    return True

            # 如果下拉列表找不到，按 Escape 关闭
            await page.keyboard.press("Escape")
            return False

        except Exception as e:
            self.logger.warning("修改每页条数失败: %s", e)
            return False

    async def _extract_homework_class_count(self, page: Page) -> str:
        """提取月度作业次数（按班级数累加）

        每条作业以卡片形式展示，班级信息格式如：
        "班级：1班、2班、8班、6班" 或 "班级：1班"
        需要从每个卡片中提取班级列表，按顿号分隔计数后累加。
        """
        self.logger.info("提取月度作业次数（班级累加），URL: %s", page.url)

        # 等待页面内容加载
        try:
            await page.wait_for_selector(
                'table, .el-table, [class*="table"], [class*="list"], [class*="card"], [class*="item"]',
                timeout=8000,
            )
        except Exception:
            self.logger.warning("等待页面加载超时")

        await page.wait_for_timeout(3000)

        # ── 分页处理：尝试将所有作业显示在一页 ──
        await self._handle_pagination(page)

        # 通过 JS 从页面中提取所有作业的班级信息并累加
        result = await page.evaluate(r"""() => {
            let total = 0;
            let cardCount = 0;
            const diagnostics = [];

            // 提取一段文本中的班级数量
            // 匹配 "班级：" 或 "班级:" 后面的班级列表，按顿号/逗号分隔
            function extractClassCount(text) {
                const match = text.match(/班级[：:]\s*(.+?)(?=(?:科目|状态|$))/s);
                if (!match) return 0;
                const classList = match[1].trim();
                const classes = classList.replace(/[|｜]+/g, '').split(/[、，,]+/).filter(s => {
                    const t = s.trim();
                    return t.length > 0 && t.includes('班');
                });
                return classes.length;
            }

            // 策略1: Element UI 表格行
            const elRows = document.querySelectorAll(
                '.el-table__body-wrapper .el-table__body tbody tr');
            if (elRows.length > 0) {
                for (const row of elRows) {
                    cardCount++;
                    const fullText = row.textContent || '';
                    const cnt = extractClassCount(fullText);
                    if (cnt > 0) total += cnt;
                    if (cardCount <= 5) {
                        diagnostics.push({card: cardCount, text: fullText.substring(0, 200), classCount: cnt});
                    }
                }
                if (total > 0) return {total, cardCount, method: 'el-table', diagnostics};
            }

            // 策略2: 标准表格行
            const tables = document.querySelectorAll('table');
            for (const table of tables) {
                const rows = table.querySelectorAll('tbody tr');
                if (rows.length === 0) continue;
                for (const row of rows) {
                    cardCount++;
                    const fullText = row.textContent || '';
                    const cnt = extractClassCount(fullText);
                    if (cnt > 0) total += cnt;
                    if (cardCount <= 5) {
                        diagnostics.push({card: cardCount, text: fullText.substring(0, 200), classCount: cnt});
                    }
                }
                if (total > 0) return {total, cardCount, method: 'standard-table', diagnostics};
            }

            // 策略3: 卡片/列表项
            cardCount = 0;
            total = 0;
            const allElements = document.querySelectorAll(
                '[class*="card"], [class*="item"], [class*="row"], [class*="list"] li, .el-table__row'
            );
            for (const el of allElements) {
                const text = el.textContent || '';
                if (!text.includes('班级')) continue;
                const classMatches = text.match(/班级[：:]/g);
                if (!classMatches || classMatches.length > 1) continue;
                cardCount++;
                const cnt = extractClassCount(text);
                if (cnt > 0) total += cnt;
                if (cardCount <= 5) {
                    diagnostics.push({card: cardCount, text: text.substring(0, 200), classCount: cnt});
                }
            }
            if (total > 0) return {total, cardCount, method: 'card-list', diagnostics};

            // 策略4: 全文扫描 — 按"班级"关键字分割，跨行不截断
            cardCount = 0;
            total = 0;
            const bodyText = document.body.innerText || '';
            const parts = bodyText.split(/(?=班级[：:])/);
            for (const part of parts) {
                if (!part.includes('班级')) continue;
                cardCount++;
                // 匹配"班级："后面到"科目"/"状态"/下一个"班级"之前的所有内容（含换行）
                const match = part.match(/班级[：:]\s*([\s\S]*?)(?=(?:科目|状态|班级[：:])|$)/);
                if (!match) continue;
                const classList = match[1].trim();
                const classes = classList.replace(/[|｜]+/g, '').split(/[、，,\n\r]+/).filter(s => {
                    const t = s.trim();
                    return t.length > 0 && t.includes('班');
                });
                const cnt = classes.length;
                if (cnt > 0) total += cnt;
                // 所有卡片都记录，文本不截断
                diagnostics.push({card: cardCount, line: classList.replace(/\s+/g, ' ').substring(0, 500), classCount: cnt, raw: part.substring(0, 300)});
            }
            // 策略5: 用「」标题分割作业条目（每条作业都有唯一标题）
            // 用「...」作为作业边界，过滤掉页面头部/底部干扰文本
            const titleMatches = bodyText.match(/[「\[][^」\]]+[」\]]/g) || [];
            const entries = bodyText.split(/(?=[「\[][^」\]]+[」\]])/).filter(p => /[「\[][^」\]]+[」\]]/.test(p));
            const timeCount = entries.length;
            let correctedTotal = 0;
            for (const entry of entries) {
                // 在每条作业中查找班级列表
                const classMatch = entry.match(/班级[：:]\s*([\s\S]*?)(?=(?:科目|状态|时间[：:])|$)/);
                if (classMatch) {
                    const classList = classMatch[1].trim();
                    const classes = classList.replace(/[|｜]+/g, '').split(/[、，,\n\r]+/).filter(s => {
                        const t = s.trim();
                        return t.length > 0 && t.includes('班');
                    });
                    correctedTotal += classes.length || 1;
                } else {
                    // 没有班级字段的作业，计为1次
                    correctedTotal += 1;
                }
            }
            // 逐条诊断
            let diagIdx = 0;
            for (const entry of entries) {
                diagIdx++;
                const cm = entry.match(/班级[：:]\s*([\s\S]*?)(?=(?:科目|状态|时间[：:])|$)/);
                const entryTitle = entry.match(/[「\[]([^」\]]+)[」\]]/);
                const title = entryTitle ? entryTitle[1].substring(0, 40) : '(no title)';
                const classText = cm ? cm[1].replace(/\s+/g, ' ').substring(0, 100) : '(no class field)';
                const classCount = cm ? cm[1].replace(/[|｜]+/g, '').split(/[、，,\n\r]+/).filter(s => s.trim().length > 0 && s.trim().includes('班')).length : 1;
                diagnostics.push({s5: true, idx: diagIdx, title, classText, classCount: classCount || 1});
            }
            diagnostics.push({note: 'strategy5: timeCount=' + timeCount + ', classTotal=' + total + ', correctedTotal=' + correctedTotal});
            if (correctedTotal > total) {
                total = correctedTotal;
            }
            return {total, cardCount, method: 'full-text', diagnostics, timeCount};
        }""")

        self.logger.info("月度作业统计(第1页): %s", result)

        # ── 翻页累加：如果分页未被消除，逐页提取并累加 ──
        needs_pagination = await page.evaluate("window.__needs_pagination || false")
        grand_total = result.get("total", 0) if result else 0
        grand_cards = result.get("cardCount", 0) if result else 0
        page_num = 1

        if needs_pagination and grand_total > 0:
            self.logger.info("检测到需要翻页采集，开始逐页累加...")
            while True:
                # 检查是否有下一页且可点击
                has_next = await page.evaluate(r"""() => {
                    const pagination = document.querySelector('.el-pagination');
                    if (!pagination) return false;
                    const nextBtn = pagination.querySelector('.btn-next');
                    return nextBtn && !nextBtn.disabled && !nextBtn.classList.contains('disabled');
                }""")
                if not has_next:
                    self.logger.info("已到最后一页(第%d页)，翻页结束", page_num)
                    break

                # 点击下一页
                await page.locator('.el-pagination .btn-next').first.click()
                page_num += 1
                await page.wait_for_timeout(3000)
                self.logger.info("翻到第%d页", page_num)

                # 提取当前页数据
                page_result = await self._extract_single_page(page, page_num)
                if page_result and isinstance(page_result, dict):
                    page_total = page_result.get("total", 0)
                    page_cards = page_result.get("cardCount", 0)
                    if page_total > 0:
                        grand_total += page_total
                        grand_cards += page_cards
                        self.logger.info("第%d页: +%d (班级), 累计=%d",
                                       page_num, page_total, grand_total)
                    else:
                        self.logger.warning("第%d页提取结果为0，可能页面未加载完成", page_num)

                if page_num >= 20:  # 安全限制，最多20页
                    self.logger.warning("达到20页上限，停止翻页")
                    break

        if result and isinstance(result, dict):
            diags = result.get("diagnostics", [])
            if diags:
                self.logger.info("卡片诊断（共%d条）:", len(diags))
                for d in diags:
                    self.logger.info("  card %s: classCount=%s, line=%s",
                                   d.get("card", "?"), d.get("classCount", "?"),
                                   d.get("text", d.get("line", "")))
            if grand_total > 0:
                if needs_pagination:
                    self.logger.info("班级累加结果: %d (共 %d 条作业, %d 页翻页累加)",
                                   grand_total, grand_cards, page_num)
                else:
                    self.logger.info("班级累加结果: %d (共 %d 条作业, 方法: %s)",
                                   grand_total, result.get("cardCount", 0),
                                   result.get("method", "?"))
                return str(grand_total)

        # 兜底: 用周表的"共计xx场"方式获取作业条数
        self.logger.info("班级累加为0，尝试获取作业条数作为兜底...")
        fallback_count = await self._extract_homework_count(page)
        if fallback_count:
            self.logger.info("兜底: 使用作业条数 %s", fallback_count)
            return fallback_count

        return ""

    @with_retry(max_attempts=2, backoff_base=2.0)
    async def scrape_monthly(self, school: dict, date_range: tuple) -> dict:
        """
        采集主站月度数据（作业次数按班级数累加）

        返回: {
            "homework_count": "作业次数（班级累加）",
        }
        """
        if not self._logged_in:
            await self.login()

        page = await self._get_page()
        start_date, end_date = date_range
        school_name = school.get("main_site_name", school["name"])

        result = {"homework_count": ""}
        new_page = None

        try:
            # 1. 导航到学校运维
            await self._navigate_to_school_ops(page)

            # 2. 搜索学校
            await self._search_school(page, school_name)

            # 3. 一键登录
            active_page = await self._one_click_login(page)
            if active_page != page:
                new_page = active_page

            # 4. 导航到考试管理
            exam_page = await self._navigate_to_exam_management(active_page)

            # 5. 设置筛选并查询（场景=作业）
            await self._set_filters_and_query(exam_page, start_date, end_date)

            # 6. 提取作业次数（按班级累加）
            result["homework_count"] = await self._extract_homework_class_count(exam_page)

            self.logger.info("主站月度数据采集完成: %s", result)

        except Exception as e:
            self.logger.error("主站月度数据采集失败: %s", e, exc_info=True)
            raise
        finally:
            # 关闭所有考试相关标签页，找到并保留学校运维页面
            if self._context:
                ops_page = None
                for p in self._context.pages:
                    if p.is_closed():
                        continue
                    if "operation" in p.url:
                        ops_page = p
                    else:
                        try:
                            await p.close()
                        except Exception:
                            pass
                if ops_page:
                    self._page = ops_page
                elif self._page and not self._page.is_closed():
                    # self._page 不是运维页面，尝试导航回去
                    try:
                        await self._page.goto(SCHOOL_OPS_URL,
                                              wait_until="domcontentloaded", timeout=30000)
                        await self._page.wait_for_timeout(1000)
                    except Exception:
                        pass

        return result
