"""首页/仪表盘路由"""
from flask import Blueprint, render_template, jsonify, request, session

from config.config_loader import get_schools
from models.weekly_record import WeeklyRecord

main_bp = Blueprint("main", __name__)


def _get_allowed_schools():
    """获取当前用户可见的学校名列表。管理员返回 None（表示全部），普通用户返回 assigned_schools 中的学校。"""
    if session.get("is_admin"):
        return None
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


@main_bp.route("/api/dashboard")
def dashboard_api():
    """仪表盘数据 API"""
    allowed = _get_allowed_schools()
    record_type = request.args.get("type", "weekly")
    if record_type == "monthly":
        from models.monthly_record import MonthlyRecord
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
