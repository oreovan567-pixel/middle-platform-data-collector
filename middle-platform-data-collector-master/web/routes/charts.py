"""图表分析页面及API"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import sqlite3
import requests
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, render_template, request, jsonify, redirect, session

from config.config_loader import get_metabase_db_path, load_config

logger = logging.getLogger(__name__)
charts_bp = Blueprint("charts", __name__)

# 学段 -> 年级映射
_GRADE_MAP = {
    "高中": ["高一", "高二", "高三"],
    "初中": ["七年级", "八年级", "九年级"],
    "小学": ["一年级", "二年级", "三年级", "四年级", "五年级", "六年级"],
}

# 标准三段（用于X轴显示）
_STANDARD_STAGES = ["高中", "初中", "小学"]


def _get_mb_conn():
    """获取 metabase.db 连接"""
    db_path = get_metabase_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"metabase.db 不存在: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _ui_stage(db_stage: str) -> str:
    """DB值 -> 前端显示：'高中部' -> '高中'"""
    if db_stage.endswith("部"):
        return db_stage[:-1]
    return db_stage


def _db_stage(ui_stage: str) -> str:
    """前端值 -> DB查询：'高中' -> '高中部'"""
    if ui_stage and not ui_stage.endswith("部"):
        return ui_stage + "部"
    return ui_stage


def _split_csv(value: str) -> list:
    """拆分逗号分隔字符串"""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


# -- 页面 --

@charts_bp.route("/charts")
def charts_page():
    return render_template("charts.html")


# -- 筛选选项 --

@charts_bp.route("/api/charts/options")
def chart_options():
    """返回所有筛选器的可选项（含按类型分组的学校列表）"""
    from models.school import School
    all_schools = School.get_all()
    schools = [
        {"name": s.name, "display_name": s.display_name or s.name, "type": s.type, "id": s.metabase_school_id}
        for s in all_schools if s.metabase_school_id
    ]

    # 按类型分组
    schools_by_type = {}
    for s in schools:
        t = s["type"] or "未分类"
        schools_by_type.setdefault(t, []).append(s)

    conn = None
    try:
        conn = _get_mb_conn()
        stages = set()
        for r in conn.execute(
            "SELECT DISTINCT stage_names FROM teacher_base WHERE stage_names IS NOT NULL AND state=1"
        ):
            for s in _split_csv(r["stage_names"]):
                stages.add(_ui_stage(s))

        grades = set()
        for r in conn.execute(
            "SELECT DISTINCT grade_names FROM teacher_base WHERE grade_names IS NOT NULL AND state=1"
        ):
            for g in _split_csv(r["grade_names"]):
                grades.add(g)

        subjects = set()
        for r in conn.execute(
            "SELECT DISTINCT subject_names FROM teacher_base WHERE subject_names IS NOT NULL AND state=1"
        ):
            for s in _split_csv(r["subject_names"]):
                subjects.add(s)

        return jsonify({
            "schools": sorted(schools, key=lambda x: x["display_name"]),
            "schools_by_type": {k: sorted(v, key=lambda x: x["display_name"]) for k, v in schools_by_type.items()},
            "stages": sorted(stages),
            "grades": sorted(grades),
            "subjects": sorted(subjects),
        })
    finally:
        if conn:
                    conn.close()


# -- 平台使用率 --

def _determine_x_axis(school_id, stage, grade, subject):
    if grade:
        return "subject"
    if stage:
        return "grade"
    if school_id:
        return "stage"
    return "school"


def _like_clause(field, value):
    return f"AND ',' || {field} || ',' LIKE '%,{value},%'"


def _query_usage(conn, x_axis, start_date, end_date, school_id, db_stage, grade, subject):
    extra_num = []
    extra_den = []
    params_num = []
    params_den = []

    if x_axis != "stage" and db_stage:
        extra_num.append(_like_clause("stage_names", db_stage))
        extra_den.append(_like_clause("stage_names", db_stage))
    if x_axis != "grade" and grade:
        extra_num.append(_like_clause("grade_names", grade))
        extra_den.append(_like_clause("grade_names", grade))
    if x_axis != "subject" and subject:
        extra_num.append(_like_clause("subject_names", subject))
        extra_den.append(_like_clause("subject_names", subject))

    extra_num_sql = " ".join(extra_num)
    extra_den_sql = " ".join(extra_den)

    if x_axis == "school":
        from models.school import School
        all_schools = School.get_all()
        items = [(s.name, s.metabase_school_id) for s in all_schools if s.metabase_school_id]
        labels = [it[0] for it in items]

        id_ph = ",".join("?" * len(items))
        id_values = [it[1] for it in items]
        num_sql = f"""
            SELECT d.tianli_school_id AS xkey, COUNT(DISTINCT d.tianli_user_id) AS cnt
            FROM dws_ingress_teacher_day d
            WHERE d.tianli_school_id IN ({id_ph})
              AND d.host = 'research-api.qimingdaren.com'
              AND d.school_name NOT LIKE '%启鸣达人%'
              AND d.school_name IS NOT NULL AND d.school_name <> ''
              AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> ''
              AND d.tianli_user_id <> '-'
              AND substr(d.stat_date,1,10) >= ? AND substr(d.stat_date,1,10) <= ?
              AND d.tianli_user_id IN (
                  SELECT t.teacher_id FROM teacher_base t
                  WHERE CAST(t.school_id AS TEXT) = CAST(d.tianli_school_id AS TEXT)
              )
              {extra_num_sql}
            GROUP BY d.tianli_school_id
        """
        num_rows = conn.execute(num_sql, id_values + [start_date, end_date] + params_num).fetchall()
        num_map = {r["xkey"]: r["cnt"] for r in num_rows}

        int_ids = [int(it[1]) for it in items]
        id_ph2 = ",".join("?" * len(int_ids))
        den_sql = f"""
            SELECT CAST(school_id AS TEXT) AS xkey, COUNT(DISTINCT teacher_id) AS cnt
            FROM teacher_base
            WHERE school_id IN ({id_ph2}) AND state=1
              {extra_den_sql}
            GROUP BY school_id
        """
        den_rows = conn.execute(den_sql, int_ids + params_den).fetchall()
        den_map = {r["xkey"]: r["cnt"] for r in den_rows}

        label_to_id = {it[0]: it[1] for it in items}
        results = []
        for label in labels:
            sid = label_to_id[label]
            num = num_map.get(sid, 0)
            den = den_map.get(sid, 0)
            rate = round(num / den * 100, 1) if den > 0 else 0
            results.append({"label": label, "numerator": num, "denominator": den, "rate": rate})
        return results

    elif x_axis == "stage":
        results = []
        for label in _STANDARD_STAGES:
            db_st = _db_stage(label)
            num_sql = f"""
                SELECT COUNT(DISTINCT d.tianli_user_id) AS cnt
                FROM dws_ingress_teacher_day d
                WHERE d.tianli_school_id = ?
                  AND d.host = 'research-api.qimingdaren.com'
                  AND d.school_name NOT LIKE '%启鸣达人%'
                  AND d.school_name IS NOT NULL AND d.school_name <> ''
                  AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> ''
                  AND d.tianli_user_id <> '-'
                  AND substr(d.stat_date,1,10) >= ? AND substr(d.stat_date,1,10) <= ?
                  {_like_clause("d.stage_names", db_st)}
                  AND d.tianli_user_id IN (
                      SELECT t.teacher_id FROM teacher_base t WHERE CAST(t.school_id AS TEXT) = ?
                  )
                  {extra_num_sql}
            """
            num = conn.execute(num_sql, [school_id, start_date, end_date, school_id] + params_num).fetchone()["cnt"]
            den_sql = f"""
                SELECT COUNT(DISTINCT teacher_id) AS cnt
                FROM teacher_base
                WHERE CAST(school_id AS TEXT) = ? AND state=1
                  {_like_clause("stage_names", db_st)}
                  {extra_den_sql}
            """
            den = conn.execute(den_sql, [school_id] + params_den).fetchone()["cnt"]
            rate = round(num / den * 100, 1) if den > 0 else 0
            results.append({"label": label, "numerator": num, "denominator": den, "rate": rate})
        return results

    elif x_axis == "grade":
        stage_grades = _GRADE_MAP.get(_ui_stage(db_stage), [])
        results = []
        for label in stage_grades:
            num_sql = f"""
                SELECT COUNT(DISTINCT d.tianli_user_id) AS cnt
                FROM dws_ingress_teacher_day d
                WHERE d.tianli_school_id = ?
                  AND d.host = 'research-api.qimingdaren.com'
                  AND d.school_name NOT LIKE '%启鸣达人%'
                  AND d.school_name IS NOT NULL AND d.school_name <> ''
                  AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> ''
                  AND d.tianli_user_id <> '-'
                  AND substr(d.stat_date,1,10) >= ? AND substr(d.stat_date,1,10) <= ?
                  {_like_clause("d.stage_names", db_stage)}
                  {_like_clause("d.grade_names", label)}
                  AND d.tianli_user_id IN (
                      SELECT t.teacher_id FROM teacher_base t WHERE CAST(t.school_id AS TEXT) = ?
                  )
                  {extra_num_sql}
            """
            num = conn.execute(num_sql, [school_id, start_date, end_date, school_id] + params_num).fetchone()["cnt"]
            den_sql = f"""
                SELECT COUNT(DISTINCT teacher_id) AS cnt
                FROM teacher_base
                WHERE CAST(school_id AS TEXT) = ? AND state=1
                  {_like_clause("stage_names", db_stage)}
                  {_like_clause("grade_names", label)}
                  {extra_den_sql}
            """
            den = conn.execute(den_sql, [school_id] + params_den).fetchone()["cnt"]
            rate = round(num / den * 100, 1) if den > 0 else 0
            results.append({"label": label, "numerator": num, "denominator": den, "rate": rate})
        return results

    elif x_axis == "subject":
        subj_rows = conn.execute(
            "SELECT DISTINCT subject_names FROM teacher_base WHERE subject_names IS NOT NULL AND state=1"
        ).fetchall()
        all_subjects = set()
        for r in subj_rows:
            for s in _split_csv(r["subject_names"]):
                all_subjects.add(s)
        labels = sorted(all_subjects)

        results = []
        for label in labels:
            num_sql = f"""
                SELECT COUNT(DISTINCT d.tianli_user_id) AS cnt
                FROM dws_ingress_teacher_day d
                WHERE d.tianli_school_id = ?
                  AND d.host = 'research-api.qimingdaren.com'
                  AND d.school_name NOT LIKE '%启鸣达人%'
                  AND d.school_name IS NOT NULL AND d.school_name <> ''
                  AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> ''
                  AND d.tianli_user_id <> '-'
                  AND substr(d.stat_date,1,10) >= ? AND substr(d.stat_date,1,10) <= ?
                  {_like_clause("d.stage_names", db_stage)}
                  {_like_clause("d.grade_names", grade)}
                  {_like_clause("d.subject_names", label)}
                  AND d.tianli_user_id IN (
                      SELECT t.teacher_id FROM teacher_base t WHERE CAST(t.school_id AS TEXT) = ?
                  )
                  {extra_num_sql}
            """
            num = conn.execute(num_sql, [school_id, start_date, end_date, school_id] + params_num).fetchone()["cnt"]
            den_sql = f"""
                SELECT COUNT(DISTINCT teacher_id) AS cnt
                FROM teacher_base
                WHERE CAST(school_id AS TEXT) = ? AND state=1
                  {_like_clause("stage_names", db_stage)}
                  {_like_clause("grade_names", grade)}
                  {_like_clause("subject_names", label)}
                  {extra_den_sql}
            """
            den = conn.execute(den_sql, [school_id] + params_den).fetchone()["cnt"]
            rate = round(num / den * 100, 1) if den > 0 else 0
            results.append({"label": label, "numerator": num, "denominator": den, "rate": rate})
        return results

    return []


@charts_bp.route("/api/charts/platform-usage")
def platform_usage():
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    school_id = request.args.get("school_id", "")
    stage = request.args.get("stage", "")
    grade = request.args.get("grade", "")
    subject = request.args.get("subject", "")

    if not start_date or not end_date:
        return jsonify({"error": "时间范围为必填项"}), 400

    db_stage = _db_stage(stage) if stage else ""
    x_axis = _determine_x_axis(school_id, stage, grade, subject)

    conn = None
    try:
        conn = _get_mb_conn()
        data = _query_usage(conn, x_axis, start_date, end_date, school_id, db_stage, grade, subject)
        return jsonify({"x_axis": x_axis, "data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
                    conn.close()


# ── 多校使用率对比：8 个模块配置 ──
# 每个模块通过 host + url 前缀从 dws_ingress_teacher_day 中区分
_MODULES = [
    {"key": "overall", "name": "平台总体数据", "host": "research-api.qimingdaren.com", "url_prefix": ""},
    {"key": "internal", "name": "平台使用数据\n(内部员工)", "host": "research-api.qimingdaren.com", "url_prefix": ""},
    {"key": "gebei", "name": "个备访问数据", "host": "research-api.qimingdaren.com",
     "url_prefix": "/teaching/research/platform/lesson/preparation/personal/my/textbook/list"},
    {"key": "jibei", "name": "集备访问数据", "host": "research-api.qimingdaren.com",
     "url_prefix": "/teaching/research/platform/lesson/preparation/lesson/textbook/list"},
    {"key": "zujuan", "name": "组卷访问数据", "host": "research-api.qimingdaren.com",
     "url_prefix": "/teaching/research/platform/param/bookTopic/question/detail/exam"},
    {"key": "shouyue", "name": "手阅作业访问数据", "host": "research-api.qimingdaren.com",
     "url_prefix": "/tutoring/assignment/work/getWorkList"},
    {"key": "xueqing", "name": "学情分析访问数据", "host": "research-api.qimingdaren.com",
     "url_prefix": "/tutoring/assignment/getWorkManagerData"},
    {"key": "cuoti", "name": "错题本访问数据", "host": "research-api.qimingdaren.com",
     "url_prefix": "/api/account/getStageClassByMobileNum"},
]


def _check_url_column(conn):
    """检查 dws_ingress_teacher_day 表是否有 url 列"""
    try:
        cols = conn.execute("PRAGMA table_info(dws_ingress_teacher_day)").fetchall()
        col_names = [c["name"] for c in cols]
        return "url" in col_names or "api_path" in col_names or "request_uri" in col_names
    except Exception:
        return False


def _build_module_query(conn, module, start_date, end_date, school_id, extra_filter, extra_params):
    """构建单个模块的查询 SQL 和参数

    返回 (active_sql, active_params, total_sql, total_params)
    """
    host = module["host"]
    url_prefix = module.get("url_prefix", "")

    has_url_col = _check_url_column(conn)

    # 活跃教师: 在时间范围内访问过该模块的教师数
    url_filter = ""
    if url_prefix and has_url_col:
        url_filter = " AND d.url LIKE ?"
        url_param_prefix = url_prefix + "%"
    elif url_prefix and not has_url_col:
        url_filter = ""  # 降级：无 url 列则忽略模块级区分

    active_sql = f"""
        SELECT COUNT(DISTINCT d.tianli_user_id) AS cnt
        FROM dws_ingress_teacher_day d
        WHERE d.host = ?
          AND d.school_name NOT LIKE '%启鸣达人%'
          AND d.school_name IS NOT NULL AND d.school_name <> ''
          AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> ''
          AND d.tianli_user_id <> '-'
          AND CAST(d.tianli_school_id AS TEXT) = ?
          AND substr(d.stat_date,1,10) >= ? AND substr(d.stat_date,1,10) <= ?
          {url_filter}
          AND d.tianli_user_id IN (
              SELECT t.teacher_id FROM teacher_base t
              WHERE CAST(t.school_id AS TEXT) = ? AND t.state = 1
              {extra_filter}
          )
    """

    active_params = [host, school_id, start_date, end_date]
    if url_filter:
        active_params.append(url_param_prefix)
    active_params.append(school_id)
    active_params.extend(extra_params)

    # 总教师数: 学校中符合筛选条件的教师
    total_sql = f"""
        SELECT COUNT(DISTINCT teacher_id) AS cnt
        FROM teacher_base
        WHERE CAST(school_id AS TEXT) = ? AND state = 1
        {extra_filter}
    """
    total_params = [school_id] + extra_params

    return active_sql, active_params, total_sql, total_params


def _build_extra_filter(stage, grade, subject):
    """构建学段/年级/学科的筛选 SQL 片段"""
    extra_filter = ""
    extra_params = []
    if stage:
        db_stage = _db_stage(stage)
        extra_filter += " AND ',' || stage_names || ',' LIKE ?"
        extra_params.append(f"%,{db_stage},%")
    if grade:
        extra_filter += " AND ',' || grade_names || ',' LIKE ?"
        extra_params.append(f"%,{grade},%")
    if subject:
        extra_filter += " AND ',' || subject_names || ',' LIKE ?"
        extra_params.append(f"%,{subject},%")
    return extra_filter, extra_params


@charts_bp.route("/api/charts/multi-school-usage")
def multi_school_usage():
    """多校使用率对比 API

    返回每所学校的使用率数据：
    - total_teachers: 教师总数
    - active_teachers: 活跃教师数（时间范围内有访问记录）
    - usage_rate: 使用率 = active / total × 100%

    注意：dws_ingress_teacher_day 表缺少 url 列，无法按模块区分
    个备/集备/组卷等细分数据，模块级对比需通过 Metabase API 获取。
    """
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    stage = request.args.get("stage", "")
    grade = request.args.get("grade", "")
    subject = request.args.get("subject", "")
    school_id_filter = request.args.get("school_id", "")

    if not start_date or not end_date:
        return jsonify({"error": "时间范围为必填项"}), 400

    extra_filter, extra_params = _build_extra_filter(stage, grade, subject)

    conn = None
    try:
        conn = _get_mb_conn()

        # 获取学校列表（支持按 school_id 筛选）
        school_sql = """
            SELECT DISTINCT CAST(school_id AS TEXT) AS sid, school_name
            FROM teacher_base
            WHERE state = 1
              AND school_name IS NOT NULL AND school_name != ''
        """
        school_params = []
        if school_id_filter:
            school_sql += " AND CAST(school_id AS TEXT) = ?"
            school_params.append(school_id_filter)
        school_sql += " ORDER BY school_name"
        school_rows = conn.execute(school_sql, school_params).fetchall()

        rows = []
        for s in school_rows:
            sid = s["sid"]
            school_name = s["school_name"]

            # 教师总数（符合学段/年级/学科筛选）
            total_sql = f"""
                SELECT COUNT(DISTINCT teacher_id) AS cnt
                FROM teacher_base
                WHERE CAST(school_id AS TEXT) = ? AND state = 1
                {extra_filter}
            """
            total = conn.execute(total_sql, [sid] + extra_params).fetchone()["cnt"]
            if total == 0:
                continue

            # 活跃教师数（时间范围内有访问记录 + 学段/年级/学科匹配）
            active_extra = ""
            active_extra_params = []
            if stage:
                db_stage = _db_stage(stage)
                active_extra += " AND ',' || d.stage_names || ',' LIKE ?"
                active_extra_params.append(f"%,{db_stage},%")
            if grade:
                active_extra += " AND ',' || d.grade_names || ',' LIKE ?"
                active_extra_params.append(f"%,{grade},%")
            if subject:
                active_extra += " AND ',' || d.subject_names || ',' LIKE ?"
                active_extra_params.append(f"%,{subject},%")

            active_sql = f"""
                SELECT COUNT(DISTINCT d.tianli_user_id) AS cnt
                FROM dws_ingress_teacher_day d
                WHERE CAST(d.tianli_school_id AS TEXT) = ?
                  AND d.host = 'research-api.qimingdaren.com'
                  AND d.school_name NOT LIKE '%启鸣达人%'
                  AND d.school_name IS NOT NULL AND d.school_name <> ''
                  AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> ''
                  AND d.tianli_user_id <> '-'
                  AND substr(d.stat_date,1,10) >= ? AND substr(d.stat_date,1,10) <= ?
                  {active_extra}
                  AND d.tianli_user_id IN (
                      SELECT t.teacher_id FROM teacher_base t
                      WHERE CAST(t.school_id AS TEXT) = ? AND t.state = 1
                      {extra_filter}
                  )
            """
            active_params_list = [sid, start_date, end_date] + active_extra_params + [sid] + extra_params
            active = conn.execute(active_sql, active_params_list).fetchone()["cnt"]

            rate = round(active / total * 100, 1) if total > 0 else 0

            rows.append({
                "school": school_name,
                "school_id": sid,
                "total_teachers": total,
                "active_teachers": active,
                "usage_rate": f"{rate}%",
                "rate_value": rate,
            })

        return jsonify({
            "rows": rows,
            "total_schools": len(rows),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
                    conn.close()


@charts_bp.route("/comparison")
def comparison_page():
    """多校使用率对比页面"""
    return render_template("comparison.html")


@charts_bp.route("/school")
def school_list_page():
    """单校详情入口 — 按负责人展示所属学校"""
    from models.school import School
    from models.user import User
    user_id = session.get("user_id")
    user_role = session.get("role", "user")

    # 获取当前用户负责的学校 (owner_id 匹配)
    if user_role in ("super_admin", "admin"):
        owned = School.get_all()
    else:
        owned = School.get_by_owner(user_id) if user_id else []

    if not owned:
        return render_template("school_detail.html", schools=[], school_id="")

    # 多学校时传入列表供 Tab 渲染
    school_list = [{"id": s.metabase_school_id or str(s.id), "name": s.display_name or s.name, "type": s.type or ""} for s in owned]
    if len(school_list) == 1:
        return redirect(f"/school/{school_list[0]['id']}")
    return render_template("school_detail.html", schools=school_list, school_id=school_list[0]["id"])


@charts_bp.route("/school/<school_id>")
def school_detail_page(school_id):
    """单校详情分析页面"""
    from models.school import School
    from models.user import User
    user_id = session.get("user_id")
    user_role = session.get("role", "user")

    # 获取当前用户负责的学校列表（供 Tab 使用）
    if user_role in ("super_admin", "admin"):
        owned = School.get_all()
    else:
        owned = School.get_by_owner(user_id) if user_id else []
    school_list = [{"id": s.metabase_school_id or str(s.id), "name": s.display_name or s.name, "type": s.type or ""} for s in owned]

    # 普通用户越权检查：URL 中的 school_id 必须属于自己负责的学校
    if user_role not in ("super_admin", "admin") and school_list:
        allowed_ids = {s["id"] for s in school_list}
        if school_id not in allowed_ids:
            return redirect(f"/school/{school_list[0]['id']}")

    return render_template("school_detail.html", schools=school_list, school_id=school_id)


# ═══════════════════════════════════════════
#  模块级使用率查询（Grafana SLS）
# ═══════════════════════════════════════════

GRAFANA_BASE = "https://grafana.qimingdaren.com"
SLS_DATASOURCE = {"type": "aliyun-log-service-datasource", "uid": "ff17gixulooowc"}

# 8 个模块的 URL 筛选规则
# 注意：SLS 中使用前缀匹配语法 url: /path* (不带引号，* 紧跟路径)
_MODULE_DEFS = [
    # 顺序固定：平台总体 → 内部员工 → 个备 → 集备 → 组卷 → 手阅 → 学情分析 → 错题本
    {"key": "overall",   "name": "平台总体数据",          "url": None},
    {"key": "internal",  "name": "平台使用数据\n(内部员工)", "url": None},
    {"key": "gebei",     "name": "个备访问数据",          "url": "/teaching/research/platform/lesson/preparation/personal/my/textbook/list"},
    {"key": "jibei",     "name": "集备访问数据",          "url": "/teaching/research/platform/lesson/preparation/lesson/textbook/list"},
    {"key": "zujuan",    "name": "组卷访问数据",          "url": "/teaching/research/platform/param/bookTopic/question/detail/exam"},
    {"key": "shouyue",   "name": "手阅作业访问数据",      "url": "/tutoring/assignment/work/getWorkList"},
    {"key": "xueqing",   "name": "学情分析访问数据",      "url": "/tutoring/assignment/getWorkManagerData"},
    {"key": "cuoti",     "name": "错题本访问数据",        "url": "/api/account/getStageClassByMobileNum"},
]


async def _query_metabase_modules(
    start_date, end_date, stage: str = "", school_id_filter=None, types: str = ""
) -> dict | None:
    """通过 Metabase API 查询多校模块使用率（与 Lida 100% 一致）

    从 Card #251 动态获取全部学校列表，查询每个学校的 6 个可查询模块。
    错题本和学情分析（text widget）返回 "-"，由 SLS 回退补充。
    附加工5个指标列：作业次数、人均作业次数、日活比例、周活比例、月活比例。

    Returns:
        {"columns": [13列], "rows": [...], "total_schools": N, "source": "metabase-api"}
        失败返回 None
    """
    from scrapers.api_lida import ApiLidaScraper, ALL_MODULE_CARDS, ALL_MODULE_NAMES
    from models.school import School

    date_range = (start_date, end_date)

    # 构建 school_id → (display_name, type, priority, owner_id) 的本地映射
    all_local = School.get_all()
    # 构建 owner_id → 手机号 映射（仅直营校有负责人）
    owner_map = {}
    for s in all_local:
        if s.owner_id:
            from models.user import User
            u = User.get_by_id(s.owner_id)
            if u:
                owner_map[s.id] = u.username
    school_meta = {
        s.metabase_school_id: (s.display_name or s.name, s.type or "", s.priority or "中", s.owner_id or 0)
        for s in all_local if s.metabase_school_id
    }

    rows = []
    async with ApiLidaScraper() as scraper:
        # 1. 从 Card #251 动态获取全部学校列表
        schools = await scraper._fetch_school_list()
        if not schools:
            logger.warning("[Metabase-API] 无法获取学校列表")
            return None

        # 按 school_id 过滤（支持单校字符串或多校集合）
        if school_id_filter:
            if isinstance(school_id_filter, str):
                schools = {k: v for k, v in schools.items() if k == school_id_filter}
            else:
                schools = {k: v for k, v in schools.items() if k in school_id_filter}
        # 按类型过滤（types 参数为逗号分隔的类型名称，同时匹配 type 字段和 display_name）
        if types and not school_id_filter:
            type_set = set(t.strip() for t in types.split(",") if t.strip())
            schools = {
                k: v for k, v in schools.items()
                if (school_meta.get(k, ("", "", "", 0))[1] in type_set
                    or school_meta.get(k, ("", "", "", 0))[0] in type_set)
            }
        if not schools:
            return None

        logger.info("[Metabase-API] 共 %d 所学校待查询", len(schools))

        # 2. 逐个学校查询 6 个可查询模块
        for sid, sname in schools.items():
            school_dict = {"name": sname, "metabase_school_id": sid}
            try:
                modules = await scraper.scrape_all_modules(school_dict, date_range, stage=stage)
                if modules is None:
                    continue

                # 3. 按 ALL_MODULE_CARDS 顺序组装前 6 列 + 2 列 text widget 占位
                values = []
                rate_values = []
                for cid in ALL_MODULE_CARDS:
                    val_str = modules.get(str(cid), "")
                    try:
                        rate = float(val_str.replace("%", ""))
                    except (ValueError, AttributeError):
                        rate = 0
                    values.append(val_str if val_str else "-")
                    rate_values.append(rate)
                values.append("-")   # 学情分析
                rate_values.append(0)
                values.append("-")   # 错题本
                rate_values.append(0)

                # 4. 从 Dashboard 21 卡片数据计算日活/周活/月活比例
                from scrapers.api_lida import CARD_D21_UV, CARD_D21_WEEKLY_ACTIVE, CARD_D21_MONTHLY_ACTIVE, CARD_D21_TOTAL_TEACHERS
                uv = total_teachers = 0
                weekly_active = monthly_active = 0
                uv_str = modules.get(str(CARD_D21_UV), "")
                teachers_str = modules.get(str(CARD_D21_TOTAL_TEACHERS), "")
                if uv_str and teachers_str:
                    try:
                        uv = float(uv_str)
                        weekly_active = float(modules.get(str(CARD_D21_WEEKLY_ACTIVE), "0") or "0")
                        monthly_active = float(modules.get(str(CARD_D21_MONTHLY_ACTIVE), "0") or "0")
                        total_teachers = float(teachers_str)
                        if total_teachers > 0:
                            daily_pct = round(uv / total_teachers * 100, 1)
                            weekly_pct = round(weekly_active / total_teachers * 100, 1)
                            monthly_pct = round(monthly_active / total_teachers * 100, 1)
                        else:
                            daily_pct = weekly_pct = monthly_pct = 0
                        logger.debug("[Metabase-API] %s D21: UV=%s 周活=%s 月活=%s 总教师=%s → 日活=%.1f%% 周活=%.1f%% 月活=%.1f%%",
                                    sname, uv, weekly_active, monthly_active, total_teachers,
                                    daily_pct, weekly_pct, monthly_pct)
                    except (ValueError, TypeError, KeyError):
                        daily_pct = weekly_pct = monthly_pct = 0
                        logger.warning("[Metabase-API] %s D21 卡片数据解析失败", sname)
                else:
                    daily_pct = weekly_pct = monthly_pct = None

                display_name, stype, spriority, row_owner_id = school_meta.get(sid, (sname, "", "中", 0))

                # 5. 附加 5 列：作业次数(占位) + 人均作业次数(占位) + 日活/周活/月活(D21 计算)
                if daily_pct is not None:
                    values.extend(["-", "-", f"{daily_pct}%", f"{weekly_pct}%", f"{monthly_pct}%"])
                    rate_values.extend([0, 0, daily_pct, weekly_pct, monthly_pct])
                else:
                    values.extend(["-", "-", "-", "-", "-"])
                    rate_values.extend([0, 0, 0, 0, 0])

                rows.append({
                    "school": sname,
                    "display_name": display_name,
                    "type": stype,
                    "school_id": sid,
                    "values": values,
                    "rate_values": rate_values,
                    "d21_uv": uv,
                    "d21_weekly_active": weekly_active,
                    "d21_monthly_active": monthly_active,
                    "d21_total_teachers": total_teachers,
                    "total_teachers": total_teachers,
                    "priority": spriority,
                    "owner_name": owner_map.get(row_owner_id, ""),
                    "owner_id": row_owner_id,
                })
            except Exception as e:
                logger.error("[Metabase-API] %s 查询失败: %s", sname, e)

    if not rows:
        return None

    # 5. 查询附加指标——仅作业次数（活跃比例已从 D21 卡片计算）
    _enrich_extra_metrics(rows, start_date, end_date, mode="metabase-api")

    return {
        "columns": ALL_MODULE_NAMES,
        "rows": rows,
        "total_schools": len(rows),
        "source": "metabase-api",
    }


def _enrich_extra_metrics(rows: list, start_date, end_date, mode: str = "full"):
    """为每行补充额外指标。

    mode="metabase-api": 仅填充 作业次数(索引8) 和 人均作业次数(索引9)，
                        活跃比例（索引10-12）已由 D21 卡片计算。
    mode="full": 填充全部 5 个附加列（用于 SLS 回退路径）。
    """
    import sqlite3 as _sqlite3
    from config.config_loader import get_metabase_db_path
    from models.database import get_connection as get_local_conn
    from models.school import School

    fill_active = (mode == "full")  # SLS 回退需要计算活跃比例

    # ── 从作业次数 API 获取数据（优先），失败则回退本地 monthly_records ──
    hw_map: dict = {}  # school_name → homework_count
    sid_to_hw: dict = {}
    
    # 1) 主路径：调用作业次数 API
    try:
        start_str = str(start_date)[:10] if start_date else ""
        end_str = str(end_date)[:10] if end_date else ""
        api_url = "https://api-error-book.qimingdaren.com/api/exam/school/examCount"
        resp = requests.post(api_url, json={
            "sysExamTypeIdList": [8],
            "startDate": start_str,
            "endDate": end_str,
        }, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            if result.get("code") == 200 and result.get("data"):
                for item in result["data"]:
                    school_name = (item.get("schoolName") or "").strip().replace(" ", "")
                    num_val = item.get("num")
                    if school_name and num_val is not None:
                        hw_map[school_name] = str(num_val)
                logger.info("[附加指标] 从 API 获取到 %d 所学校的作业次数", len(hw_map))
            else:
                logger.warning("[附加指标] API 返回异常: %s", result.get("msg", "未知错误"))
        else:
            logger.warning("[附加指标] API 请求失败 HTTP %d", resp.status_code)
    except Exception as e:
        logger.warning("[附加指标] 作业次数 API 请求失败: %s", e)

    # 2) 回退：从本地 monthly_records 获取（仅当 API 未获取到数据时）
    if not hw_map:
        try:
            try:
                sdt = datetime.strptime(str(start_date), "%Y-%m-%d").date() if not isinstance(start_date, date) else start_date
                edt = datetime.strptime(str(end_date), "%Y-%m-%d").date() if not isinstance(end_date, date) else end_date
            except (ValueError, TypeError):
                sdt = start_date
                edt = end_date
            year = sdt.year if hasattr(sdt, 'year') else 2026
            with get_local_conn() as local_conn:
                mr_rows = local_conn.execute(
                    "SELECT school_name, homework_count FROM monthly_records "
                    "WHERE year=? AND homework_count IS NOT NULL AND homework_count != '' "
                    "ORDER BY collected_at DESC",
                    (year,),
                ).fetchall()
                for r in mr_rows:
                    hw = r["homework_count"]
                    if hw and r["school_name"] not in hw_map:
                        try:
                            hw_map[r["school_name"]] = str(int(float(str(hw).replace(",", ""))))
                        except (ValueError, TypeError):
                            pass
            logger.info("[附加指标] 从 monthly_records（回退）获取到 %d 所学校的作业次数", len(hw_map))
        except Exception as e:
            logger.warning("[附加指标] 本地回退也失败: %s", e)

    # ── 构建 school_id → homework_count 映射（名称模糊匹配）──
    # 硬编码映射：系统名称 → API 正式校名
    _NAME_OVERRIDE = {
        "保山学校": "云南保山天立学校",
        "泸州春雨": "泸州天立春雨学校",
        "资阳天立": "资阳市雁江区天立学校",
        "乌兰察布西区": "乌兰察布天立学校",
        "泸州天立中学": "泸州天立学校",
        "来安天立": "来安天立学校",
        "成都龙泉": "成都天立学校-东区",
    }
    if hw_map:
        all_schools = School.get_all()
        for s in all_schools:
            if not s.metabase_school_id:
                continue
            hw = hw_map.get(s.display_name) or hw_map.get(s.name)
            # 第零层：硬编码映射覆盖（精确对应关系优先）
            if not hw:
                mapped = _NAME_OVERRIDE.get(s.display_name) or _NAME_OVERRIDE.get(s.name)
                if mapped:
                    hw = hw_map.get(mapped)
            # 第一层：子串包含匹配
            if not hw:
                for hw_name in hw_map:
                    if hw_name and (s.display_name or "") and (
                        hw_name in (s.display_name or "") or (s.display_name or "") in hw_name
                    ):
                        hw = hw_map[hw_name]
                        break
            if not hw:
                for hw_name in hw_map:
                    if hw_name and s.name and (hw_name in s.name or s.name in hw_name):
                        hw = hw_map[hw_name]
                        break
            # 第二层：去噪后最长公共子串匹配
            # 去掉"天立/学校/校区/小学"等通用词，比较剩余核心名
            if not hw:
                _STOP = ("天立", "学校", "校区", "小学", "中学", "大学", "学院", "幼儿园")
                def _strip(name: str) -> str:
                    for w in _STOP:
                        name = name.replace(w, "")
                    return name.strip()
                def _lcs_len(a: str, b: str) -> int:
                    m, n = len(a), len(b)
                    dp = [[0] * (n + 1) for _ in range(m + 1)]
                    best = 0
                    for i in range(1, m + 1):
                        for j in range(1, n + 1):
                            if a[i - 1] == b[j - 1]:
                                dp[i][j] = dp[i - 1][j - 1] + 1
                                if dp[i][j] > best:
                                    best = dp[i][j]
                    return best
                our_clean = _strip(s.name)
                disp_clean = _strip(s.display_name or "")
                # 用 display_name 也试一次（如"彝良学校"→"彝良"）
                best_name = None
                best_ratio = 0.0
                best_lcs = 0
                for hw_name in hw_map:
                    hw_clean = _strip(hw_name)
                    for oc in (our_clean, disp_clean):
                        if not oc:
                            continue
                        lcs = _lcs_len(oc, hw_clean)
                        shorter = min(len(oc), len(hw_clean))
                        ratio = lcs / shorter if shorter > 0 else 0
                        # 优先 ratio 高的，ratio 相同时取 LCS 更长的
                        if ratio > best_ratio or (ratio == best_ratio and lcs > best_lcs):
                            best_ratio = ratio
                            best_lcs = lcs
                            best_name = hw_name
                if best_ratio >= 0.6 and best_name:
                    # 去噪后名字太短（如"泸州天立"→"泸州"仅2字）容易误匹配，跳过
                    if max(len(our_clean or ""), len(disp_clean or "")) >= 3:
                        hw = hw_map[best_name]
            if hw:
                sid_to_hw[s.metabase_school_id] = hw
        logger.info("[附加指标] 名称匹配后得到 %d 所学校的作业次数映射", len(sid_to_hw))

    # ── 填充每行数据 ──
    if not fill_active:
        # Metabase API 路径：仅填充 作业次数 和 人均作业次数
        for row in rows:
            sid = row["school_id"]
            hw = sid_to_hw.get(sid, "")
            total = row.get("d21_total_teachers", 0)
            hw_num = 0
            per_capita = ""
            per_capita_num = 0
            if hw:
                try:
                    hw_num = float(str(hw).replace(",", ""))
                except (ValueError, TypeError):
                    pass
            if hw and total:
                try:
                    per_capita_num = round(hw_num / float(total), 1)
                    per_capita = str(per_capita_num)
                except (ValueError, TypeError):
                    pass
            row["values"][8] = hw if hw else "-"
            row["values"][9] = per_capita if per_capita else "-"
            row["rate_values"][8] = hw_num
            row["rate_values"][9] = per_capita_num
        logger.info("[附加指标] 已为 %d 行填充作业次数", len(rows))
        return

    # ── 从 metabase.db 查询活跃比例 ──
    start_str = str(start_date)
    end_str = str(end_date)
    try:
        mb_path = get_metabase_db_path()
        mb_conn = _sqlite3.connect(str(mb_path))
        mb_conn.row_factory = _sqlite3.Row

        for row in rows:
            sid = row["school_id"]
            sname = row["school"]
            try:
                # 学校总教师数
                total_row = mb_conn.execute(
                    "SELECT COUNT(*) AS c FROM teacher_base "
                    "WHERE CAST(school_id AS TEXT)=? AND state=1",
                    (sid,),
                ).fetchone()
                total = total_row["c"] if total_row else 0

                if total > 0:
                    # 日活：任意1天有访问
                    daily = mb_conn.execute(
                        "SELECT COUNT(DISTINCT d.tianli_user_id) AS c "
                        "FROM dws_ingress_teacher_day d "
                        "WHERE d.host='research-api.qimingdaren.com' "
                        "AND CAST(d.tianli_school_id AS TEXT)=? "
                        "AND d.school_name NOT LIKE '%启鸣达人%' "
                        "AND d.school_name IS NOT NULL AND d.school_name<>'' "
                        "AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id<>'' AND d.tianli_user_id<>'-' "
                        "AND substr(d.stat_date,1,10)>=? AND substr(d.stat_date,1,10)<=? "
                        "AND d.tianli_user_id IN ("
                        "  SELECT t.teacher_id FROM teacher_base t WHERE CAST(t.school_id AS TEXT)=?"
                        ")",
                        (sid, start_str, end_str, sid),
                    ).fetchone()["c"]

                    # 周活：>=3天
                    weekly = mb_conn.execute(
                        "SELECT COUNT(*) AS c FROM ("
                        "  SELECT d.tianli_user_id FROM dws_ingress_teacher_day d"
                        "  WHERE d.host='research-api.qimingdaren.com' "
                        "  AND CAST(d.tianli_school_id AS TEXT)=? "
                        "  AND d.school_name NOT LIKE '%启鸣达人%' "
                        "  AND d.school_name IS NOT NULL AND d.school_name<>'' "
                        "  AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id<>'' AND d.tianli_user_id<>'-' "
                        "  AND substr(d.stat_date,1,10)>=? AND substr(d.stat_date,1,10)<=? "
                        "  AND d.tianli_user_id IN ("
                        "    SELECT t.teacher_id FROM teacher_base t WHERE CAST(t.school_id AS TEXT)=?"
                        "  )"
                        "  GROUP BY d.tianli_user_id"
                        "  HAVING COUNT(DISTINCT substr(d.stat_date,1,10))>=3"
                        ")",
                        (sid, start_str, end_str, sid),
                    ).fetchone()["c"]

                    # 月活：>=4天
                    monthly = mb_conn.execute(
                        "SELECT COUNT(*) AS c FROM ("
                        "  SELECT d.tianli_user_id FROM dws_ingress_teacher_day d"
                        "  WHERE d.host='research-api.qimingdaren.com' "
                        "  AND CAST(d.tianli_school_id AS TEXT)=? "
                        "  AND d.school_name NOT LIKE '%启鸣达人%' "
                        "  AND d.school_name IS NOT NULL AND d.school_name<>'' "
                        "  AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id<>'' AND d.tianli_user_id<>'-' "
                        "  AND substr(d.stat_date,1,10)>=? AND substr(d.stat_date,1,10)<=? "
                        "  AND d.tianli_user_id IN ("
                        "    SELECT t.teacher_id FROM teacher_base t WHERE CAST(t.school_id AS TEXT)=?"
                        "  )"
                        "  GROUP BY d.tianli_user_id"
                        "  HAVING COUNT(DISTINCT substr(d.stat_date,1,10))>=4"
                        ")",
                        (sid, start_str, end_str, sid),
                    ).fetchone()["c"]

                    daily_pct = round(daily / total * 100, 1) if total else 0
                    weekly_pct = round(weekly / total * 100, 1) if total else 0
                    monthly_pct = round(monthly / total * 100, 1) if total else 0

                    # 作业次数（通过 school_id 匹配，兼容 Metabase 与本地名称差异）
                    hw = sid_to_hw.get(sid, "")
                    hw_num = 0
                    per_capita = ""
                    per_capita_num = 0
                    if hw:
                        try:
                            hw_num = float(str(hw).replace(",", ""))
                        except (ValueError, TypeError):
                            pass
                    if hw and total:
                        try:
                            per_capita_num = round(hw_num / total, 1)
                            per_capita = str(per_capita_num)
                        except (ValueError, TypeError):
                            pass

                    row["values"].extend([
                        hw if hw else "-",
                        per_capita if per_capita else "-",
                        f"{daily_pct}%",
                        f"{weekly_pct}%",
                        f"{monthly_pct}%",
                    ])
                    row["rate_values"].extend([
                        hw_num, per_capita_num,
                        daily_pct, weekly_pct, monthly_pct,
                    ])
                    # 存储绝对值供前端 KPI 使用
                    row["d21_uv"] = daily
                    row["d21_weekly_active"] = weekly
                    row["d21_monthly_active"] = monthly
                    row["d21_total_teachers"] = total
                else:
                    row["values"].extend(["-", "-", "-", "-", "-"])
                    row["rate_values"].extend([0, 0, 0, 0, 0])
            except Exception as e:
                logger.warning("[附加指标] %s(%s) 查询失败: %s", sname, sid, e)
                row["values"].extend(["-", "-", "-", "-", "-"])
                row["rate_values"].extend([0, 0, 0, 0, 0])

        mb_conn.close()
        logger.info("[附加指标] 已为 %d 行补充 5 个附加列", len(rows))
    except Exception as e:
        logger.warning("[附加指标] metabase.db 查询失败: %s", e)
        for row in rows:
            row["values"].extend(["-", "-", "-", "-", "-"])
            row["rate_values"].extend([0, 0, 0, 0, 0])


def _get_grafana_auth() -> dict:
    """获取 Grafana API 认证头

    优先级：
    1. 环境变量 GRAFANA_API_TOKEN
    2. 环境变量 GRAFANA_USERNAME + GRAFANA_PASSWORD
    3. config.yaml 中的 api_token
    4. config.yaml 中的 username + password
    """
    import os

    # 环境变量优先
    env_token = os.environ.get("GRAFANA_API_TOKEN", "")
    if env_token:
        return {"Authorization": f"Bearer {env_token}"}

    env_user = os.environ.get("GRAFANA_USERNAME", "")
    env_pass = os.environ.get("GRAFANA_PASSWORD", "")
    if env_user and env_pass:
        encoded = base64.b64encode(f"{env_user}:{env_pass}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    # config.yaml
    try:
        cfg = load_config()
        creds = cfg.get("credentials", {}).get("grafana", {})
        token = creds.get("api_token", "")
        if token:
            return {"Authorization": f"Bearer {token}"}
        username = creds.get("username", "admin")
        password = creds.get("password", "")
        if not password or password == "your_password":
            return None
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    except Exception as e:
        logger.warning("Grafana 凭证获取失败: %s", e)
        return None


def _query_sls_batch(start_ts_ms: int, end_ts_ms: int) -> dict:
    """批量查询 SLS 获取所有模块的活跃教师数

    将 8 个模块的查询合并为一次 HTTP 请求，提高效率。

    返回: {module_key: {school_id: active_count}}
    """
    import os
    import urllib.request

    auth_headers = _get_grafana_auth()
    if not auth_headers:
        logger.warning("Grafana 未配置有效凭证，跳过 SLS 批量查询")
        return {}

    queries = []
    for mod in _MODULE_DEFS:
        key = mod["key"]
        url = mod["url"]
        url_filter = f' and url:{url}*' if url else ""
        query = (
            f'* and host:"research-api.qimingdaren.com"'
            f' and not tianli_school_id:"-"'
            f'{url_filter}'
            f' | SELECT tianli_school_id, COUNT(DISTINCT tianli_user_id) as count'
            f' GROUP BY tianli_school_id'
        )
        queries.append({
            "refId": key,
            "datasource": SLS_DATASOURCE,
            "query": query,
            "type": "logstore",
            "logstore": "nginx-ingress",
        })

    payload = {
        "queries": queries,
        "from": str(start_ts_ms),
        "to": str(end_ts_ms),
    }

    try:
        req = urllib.request.Request(
            f"{GRAFANA_BASE}/api/ds/query",
            data=json.dumps(payload).encode(),
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())

        result = {}
        for ref_id, ref_data in data.get("results", {}).items():
            school_map = {}
            for frame in ref_data.get("frames", []):
                fields = frame.get("schema", {}).get("fields", [])
                values = frame.get("data", {}).get("values", [])
                if len(fields) >= 2 and len(values) >= 2:
                    for i in range(len(values[0])):
                        sid = str(values[0][i])
                        cnt = int(values[1][i]) if values[1][i] else 0
                        if sid:
                            school_map[sid] = school_map.get(sid, 0) + cnt
            result[ref_id] = school_map
        return result
    except Exception as e:
        logger.warning("SLS 批量查询失败: %s", e)
        return {}


@charts_bp.route("/api/charts/trend")
def trend_chart():
    """全校周期趋势 API — 总日活人数趋势

    根据筛选时间范围自动选择聚合粒度:
    - <= 31天: 按天聚合，返回折线图
    - 32-90天: 按周聚合，返回柱状图
    - > 90天: 按月聚合，返回柱状图

    单一维度: 总日活人数（UV）
    """
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    school_id_filter = request.args.get("school_id", "")
    types = request.args.get("types", "")
    stage = request.args.get("stage", "")

    if not start_date or not end_date:
        return jsonify({"error": "时间范围为必填项"}), 400

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "日期格式错误"}), 400

    days = (end_dt - start_dt).days + 1

    # 决定聚合粒度
    if days <= 31:
        granularity = "day"
        chart_type = "line"
    elif days <= 90:
        granularity = "week"
        chart_type = "bar"
    else:
        granularity = "month"
        chart_type = "bar"

    # 定义 SQL 日期聚合表达式
    if granularity == "day":
        date_group = "substr(d.stat_date,1,10)"
    elif granularity == "week":
        date_group = "strftime('%Y-W%W', substr(d.stat_date,1,10))"
    else:
        date_group = "strftime('%Y-%m', substr(d.stat_date,1,10))"

    # 获取学校列表
    conn = None
    try:
        conn = _get_mb_conn()

        # 构建学校过滤
        school_sql = """
            SELECT DISTINCT CAST(school_id AS TEXT) AS sid
            FROM teacher_base WHERE state = 1
              AND school_name IS NOT NULL AND school_name != ''
        """
        school_params = []
        if school_id_filter:
            school_sql += " AND CAST(school_id AS TEXT) = ?"
            school_params.append(school_id_filter)
        elif types:
            from models.school import School
            type_set = set(t.strip() for t in types.split(",") if t.strip())
            all_local = School.get_all()
            allowed_sids = [
                s.metabase_school_id for s in all_local
                if s.metabase_school_id and (
                    (s.type or "") in type_set
                    or (s.display_name or "") in type_set
                    or (s.name or "") in type_set
                )
            ]
            if allowed_sids:
                placeholders = ",".join(["?"] * len(allowed_sids))
                school_sql += f" AND CAST(school_id AS TEXT) IN ({placeholders})"
                school_params.extend(allowed_sids)
            else:
                return jsonify({"labels": [], "datasets": [], "chart_type": chart_type, "granularity": granularity})

        school_rows = conn.execute(school_sql, school_params).fetchall()
        sids = [r["sid"] for r in school_rows]

        if not sids:
            return jsonify({"labels": [], "datasets": [], "chart_type": chart_type, "granularity": granularity})

        # 查询日活 UV — 总日活人数
        sid_placeholders = ",".join(["?"] * len(sids))
        uv_sql = f"""
            SELECT {date_group} AS period,
                   COUNT(DISTINCT d.tianli_user_id) AS uv
            FROM dws_ingress_teacher_day d
            WHERE d.host = 'research-api.qimingdaren.com'
              AND d.school_name NOT LIKE '%启鸣达人%'
              AND d.school_name IS NOT NULL AND d.school_name <> ''
              AND d.tianli_user_id IS NOT NULL AND d.tianli_user_id <> '' AND d.tianli_user_id <> '-'
              AND CAST(d.tianli_school_id AS TEXT) IN ({sid_placeholders})
              AND substr(d.stat_date,1,10) >= ? AND substr(d.stat_date,1,10) <= ?
              AND d.tianli_user_id IN (
                  SELECT t.teacher_id FROM teacher_base t
                  WHERE CAST(t.school_id AS TEXT) = CAST(d.tianli_school_id AS TEXT)
                    AND t.state = 1
              )
            GROUP BY {date_group}
            ORDER BY period
        """
        uv_rows = conn.execute(uv_sql, sids + [start_date, end_date]).fetchall()

        # 组装返回数据 — 单一维度：总日活人数
        labels = []
        uv_data = []
        for r in uv_rows:
            labels.append(r["period"])
            uv_data.append(r["uv"])

        datasets = [{
            "label": "日活人数",
            "data": uv_data,
            "borderColor": "rgb(99,102,241)",
            "backgroundColor": "rgba(99,102,241,0.1)" if chart_type == "line" else "rgb(99,102,241)",
            "fill": chart_type == "line",
            "tension": 0.3 if chart_type == "line" else 0,
            "borderWidth": 2,
            "borderRadius": 4 if chart_type == "bar" else 0,
            "pointRadius": 3 if chart_type == "line" else 0,
            "pointHoverRadius": 6 if chart_type == "line" else 0,
        }]

        return jsonify({
            "labels": labels,
            "datasets": datasets,
            "chart_type": chart_type,
            "granularity": granularity,
        })

    except Exception as e:
        logger.warning("趋势图表查询失败: %s", e)
        return jsonify({"labels": [], "datasets": [], "chart_type": "line", "granularity": "day", "error": str(e)})
    finally:
        if conn:
            conn.close()


@charts_bp.route("/api/charts/module-usage")
def module_usage():
    """多校 8 模块使用率 API

    数据源优先级：
    1. Metabase API（与 Lida 数据 100% 一致）
    2. Grafana SLS 回退
    3. metabase.db 整体数据回退
    """

    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    stage = request.args.get("stage", "")
    grade = request.args.get("grade", "")
    subject = request.args.get("subject", "")
    school_id_filter = request.args.get("school_id", "")
    # 支持逗号分隔的多校查询（多校对比页）
    school_id_set = set(_split_csv(school_id_filter)) if school_id_filter else set()
    types = request.args.get("types", "")

    if not start_date or not end_date:
        return jsonify({"error": "时间范围为必填项"}), 400

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "日期格式错误"}), 400

    # ── 尝试 Metabase API（首选数据源）──
    try:
        metabase_result = asyncio.run(_query_metabase_modules(
            start_dt.date(), end_dt.date(), stage, school_id_set, types
        ))
        if metabase_result is not None and metabase_result.get("rows"):
            return jsonify(metabase_result)
    except Exception as e:
        logger.warning("Metabase API 查询失败，回退到 SLS: %s", e)

    # ── 回退到 SLS / metabase.db ──
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000) + 86399000

    extra_filter, extra_params = _build_extra_filter(stage, grade, subject)

    # 从 metabase.db 获取教师总数（用于计算使用率分母）
    conn = None
    try:
        conn = _get_mb_conn()

        # 获取学校列表
        school_sql = """
            SELECT DISTINCT CAST(school_id AS TEXT) AS sid, school_name
            FROM teacher_base
            WHERE state = 1
              AND school_name IS NOT NULL AND school_name != ''
        """
        school_params = []
        if school_id_set:
            placeholders = ",".join(["?"] * len(school_id_set))
            school_sql += f" AND CAST(school_id AS TEXT) IN ({placeholders})"
            school_params.extend(school_id_set)
        school_sql += " ORDER BY school_name"
        school_rows = conn.execute(school_sql, school_params).fetchall()

        # 构建学校数据
        schools_info = {}
        for s in school_rows:
            sid = s["sid"]
            total_sql = f"""
                SELECT COUNT(DISTINCT teacher_id) AS cnt
                FROM teacher_base
                WHERE CAST(school_id AS TEXT) = ? AND state = 1
                {extra_filter}
            """
            total = conn.execute(total_sql, [sid] + extra_params).fetchone()["cnt"]
            if total > 0:
                schools_info[sid] = {
                    "school": s["school_name"],
                    "school_id": sid,
                    "total_teachers": total,
                }
    finally:
        if conn:
            conn.close()

    # 查询 SLS 获取每个模块的活跃教师数（批量查询）
    module_active = {}  # {module_key: {school_id: active_count}}
    can_query_sls = _get_grafana_auth() is not None

    if can_query_sls:
        module_active = _query_sls_batch(start_ms, end_ms)
        if not module_active:
            logger.warning("SLS 批量查询返回空结果，回退到 metabase")
            can_query_sls = False
        else:
            for key in _MODULE_DEFS:
                count = len(module_active.get(key["key"], {}))
                logger.info("模块 %s: %d 所学校有数据", key["key"], count)

    # 组装结果
    from scrapers.api_lida import ALL_MODULE_NAMES
    module_names = list(ALL_MODULE_NAMES)  # 13 列
    rows = []

    # 获取 school_id → (display_name, type, priority, owner_id) 映射
    from models.school import School
    all_local = School.get_all()
    # 构建 owner_id → 手机号 映射（仅直营校有负责人）
    owner_map = {}
    for s in all_local:
        if s.owner_id:
            from models.user import User
            u = User.get_by_id(s.owner_id)
            if u:
                owner_map[s.id] = u.username
    school_meta = {
        s.metabase_school_id: (s.display_name or s.name, s.type or "", s.priority or "中", s.owner_id or 0)
        for s in all_local if s.metabase_school_id
    }

    for sid, info in schools_info.items():
        total = info["total_teachers"]
        display_name, stype, spriority, row_owner_id = school_meta.get(sid, (info["school"], "", "中", 0))

        if can_query_sls and module_active:
            values = []
            rate_values = []
            for mod in _MODULE_DEFS:
                active = module_active.get(mod["key"], {}).get(sid, 0)
                rate = round(active / total * 100, 1) if total > 0 else 0
                values.append(f"{rate}%")
                rate_values.append(rate)
        else:
            # 回退模式：只用 metabase 计算整体活跃，其他模块显示 "-"
            conn2 = _get_mb_conn()
            try:
                overall_sql = """
                    SELECT COUNT(DISTINCT tianli_user_id) AS cnt
                    FROM dws_ingress_teacher_day
                    WHERE CAST(tianli_school_id AS TEXT) = ?
                      AND host = 'research-api.qimingdaren.com'
                      AND tianli_user_id IS NOT NULL AND tianli_user_id <> '' AND tianli_user_id <> '-'
                      AND substr(stat_date,1,10) >= ? AND substr(stat_date,1,10) <= ?
                """
                overall_active = conn2.execute(overall_sql, [sid, start_date, end_date]).fetchone()["cnt"]
            finally:
                conn2.close()
            overall_rate = round(overall_active / total * 100, 1) if total > 0 else 0

            values = []
            rate_values = []
            for mod in _MODULE_DEFS:
                if mod["key"] == "overall":
                    values.append(f"{overall_rate}%")
                    rate_values.append(overall_rate)
                else:
                    values.append("-")
                    rate_values.append(0)

        rows.append({
            "school": info["school"],
            "display_name": display_name,
            "type": stype,
            "school_id": sid,
            "total_teachers": total,
            "values": values,
            "rate_values": rate_values,
            "priority": spriority,
            "owner_name": owner_map.get(row_owner_id, ""),
            "owner_id": row_owner_id,
        })

    # 补充 5 个附加指标
    _enrich_extra_metrics(rows, start_date, end_date)

    return jsonify({
        "columns": module_names,
        "rows": rows,
        "total_schools": len(rows),
        "source": "sls" if can_query_sls else "metabase",
    })
