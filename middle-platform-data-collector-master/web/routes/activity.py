"""活跃统计页面及API — 优先使用 Metabase API 实时数据"""
import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from flask import Blueprint, render_template, request, jsonify

from config.config_loader import get_metabase_db_path

logger = logging.getLogger(__name__)
activity_bp = Blueprint("activity", __name__)


def _get_mb_conn():
    """获取 metabase.db 连接"""
    db_path = get_metabase_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"metabase.db 不存在: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


async def _query_d21_activity(school_name: str, start_date: str, end_date: str) -> Optional[dict]:
    """通过 Metabase API (Dashboard 21) 查询学校活跃数据
    
    Returns:
        {"uv": int, "weekly_active": int, "monthly_active": int, "total_teachers": int}
        失败返回 None
    """
    from scrapers.api_lida import (
        ApiLidaScraper, CARD_D21_UV, CARD_D21_WEEKLY_ACTIVE,
        CARD_D21_MONTHLY_ACTIVE, CARD_D21_TOTAL_TEACHERS,
    )
    
    async with ApiLidaScraper() as scraper:
        if not await scraper._login():
            logger.warning("[Activity-Metabase] 登录失败")
            return None
        
        try:
            uv_str = await scraper._query_d21_dashcard(
                CARD_D21_UV, school_name, start_date, end_date
            )
            weekly_str = await scraper._query_d21_dashcard(
                CARD_D21_WEEKLY_ACTIVE, school_name, start_date, end_date
            )
            monthly_str = await scraper._query_d21_dashcard(
                CARD_D21_MONTHLY_ACTIVE, school_name, start_date, end_date
            )
            total_str = await scraper._query_d21_dashcard(
                CARD_D21_TOTAL_TEACHERS, school_name, start_date, end_date
            )
            
            uv = int(float(uv_str)) if uv_str else 0
            weekly_active = int(float(weekly_str)) if weekly_str else 0
            monthly_active = int(float(monthly_str)) if monthly_str else 0
            total_teachers = int(float(total_str)) if total_str else 0
            
            logger.info(
                "[Activity-Metabase] %s: UV=%d 周活=%d 月活=%d 总教师=%d",
                school_name, uv, weekly_active, monthly_active, total_teachers
            )
            
            return {
                "uv": uv,
                "weekly_active": weekly_active,
                "monthly_active": monthly_active,
                "total_teachers": total_teachers,
                "source": "metabase-api",
            }
        except Exception as e:
            logger.warning("[Activity-Metabase] D21 查询失败: %s", e)
            return None


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
    """周活跃统计 — 优先 Metabase API，回退本地 DB"""
    start = request.args.get("start_date", "")
    end = request.args.get("end_date", "")
    school = request.args.get("school_name", "")
    if not start or not end or not school:
        return jsonify({"error": "缺少参数"}), 400

    # ── 优先使用 Metabase API ──
    try:
        d21 = asyncio.run(_query_d21_activity(school, start, end))
        if d21 and d21.get("total_teachers", 0) > 0:
            total = d21["total_teachers"]
            used = d21["uv"]
            active = d21["weekly_active"]
            activity_rate = round(active / used * 100, 1) if used else 0
            weekly_ratio = round(active / total * 100, 1) if total else 0
            return jsonify({
                "total_teachers": total,
                "used_teachers": used,
                "active_teachers": active,
                "activity_rate": activity_rate,
                "weekly_ratio": weekly_ratio,
                "source": "metabase-api",
            })
    except Exception as e:
        logger.warning("[Activity-Weekly] Metabase API 失败，回退本地DB: %s", e)

    # ── 回退到本地数据库 ──
    conn = None
    try:
        conn = _get_mb_conn()
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM teacher_base WHERE school_name=? AND state=1",
            (school,),
        ).fetchone()["c"]

        used = conn.execute("""
            SELECT COUNT(DISTINCT tianli_user_id) AS c
            FROM dws_ingress_teacher_day
            WHERE school_name=?
              AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
              AND pv_count IS NOT NULL AND pv_count>0
        """, (school, start, end)).fetchone()["c"]

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

        activity_rate = round(active / used * 100, 1) if used else 0
        weekly_ratio = round(active / total * 100, 1) if total else 0

        return jsonify({
            "total_teachers": total,
            "used_teachers": used,
            "active_teachers": active,
            "activity_rate": activity_rate,
            "weekly_ratio": weekly_ratio,
            "source": "metabase",
        })
    finally:
        if conn:
            conn.close()


@activity_bp.route("/api/activity/monthly")
def monthly_stats():
    """月活跃统计 — 优先 Metabase API，回退本地 DB"""
    start = request.args.get("start_date", "")
    end = request.args.get("end_date", "")
    school = request.args.get("school_name", "")
    if not start or not end or not school:
        return jsonify({"error": "缺少参数"}), 400

    # ── 优先使用 Metabase API ──
    try:
        d21 = asyncio.run(_query_d21_activity(school, start, end))
        if d21 and d21.get("total_teachers", 0) > 0:
            total = d21["total_teachers"]
            daily = d21["uv"]
            weekly = d21["weekly_active"]
            monthly = d21["monthly_active"]
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
                "source": "metabase-api",
            })
    except Exception as e:
        logger.warning("[Activity-Monthly] Metabase API 失败，回退本地DB: %s", e)

    # ── 回退到本地数据库 ──
    conn = None
    try:
        conn = _get_mb_conn()
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM teacher_base WHERE school_name=? AND state=1",
            (school,),
        ).fetchone()["c"]

        daily = conn.execute("""
            SELECT COUNT(DISTINCT tianli_user_id) AS c
            FROM dws_ingress_teacher_day
            WHERE school_name=?
              AND substr(stat_date,1,10)>=? AND substr(stat_date,1,10)<=?
              AND pv_count IS NOT NULL AND pv_count>0
        """, (school, start, end)).fetchone()["c"]

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
            "source": "metabase",
        })
    finally:
        if conn:
            conn.close()
