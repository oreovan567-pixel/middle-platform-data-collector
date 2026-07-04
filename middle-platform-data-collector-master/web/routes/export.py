"""数据导出 API"""
import re

from flask import Blueprint, request, jsonify, send_file, session

from models.weekly_record import WeeklyRecord
from models.monthly_record import MonthlyRecord
from services.exporter import export_weekly, export_monthly

export_bp = Blueprint("export", __name__)


def _safe_filename(label: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]', '_', label)


def _get_allowed_schools():
    """获取当前用户可见的学校列表"""
    user_id = session.get("user_id")
    if not user_id:
        return None
    from models.user import User
    user = User.get_by_id(user_id)
    if not user:
        return None
    if user.is_admin or not user.assigned_schools:
        return None
    return user.school_list


@export_bp.route("/weekly")
def export_weekly_data():
    """导出单周数据"""
    allowed = _get_allowed_schools()
    year = request.args.get("year", type=int)
    week_number = request.args.get("week_number", type=str) or ""
    school_name = request.args.get("school_name", "")
    month_prefix = request.args.get("month_prefix", "")

    if not year:
        return jsonify({"error": "请提供 year 参数"}), 400

    # 非管理员如果选择了不在自己范围内的学校，拒绝
    if allowed is not None and school_name and school_name not in allowed:
        return jsonify({"error": "无权访问该学校数据"}), 403

    records = WeeklyRecord.query_flexible(year, week_number, school_name, month_prefix)
    # 过滤记录
    if allowed is not None:
        records = [r for r in records if r.school_name in allowed]
    if not records:
        return jsonify({"error": "没有找到对应数据"}), 404

    filepath = export_weekly(records, year, week_number)
    from services.exporter import _build_export_name
    _period = week_number or (month_prefix if month_prefix else "")
    dl_name = _build_export_name(year, _period) + ".xlsx"
    return send_file(
        filepath,
        as_attachment=True,
        download_name=dl_name,
    )


@export_bp.route("/preview")
def preview_data():
    """预览数据（JSON）"""
    allowed = _get_allowed_schools()
    year = request.args.get("year", type=int)
    week_number = request.args.get("week_number", type=str) or ""
    school_name = request.args.get("school_name", "")
    month_prefix = request.args.get("month_prefix", "")

    if not year:
        return jsonify({"error": "请提供 year 参数"}), 400

    # 非管理员如果选择了不在自己范围内的学校，返回空
    if allowed is not None and school_name and school_name not in allowed:
        return jsonify({"records": []})

    records = WeeklyRecord.query_flexible(year, week_number, school_name, month_prefix)
    if allowed is not None:
        records = [r for r in records if r.school_name in allowed]
    return jsonify({"records": [r.to_dict() for r in records]})


@export_bp.route("/monthly")
def export_monthly_data():
    """导出月度数据"""
    allowed = _get_allowed_schools()
    year = request.args.get("year", type=int)
    month_number = request.args.get("month_number", "")
    school_name = request.args.get("school_name", "")

    if not year:
        return jsonify({"error": "请提供 year 参数"}), 400

    if allowed is not None and school_name and school_name not in allowed:
        return jsonify({"error": "无权访问该学校数据"}), 403

    records = MonthlyRecord.query_flexible(year, month_number, school_name)
    if allowed is not None:
        records = [r for r in records if r.school_name in allowed]
    if not records:
        return jsonify({"error": "没有找到对应数据"}), 404

    filepath = export_monthly(records, year, month_number)
    from services.exporter import _build_export_name
    dl_name = _build_export_name(year, month_number) + ".xlsx"
    return send_file(
        filepath,
        as_attachment=True,
        download_name=dl_name,
    )

@export_bp.route("/distinct_weeks")
def distinct_weeks():
    """查询指定年份已有的不重复周标签"""
    year = request.args.get("year", type=int)
    if not year:
        return jsonify({"error": "请提供 year 参数"}), 400
    weeks = WeeklyRecord.query_distinct_weeks(year)
    return jsonify({"weeks": weeks})
