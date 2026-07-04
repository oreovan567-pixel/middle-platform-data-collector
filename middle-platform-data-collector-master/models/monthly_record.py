"""月度记录数据模型"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

from models.database import get_connection


@dataclass
class MonthlyRecord:
    school_name: str
    year: int
    month_number: str
    collected_at: str
    status: str = "success"
    month_start_date: str = ""
    month_end_date: str = ""
    # LIDA: 整体使用率
    overall_usage_rate: str = ""
    # LIDA: 平台使用率 (4段)
    platform_usage: str = ""
    platform_usage_hs: str = ""
    platform_usage_ms: str = ""
    platform_usage_ps: str = ""
    # LIDA: 整体集备
    overall_jibei: str = ""
    # LIDA: 集备模块 (3段)
    jibei_hs: str = ""
    jibei_ms: str = ""
    jibei_ps: str = ""
    # LIDA: 组卷模块 (4段)
    zujuan: str = ""
    zujuan_hs: str = ""
    zujuan_ms: str = ""
    zujuan_ps: str = ""
    # 主站: 作业次数(班级累加)
    homework_count: str = ""
    # Grafana: 活跃度
    daily_active_ratio: str = ""
    weekly_active_ratio: str = ""
    monthly_active_ratio: str = ""
    error_message: str = ""
    platform_elapsed: str = ""
    data_source: str = "grafana"
    id: int | None = None

    def save(self):
        """插入或更新记录（UPSERT）"""
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO monthly_records (
                    school_name, year, month_number, month_start_date, month_end_date,
                    overall_usage_rate,
                    platform_usage, platform_usage_hs, platform_usage_ms, platform_usage_ps,
                    overall_jibei,
                    jibei_hs, jibei_ms, jibei_ps,
                    zujuan, zujuan_hs, zujuan_ms, zujuan_ps,
                    homework_count,
                    daily_active_ratio, weekly_active_ratio, monthly_active_ratio,
                    collected_at, status, error_message, platform_elapsed, data_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(school_name, year, month_number) DO UPDATE SET
                    month_start_date = excluded.month_start_date,
                    month_end_date = excluded.month_end_date,
                    overall_usage_rate = excluded.overall_usage_rate,
                    platform_usage = excluded.platform_usage,
                    platform_usage_hs = excluded.platform_usage_hs,
                    platform_usage_ms = excluded.platform_usage_ms,
                    platform_usage_ps = excluded.platform_usage_ps,
                    overall_jibei = excluded.overall_jibei,
                    jibei_hs = excluded.jibei_hs,
                    jibei_ms = excluded.jibei_ms,
                    jibei_ps = excluded.jibei_ps,
                    zujuan = excluded.zujuan,
                    zujuan_hs = excluded.zujuan_hs,
                    zujuan_ms = excluded.zujuan_ms,
                    zujuan_ps = excluded.zujuan_ps,
                    homework_count = excluded.homework_count,
                    daily_active_ratio = excluded.daily_active_ratio,
                    weekly_active_ratio = excluded.weekly_active_ratio,
                    monthly_active_ratio = excluded.monthly_active_ratio,
                    collected_at = excluded.collected_at,
                    status = excluded.status,
                    error_message = excluded.error_message,
                    platform_elapsed = excluded.platform_elapsed,
                    data_source = excluded.data_source
            """, (
                self.school_name, self.year, self.month_number,
                self.month_start_date, self.month_end_date,
                self.overall_usage_rate,
                self.platform_usage, self.platform_usage_hs,
                self.platform_usage_ms, self.platform_usage_ps,
                self.overall_jibei,
                self.jibei_hs, self.jibei_ms, self.jibei_ps,
                self.zujuan, self.zujuan_hs, self.zujuan_ms, self.zujuan_ps,
                self.homework_count,
                self.daily_active_ratio, self.weekly_active_ratio, self.monthly_active_ratio,
                self.collected_at, self.status, self.error_message,
                self.platform_elapsed, self.data_source,
            ))

    @staticmethod
    def query(year: int, month_number: str, school_name: str = "") -> list["MonthlyRecord"]:
        """按条件查询记录"""
        with get_connection() as conn:
            if school_name:
                rows = conn.execute(
                    "SELECT * FROM monthly_records WHERE year=? AND month_number=? AND school_name=? ORDER BY school_name",
                    (year, month_number, school_name),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM monthly_records WHERE year=? AND month_number=? ORDER BY school_name",
                    (year, month_number),
                ).fetchall()
            return [MonthlyRecord._from_row(r) for r in rows]

    @staticmethod
    def query_flexible(year: int, month_number: str = "", school_name: str = "") -> list["MonthlyRecord"]:
        """灵活查询：年份必选，月次、学校可选。按采集时间倒序排列。"""
        sql = "SELECT * FROM monthly_records WHERE year=?"
        params: list = [year]
        if month_number:
            sql += " AND month_number=?"
            params.append(month_number)
        if school_name:
            sql += " AND school_name=?"
            params.append(school_name)
        sql += " ORDER BY collected_at DESC"
        with get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [MonthlyRecord._from_row(r) for r in rows]

    @staticmethod
    def query_latest(limit: int = 50) -> list["MonthlyRecord"]:
        """查询最近的记录"""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM monthly_records ORDER BY collected_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [MonthlyRecord._from_row(r) for r in rows]

    @staticmethod
    def query_recent_days(days: int = 30) -> list["MonthlyRecord"]:
        """查询最近N天的采集记录"""
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM monthly_records WHERE collected_at >= ? ORDER BY collected_at DESC",
                (cutoff_date,),
            ).fetchall()
            return [MonthlyRecord._from_row(r) for r in rows]

    @staticmethod
    def query_distinct_months(year: int) -> list[str]:
        """查询指定年份已有的不重复月次标签"""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT month_number FROM monthly_records WHERE year=? ORDER BY collected_at",
                (year,),
            ).fetchall()
            return [r["month_number"] for r in rows]

    @staticmethod
    def _from_row(row) -> "MonthlyRecord":
        return MonthlyRecord(
            id=row["id"],
            school_name=row["school_name"],
            year=row["year"],
            month_number=row["month_number"],
            month_start_date=row["month_start_date"] or "",
            month_end_date=row["month_end_date"] or "",
            overall_usage_rate=row["overall_usage_rate"] or "",
            platform_usage=row["platform_usage"] or "",
            platform_usage_hs=row["platform_usage_hs"] or "",
            platform_usage_ms=row["platform_usage_ms"] or "",
            platform_usage_ps=row["platform_usage_ps"] or "",
            overall_jibei=row["overall_jibei"] or "",
            jibei_hs=row["jibei_hs"] or "",
            jibei_ms=row["jibei_ms"] or "",
            jibei_ps=row["jibei_ps"] or "",
            zujuan=row["zujuan"] or "",
            zujuan_hs=row["zujuan_hs"] or "",
            zujuan_ms=row["zujuan_ms"] or "",
            zujuan_ps=row["zujuan_ps"] or "",
            homework_count=row["homework_count"] or "",
            daily_active_ratio=row["daily_active_ratio"] or "",
            weekly_active_ratio=row["weekly_active_ratio"] or "",
            monthly_active_ratio=row["monthly_active_ratio"] or "",
            collected_at=row["collected_at"],
            status=row["status"],
            error_message=row["error_message"] or "",
            platform_elapsed=row["platform_elapsed"] if "platform_elapsed" in row.keys() else "",
            data_source=row["data_source"] if "data_source" in row.keys() else "grafana",
        )

    def to_dict(self) -> dict:
        return asdict(self)
