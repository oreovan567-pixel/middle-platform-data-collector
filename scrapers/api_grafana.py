"""Grafana HTTP API 直连采集器（纯 aiohttp，无需浏览器）

通过 Grafana REST API 查询面板数据：
  - GET /api/dashboards/uid/{uid}  获取面板配置
  - POST /api/ds/query             查询面板数据

周表 dashboard: 中台周报表 (02b914fa-f10e-42ba-887e-2cf1c69946f7)
月表 dashboard: 中台使用统计 (f73cc2e6-cc1b-40e0-8e23-b2944e2bb56e)
"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import re
from datetime import date, datetime
from typing import Any

import aiohttp

from config.config_loader import get_credentials

logger = logging.getLogger(__name__)

GRAFANA_BASE = "https://grafana.qimingdaren.com"

# Dashboard UIDs（来自发现脚本）
DASHBOARD_UID_WEEKLY = "02b914fa-f10e-42ba-887e-2cf1c69946f7"
DASHBOARD_UID_MONTHLY = "f73cc2e6-cc1b-40e0-8e23-b2944e2bb56e"

# 周表面板标题 → 字段名映射
WEEKLY_PANEL_MAP = {
    "本周活跃教师": "weekly_active_teachers",
    "本周使用总教师": "weekly_total_teachers",
    "周活跃教师比例": "weekly_active_ratio",
}

# 月表: 从"教师活跃度学校占比"表格面板（4个targets）直接获取
# Target A (MySQL): school_teacher_count   Target B (SLS): month_user_count (uc>=4)
# Target C (SLS): week_user_count (use_days>=3/week)   Target D (SLS): day_user_count


class ApiGrafanaScraper:
    """纯 HTTP 方式的 Grafana 采集器"""

    def __init__(self):
        self._creds = get_credentials("grafana")
        self._session: aiohttp.ClientSession | None = None
        self._school_id_cache: dict[str, str] = {}  # school_name -> school_id
        self._auth_headers: dict[str, str] = {}
        self._init_auth()

    def _init_auth(self):
        """初始化 Basic Auth 请求头"""
        username = self._creds.get("username", "")
        password = self._creds.get("password", "")
        token = self._creds.get("api_token", "")
        if token:
            self._auth_headers = {"Authorization": f"Bearer {token}"}
        else:
            encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
            self._auth_headers = {"Authorization": f"Basic {encoded}"}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers=self._auth_headers,
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

    # ── 学校 ID 映射 ──

    async def _resolve_school_id(self, school_name: str) -> str:
        """通过 Grafana 变量数据源查询学校名称对应的 ID

        查询 school 表 (mysql-grafana-variable 数据源):
          SELECT school_id, school_name FROM school;
        """
        if school_name in self._school_id_cache:
            return self._school_id_cache[school_name]

        session = await self._get_session()

        # 一次性加载所有学校映射
        payload = {
            "queries": [
                {
                    "refId": "A",
                    "datasource": {"type": "mysql", "uid": "bf2dg687jtog0a"},
                    "rawSql": "SELECT school_id, school_name FROM school;",
                    "format": "table",
                }
            ],
            "from": "now-1h",
            "to": "now",
        }

        try:
            async with session.post(
                f"{GRAFANA_BASE}/api/ds/query",
                json=payload,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", {})
                    for ref_data in results.values():
                        frames = ref_data.get("frames", [])
                        for frame in frames:
                            fields = frame.get("schema", {}).get("fields", [])
                            values = frame.get("data", {}).get("values", [])
                            if len(fields) >= 2 and len(values) >= 2:
                                # fields[0]=school_id, fields[1]=school_name
                                for i in range(len(values[0])):
                                    sid = str(values[0][i]) if i < len(values[0]) else ""
                                    name = str(values[1][i]) if i < len(values[1]) else ""
                                    if name:
                                        self._school_id_cache[name] = sid

                    # 精确匹配
                    cached_id = self._school_id_cache.get(school_name, "")
                    if cached_id:
                        logger.info("学校 ID 映射: %s -> %s", school_name, cached_id)
                        return cached_id

                    # 模糊匹配：grafana_name 可能是"启鸣达人学校"而配置中是"启鸣达人"
                    for name, sid in self._school_id_cache.items():
                        if school_name in name or name in school_name:
                            self._school_id_cache[school_name] = sid
                            logger.info("学校 ID 模糊映射: %s -> %s (%s)",
                                        school_name, sid, name)
                            return sid
        except Exception as e:
            logger.warning("查询学校 ID 失败: %s", e)

        logger.warning("未找到学校 '%s' 的 ID，使用名称代替", school_name)
        return school_name

    # ── 面板查询 ──

    async def _query_panel(
        self,
        panel_targets: list[dict],
        start_date: date,
        end_date: date,
        school_id: str,
        keep_arrays: bool = False,
        quote_school_id: bool = False,
    ) -> dict[str, Any]:
        """查询面板数据，返回 refId -> value 的映射"""
        session = await self._get_session()

        from_ts = int(
            datetime.combine(start_date, datetime.min.time()).timestamp() * 1000
        )
        to_ts = (
            int(datetime.combine(end_date, datetime.min.time()).timestamp() * 1000)
            + 86399000
        )

        # 构建查询 payload，替换学校变量
        queries = []
        for target in panel_targets:
            q = dict(target)
            ds_type = q.get("datasource", {}).get("type", "")
            is_aliyun = "aliyun" in ds_type

            # 替换 SQL 中的学校变量
            if "query" in q and isinstance(q["query"], str):
                query = q["query"]
                # 月表 SLS 的 tianli_school_id 是 varchar，需引号；周表是 bigint，不需引号
                sls_school_id = f"'{school_id}'" if (quote_school_id and is_aliyun) else school_id
                query = query.replace("${school_id:csv}", sls_school_id)
                query = query.replace("${school:csv}", sls_school_id)
                query = query.replace("${__to}", str(to_ts))
                logger.info(f"[API] SLS 查询 SQL (school_id={school_id}): {query[:200]}")
                q["query"] = query
            if "rawSql" in q and isinstance(q["rawSql"], str):
                raw = q["rawSql"]
                sls_school_id_raw = f"'{school_id}'" if (quote_school_id and is_aliyun) else school_id
                raw = raw.replace("${school_id:csv}", sls_school_id_raw)
                raw = raw.replace("${school:csv}", sls_school_id_raw)
                raw = raw.replace("${__to}", str(to_ts))
                raw = raw.replace("${__from}", str(from_ts))
                q["rawSql"] = raw
            queries.append(q)

        payload = {
            "queries": queries,
            "from": str(from_ts),
            "to": str(to_ts),
        }

        try:
            async with session.post(
                f"{GRAFANA_BASE}/api/ds/query",
                json=payload,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("面板查询返回 %d: %s", resp.status, text[:300])
                    return {}

                data = await resp.json()
                return self._extract_values(data, keep_arrays=keep_arrays)
        except Exception as e:
            logger.error("面板查询异常: %s", e)
            return {}

    def _extract_values(self, data: dict, keep_arrays: bool = False) -> dict[str, Any]:
        """从 Grafana ds/query 响应中提取数值"""
        result = {}
        results = data.get("results", {})

        for ref_id, ref_data in results.items():
            frames = ref_data.get("frames", [])
            for frame in frames:
                fields = frame.get("schema", {}).get("fields", [])
                values = frame.get("data", {}).get("values", [])

                # 提取每个非时间字段的最后一个值
                for i, field in enumerate(fields):
                    if i >= len(values):
                        continue
                    arr = values[i]
                    if not isinstance(arr, list) or len(arr) == 0:
                        continue

                    field_name = field.get("name", "")
                    field_type = field.get("type", "")

                    # 跳过时间字段
                    if field_type == "time":
                        continue

                    # keep_arrays 模式: 保留整列数据（用于表格面板按学校匹配）
                    if keep_arrays:
                        result[field_name] = arr
                        result[f"{ref_id}:{field_name}"] = arr
                        continue
                    
                    # 默认模式: 取最后一个值（包括0，0也是有效数据）
                    last_val = None
                    for v in reversed(arr):
                        if v is not None:
                            last_val = v
                            break
                    
                    if last_val is not None:
                        result[field_name] = last_val
                        result[f"{ref_id}:{field_name}"] = last_val
                    
                    # 也记录 refId 级别的第一个值
                    if ref_id not in result and last_val is not None:
                        result[ref_id] = last_val
        return result

    async def _get_dashboard_panels(self, uid: str) -> list[dict]:
        """获取 dashboard 的面板配置（递归展平 row 子面板）"""
        session = await self._get_session()
        try:
            async with session.get(
                f"{GRAFANA_BASE}/api/dashboards/uid/{uid}"
            ) as resp:
                if resp.status != 200:
                    logger.warning("获取 dashboard %s 失败: %d", uid, resp.status)
                    return []
                data = await resp.json()
                panels = data.get("dashboard", {}).get("panels", [])
                return self._flatten_panels(panels)
        except Exception as e:
            logger.error("获取 dashboard 异常: %s", e)
            return []

    @staticmethod
    def _flatten_panels(panels: list[dict]) -> list[dict]:
        """递归展平 row 面板中的子面板"""
        flat = []
        for panel in panels:
            flat.append(panel)
            sub_panels = panel.get("panels", [])
            if sub_panels:
                flat.extend(ApiGrafanaScraper._flatten_panels(sub_panels))
        return flat

    def _find_panel_targets(
        self, panels: list[dict], title_match: str
    ) -> list[dict] | None:
        """从面板列表中找到指定标题的面板，返回其 targets
        优先精确匹配，回退子串匹配"""
        # 第一轮：精确匹配
        for panel in panels:
            title = panel.get("title", "")
            if title == title_match:
                targets = panel.get("targets", [])
                if targets:
                    return targets
        # 第二轮：子串匹配（兜底）
        for panel in panels:
            title = panel.get("title", "")
            if title_match in title:
                targets = panel.get("targets", [])
                if targets:
                    return targets
        return None

    # ── 周表采集 ──

    async def scrape(self, school: dict, date_range: tuple) -> dict:
        """
        周表采集：本周活跃教师、本周使用总教师
        与浏览器版保持一致的面板映射。

        返回: {
            "weekly_active_teachers": "本周活跃教师（≥3天活跃）",
            "weekly_total_teachers": "本周使用总教师（登录过的）",
            "weekly_active_ratio": "活跃比例",
        }
        """
        start_date, end_date = date_range
        school_name = school.get("grafana_name", school["name"])
        logger.info("[API] Grafana 周表采集: %s (%s ~ %s)", school_name, start_date, end_date)

        result = {
            "weekly_active_teachers": "",
            "weekly_total_teachers": "",
            "weekly_active_ratio": "",
        }

        # 获取学校 id
        school_id = await self._resolve_school_id(school_name)

        session = await self._get_session()

        # ── SLS 面板查询（与浏览器版完全一致的映射） ──
        panels = await self._get_dashboard_panels(DASHBOARD_UID_WEEKLY)
        if panels:
            # 面板 → 字段 映射（同浏览器版 WEEKLY_PANEL_MAP）
            for panel_title, field_key in [
                ("本周活跃教师", "weekly_active_teachers"),
                ("本周使用总教师", "weekly_total_teachers"),
            ]:
                targets = self._find_panel_targets(panels, panel_title)
                if not targets:
                    logger.debug("未找到面板: %s", panel_title)
                    continue
                values = await self._query_panel(targets, start_date, end_date, school_id)
                val = values.get("user_count")
                if val is None:
                    val = values.get("A:user_count")
                if val is None:
                    val = values.get("A")
                if val is not None:
                    result[field_key] = str(int(float(val)))
                    logger.info("[API] %s (%s) = %s", field_key, panel_title, result[field_key])

        # ── 活跃教师 SLS 空结果处理：如果 SLS 未返回数据，视为 0 活跃 ──
        if not result["weekly_active_teachers"]:
            result["weekly_active_teachers"] = "0"
            logger.info("[API] weekly_active_teachers SLS 返回空，视为 0")

        # ── teacher_base 兜底（仅当 total 为空或为0时） ──
        if not result["weekly_total_teachers"] or result["weekly_total_teachers"] == "0":
            try:
                payload = {
                    "queries": [{
                        "refId": "A",
                        "datasource": {"type": "mysql", "uid": "dfkbye10p9lvke"},
                        "rawSql": f"SELECT COUNT(*) as teacher_count "
                                  f"FROM teacher_base "
                                  f"WHERE school_id = {school_id} AND state = 1",
                        "format": "table",
                    }],
                    "from": "now-1h",
                    "to": "now",
                }
                async with session.post(
                    f"{GRAFANA_BASE}/api/ds/query",
                    json=payload,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        val = None
                        for ref_data in data.get("results", {}).values():
                            for frame in ref_data.get("frames", []):
                                vals = frame.get("data", {}).get("values", [])
                                if vals and vals[0]:
                                    val = vals[0][-1]
                                    break
                            if val is not None:
                                break
                        if val is not None:
                            result["weekly_total_teachers"] = str(int(float(val)))
                            logger.info("[API] weekly_total_teachers (teacher_base fallback) = %s",
                                        result["weekly_total_teachers"])
            except Exception as e:
                logger.debug("weekly total fallback error: %s", e)

        # ── school 表兜底（当 teacher_base 也返回0时） ──
        if not result["weekly_total_teachers"] or result["weekly_total_teachers"] == "0":
            try:
                payload = {
                    "queries": [{
                        "refId": "A",
                        "datasource": {"type": "mysql", "uid": "bf2dg687jtog0a"},
                        "rawSql": f"SELECT total_teacher_count FROM school WHERE school_id = {school_id}",
                        "format": "table",
                    }],
                    "from": "now-1h",
                    "to": "now",
                }
                async with session.post(
                    f"{GRAFANA_BASE}/api/ds/query",
                    json=payload,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        val = None
                        for ref_data in data.get("results", {}).values():
                            for frame in ref_data.get("frames", []):
                                vals = frame.get("data", {}).get("values", [])
                                if vals and vals[0]:
                                    val = vals[0][-1]
                                    break
                            if val is not None:
                                break
                        if val is not None and float(val) > 0:
                            result["weekly_total_teachers"] = str(int(float(val)))
                            logger.info("[API] weekly_total_teachers (school表兜底) = %s",
                                        result["weekly_total_teachers"])
            except Exception as e:
                logger.debug("weekly total school fallback error: %s", e)

        # ── 直接查询"周活跃教师比例"面板，用 A/B 计算比例 ──
        if panels:
            ratio_targets = self._find_panel_targets(panels, "周活教师比例")
            logger.info("[API] 比例面板查找: found=%s, panel_titles=%s",
                        ratio_targets is not None,
                        [p.get('title','') for p in panels])
            if ratio_targets:
                # 教师总数应取周结束日期那天的 data_statistics_group 值，
                # 而非面板默认的 WEEKDAY=0（周一快照）。
                end_date_str = end_date.strftime("%Y%m%d")
                ratio_targets = [dict(t) for t in ratio_targets]  # shallow copy
                for t in ratio_targets:
                    ds = t.get("datasource", {})
                    ds_type = ds.get("type", "") if isinstance(ds, dict) else ""
                    if "mysql" in ds_type and "rawSql" in t:
                        raw = t["rawSql"]
                        if "data_statistics_group" in raw:
                            # 移除 WEEKDAY 过滤，改为按结束日期筛选
                            raw = re.sub(
                                r"AND\s+WEEKDAY\(.+?\)\s*=\s*\d+",
                                "",
                                raw,
                                flags=re.IGNORECASE,
                            )
                            raw = re.sub(
                                r"GROUP BY\s+b\.date\s*;?\s*$",
                                f"AND b.date = {end_date_str} "
                                f"GROUP BY b.date;",
                                raw,
                                flags=re.IGNORECASE,
                            )
                            t["rawSql"] = raw
                            logger.info("[API] 教师总数SQL已修改: end_date=%s", end_date_str)

                ratio_values = await self._query_panel(
                    ratio_targets, start_date, end_date, school_id
                )
                # 面板有两个 target:
                #   A (SLS): user_count = 活跃教师数
                #   B (MySQL): school_teacher_count = 全校教师数
                # Grafana 用 A/B 计算比例，我们需要同样计算
                active_val = ratio_values.get("user_count")
                if active_val is None:
                    active_val = ratio_values.get("A:user_count")
                total_val = ratio_values.get("school_teacher_count")
                if total_val is None:
                    total_val = ratio_values.get("B:school_teacher_count")
                logger.info("[API] 比例面板返回值: keys=%s, active_val=%s, total_val=%s",
                            list(ratio_values.keys()), active_val, total_val)
                
                try:
                    # SLS 返回空时视为 0 活跃
                    if active_val is None:
                        active_val = 0
                        logger.info("[API] 比例面板 active_val=None, 视为 0")
                    
                    # 如果比例面板没有返回教师总数，用 weekly_total_teachers 兜底
                    if total_val is None:
                        if result["weekly_total_teachers"] and result["weekly_total_teachers"] != "0":
                            total_val = float(result["weekly_total_teachers"])
                            logger.info("[API] 比例面板 total 使用 weekly_total_teachers 兜底: %s", total_val)

                    if total_val is not None:
                        active_f = float(active_val)
                        total_f = float(total_val)
                        if total_f > 0:
                            ratio_pct = round(active_f / total_f * 100, 2)
                            result["weekly_active_ratio"] = f"{ratio_pct}%"
                            logger.info("[API] weekly_active_ratio (面板 A/B) = %.2f%% (active=%s, total=%s)",
                                        ratio_pct, active_val, total_val)
                        elif active_f == 0:
                            result["weekly_active_ratio"] = "0.0%"
                            logger.info("[API] weekly_active_ratio (面板 A/B) = 0.0%% (active=%s, total=%s)",
                                        active_val, total_val)
                except (ValueError, TypeError, ZeroDivisionError) as e:
                    logger.debug("ratio calculation error: %s", e)
            # 注意：不做兜底计算。面板没值就保持空。

        return result

    # ── 月表采集 ──

    async def scrape_monthly(self, school: dict, date_range: tuple) -> dict:
        """
        月表采集：从"教师活跃度学校占比"表格面板获取日活/周活/月活教师数据

        面板包含 4 个 targets:
          A (MySQL) → school_teacher_count   B (SLS) → month_user_count (uc>=4)
          C (SLS) → week_user_count (use_days>=3/week)  D (SLS) → day_user_count
        计算: ratio = count / total * 100%
        交叉验证: 预计占比(count/total) 与采集占比比较小数点前两位

        返回: {
            "daily_active_ratio": "日活占比",
            "weekly_active_ratio": "周活占比",
            "monthly_active_ratio": "月活占比",
            "data_anomaly": bool,
            "anomaly_message": str,
        }
        """
        start_date, end_date = date_range
        school_name = school.get("grafana_name", school["name"])
        logger.info("[API] Grafana 月表采集: %s (%s ~ %s)", school_name, start_date, end_date)

        result = {
            "daily_active_ratio": "",
            "weekly_active_ratio": "",
            "monthly_active_ratio": "",
            "data_anomaly": False,
            "anomaly_message": "",
        }

        school_id = await self._resolve_school_id(school_name)

        # 获取月表 dashboard
        panels = await self._get_dashboard_panels(DASHBOARD_UID_MONTHLY)
        if not panels:
            logger.warning("未获取到月表 dashboard 面板")
            return result

        # 1. 找到"教师活跃度学校占比"表格面板（有 4 个 targets）
        activity_panel = None
        for panel in panels:
            if (panel.get("title") == "教师活跃度学校占比"
                    and len(panel.get("targets", [])) == 4):
                activity_panel = panel
                break

        if not activity_panel:
            logger.warning("未找到'教师活跃度学校占比'面板(4个targets)")
            result["data_anomaly"] = True
            result["anomaly_message"] = "未找到教师活跃度学校占比面板"
            return result

        targets = activity_panel["targets"]

        # 2. 查询 4 个 targets 获取 count 数据
        counts = {
            "school_teacher_count": 0,
            "day_user_count": 0,
            "week_user_count": 0,
            "month_user_count": 0,
        }

        # Target A (MySQL): school_teacher_count — 返回所有学校，需按 school_id 提取
        a_values = await self._query_panel([targets[0]], start_date, end_date, school_id, keep_arrays=True)
        school_total = self._extract_field_for_school(a_values, school_id,
                                                       "school_teacher_count")
        if school_total is not None:
            counts["school_teacher_count"] = float(school_total)
            logger.info("[API] school_teacher_count = %s", counts["school_teacher_count"])
        else:
            logger.warning("[API] school_teacher_count 获取失败")

        # Targets B/C/D (SLS): 已通过 school_id 过滤，只返回一行
        sls_field_map = {
            1: "month_user_count",
            2: "week_user_count",
            3: "day_user_count",
        }
        for idx, field_name in sls_field_map.items():
            values = await self._query_panel([targets[idx]], start_date, end_date, school_id, quote_school_id=True)
            val = values.get(field_name) or values.get(f"A:{field_name}")
            if val is not None:
                counts[field_name] = float(val)
                logger.info("[API] %s = %s", field_name, counts[field_name])
            else:
                counts[field_name] = 0
                logger.info("[API] %s = 0 (无查询结果, keys=%s)", field_name, list(values.keys()))

        total = counts["school_teacher_count"]
        day_count = counts["day_user_count"]
        week_count = counts["week_user_count"]
        month_count = counts["month_user_count"]

        logger.info("[API] 活跃度数据: day=%s, week=%s, month=%s, total=%s",
                    day_count, week_count, month_count, total)

        # 3. 计算占比: ratio = count / total * 100
        if total <= 0:
            logger.warning("[API] 教师总数为 0，无法计算占比")
            result["data_anomaly"] = True
            result["anomaly_message"] = "教师总数为0"
            return result

        day_ratio = round(day_count / total * 100, 2)
        week_ratio = round(week_count / total * 100, 2)
        month_ratio = round(month_count / total * 100, 2)

        result["daily_active_ratio"] = f"{day_ratio}%"
        result["weekly_active_ratio"] = f"{week_ratio}%"
        result["monthly_active_ratio"] = f"{month_ratio}%"

        logger.info("[API] 采集占比: day=%s%%, week=%s%%, month=%s%%",
                    day_ratio, week_ratio, month_ratio)

        # 4. 交叉验证: 预计占比 = count / total * 100，比较小数点前两位
        expected_day = int(day_count / total * 100)
        expected_week = int(week_count / total * 100)
        expected_month = int(month_count / total * 100)

        collected_day = int(day_ratio)
        collected_week = int(week_ratio)
        collected_month = int(month_ratio)

        mismatches = []
        if expected_day != collected_day:
            mismatches.append(f"日活(预计{expected_day}%%,采集{collected_day}%%)")
        if expected_week != collected_week:
            mismatches.append(f"周活(预计{expected_week}%%,采集{collected_week}%%)")
        if expected_month != collected_month:
            mismatches.append(f"月活(预计{expected_month}%%,采集{collected_month}%%)")

        if mismatches:
            msg = ", ".join(mismatches)
            logger.warning("[API] 活跃度数据异常: %s", msg)
            result["data_anomaly"] = True
            result["anomaly_message"] = f"活跃度交叉验证不通过: {msg}"
        else:
            logger.info("[API] 活跃度交叉验证通过")

        # 5. 附加合理性检查: 日活 >= 周活 >= 月活
        if not (day_ratio >= week_ratio >= month_ratio):
            msg = (f"活跃度递减异常: 日活{day_ratio}% < 周活{week_ratio}% "
                   f"或 周活{week_ratio}% < 月活{month_ratio}%")
            logger.warning("[API] %s", msg)
            result["data_anomaly"] = True
            if result["anomaly_message"]:
                result["anomaly_message"] += f"; {msg}"
            else:
                result["anomaly_message"] = msg

        return result

    def _extract_field_for_school(
        self, values: dict, school_id: str, field_name: str
    ) -> float | None:
        """从表格查询结果中提取指定学校的字段值。

        表格查询返回所有学校数据（values 是 field_name -> list），
        通过 tianli_school_id 列定位行索引再提取目标字段。
        """
        sid_list = values.get("tianli_school_id", [])
        val_list = values.get(field_name, [])
        if not isinstance(sid_list, list) or not isinstance(val_list, list):
            direct = values.get(field_name)
            if direct is not None:
                return float(direct)
            return None
        for i, sid in enumerate(sid_list):
            if str(sid) == str(school_id) and i < len(val_list):
                return float(val_list[i])
        return None

        return result

    async def _get_teacher_total(
        self,
        panels: list[dict],
        start_date: date,
        end_date: date,
        school_id: str,
    ) -> float:
        """Get teacher total count.

        Uses direct SQL with school_id filter first,
        falls back to table panel query with school_id result filtering.
        """
        session = await self._get_session()

        # Direct SQL with school_id filter
        # teacher_base is in dfkbye10p9lvke datasource
        # data_statistics tables are in cf17hn9qgj08we datasource
        sql_queries = [
            (
                "dfkbye10p9lvke",
                f"SELECT COUNT(*) as teacher_count "
                f"FROM teacher_base "
                f"WHERE school_id = {school_id} AND state = 1"
            ),
            (
                "cf17hn9qgj08we",
                f"SELECT sum(b.total_teacher_count) as teacher_count "
                f"FROM data_statistics_lesson_group a "
                f"JOIN data_statistics_group b ON a.id = b.lesson_group_id "
                f"WHERE a.school_id = {school_id} "
                f"AND b.date = date_format(now(), '%Y%m%d')"
            ),
        ]

        for ds_uid, sql in sql_queries:
            try:
                payload = {
                    "queries": [
                        {
                            "refId": "A",
                            "datasource": {"type": "mysql", "uid": ds_uid},
                            "rawSql": sql,
                            "format": "table",
                        }
                    ],
                    "from": "now-1h",
                    "to": "now",
                }
                async with session.post(
                    f"{GRAFANA_BASE}/api/ds/query",
                    json=payload,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        extracted = self._extract_values(data)
                        val = extracted.get("teacher_count") or extracted.get("A")
                        if val and float(val) > 0:
                            logger.info("[API] teacher total (direct SQL) = %s", val)
                            return float(val)
                    else:
                        text = await resp.text()
                        logger.debug("teacher total query %d: %s", resp.status, text[:200])
            except Exception as e:
                logger.debug("teacher total SQL error: %s", e)
                continue

        # Fallback: table panel query, filter result by school_id
        for panel in panels:
            ptype = panel.get("type", "")
            title = panel.get("title", "")
            if ptype == "table" and ("\u5360\u6bd4" in title or "\u6559\u5e08" in title):
                targets = panel.get("targets", [])
                for target in targets:
                    ds = target.get("datasource", {})
                    ds_type = ds.get("type", "") if isinstance(ds, dict) else ""
                    if "mysql" not in ds_type:
                        continue
                    values = await self._query_panel(
                        [target], start_date, end_date, school_id
                    )
                    sid_val = (
                        values.get("tianli_school_id")
                        or values.get("A:tianli_school_id")
                    )
                    count_val = (
                        values.get("school_teacher_count")
                        or values.get("A:school_teacher_count")
                        or values.get("teacher_count")
                    )
                    if (
                        sid_val is not None
                        and count_val is not None
                        and str(sid_val) == str(school_id)
                    ):
                        logger.info("[API] teacher total (table panel) = %s", count_val)
                        return float(count_val)

        logger.warning("Failed to get teacher total (school_id=%s)", school_id)
        return 0

    async def _try_get_ratio_panels(
        self,
        panels: list[dict],
        start_date: date,
        end_date: date,
        school_id: str,
    ) -> dict:
        """尝试直接从占比面板获取数据"""
        result = {}
        ratio_map = {
            "日活": "daily_active_ratio",
            "周活": "weekly_active_ratio",
            "月活": "monthly_active_ratio",
        }

        for panel in panels:
            title = panel.get("title", "")
            targets = panel.get("targets", [])
            if not targets:
                continue

            for keyword, field_key in ratio_map.items():
                if keyword in title and ("占比" in title or "比例" in title):
                    values = await self._query_panel(targets, start_date, end_date, school_id)
                    # 查找百分比值
                    for k, v in values.items():
                        if isinstance(v, (int, float)) and v > 0:
                            if v <= 1:  # 小数形式的百分比
                                result[field_key] = f"{round(v * 100, 2)}%"
                            elif v <= 100:
                                result[field_key] = f"{round(v, 2)}%"
                            break

        return result
