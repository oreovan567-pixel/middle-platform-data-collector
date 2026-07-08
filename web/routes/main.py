"""首页/仪表盘/历史记录路由"""
from datetime import date, datetime

from flask import Blueprint, render_template, jsonify, request, session, redirect

from config.config_loader import get_schools
from models.weekly_record import WeeklyRecord
from models.monthly_record import MonthlyRecord

main_bp = Blueprint("main", __name__)


def _get_allowed_schools():
    """获取当前用户可见的学校名列表。管理员返回 None（表示全部），普通用户返回 assigned_schools 中的学校。"""
    if session.get("is_admin"):
        return None  # 管理员看全部
    user_id = session.get("user_id")
    if not user_id:
        return None
    from models.user import User
    user = User.get_by_id(user_id)
    if not user:
        return None
    return user.school_list


def _filter_schools(schools, allowed):
    """根据 allowed 列表过滤学校列表。allowed=None 返回全部。"""
    if allowed is None:
        return schools
    return [s for s in schools if s["name"] in allowed]


def _filter_records(records, allowed):
    """根据 allowed 过滤记录列表。allowed=None 返回全部。"""
    if allowed is None:
        return records
    return [r for r in records if r.school_name in allowed]


@main_bp.route("/")
def index():
    """全校总览大盘"""
    return render_template("index.html")


@main_bp.route("/collect")
def collect_page():
    """数据采集页"""
    allowed = _get_allowed_schools()
    schools = get_schools()
    visible_schools = _filter_schools(schools, allowed)
    today = date.today()
    user_schools = None
    if allowed is not None:
        user_schools = allowed
    return render_template(
        "collect.html",
        schools=visible_schools,
        current_year=today.year,
        user_schools=user_schools,
    )


@main_bp.route("/history")
def history_page():
    """历史记录页"""
    allowed = _get_allowed_schools()
    schools = _filter_schools(get_schools(), allowed)
    return render_template(
        "history.html",
        schools=schools,
        current_year=date.today().year,
    )


@main_bp.route("/api/dashboard")
def dashboard_api():
    """仪表盘数据 API"""
    allowed = _get_allowed_schools()
    record_type = request.args.get("type", "weekly")
    if record_type == "monthly":
        records = _filter_records(MonthlyRecord.query_recent_days(days=30), allowed)
        return jsonify({
            "records": [r.to_dict() for r in records],
            "schools": _filter_schools(get_schools(), allowed),
            "type": "monthly",
        })
    else:
        records = _filter_records(WeeklyRecord.query_recent_days(days=10), allowed)
        return jsonify({
            "records": [r.to_dict() for r in records],
            "schools": _filter_schools(get_schools(), allowed),
            "type": "weekly",
        })


@main_bp.route("/api/history/monthly")
def monthly_history_api():
    """月度历史记录查询 API"""
    allowed = _get_allowed_schools()
    year = request.args.get("year", date.today().year, type=int)
    month_number = request.args.get("month_number", "")
    school_name = request.args.get("school_name", "")

    # 非管理员如果选择了不在自己范围内的学校，返回空
    if allowed is not None and school_name and school_name not in allowed:
        return jsonify({"records": []})

    records = MonthlyRecord.query_flexible(
        year=year,
        month_number=month_number,
        school_name=school_name,
    )
    records = _filter_records(records, allowed)
    return jsonify({
        "records": [r.to_dict() for r in records],
    })


@main_bp.route("/users")
def users_page():
    """用户管理页面（超级管理员）"""
    if not session.get("is_admin") and session.get("role") != "super_admin":
        return redirect("/")
    return render_template("users.html")


@main_bp.route("/settings")
def settings_page():
    """个人设置页面"""
    return render_template("settings.html")
