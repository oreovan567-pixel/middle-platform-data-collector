"""Metabase HTTP API 直连采集器（替代原 Lida 浏览器采集）

通过 Metabase REST API 查询 Dashboard 6 卡片数据：
  - POST /api/session          获取 session token
  - POST /api/card/{id}/query  查询卡片数据（带筛选参数）

Dashboard 6 - 平台使用统计：
  卡片 244: 平台总体数据 → [使用率, 访问次数, 平均访问次数]
  卡片 246: 集备访问数据 → [使用率, 访问次数, 平均访问次数]
  卡片 248: 组卷访问数据 → [使用率, 访问次数, 平均访问次数]
  卡片 249: 手阅作业访问数据 → [使用率, 答题卡扫描数量]
  卡片 254: 教师详情 → 20列×N行（可按学段/年级过滤）
"""
from __future__ import annotations
import asyncio
import logging
from datetime import date
from typing import Any

import aiohttp

from config.config_loader import get_credentials

logger = logging.getLogger(__name__)

# Dashboard 6 卡片 ID
CARD_PLATFORM_OVERALL = 244   # 平台总体数据 → [使用率, 访问次数, 平均访问次数]
CARD_GEBEI = 245            # 个备访问数据 → [使用率, 访问次数, 平均访问次数]
CARD_JIBEI = 246            # 集备访问数据 → [使用率, 访问次数, 平均访问次数]
CARD_ZUJUAN = 248           # 组卷访问数据 → [使用率, 访问次数, 平均访问次数]
CARD_HANDWRITTEN = 249      # 手阅作业访问数据 → [使用率, 答题卡扫描数量]
CARD_TEACHER_DETAIL = 254   # 教师详情 → 多列×N行（可按学段/年级过滤）
CARD_STAGES = 255           # Education Stages → 学段映射表
CARD_INTERNAL = 353         # 平台使用数据（内部员工）→ [使用率, 访问次数, 平均访问次数]

# Dashboard 21（中台使用总览-LGH）卡片 ID —— 用于计算日活/周活/月活比例
CARD_D21_UV = 370              # UV（去重访问人数）
CARD_D21_WEEKLY_ACTIVE = 368   # 周活教师（>=3天去重）
CARD_D21_MONTHLY_ACTIVE = 369  # 月活（>=4天去重）
CARD_D21_TOTAL_TEACHERS = 372  # 总教师人数
CARD_D21_USAGE_RATE = 374      # 平台使用率（支持学段筛选）

# Dashboard 21 活跃指标卡片列表（与 Dashboard 6 模块卡片一起并发查询）
D21_ACTIVE_CARDS = [
    CARD_D21_UV,
    CARD_D21_WEEKLY_ACTIVE,
    CARD_D21_MONTHLY_ACTIVE,
    CARD_D21_TOTAL_TEACHERS,
]

# Dashboard 21 参数 ID（固定值，来自仪表盘 /api/dashboard/21）
D21_PARAM_IDS = {
    "学校": "2724ce9c",
    "学段": "295c5125",
    "开始时间": "c52bd1c5",
    "结束时间": "10703252",
}
# Dashboard 21 dashcard ID → (card_id, card_name)
D21_DASHCARDS = {
    CARD_D21_UV: 475,
    CARD_D21_WEEKLY_ACTIVE: 473,
    CARD_D21_MONTHLY_ACTIVE: 474,
    CARD_D21_TOTAL_TEACHERS: 477,
    CARD_D21_USAGE_RATE: 480,
}

# text widget 卡片（不可查询，Dashboard 6 中仅作静态展示）
# 247=错题本访问数据, 250=学情分析访问数据 → 需通过 SLS 回退获取
TEXT_WIDGET_CARDS = {247, 250}

# 6 个可查询的数据模块卡片 ID 列表（按用户指定顺序排列）
ALL_MODULE_CARDS = [
    CARD_PLATFORM_OVERALL,  # 244: 平台总体数据
    CARD_INTERNAL,          # 353: 平台使用数据（内部员工）
    CARD_GEBEI,             # 245: 个备访问数据
    CARD_JIBEI,             # 246: 集备访问数据
    CARD_ZUJUAN,            # 248: 组卷访问数据
    CARD_HANDWRITTEN,       # 249: 手阅作业访问数据
]

# 完整展示顺序（含 text widget 和附加指标列，用于前端列顺序）
ALL_MODULE_NAMES = [
    "平台总体数据",                    # Card 244
    "平台使用数据（内部员工）",         # Card 353
    "个备访问数据",                    # Card 245
    "集备访问数据",                    # Card 246
    "组卷访问数据",                    # Card 248
    "手阅作业访问数据",                # Card 249
    "学情分析访问数据",                # Card 250 (text widget，可折叠)
    "错题本访问数据",                  # Card 247 (text widget，可折叠)
    "作业次数",                        # 附加：来自月表采集 main_site
    "人均作业次数",                    # 附加：作业次数/教师总数
    "日活比例",                        # 附加：参考月表采集 Grafana/DB
    "周活比例",                        # 附加：参考月表采集 Grafana/DB
    "月活比例",                        # 附加：参考月表采集 Grafana/DB
]

# 模块名称映射
CARD_NAMES = {
    CARD_PLATFORM_OVERALL: "平台总体数据",
    CARD_INTERNAL: "平台使用数据（内部员工）",
    CARD_GEBEI: "个备访问数据",
    CARD_JIBEI: "集备访问数据",
    CARD_ZUJUAN: "组卷访问数据",
    CARD_HANDWRITTEN: "手阅作业访问数据",
}


class ApiLidaScraper:
    """纯 HTTP 方式的 Metabase 采集器（替代 Lida）"""

    def __init__(self):
        self._creds = get_credentials("metabase")
        self._session: aiohttp.ClientSession | None = None
        self._metabase_base: str = self._creds.get("url", "https://metabase.qimingdaren.com").rstrip("/")
        self._dashboard_id: int = int(self._creds.get("dashboard_id", 6))
        self._session_id: str = ""

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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── Metabase 登录 ──

    async def _login(self) -> bool:
        """登录 Metabase 获取 session token"""
        if self._session_id:
            return True

        session = await self._get_session()
        try:
            async with session.post(
                f"{self._metabase_base}/api/session",
                json={
                    "username": self._creds.get("username", ""),
                    "password": self._creds.get("password", ""),
                },
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._session_id = data.get("id", "")
                    if self._session_id:
                        logger.info("[Metabase] 登录成功, session=%s...", self._session_id[:8])
                        return True
                else:
                    text = await resp.text()
                    logger.warning("[Metabase] 登录失败: %d %s", resp.status, text[:200])
        except Exception as e:
            logger.error("[Metabase] 登录异常: %s", e)

        return False

    # ── 卡片查询 ──

    async def _query_card(self, card_id: int, parameters: list | None = None) -> dict:
        """查询 Metabase 卡片"""
        if not self._session_id:
            if not await self._login():
                return {}

        session = await self._get_session()
        headers = {"X-Metabase-Session": self._session_id}

        try:
            payload = {"parameters": parameters or []}
            async with session.post(
                f"{self._metabase_base}/api/card/{card_id}/query",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status in (200, 202):
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.warning("[Metabase] 卡片 %d 查询失败: %d %s",
                                   card_id, resp.status, text[:200])
        except Exception as e:
            logger.error("[Metabase] 卡片 %d 查询异常: %s", card_id, e)

        return {}

    # ── Dashboard 21 dashcard 查询 ──

    async def _query_d21_dashcard(self, card_id: int, school_name: str,
                                   start_date: str, end_date: str) -> str:
        """通过 Dashboard 21 的 dashcard 端点查询卡片（带仪表盘级参数）

        Dashboard 21 的卡片 SQL 是硬编码的学校列表，不支持卡片级参数。
        必须通过仪表盘的 dashcard 端点 + 参数映射来按学校筛选。
        """
        dashcard_id = D21_DASHCARDS.get(card_id)
        if not dashcard_id:
            return ""

        session = await self._get_session()
        headers = {"X-Metabase-Session": self._session_id, "Content-Type": "application/json"}
        url = f"{self._metabase_base}/api/dashboard/21/dashcard/{dashcard_id}/card/{card_id}/query"

        params = [
            {"id": D21_PARAM_IDS["学校"], "type": "string/=",
             "target": ["variable", ["template-tag", "school_name"]],
             "value": school_name},
            {"id": D21_PARAM_IDS["开始时间"], "type": "date/single",
             "target": ["variable", ["template-tag", "start_date"]],
             "value": str(start_date)},
            {"id": D21_PARAM_IDS["结束时间"], "type": "date/single",
             "target": ["variable", ["template-tag", "end_date"]],
             "value": str(end_date)},
        ]

        try:
            async with session.post(url, json={"parameters": params}, headers=headers) as resp:
                result = await resp.json()
            row, _ = self._extract_row(result)
            if row and row[0] is not None:
                return str(row[0])
        except Exception as e:
            logger.warning("[Metabase] D21 dashcard %d 查询失败: %s", card_id, e)
        return ""

    async def _query_d21_usage_rate(self, school_name: str, start_date: str,
                                     end_date: str, stage: str = "") -> float:
        """通过 Card 374 (dashcard 480) 查询平台使用率（支持学段筛选）

        返回使用率百分比，如 13.85 表示 13.85%。
        stage 可选: "高中部" / "初中部" / "小学部"，为空时返回总体使用率。
        """
        dashcard_id = D21_DASHCARDS.get(CARD_D21_USAGE_RATE)
        if not dashcard_id:
            return 0.0

        session = await self._get_session()
        headers = {"X-Metabase-Session": self._session_id, "Content-Type": "application/json"}
        url = f"{self._metabase_base}/api/dashboard/21/dashcard/{dashcard_id}/card/{CARD_D21_USAGE_RATE}/query"

        params = [
            {"id": D21_PARAM_IDS["学校"], "type": "string/=",
             "target": ["variable", ["template-tag", "school_name"]],
             "value": school_name},
            {"id": D21_PARAM_IDS["开始时间"], "type": "date/single",
             "target": ["variable", ["template-tag", "start_date"]],
             "value": str(start_date)},
            {"id": D21_PARAM_IDS["结束时间"], "type": "date/single",
             "target": ["variable", ["template-tag", "end_date"]],
             "value": str(end_date)},
        ]
        if stage:
            params.append({
                "id": D21_PARAM_IDS["学段"], "type": "string/=",
                "target": ["variable", ["template-tag", "stage"]],
                "value": stage,
            })

        try:
            async with session.post(url, json={"parameters": params}, headers=headers) as resp:
                result = await resp.json()
            rows = result.get("data", {}).get("rows", [])
            if rows and len(rows[0]) >= 2:
                return float(rows[0][1])
        except Exception as e:
            logger.warning("[Metabase] D21 usage rate query failed: %s", e)
        return 0.0

    # ── 学校列表 ──

    async def _fetch_school_list(self) -> dict[str, str]:
        """从 Card #251 获取全部学校列表（school_id → school_name）"""
        card_result = await self._query_card(251, [])
        data = card_result.get("data", {})
        rows = data.get("rows", [])
        schools = {}
        for row in rows:
            if len(row) >= 2:
                sid = str(row[0])
                sname = str(row[1])
                if sid and sname:
                    schools[sid] = sname
        logger.info("[Metabase] 获取到 %d 所学校", len(schools))
        return schools

    # ── 参数构建 ──

    def _build_params(self, school: dict, date_range: tuple, stage: str = "") -> list:
        """构建 Metabase 查询参数"""
        start_date, end_date = date_range
        school_id = school.get("metabase_school_id", "")

        params = [
            {
                "type": "string/=",
                "id": "e87fadb4",
                "value": [school_id],
                "target": ["variable", ["template-tag", "school_id"]],
            },
            {
                "type": "date/single",
                "id": "291f0a0b",
                "value": start_date.isoformat(),
                "target": ["variable", ["template-tag", "start_date"]],
            },
            {
                "type": "date/single",
                "id": "e73d8b12",
                "value": end_date.isoformat(),
                "target": ["variable", ["template-tag", "end_date"]],
            },
        ]

        if stage:
            params.append({
                "type": "string/=",
                "id": "48d562ed",
                "value": [stage],
                "target": ["variable", ["template-tag", "stage"]],
            })

        return params

    # ── 数据提取 ──

    def _extract_row(self, card_result: dict) -> tuple[list, list]:
        """提取卡片查询结果的第一行数据和列信息"""
        data = card_result.get("data", {})
        rows = data.get("rows", [])
        cols = data.get("cols", [])
        row = rows[0] if rows else []
        return row, cols

    def _fmt(self, val: Any, col_name: str = "") -> str:
        """格式化值为字符串（列感知：使用率→百分比，访问次数→整数）"""
        if val is None:
            return ""
        # 使用率列：小数 → 百分比
        if "率" in col_name and isinstance(val, (int, float)):
            pct = round(float(val) * 100, 2)
            # 去掉多余的零: 2.370 → 2.37, 0.000 → 0
            if pct == int(pct):
                return f"{int(pct)}%"
            return f"{pct}%"
        # 访问次数/数量列：整数
        if isinstance(val, float) and val == int(val):
            return str(int(val))
        if isinstance(val, float):
            return str(round(val, 2))
        return str(val)

    # ── 多模块查询 ──

    async def scrape_all_modules(
        self, school: dict, date_range: tuple, stage: str = ""
    ) -> dict | None:
        """
        一次性查询所有 8 个模块的使用率。

        返回: {
            "244": "平台总体使用率",
            "245": "个备使用率",
            ...,
            "353": "平台使用率(内部员工)",
        }
        """
        school_name = school.get("name", "unknown")
        school_id = school.get("metabase_school_id", "")
        stage_label = f"({stage})" if stage else ""

        result = {}
        if not school_id:
            return result

        if not await self._login():
            logger.error("[Metabase] 登录失败，无法采集 %s", school_name)
            return None

        params = self._build_params(school, date_range, stage=stage)

        # 并发查询模块卡片
        tasks = {cid: self._query_card(cid, params) for cid in ALL_MODULE_CARDS}
        for cid in ALL_MODULE_CARDS:
            tasks[cid] = asyncio.ensure_future(tasks[cid])

        for cid in ALL_MODULE_CARDS:
            card_result = await tasks[cid]
            row, cols = self._extract_row(card_result)
            if row:
                col_name = cols[0].get("display_name", cols[0].get("name", "")) if cols else ""
                result[str(cid)] = self._fmt(row[0], col_name)
                logger.info("[Metabase] %s %s%s: %s → %s",
                            school_name, CARD_NAMES.get(cid, str(cid)),
                            stage_label, row[0], result[str(cid)])
            else:
                result[str(cid)] = ""

        # 查询 Dashboard 21 活跃指标（通过 dashcard 端点）
        # 注意：D21 卡片本身不支持 stage 参数，无论是否有学段筛选都查询（不传 stage）
        try:
            start_str = str(date_range[0])
            end_str = str(date_range[1])
            for cid in D21_ACTIVE_CARDS:
                val = await self._query_d21_dashcard(cid, school_name, start_str, end_str)
                result[str(cid)] = val
                logger.info("[Metabase] %s D21-%s: %s", school_name, cid, val or "(empty)")
        except Exception as e:
            logger.warning("[Metabase] %s D21 dashcard 查询异常: %s", school_name, e)
            for cid in D21_ACTIVE_CARDS:
                result[str(cid)] = ""

        return result

    # ── 周表采集 ──

    async def scrape(self, school: dict, date_range: tuple) -> dict | None:
        """
        周表采集：整体使用率、整体集备、级部集备、学部集备

        返回: {
            "overall_usage_rate": "整体使用率",
            "overall_jibei": "整体集备",
            "grade_jibei": "级部集备",
            "department_jibei": "学部集备",
        }
        """
        school_name = school.get("name", "unknown")
        school_id = school.get("metabase_school_id", "")
        logger.info("[Metabase] 周表采集: %s (school_id=%s)", school_name, school_id)

        result = {
            "overall_usage_rate": "",
            "overall_jibei": "",
            "grade_jibei": "",
            "department_jibei": "",
        }

        if not school_id:
            logger.warning("[Metabase] 学校 %s 未配置 metabase_school_id，跳过", school_name)
            return result

        # 确保已登录
        if not await self._login():
            logger.error("[Metabase] 登录失败，无法采集 %s", school_name)
            return None

        # 基础参数（无学段过滤）
        base_params = self._build_params(school, date_range)

        # 查询平台总体数据 → overall_usage_rate
        r244 = await self._query_card(CARD_PLATFORM_OVERALL, base_params)
        row, cols = self._extract_row(r244)
        if row:
            col_name = cols[0].get("display_name", cols[0].get("name", "")) if cols else ""
            result["overall_usage_rate"] = self._fmt(row[0], col_name)  # 使用率 → 百分比
            logger.info("[Metabase] %s 使用率: %s → %s", school_name, row[0], result["overall_usage_rate"])

        # 查询集备数据 → overall_jibei (使用率/百分比)
        r246 = await self._query_card(CARD_JIBEI, base_params)
        row, cols = self._extract_row(r246)
        if row:
            col_name = cols[0].get("display_name", cols[0].get("name", "")) if cols else ""
            result["overall_jibei"] = self._fmt(row[0], col_name)  # 使用率 → 百分比
            logger.info("[Metabase] %s 集备: %s → %s", school_name, row[0], result["overall_jibei"])

        # 级部集备: 集备卡片(246) + 年级筛选(nianji) → 取使用率
        nianji = school.get("nianji", "")
        if nianji:
            grade_params = self._build_params(school, date_range, stage="")
            grade_params.append({
                "type": "string/=",
                "id": "grade_filter",
                "value": [nianji],
                "target": ["variable", ["template-tag", "grade"]],
            })
            r246_grade = await self._query_card(CARD_JIBEI, grade_params)
            row, cols = self._extract_row(r246_grade)
            if row:
                col_name = cols[0].get("display_name", cols[0].get("name", "")) if cols else ""
                result["grade_jibei"] = self._fmt(row[0], col_name)  # 使用率 → 百分比
                logger.info("[Metabase] %s 级部集备(nianji=%s): %s → %s",
                            school_name, nianji, row[0], result["grade_jibei"])

        # 学部集备: 集备卡片(246) + 学段筛选(xueduan) → 取使用率
        xueduan = school.get("xueduan", "")
        if xueduan:
            dept_params = self._build_params(school, date_range, stage=xueduan)
            r246_dept = await self._query_card(CARD_JIBEI, dept_params)
            row, cols = self._extract_row(r246_dept)
            if row:
                col_name = cols[0].get("display_name", cols[0].get("name", "")) if cols else ""
                result["department_jibei"] = self._fmt(row[0], col_name)  # 使用率 → 百分比
                logger.info("[Metabase] %s 学部集备(xueduan=%s): %s → %s",
                            school_name, xueduan, row[0], result["department_jibei"])

        return result

    # ── 月表采集 ──

    async def scrape_monthly(self, school: dict, date_range: tuple) -> dict | None:
        """
        月表采集：整体数据 + 按学段分别查询（使用 scrape_all_modules 优化）

        返回: {
            "overall_usage_rate": "整体使用率",
            "overall_jibei": "整体集备",
            "platform_usage": "平台使用",
            "platform_usage_hs": "高中平台使用",
            "platform_usage_ms": "初中平台使用",
            "platform_usage_ps": "小学平台使用",
            "jibei_hs": "高中集备",
            "jibei_ms": "初中集备",
            "jibei_ps": "小学集备",
            "zujuan": "组卷",
            "zujuan_hs": "高中组卷",
            "zujuan_ms": "初中组卷",
            "zujuan_ps": "小学组卷",
            # 8 模块完整数据
            "modules": { "244": "...", "245": "...", ... },
        }
        """
        school_name = school.get("name", "unknown")
        school_id = school.get("metabase_school_id", "")
        logger.info("[Metabase] 月表采集: %s (school_id=%s)", school_name, school_id)

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
            "modules": {},
        }

        if not school_id:
            logger.warning("[Metabase] 学校 %s 未配置 metabase_school_id，跳过", school_name)
            return result

        if not await self._login():
            logger.error("[Metabase] 登录失败，无法采集 %s", school_name)
            return None

        # ── 整体数据：一次性获取所有 8 个模块 ──
        all_modules = await self.scrape_all_modules(school, date_range)
        if all_modules:
            result["modules"] = all_modules
            # 从模块数据中提取兼容字段
            result["overall_usage_rate"] = all_modules.get(str(CARD_PLATFORM_OVERALL), "")
            result["platform_usage"] = all_modules.get(str(CARD_PLATFORM_OVERALL), "")
            result["overall_jibei"] = all_modules.get(str(CARD_JIBEI), "")
            result["zujuan"] = all_modules.get(str(CARD_ZUJUAN), "")
            logger.info("[Metabase] %s 8模块数据: %s", school_name,
                        {CARD_NAMES.get(int(k), k): v for k, v in all_modules.items() if v})

        # ── 按学段分别查询（每个学段一次 scrape_all_modules）──
        stages = [
            ("高中", "hs"),
            ("初中", "ms"),
            ("小学", "ps"),
        ]
        for stage_name, suffix in stages:
            stage_modules = await self.scrape_all_modules(school, date_range, stage=stage_name)
            if stage_modules:
                result[f"platform_usage_{suffix}"] = stage_modules.get(str(CARD_PLATFORM_OVERALL), "")
                result[f"jibei_{suffix}"] = stage_modules.get(str(CARD_JIBEI), "")
                result[f"zujuan_{suffix}"] = stage_modules.get(str(CARD_ZUJUAN), "")
                logger.info("[Metabase] %s %s: 使用率=%s, 集备=%s, 组卷=%s",
                            school_name, stage_name,
                            result[f"platform_usage_{suffix}"],
                            result[f"jibei_{suffix}"],
                            result[f"zujuan_{suffix}"])

        return result

    # ── 可用性检查 ──

    @property
    def is_available(self) -> bool:
        """检查 API 采集器是否可用"""
        return bool(self._session_id)
