"""活跃统计页面及API"""
import sqlite3
from pathlib import Path

from flask import Blueprint, render_template, request, jsonify

from config.config_loader import get_metabase_db_path

activity_bp = Blueprint("activity", __name__)


def _get_mb_conn():
    """获取 metabase.db 连接"""
    db_path = get_metabase_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"metabase.db 不存在: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


@activity_bp.route("/activity")
def activity_page():
    """活跃统计看板页面"""
    return render_template("activity.html")


@activity_bp.route("/api/activity/schools")
def list_schools():
    """返回 teacher_base 中启用的学校列表"""
    conn = None
    try:
        conn = _get_mb_conn()
        rows = conn.execute("""
            SELECT school_name, COUNT(*) AS cnt
            FROM teacher_base
            WHERE school_name IS NOT NULL AND school_name != '' AND state=1
            GROUP BY school_name
            ORDER BY cnt DESC
        """).fetchall()
        schools = [r["school_name"] for r in rows]
        return jsonify({"schools": schools})
    finally:
        if conn:
                conn.close()


@activity_bp.route("/api/activity/weekly")
def weekly_stats():
    """周活跃统计"""
    start = request.args.get("start_date", "")
    end = request.args.get("end_date", "")
    school = request.args.get("school_name", "")
    if not start or not end or not school:
        return jsonify({"error": "缺少参数"}), 400

    conn = None
    try:
        conn = _get_mb_conn()
        # 1. 学校总教师数 (state=1 启用)
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM teacher_base WHERE school_name=? AND state=1",
            (school,),
        ).fetchone()["c"]

        # 2. 使用教师总数 (去重, 时间范围内有访问记录)
        used = conn.execute("""
            SELECT COUNT(DISTINCT tianli_user_id) AS c
            FROM dws_ingress_teacher_day
            WHERE school_name=?
              AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
              AND pv_count IS NOT NULL AND pv_count>0
        """, (school, start, end)).fetchone()["c"]

        # 3. 活跃教师数 (访问天数>=3, 去重)
        active = conn.execute("""
            SELECT COUNT(*) AS c FROM (
                SELECT tianli_user_id
                FROM dws_ingress_teacher_day
                WHERE school_name=?
                  AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
                  AND pv_count IS NOT NULL AND pv_count>0
                GROUP BY tianli_user_id
                HAVING COUNT(DISTINCT substr(stat_date,1,10)) >= 3
            )
        """, (school, start, end)).fetchone()["c"]

        # 4. 整体活跃度 & 周活比例
        activity_rate = round(active / used * 100, 1) if used else 0
        weekly_ratio = round(active / total * 100, 1) if total else 0

        return jsonify({
            "total_teachers": total,
            "used_teachers": used,
            "active_teachers": active,
            "activity_rate": activity_rate,
            "weekly_ratio": weekly_ratio,
        })
    finally:
        if conn:
                conn.close()


@activity_bp.route("/api/activity/monthly")
def monthly_stats():
    """月活跃统计"""
    start = request.args.get("start_date", "")
    end = request.args.get("end_date", "")
    school = request.args.get("school_name", "")
    if not start or not end or not school:
        return jsonify({"error": "缺少参数"}), 400

    conn = None
    try:
        conn = _get_mb_conn()
        # 1. 学校总教师数
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM teacher_base WHERE school_name=? AND state=1",
            (school,),
        ).fetchone()["c"]

        # 2. 日活教师数 (去重, 时间范围内有访问记录)
        daily = conn.execute("""
            SELECT COUNT(DISTINCT tianli_user_id) AS c
            FROM dws_ingress_teacher_day
            WHERE school_name=?
              AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
              AND pv_count IS NOT NULL AND pv_count>0
        """, (school, start, end)).fetchone()["c"]

        # 3. 周活教师数 (访问>=3天, 去重)
        weekly = conn.execute("""
            SELECT COUNT(*) AS c FROM (
                SELECT tianli_user_id
                FROM dws_ingress_teacher_day
                WHERE school_name=?
                  AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
                  AND pv_count IS NOT NULL AND pv_count>0
                GROUP BY tianli_user_id
                HAVING COUNT(DISTINCT substr(stat_date,1,10)) >= 3
            )
        """, (school, start, end)).fetchone()["c"]

        # 4. 月活教师数 (访问>=4天, 去重)
        monthly = conn.execute("""
            SELECT COUNT(*) AS c FROM (
                SELECT tianli_user_id
                FROM dws_ingress_teacher_day
                WHERE school_name=?
                  AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
                  AND pv_count IS NOT NULL AND pv_count>0
                GROUP BY tianli_user_id
                HAVING COUNT(DISTINCT substr(stat_date,1,10)) >= 4
            )
        """, (school, start, end)).fetchone()["c"]

        daily_ratio = round(daily / total * 100, 1) if total else 0
        weekly_ratio = round(weekly / total * 100, 1) if total else 0
        monthly_ratio = round(monthly / total * 100, 1) if total else 0

        return jsonify({
            "total_teachers": total,
            "daily_active": daily,
            "daily_ratio": daily_ratio,
            "weekly_active": weekly,
            "weekly_ratio": weekly_ratio,
            "monthly_active": monthly,
            "monthly_ratio": monthly_ratio,
        })
    finally:
        if conn:
                conn.close()
