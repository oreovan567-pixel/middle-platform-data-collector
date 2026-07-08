"""数据库直查采集器 - 直接查询 metabase.db 计算活跃度指标

轻量级采集模式，不启动浏览器，直接查询 metabase.db 数据库。
支持批量学校采集，结果保存到 weekly_records / monthly_records。
"""
from __future__ import annotations
import asyncio
import json
import logging
import queue
import threading
from datetime import date, datetime
from pathlib import Path

from models.database import get_connection
from models.weekly_record import WeeklyRecord
from models.monthly_record import MonthlyRecord

logger = logging.getLogger(__name__)

# metabase.db 路径（与 activity.py 保持一致）
_MB_DB = Path(r"E:\worktools\Qodercode\metabase_sync\data\metabase.db")


class ProgressEvent:
    """采集进度事件（与 Collector.ProgressEvent 格式一致）"""

    def __init__(self, school: str, platform: str, status: str, message: str = "",
                 elapsed_seconds: float = 0):
        self.school = school
        self.platform = platform
        self.status = status
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


class DbCollector:
    """
    数据库直查采集器。
    直接查询 metabase.db 计算活跃度指标，不启动浏览器。
    与 Collector 共享互斥锁，同一时间只能有一个采集器运行。
    """

    def __init__(self):
        self._subscribers: dict[int, queue.Queue] = {}
        self._sub_lock = threading.Lock()
        self._sub_counter = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._current_task_id: int | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def current_task_id(self) -> int | None:
        return self._current_task_id

    def subscribe(self) -> tuple[int, queue.Queue]:
        with self._sub_lock:
            self._sub_counter += 1
            sub_id = self._sub_counter
            q: queue.Queue = queue.Queue()
            self._subscribers[sub_id] = q
            return sub_id, q

    def unsubscribe(self, sub_id: int):
        with self._sub_lock:
            self._subscribers.pop(sub_id, None)

    def _push_event(self, event: ProgressEvent):
        with self._sub_lock:
            for q in self._subscribers.values():
                q.put(event)

    def start_collect(
        self,
        school_names: list[str],
        year: int,
        week_number: str,
        start_date: date,
        end_date: date,
        record_type: str = "weekly",
        month_number: str = "",
    ) -> int:
        """启动数据库直查采集任务"""
        if self._running:
            raise RuntimeError("已有数据库采集任务正在执行")

        task_id = self._create_task(year, week_number, school_names, record_type)
        self._current_task_id = task_id

        self._running = True
        self._thread = threading.Thread(
            target=self._run_thread,
            args=(task_id, school_names, year, week_number,
                  start_date, end_date, record_type, month_number),
            daemon=True,
        )
        self._thread.start()
        return task_id

    def _create_task(self, year, week_number, schools, record_type="weekly"):
        with get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO collect_tasks (year, week_number, schools, status, started_at, record_type) "
                "VALUES (?, ?, ?, 'running', ?, ?)",
                (year, week_number, json.dumps(schools, ensure_ascii=False),
                 datetime.now().isoformat(), record_type),
            )
            return cursor.lastrowid

    def _update_task(self, task_id, status, result_summary=""):
        with get_connection() as conn:
            conn.execute(
                "UPDATE collect_tasks SET status=?, finished_at=?, result_summary=? WHERE id=?",
                (status, datetime.now().isoformat(), result_summary, task_id),
            )

    def _run_thread(self, task_id, school_names, year, week_number,
                    start_date, end_date, record_type, month_number):
        """后台线程入口"""
        asyncio.run(self._run_async(
            task_id, school_names, year, week_number,
            start_date, end_date, record_type, month_number
        ))

    async def _run_async(self, task_id, school_names, year, week_number,
                         start_date, end_date, record_type, month_number):
        """异步采集主逻辑"""
        import sqlite3
        is_monthly = (record_type == "monthly")
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        results_summary = []

        collect_start = asyncio.get_event_loop().time()

        # 等待前端 SSE 连接建立（前端在 POST 成功后才连接 SSE）
        await asyncio.sleep(2)

        self._push_event(ProgressEvent("", "system", "running",
                                       "数据库直查采集启动..."))

        try:
            mb_conn = sqlite3.connect(str(_MB_DB))
            mb_conn.row_factory = sqlite3.Row

            for i, school_name in enumerate(school_names):
                t0 = asyncio.get_event_loop().time()
                self._push_event(ProgressEvent(
                    school_name, "database", "running",
                    f"正在查询数据库 ({i+1}/{len(school_names)})..."
                ))

                try:
                    if is_monthly:
                        self._collect_monthly(mb_conn, school_name, year,
                                              month_number or week_number,
                                              start_str, end_str)
                    else:
                        self._collect_weekly(mb_conn, school_name, year,
                                             week_number, start_str, end_str)

                    elapsed = asyncio.get_event_loop().time() - t0
                    self._push_event(ProgressEvent(
                        school_name, "database", "completed",
                        "数据库查询完成",
                        elapsed_seconds=elapsed
                    ))
                    results_summary.append({
                        "school": school_name, "status": "success", "error": ""
                    })

                except Exception as e:
                    logger.error("[DB采集] %s 失败: %s", school_name, e)
                    self._push_event(ProgressEvent(
                        school_name, "database", "failed",
                        f"查询失败: {e}"
                    ))
                    results_summary.append({
                        "school": school_name, "status": "failed", "error": str(e)
                    })

            mb_conn.close()

        except Exception as e:
            logger.error("数据库采集任务异常: %s", e, exc_info=True)
            self._update_task(task_id, "failed", str(e))
        finally:
            self._running = False
            self._current_task_id = None
            summary_json = json.dumps(results_summary, ensure_ascii=False)
            self._update_task(task_id, "completed", summary_json)
            total_elapsed = asyncio.get_event_loop().time() - collect_start
            self._push_event(ProgressEvent(
                "", "system", "completed",
                "数据库直查采集完成",
                elapsed_seconds=total_elapsed
            ))

    def _collect_weekly(self, mb_conn, school_name, year, week_number,
                        start_str, end_str):
        """周表：查询 metabase.db 并保存"""

        # 1. 学校总教师数
        total = mb_conn.execute(
            "SELECT COUNT(*) AS c FROM teacher_base WHERE school_name=? AND state=1",
            (school_name,),
        ).fetchone()["c"]

        # 2. 使用教师总数
        used = mb_conn.execute("""
            SELECT COUNT(DISTINCT tianli_user_id) AS c
            FROM dws_ingress_teacher_day
            WHERE school_name=?
              AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
              AND pv_count IS NOT NULL AND pv_count>0
        """, (school_name, start_str, end_str)).fetchone()["c"]

        # 3. 活跃教师数 (>=3天)
        active = mb_conn.execute("""
            SELECT COUNT(*) AS c FROM (
                SELECT tianli_user_id
                FROM dws_ingress_teacher_day
                WHERE school_name=?
                  AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
                  AND pv_count IS NOT NULL AND pv_count>0
                GROUP BY tianli_user_id
                HAVING COUNT(DISTINCT substr(stat_date,1,10)) >= 3
            )
        """, (school_name, start_str, end_str)).fetchone()["c"]

        # 4. 计算比例
        overall_activity = str(round(active / used * 100, 2)) + "%" if used else ""
        active_ratio = str(round(active / total * 100, 2)) + "%" if total else ""

        # 5. 保存记录
        record = WeeklyRecord(
            school_name=school_name,
            year=year,
            week_number=week_number,
            week_start_date=start_str,
            week_end_date=end_str,
            collected_at=datetime.now().isoformat(),
            status="success",
            overall_usage_rate=str(used),
            weekly_active_teachers=str(active),
            weekly_total_teachers=str(total),
            weekly_overall_activity=overall_activity,
            weekly_active_ratio=active_ratio,
            data_source="database",
        )
        record.save()

    def _collect_monthly(self, mb_conn, school_name, year, month_number,
                         start_str, end_str):
        """月表：查询 metabase.db 并保存"""

        # 1. 学校总教师数
        total = mb_conn.execute(
            "SELECT COUNT(*) AS c FROM teacher_base WHERE school_name=? AND state=1",
            (school_name,),
        ).fetchone()["c"]

        # 2. 日活教师数 (去重)
        daily = mb_conn.execute("""
            SELECT COUNT(DISTINCT tianli_user_id) AS c
            FROM dws_ingress_teacher_day
            WHERE school_name=?
              AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
              AND pv_count IS NOT NULL AND pv_count>0
        """, (school_name, start_str, end_str)).fetchone()["c"]

        # 3. 周活教师数 (>=3天)
        weekly = mb_conn.execute("""
            SELECT COUNT(*) AS c FROM (
                SELECT tianli_user_id
                FROM dws_ingress_teacher_day
                WHERE school_name=?
                  AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
                  AND pv_count IS NOT NULL AND pv_count>0
                GROUP BY tianli_user_id
                HAVING COUNT(DISTINCT substr(stat_date,1,10)) >= 3
            )
        """, (school_name, start_str, end_str)).fetchone()["c"]

        # 4. 月活教师数 (>=4天)
        monthly = mb_conn.execute("""
            SELECT COUNT(*) AS c FROM (
                SELECT tianli_user_id
                FROM dws_ingress_teacher_day
                WHERE school_name=?
                  AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
                  AND pv_count IS NOT NULL AND pv_count>0
                GROUP BY tianli_user_id
                HAVING COUNT(DISTINCT substr(stat_date,1,10)) >= 4
            )
        """, (school_name, start_str, end_str)).fetchone()["c"]

        # 5. 保存记录（存绝对数值，非百分比）
        record = MonthlyRecord(
            school_name=school_name,
            year=year,
            month_number=month_number,
            month_start_date=start_str,
            month_end_date=end_str,
            collected_at=datetime.now().isoformat(),
            status="success",
            overall_usage_rate=str(total),
            daily_active_ratio=str(daily),
            weekly_active_ratio=str(weekly),
            monthly_active_ratio=str(monthly),
            data_source="database",
        )
        record.save()
