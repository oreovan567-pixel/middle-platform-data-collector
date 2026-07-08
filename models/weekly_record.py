"""周表记录数据模型"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

from models.database import get_connection


@dataclass
class WeeklyRecord:
    school_name: str
    year: int
    week_number: str
    collected_at: str
    status: str = "success"
    week_start_date: str = ""
    week_end_date: str = ""
    overall_usage_rate: str = ""
    overall_jibei: str = ""
    grade_jibei: str = ""
    department_jibei: str = ""
    homework_count: str = ""
    weekly_active_teachers: str = ""
    weekly_total_teachers: str = ""
    weekly_overall_activity: str = ""
    weekly_active_ratio: str = ""
    error_message: str = ""
    platform_elapsed: str = ""
    data_source: str = "grafana"
    id: int | None = None

    def save(self):
        """插入或更新记录（UPSERT）"""
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO weekly_records (
                    school_name, year, week_number, week_start_date, week_end_date,
                    overall_usage_rate, overall_jibei, grade_jibei, department_jibei,
                    homework_count, weekly_active_teachers, weekly_total_teachers,
                    weekly_overall_activity, weekly_active_ratio, collected_at, status, error_message, platform_elapsed, data_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(school_name, year, week_number) DO UPDATE SET
                    week_start_date = excluded.week_start_date,
                    week_end_date = excluded.week_end_date,
                    overall_usage_rate = excluded.overall_usage_rate,
                    overall_jibei = excluded.overall_jibei,
                    grade_jibei = excluded.grade_jibei,
                    department_jibei = excluded.department_jibei,
                    homework_count = excluded.homework_count,
                    weekly_active_teachers = excluded.weekly_active_teachers,
                    weekly_total_teachers = excluded.weekly_total_teachers,
                    weekly_overall_activity = excluded.weekly_overall_activity,
                    weekly_active_ratio = excluded.weekly_active_ratio,
                    collected_at = excluded.collected_at,
                    status = excluded.status,
                    error_message = excluded.error_message,
                    platform_elapsed = excluded.platform_elapsed,
                    data_source = excluded.data_source
            """, (
                self.school_name, self.year, self.week_number,
                self.week_start_date, self.week_end_date,
                self.overall_usage_rate, self.overall_jibei,
                self.grade_jibei, self.department_jibei,
                self.homework_count, self.weekly_active_teachers,
                self.weekly_total_teachers, self.weekly_overall_activity, self.weekly_active_ratio,
                self.collected_at, self.status, self.error_message,
                self.platform_elapsed, self.data_source,
            ))

    @staticmethod
    def query(year: int, week_number: str, school_name: str = "") -> list["WeeklyRecord"]:
        """按条件查询记录"""
        with get_connection() as conn:
            if school_name:
                rows = conn.execute(
                    "SELECT * FROM weekly_records WHERE year=? AND week_number=? AND school_name=? ORDER BY school_name",
                    (year, week_number, school_name),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM weekly_records WHERE year=? AND week_number=? ORDER BY school_name",
                    (year, week_number),
                ).fetchall()
            return [WeeklyRecord._from_row(r) for r in rows]

    @staticmethod
    def query_flexible(year: int, week_number: str = "", school_name: str = "", month_prefix: str = "") -> list["WeeklyRecord"]:
        """灵活查询：年份必选，周次、学校、月份前缀可选。按采集时间倒序排列。"""
        sql = "SELECT * FROM weekly_records WHERE year=?"
        params: list = [year]
        if week_number:
            sql += " AND week_number=?"
            params.append(week_number)
        elif month_prefix:
            sql += " AND week_number LIKE ?"
            params.append(month_prefix + '%')
        if school_name:
            sql += " AND school_name=?"
            params.append(school_name)
        sql += " ORDER BY collected_at DESC"
        with get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [WeeklyRecord._from_row(r) for r in rows]

    @staticmethod
    def query_latest(limit: int = 50) -> list["WeeklyRecord"]:
        """查询最近的记录"""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM weekly_records ORDER BY collected_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [WeeklyRecord._from_row(r) for r in rows]

    @staticmethod
    def query_recent_days(days: int = 10) -> list["WeeklyRecord"]:
        """查询最近N天的采集记录"""
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM weekly_records WHERE collected_at >= ? ORDER BY collected_at DESC",
                (cutoff_date,),
            ).fetchall()
            return [WeeklyRecord._from_row(r) for r in rows]

    @staticmethod
    def query_distinct_weeks(year: int) -> list[str]:
        """查询指定年份已有的不重复周标签"""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT week_number FROM weekly_records WHERE year=? ORDER BY collected_at",
                (year,),
            ).fetchall()
            return [r["week_number"] for r in rows]

    @staticmethod
    def _from_row(row) -> "WeeklyRecord":
        return WeeklyRecord(
            id=row["id"],
            school_name=row["school_name"],
            year=row["year"],
            week_number=row["week_number"],
            week_start_date=row["week_start_date"] or "",
            week_end_date=row["week_end_date"] or "",
            overall_usage_rate=row["overall_usage_rate"] or "",
            overall_jibei=row["overall_jibei"] or "",
            grade_jibei=row["grade_jibei"] or "",
            department_jibei=row["department_jibei"] or "",
            homework_count=row["homework_count"] or "",
            weekly_active_teachers=row["weekly_active_teachers"] or "",
            weekly_total_teachers=row["weekly_total_teachers"] or "",
            weekly_overall_activity=row["weekly_overall_activity"] or "",
            weekly_active_ratio=row["weekly_active_ratio"] or "",
            collected_at=row["collected_at"],
            status=row["status"],
            error_message=row["error_message"] or "",
            platform_elapsed=row["platform_elapsed"] if "platform_elapsed" in row.keys() else "",
            data_source=row["data_source"] if "data_source" in row.keys() else "grafana",
        )

    def to_dict(self) -> dict:
        return asdict(self)
