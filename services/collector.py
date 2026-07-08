"""采集编排器 - 串联3个平台爬虫，按平台优先采集（异步版）

采集策略：
  - 支持 API 直连模式（api_mode=true）：纯 HTTP 请求，速度快
  - 支持浏览器模式（默认）：Playwright 自动化，兼容性好
  - API 模式失败时自动降级到浏览器方式
  - Lida / Grafana：打开一次系统，切换学校逐个采集，全部完成后关闭
  - 主站：每个学校重新打开页面、采集、关闭，确保状态干净
  - 支持周表(weekly)和月表(monthly)两种采集模式
"""
from __future__ import annotations
import asyncio
import json
import logging
import queue
import threading
from datetime import date, datetime

from config.config_loader import get_schools, get_school, load_config
from models.database import get_connection
from models.weekly_record import WeeklyRecord
from models.monthly_record import MonthlyRecord
# 浏览器采集器（可选依赖，Vercel 等 serverless 环境无 playwright）
try:
    from scrapers.browser_manager import BrowserManager
    from scrapers.grafana_scraper import GrafanaScraper
    from scrapers.main_site_scraper import MainSiteScraper
    BROWSER_SCRAPERS_AVAILABLE = True
except ImportError:
    BROWSER_SCRAPERS_AVAILABLE = False

# API 直连采集器（可选依赖）
try:
    from scrapers.api_grafana import ApiGrafanaScraper
    from scrapers.api_lida import ApiLidaScraper
    from scrapers.api_main_site import ApiMainSiteScraper
    API_SCRAPERS_AVAILABLE = True
except ImportError:
    API_SCRAPERS_AVAILABLE = False

logger = logging.getLogger(__name__)


class ProgressEvent:
    """采集进度事件"""

    def __init__(self, school: str, platform: str, status: str, message: str = "",
                 elapsed_seconds: float = 0):
        self.school = school
        self.platform = platform
        self.status = status  # pending / running / completed / failed
        self.message = message
        self.elapsed_seconds = round(elapsed_seconds, 1)

    def to_dict(self) -> dict:
        d = {
            "school": self.school,
            "platform": self.platform,
            "status": self.status,
            "message": self.message,
        }
        if self.elapsed_seconds > 0:
            d["elapsed_seconds"] = self.elapsed_seconds
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class Collector:
    """
    采集编排器。
    按平台优先策略调用3个爬虫，合并数据后写入数据库。
    支持 API 直连模式（api_mode=true）和浏览器模式，API 失败自动降级。
    通过 pub/sub 模式广播进度事件（支持多客户端）。
    支持 record_type='weekly'(周表) 和 'monthly'(月表)。
    """

    def __init__(self):
        self._subscribers: dict[int, queue.Queue] = {}
        self._sub_lock = threading.Lock()
        self._sub_counter = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._pause_event = threading.Event()
        self._pause_event.set()  # 初始状态: 不暂停
        self._is_paused = False
        self._current_task_id: int | None = None
        self._current_user_id: int | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    @property
    def current_task_id(self) -> int | None:
        return self._current_task_id

    @property
    def current_user_id(self) -> int | None:
        return self._current_user_id

    def subscribe(self) -> tuple[int, queue.Queue]:
        """新 SSE 客户端调用，返回 (sub_id, 专属队列)"""
        with self._sub_lock:
            self._sub_counter += 1
            sub_id = self._sub_counter
            q: queue.Queue[ProgressEvent] = queue.Queue()
            self._subscribers[sub_id] = q
            logger.debug("SSE 客户端订阅: sub_id=%d, 当前订阅数=%d", sub_id, len(self._subscribers))
            return sub_id, q

    def unsubscribe(self, sub_id: int):
        """SSE 客户端断开时调用"""
        with self._sub_lock:
            removed = self._subscribers.pop(sub_id, None)
            if removed:
                logger.debug("SSE 客户端取消订阅: sub_id=%d, 剩余订阅数=%d", sub_id, len(self._subscribers))

    def pause(self):
        """暂停采集"""
        if self._running and not self._is_paused:
            self._pause_event.clear()
            self._is_paused = True
            logger.info("采集已暂停")

    def resume(self):
        """继续采集"""
        if self._running and self._is_paused:
            self._pause_event.set()
            self._is_paused = False
            logger.info("采集已继续")

    def start_collect(
        self,
        school_names: list[str],
        year: int,
        week_number: str,
        start_date: date,
        end_date: date,
        platforms: list[str] | None = None,
        record_type: str = "weekly",
        month_number: str = "",
        user_id: int | None = None,
        data_source: str = "grafana",
    ) -> int:
        """
        启动采集任务（在后台线程中执行）。
        platforms: 可选，指定采集平台列表。为 None 时采集全部。
        record_type: 'weekly'(周表) 或 'monthly'(月表)。
        month_number: 月表模式下的月次标签，如"五月"。
        data_source: 'grafana'(爬虫) 或 'database'(数据库直查替换Grafana)。
        返回任务ID。
        """
        # 防御性检查: 如果 _running=True 但线程已死，说明上次采集异常退出
        if self._running and self._thread is not None and not self._thread.is_alive():
            logger.warning("检测到上次采集线程已异常退出，重置状态")
            self._running = False
            self._is_paused = False
            self._pause_event.set()

        if self._running:
            raise RuntimeError("已有采集任务正在执行")

        task_id = self._create_task(year, week_number, school_names, record_type)
        self._current_task_id = task_id
        self._current_user_id = user_id

        self._running = True
        self._thread = threading.Thread(
            target=self._run_collect_thread,
            args=(task_id, school_names, year, week_number, start_date, end_date,
                  platforms, record_type, month_number, data_source),
            daemon=True,
        )
        self._thread.start()
        return task_id

    def _create_task(self, year: int, week_number: str, schools: list[str],
                     record_type: str = "weekly") -> int:
        with get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO collect_tasks (year, week_number, schools, status, started_at, record_type) VALUES (?, ?, ?, 'running', ?, ?)",
                (year, week_number, json.dumps(schools, ensure_ascii=False),
                 datetime.now().isoformat(), record_type),
            )
            return cursor.lastrowid

    def _update_task(self, task_id: int, status: str, result_summary: str = ""):
        with get_connection() as conn:
            conn.execute(
                "UPDATE collect_tasks SET status=?, finished_at=?, result_summary=? WHERE id=?",
                (status, datetime.now().isoformat(), result_summary, task_id),
            )

    def _run_collect_thread(
        self,
        task_id: int,
        school_names: list[str],
        year: int,
        week_number: str,
        start_date: date,
        end_date: date,
        platforms: list[str] | None = None,
        record_type: str = "weekly",
        month_number: str = "",
        data_source: str = "grafana",
    ):
        """后台线程入口：创建新的事件循环运行异步采集逻辑"""
        asyncio.run(self._run_collect_async(
            task_id, school_names, year, week_number, start_date, end_date,
            platforms, record_type, month_number, data_source
        ))

    async def _run_collect_async(
        self,
        task_id: int,
        school_names: list[str],
        year: int,
        week_number: str,
        start_date: date,
        end_date: date,
        platforms: list[str] | None = None,
        record_type: str = "weekly",
        month_number: str = "",
        data_source: str = "grafana",
    ):
        """异步采集主逻辑"""
        is_monthly = (record_type == "monthly")
        run_all = platforms is None
        date_range = (start_date, end_date)
        bm = BrowserManager()
        results_summary = []

        # 用于调用爬虫方法: 月度模式用 scrape_monthly，周表用 scrape
        scrape_method = "scrape_monthly" if is_monthly else "scrape"

        # 检查是否启用 API 模式
        config = load_config()
        api_mode = config.get("api_mode", False) and API_SCRAPERS_AVAILABLE
        if api_mode:
            logger.info("API 直连模式已启用")
        elif config.get("api_mode", False) and not API_SCRAPERS_AVAILABLE:
            logger.warning("api_mode=true 但 aiohttp 未安装，使用浏览器模式")

        collect_start_time = asyncio.get_event_loop().time()

        try:
            # 浏览器采集器（仅在需要时启动）
            # 方案B: API 模式下也启动浏览器，用于获取 ks cookie
            need_browser = True
            await bm.start()

            lida = None  # Lida 已替换为 Metabase API，不再使用浏览器
            grafana = GrafanaScraper(bm) if (not api_mode and (run_all or "grafana" in platforms)) else None
            main_site = MainSiteScraper(bm) if (not api_mode and (run_all or "main_site" in platforms)) else None

            # API 采集器
            api_grafana = ApiGrafanaScraper() if (api_mode and (run_all or "grafana" in platforms)) else None
            api_lida = ApiLidaScraper() if (run_all or "lida" in platforms) else None
            api_main_site = None
            if api_mode and (run_all or "main_site" in platforms):
                api_main_site = ApiMainSiteScraper()
                api_main_site.set_browser_manager(bm)  # 方案B: 传入 bm 用于获取 ks cookie

            school_configs: dict[str, dict | None] = {}
            for name in school_names:
                school_configs[name] = get_school(name)

            results_cache: dict[str, dict] = {name: {} for name in school_names}
            errors_cache: dict[str, list] = {name: [] for name in school_names}
            elapsed_cache: dict[str, dict] = {}  # school -> {platform: seconds}

            # ── 定义各平台采集函数（用于并行执行）──

            async def _collect_lida_for_school(school_name: str):
                """Metabase 平台采集（单学校，纯 API）"""
                school = school_configs[school_name]
                if not school:
                    self._push_event(ProgressEvent(school_name, "lida", "failed", "未找到学校配置"))
                    errors_cache[school_name].append("lida: 配置不存在")
                    return
                t0 = asyncio.get_event_loop().time()
                self._push_event(ProgressEvent(school_name, "lida", "running", "正在采集Metabase平台数据..."))

                if not api_lida:
                    self._push_event(ProgressEvent(school_name, "lida", "failed", "Metabase API 不可用"))
                    errors_cache[school_name].append("lida: API 不可用")
                    return

                lida_data = None
                try:
                    scraper_fn = api_lida.scrape_monthly if is_monthly else api_lida.scrape
                    lida_data = await scraper_fn(school, date_range)
                    if lida_data is not None:
                        logger.info("[%s] Metabase API 采集成功", school_name)
                    else:
                        logger.warning("[%s] Metabase API 返回 None", school_name)
                        errors_cache[school_name].append("lida: API 返回空")
                        self._push_event(ProgressEvent(school_name, "lida", "failed", "Metabase采集失败"))
                        return
                except Exception as e:
                    logger.error("[%s] Metabase API 采集失败: %s", school_name, e)
                    errors_cache[school_name].append(f"lida: {e}")
                    self._push_event(ProgressEvent(school_name, "lida", "failed", f"Metabase采集失败: {e}"))
                    return

                if is_monthly:
                    results_cache[school_name].update({
                        "overall_usage_rate": lida_data.get("overall_usage_rate", ""),
                        "overall_jibei": lida_data.get("overall_jibei", ""),
                        "platform_usage": lida_data.get("platform_usage", ""),
                        "platform_usage_hs": lida_data.get("platform_usage_hs", ""),
                        "platform_usage_ms": lida_data.get("platform_usage_ms", ""),
                        "platform_usage_ps": lida_data.get("platform_usage_ps", ""),
                        "jibei_hs": lida_data.get("jibei_hs", ""),
                        "jibei_ms": lida_data.get("jibei_ms", ""),
                        "jibei_ps": lida_data.get("jibei_ps", ""),
                        "zujuan": lida_data.get("zujuan", ""),
                        "zujuan_hs": lida_data.get("zujuan_hs", ""),
                        "zujuan_ms": lida_data.get("zujuan_ms", ""),
                        "zujuan_ps": lida_data.get("zujuan_ps", ""),
                    })
                else:
                    results_cache[school_name].update({
                        "overall_usage_rate": lida_data.get("overall_usage_rate", ""),
                        "overall_jibei": lida_data.get("overall_jibei", ""),
                        "grade_jibei": lida_data.get("grade_jibei", ""),
                        "department_jibei": lida_data.get("department_jibei", ""),
                    })
                elapsed = asyncio.get_event_loop().time() - t0
                self._push_event(ProgressEvent(
                    school_name, "lida", "completed",
                    "Metabase采集完成(API)",
                    elapsed_seconds=elapsed))
                elapsed_cache.setdefault(school_name, {})["lida"] = round(elapsed, 1)

            async def _collect_grafana_for_school(school_name: str):
                """Grafana 平台采集（单学校）"""
                school = school_configs[school_name]
                if not school:
                    self._push_event(ProgressEvent(school_name, "grafana", "failed", "未找到学校配置"))
                    errors_cache[school_name].append("grafana: 配置不存在")
                    return
                t0 = asyncio.get_event_loop().time()
                self._push_event(ProgressEvent(school_name, "grafana", "running", "正在采集Grafana数据..."))
                grafana_data = None
                used_api = False

                # 尝试 API 直连
                if api_grafana:
                    try:
                        scraper_fn = api_grafana.scrape_monthly if is_monthly else api_grafana.scrape
                        grafana_data = await scraper_fn(school, date_range)
                        has_data = any(v for v in grafana_data.values() if v)
                        if has_data:
                            used_api = True
                            logger.info("[%s] grafana API 采集成功", school_name)
                        else:
                            logger.warning("[%s] grafana API 返回空数据，降级到浏览器", school_name)
                            grafana_data = None
                    except Exception as e:
                        logger.warning("[%s] grafana API 采集失败，降级到浏览器: %s", school_name, e)
                        grafana_data = None

                # 降级到浏览器
                if grafana_data is None and (run_all or "grafana" in platforms):
                    nonlocal grafana
                    if not grafana:
                        if not bm._browser:
                            await bm.start()
                        grafana = GrafanaScraper(bm)
                    try:
                        scraper_fn = getattr(grafana, scrape_method)
                        grafana_data = await scraper_fn(school, date_range)
                        logger.info("[%s] grafana 浏览器采集%s", school_name,
                                    "(API降级)" if api_grafana else "")
                    except Exception as e:
                        logger.error("[%s] grafana采集失败: %s", school_name, e)
                        errors_cache[school_name].append(f"grafana: {e}")
                        self._push_event(ProgressEvent(school_name, "grafana", "failed", f"Grafana采集失败: {e}"))
                        return

                if grafana_data:
                    mode_tag = "API" if used_api else "浏览器"
                    if is_monthly:
                        results_cache[school_name].update({
                            "daily_active_ratio": grafana_data.get("daily_active_ratio", ""),
                            "weekly_active_ratio": grafana_data.get("weekly_active_ratio", ""),
                            "monthly_active_ratio": grafana_data.get("monthly_active_ratio", ""),
                        })
                        if grafana_data.get("data_anomaly"):
                            errors_cache[school_name].append(
                                f"grafana: {grafana_data.get('anomaly_message', '数据异常')}")
                    else:
                        results_cache[school_name].update({
                            "weekly_active_teachers": grafana_data.get("weekly_active_teachers", ""),
                            "weekly_total_teachers": grafana_data.get("weekly_total_teachers", ""),
                            "weekly_active_ratio": grafana_data.get("weekly_active_ratio", ""),
                        })
                    elapsed = asyncio.get_event_loop().time() - t0
                    self._push_event(ProgressEvent(
                        school_name, "grafana", "completed",
                        f"Grafana采集完成({mode_tag})",
                        elapsed_seconds=elapsed))
                    elapsed_cache.setdefault(school_name, {})["grafana"] = round(elapsed, 1)

            async def _collect_db_grafana_for_school(school_name: str):
                """数据库直查替代 Grafana（单学校，纯 SQL 查询）"""
                import sqlite3 as _sqlite3
                school = school_configs[school_name]
                if not school:
                    self._push_event(ProgressEvent(school_name, "grafana", "failed", "未找到学校配置"))
                    errors_cache[school_name].append("grafana: 配置不存在")
                    return
                t0 = asyncio.get_event_loop().time()
                self._push_event(ProgressEvent(school_name, "grafana", "running", "正在查询数据库(替代Grafana)..."))
                try:
                    _MB_DB_PATH = r"E:\worktools\Qodercode\metabase_sync\data\metabase.db"
                    mb_conn = _sqlite3.connect(_MB_DB_PATH)
                    mb_conn.row_factory = _sqlite3.Row
                    start_str = start_date.isoformat()
                    end_str = end_date.isoformat()

                    # 通过 metabase_school_id 获取 metabase 中的正确学校名称
                    mb_school_id = school.get("metabase_school_id", "")
                    mb_school_name = school_name  # 默认使用系统名称
                    if mb_school_id:
                        try:
                            row = mb_conn.execute(
                                "SELECT DISTINCT school_name FROM teacher_base WHERE school_id=?",
                                (int(mb_school_id),),
                            ).fetchone()
                            if row:
                                mb_school_name = row["school_name"]
                                if mb_school_name != school_name:
                                    logger.info("[DB-Grafana] %s -> metabase名称: %s", school_name, mb_school_name)
                        except (ValueError, Exception) as _e:
                            logger.warning("[DB-Grafana] %s school_id解析失败: %s", school_name, _e)

                    total = mb_conn.execute(
                        "SELECT COUNT(*) AS c FROM teacher_base WHERE school_name=? AND state=1",
                        (mb_school_name,),
                    ).fetchone()["c"]

                    if is_monthly:
                        daily = mb_conn.execute(
                            "SELECT COUNT(DISTINCT d.tianli_user_id) AS c "
                            "FROM dws_ingress_teacher_day d "
                            "WHERE d.host = 'research-api.qimingdaren.com' AND CAST(d.tianli_school_id AS TEXT) = ? "
                            "AND substr(d.stat_date,1,10)>=? AND substr(d.stat_date,1,10)<=? "
                            "AND d.school_name NOT LIKE '%启鸣达人%' "
                            "AND d.school_name IS NOT NULL AND d.school_name <> '' "
                            "AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> '' "
                            "AND d.tianli_user_id <> '-' "
                            "AND d.tianli_user_id IN ("
                            "  SELECT t.teacher_id FROM teacher_base t WHERE t.school_id = ?"
                            ")",
                            (str(mb_school_id), start_str, end_str, str(mb_school_id))).fetchone()["c"]

                        weekly_cnt = mb_conn.execute(
                            "SELECT COUNT(*) AS c FROM ("
                            "  SELECT d.tianli_user_id FROM dws_ingress_teacher_day d"
                            "  WHERE d.host = 'research-api.qimingdaren.com' AND CAST(d.tianli_school_id AS TEXT) = ?"
                            "  AND substr(d.stat_date,1,10)>=? AND substr(d.stat_date,1,10)<=?"
                            "  AND d.school_name NOT LIKE '%启鸣达人%'"
                            "  AND d.school_name IS NOT NULL AND d.school_name <> ''"
                            "  AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> ''"
                            "  AND d.tianli_user_id <> '-'"
                            "  AND d.tianli_user_id IN ("
                            "    SELECT t.teacher_id FROM teacher_base t WHERE t.school_id = ?"
                            "  )"
                            "  GROUP BY d.tianli_user_id"
                            "  HAVING COUNT(DISTINCT substr(d.stat_date,1,10)) >= 3"
                            ")", (str(mb_school_id), start_str, end_str, str(mb_school_id))).fetchone()["c"]

                        monthly_cnt = mb_conn.execute(
                            "SELECT COUNT(*) AS c FROM ("
                            "  SELECT d.tianli_user_id FROM dws_ingress_teacher_day d"
                            "  WHERE d.host = 'research-api.qimingdaren.com' AND CAST(d.tianli_school_id AS TEXT) = ?"
                            "  AND substr(d.stat_date,1,10)>=? AND substr(d.stat_date,1,10)<=?"
                            "  AND d.school_name NOT LIKE '%启鸣达人%'"
                            "  AND d.school_name IS NOT NULL AND d.school_name <> ''"
                            "  AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> ''"
                            "  AND d.tianli_user_id <> '-'"
                            "  AND d.tianli_user_id IN ("
                            "    SELECT t.teacher_id FROM teacher_base t WHERE t.school_id = ?"
                            "  )"
                            "  GROUP BY d.tianli_user_id"
                            "  HAVING COUNT(DISTINCT substr(d.stat_date,1,10)) >= 4"
                            ")", (str(mb_school_id), start_str, end_str, str(mb_school_id))).fetchone()["c"]

                        daily_pct = str(round(daily / total * 100, 2)) + "%" if total else ""
                        weekly_pct = str(round(weekly_cnt / total * 100, 2)) + "%" if total else ""
                        monthly_pct = str(round(monthly_cnt / total * 100, 2)) + "%" if total else ""
                        results_cache[school_name].update({
                            "daily_active_ratio": daily_pct,
                            "weekly_active_ratio": weekly_pct,
                            "monthly_active_ratio": monthly_pct,
                        })
                    else:
                        used = mb_conn.execute(
                            "SELECT COUNT(DISTINCT d.tianli_user_id) AS c "
                            "FROM dws_ingress_teacher_day d "
                            "WHERE d.host = 'research-api.qimingdaren.com' AND CAST(d.tianli_school_id AS TEXT) = ? "
                            "AND substr(d.stat_date,1,10)>=? AND substr(d.stat_date,1,10)<=? "
                            "AND d.school_name NOT LIKE '%启鸣达人%' "
                            "AND d.school_name IS NOT NULL AND d.school_name <> '' "
                            "AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> '' "
                            "AND d.tianli_user_id <> '-' "
                            "AND d.tianli_user_id IN ("
                            "  SELECT t.teacher_id FROM teacher_base t WHERE t.school_id = ?"
                            ")",
                            (str(mb_school_id), start_str, end_str, str(mb_school_id))).fetchone()["c"]

                        active = mb_conn.execute(
                            "SELECT COUNT(*) AS c FROM ("
                            "  SELECT d.tianli_user_id FROM dws_ingress_teacher_day d"
                            "  WHERE d.host = 'research-api.qimingdaren.com' AND CAST(d.tianli_school_id AS TEXT) = ?"
                            "  AND substr(d.stat_date,1,10)>=? AND substr(d.stat_date,1,10)<=?"
                            "  AND d.school_name NOT LIKE '%启鸣达人%'"
                            "  AND d.school_name IS NOT NULL AND d.school_name <> ''"
                            "  AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> ''"
                            "  AND d.tianli_user_id <> '-'"
                            "  AND d.tianli_user_id IN ("
                            "    SELECT t.teacher_id FROM teacher_base t WHERE t.school_id = ?"
                            "  )"
                            "  GROUP BY d.tianli_user_id"
                            "  HAVING COUNT(DISTINCT substr(d.stat_date,1,10)) >= 3"
                            ")", (str(mb_school_id), start_str, end_str, str(mb_school_id))).fetchone()["c"]

                        active_ratio = str(round(active / total * 100, 2)) + "%" if total else ""
                        results_cache[school_name].update({
                            "weekly_active_teachers": str(active),
                            "weekly_total_teachers": str(used),
                            "weekly_active_ratio": active_ratio,
                            "db_used_teachers": str(used),
                        })

                    mb_conn.close()
                    elapsed = asyncio.get_event_loop().time() - t0
                    self._push_event(ProgressEvent(
                        school_name, "grafana", "completed",
                        "数据库查询完成(替代Grafana)",
                        elapsed_seconds=round(elapsed, 1)))
                    elapsed_cache.setdefault(school_name, {})["grafana"] = round(elapsed, 1)
                except Exception as e:
                    logger.error("[DB-Grafana] %s 查询失败: %s", school_name, e)
                    errors_cache[school_name].append(f"grafana: DB查询失败: {e}")
                    self._push_event(ProgressEvent(school_name, "grafana", "failed", f"数据库查询失败: {e}"))

            async def _collect_main_site_for_school(school_name: str):
                """主站采集（单学校）"""
                school = school_configs[school_name]
                if not school:
                    self._push_event(ProgressEvent(school_name, "main_site", "failed", "未找到学校配置"))
                    errors_cache[school_name].append("main_site: 配置不存在")
                    return
                t0 = asyncio.get_event_loop().time()
                self._push_event(ProgressEvent(school_name, "main_site", "running", "正在采集主站数据..."))
                main_data = None
                used_api = False

                # 尝试 API 直连
                if api_main_site:
                    try:
                        scraper_fn = api_main_site.scrape_monthly if is_monthly else api_main_site.scrape
                        main_data = await scraper_fn(school, date_range)
                        api_success = main_data.pop("_api_success", False) if main_data else False
                        if api_success:
                            used_api = True
                            logger.info("[%s] 主站 API 采集成功", school_name)
                        else:
                            has_data = any(v for v in main_data.values() if v)
                            if has_data:
                                used_api = True
                                logger.info("[%s] 主站 API 采集成功(有数据)", school_name)
                            else:
                                logger.warning("[%s] 主站 API 返回空数据，降级到浏览器", school_name)
                                main_data = None
                    except Exception as e:
                        logger.warning("[%s] 主站 API 采集失败，降级到浏览器: %s", school_name, e)
                        main_data = None

                # 降级到浏览器（复用 API 的共享 context，避免 Cloud 重复登录）
                if main_data is None and (run_all or "main_site" in platforms):
                    nonlocal main_site
                    if not main_site:
                        if not bm._browser:
                            await bm.start()
                        main_site = MainSiteScraper(bm)
                        # 复用 API 的共享 context（Cloud 已登录，cookie 共享）
                        if api_main_site and api_main_site._shared_context:
                            shared_ctx = api_main_site._shared_context
                            if not shared_ctx.is_closed():
                                main_site._context = shared_ctx
                                main_site._is_shared_context = True
                                main_site._page = await main_site._context.new_page()
                                main_site._logged_in = api_main_site._shared_ctx_logged_in
                                logger.info("[%s] 浏览器复用 API 共享 context (logged_in=%s)",
                                            school_name, api_main_site._shared_ctx_logged_in)
                    try:
                        scraper_fn = getattr(main_site, scrape_method)
                        main_data = await scraper_fn(school, date_range)
                        logger.info("[%s] 主站浏览器采集%s", school_name,
                                    "(API降级)" if api_main_site else "")
                    except Exception as e:
                        logger.error("[%s] 主站采集失败: %s", school_name, e)
                        errors_cache[school_name].append(f"main_site: {e}")
                        self._push_event(ProgressEvent(school_name, "main_site", "failed", f"主站采集失败: {e}"))
                        # 重置 main_site 以便下一学校可以重新复用共享 context
                        if main_site:
                            main_site._logged_in = False
                            main_site._page = None
                            main_site._context = None
                            main_site._is_shared_context = False
                        main_site = None
                        return

                if main_data:
                    mode_tag = "API" if used_api else "浏览器"
                    results_cache[school_name].update({
                        "homework_count": main_data.get("homework_count", ""),
                    })
                    elapsed = asyncio.get_event_loop().time() - t0
                    self._push_event(ProgressEvent(
                        school_name, "main_site", "completed",
                        f"主站采集完成({mode_tag})",
                        elapsed_seconds=elapsed))
                    elapsed_cache.setdefault(school_name, {})["main_site"] = round(elapsed, 1)

            # ── 按平台采集: 每个平台依次采集所有学校 ──
            logger.info("开始按平台采集: Grafana → Metabase → Main site...")
            # collect_start_time 用于最终耗时统计

            def _check_pause(school_name: str):
                """检查暂停状态"""
                if not self._pause_event.is_set():
                    self._push_event(ProgressEvent(school_name, "system", "pending", "采集已暂停，等待继续..."))
                    self._pause_event.wait()
                    self._push_event(ProgressEvent(school_name, "system", "running", "采集已继续"))



            # ── Phase 1: Grafana 或 数据库直查（所有学校）──
            if data_source == "database":
                logger.info("=== Phase 1: 数据库直查 替代Grafana（%d 个学校）===", len(school_names))
                for school_name in school_names:
                    _check_pause(school_name)
                    logger.info("=== 数据库直查: %s ===", school_name)
                    await _collect_db_grafana_for_school(school_name)
            elif grafana or api_grafana:
                logger.info("=== Phase 1: Grafana 采集（%d 个学校）===", len(school_names))
                for school_name in school_names:
                    _check_pause(school_name)
                    logger.info("=== Grafana 采集: %s ===", school_name)
                    await _collect_grafana_for_school(school_name)
                if grafana:
                    await grafana.close()
                if api_grafana:
                    await api_grafana.close()

            # ── Phase 2+3: Lida + Main Site 并行（各平台内学校顺序执行）──
            async def _run_lida_all():
                """Metabase 平台：依次采集所有学校（纯 API）"""
                if not api_lida:
                    return
                logger.info("=== Metabase 采集（%d 个学校）===", len(school_names))
                for school_name in school_names:
                    _check_pause(school_name)
                    logger.info("=== Metabase 采集: %s ===", school_name)
                    await _collect_lida_for_school(school_name)

                if api_lida:
                    await api_lida.close()

            async def _run_main_site_all():
                """主站平台：依次采集所有学校，API 和浏览器共享同一个 context"""
                nonlocal main_site
                if not (main_site or api_main_site):
                    return

                # 创建共享浏览器 context（API 和浏览器共用，Cloud 只登录一次）
                shared_ctx = None
                if api_main_site:
                    if not bm._browser:
                        await bm.start()
                    shared_ctx = await bm._browser.new_context(
                        no_viewport=True, bypass_csp=True)
                    api_main_site.set_shared_context(shared_ctx)
                    logger.info("=== 主站采集（%d 个学校，共享 context）===", len(school_names))
                else:
                    logger.info("=== 主站采集（%d 个学校）===", len(school_names))

                for school_name in school_names:
                    _check_pause(school_name)
                    logger.info("=== 主站采集: %s ===", school_name)
                    await _collect_main_site_for_school(school_name)
                    # 每校完成后关闭多余标签页，保留运维页面
                    if main_site and school_name != school_names[-1]:
                        await main_site.cleanup_between_schools()
                    # 两校之间短暂延迟，避免触发 Cloud 安全验证
                    if school_name != school_names[-1]:
                        import asyncio as _aio
                        await _aio.sleep(2)

                if main_site:
                    await main_site.close()
                if api_main_site:
                    await api_main_site.close()
                # 关闭共享 context（如果浏览器没有接管的话）
                if shared_ctx and (not main_site or main_site._context is not shared_ctx):
                    try:
                        await shared_ctx.close()
                    except Exception:
                        pass

            # Lida + 主站并行采集
            # 共享 context 方案：主站 API 和浏览器共用同一个浏览器 context，
            # Cloud 只登录一次，避免独立 context 登录杀死会话的问题。
            # Lida 是不同平台，不受影响。
            parallel_phases = []
            if api_lida:
                parallel_phases.append(_run_lida_all())
            if main_site or api_main_site:
                parallel_phases.append(_run_main_site_all())
            if parallel_phases:
                logger.info("=== Lida + 主站 并行采集 ===")
                await asyncio.gather(*parallel_phases)
            logger.info("=== Lida + 主站 采集完成 ===")


            # ── 合并结果并保存记录 ──
            for school_name in school_names:
                school = school_configs[school_name]
                if not school:
                    self._push_event(ProgressEvent(
                        school_name, "all", "failed", f"未找到学校配置: {school_name}"
                    ))
                    results_summary.append({"school": school_name, "status": "failed", "error": "配置不存在"})
                    continue

                data = results_cache[school_name]
                errors = errors_cache[school_name]

                if is_monthly:
                    record = MonthlyRecord(
                        school_name=school_name,
                        year=year,
                        month_number=month_number or week_number,
                        collected_at=datetime.now().isoformat(),
                        month_start_date=start_date.isoformat(),
                        month_end_date=end_date.isoformat(),
                    )
                    record.overall_usage_rate = data.get("overall_usage_rate", "")
                    record.overall_jibei = data.get("overall_jibei", "")
                    record.platform_usage = data.get("platform_usage", "")
                    record.platform_usage_hs = data.get("platform_usage_hs", "")
                    record.platform_usage_ms = data.get("platform_usage_ms", "")
                    record.platform_usage_ps = data.get("platform_usage_ps", "")
                    record.jibei_hs = data.get("jibei_hs", "")
                    record.jibei_ms = data.get("jibei_ms", "")
                    record.jibei_ps = data.get("jibei_ps", "")
                    record.zujuan = data.get("zujuan", "")
                    record.zujuan_hs = data.get("zujuan_hs", "")
                    record.zujuan_ms = data.get("zujuan_ms", "")
                    record.zujuan_ps = data.get("zujuan_ps", "")
                    record.homework_count = data.get("homework_count", "")
                    record.daily_active_ratio = data.get("daily_active_ratio", "")
                    record.weekly_active_ratio = data.get("weekly_active_ratio", "")
                    record.monthly_active_ratio = data.get("monthly_active_ratio", "")

                    if errors:
                        record.status = "partial" if any([
                            record.overall_usage_rate, record.homework_count,
                            record.daily_active_ratio
                        ]) else "failed"
                        record.error_message = "; ".join(errors)
                    else:
                        record.status = "success"

                    # 数据库模式: 标记数据来源
                    if data_source == "database":
                        record.data_source = "database"
                    record.platform_elapsed = json.dumps(elapsed_cache.get(school_name, {}))
                    record.save()
                else:
                    record = WeeklyRecord(
                        school_name=school_name,
                        year=year,
                        week_number=week_number,
                        collected_at=datetime.now().isoformat(),
                        week_start_date=start_date.isoformat(),
                        week_end_date=end_date.isoformat(),
                    )
                    record.overall_usage_rate = data.get("overall_usage_rate", "")
                    record.overall_jibei = data.get("overall_jibei", "")
                    record.grade_jibei = data.get("grade_jibei", "")
                    record.department_jibei = data.get("department_jibei", "")
                    record.weekly_active_teachers = data.get("weekly_active_teachers", "")
                    record.weekly_total_teachers = data.get("weekly_total_teachers", "")
                    # 计算本周整体活跃度 = 活跃教师/总教师 * 100%
                    try:
                        at = float(str(record.weekly_active_teachers).replace(',', ''))
                        tt = float(str(record.weekly_total_teachers).replace(',', ''))
                        if tt > 0:
                            record.weekly_overall_activity = str(round(at / tt * 100, 2)) + '%'
                    except (ValueError, TypeError):
                        pass
                    record.weekly_active_ratio = data.get("weekly_active_ratio", "")
                    record.homework_count = data.get("homework_count", "")

                    if errors:
                        record.status = "partial" if any([
                            record.overall_usage_rate, record.weekly_active_teachers, record.homework_count
                        ]) else "failed"
                        record.error_message = "; ".join(errors)
                    else:
                        record.status = "success"

                    # 数据库模式: 标记数据来源
                    if data_source == "database":
                        record.data_source = "database"
                    record.platform_elapsed = json.dumps(elapsed_cache.get(school_name, {}))
                    record.save()

                results_summary.append({
                    "school": school_name,
                    "status": record.status,
                    "error": record.error_message,
                })

                self._push_event(ProgressEvent(
                    school_name, "all",
                    record.status,
                    f"{school_name} 采集{'完成' if record.status == 'success' else '部分完成: ' + record.error_message}"
                ))

        except Exception as e:
            logger.error("采集任务异常: %s", e, exc_info=True)
            self._update_task(task_id, "failed", str(e))
        finally:
            try:
                await asyncio.wait_for(bm.stop(), timeout=15)
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning("浏览器关闭超时或异常: %s", e)
            self._running = False
            self._is_paused = False
            self._pause_event.set()  # 确保不会卡住
            self._current_task_id = None
            self._current_user_id = None
            summary_json = json.dumps(results_summary, ensure_ascii=False)
            self._update_task(task_id, "completed", summary_json)
            total_elapsed = asyncio.get_event_loop().time() - collect_start_time
            self._push_event(ProgressEvent("", "system", "completed", "所有学校采集完成",
                                           elapsed_seconds=total_elapsed))

    def _push_event(self, event: ProgressEvent):
        """广播进度事件到所有订阅者"""
        with self._sub_lock:
            for q in self._subscribers.values():
                q.put(event)
