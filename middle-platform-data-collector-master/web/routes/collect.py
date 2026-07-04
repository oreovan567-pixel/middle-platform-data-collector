"""采集任务相关 API"""
import json
import queue
from datetime import date

from flask import Blueprint, request, jsonify, Response, stream_with_context

from config.config_loader import get_schools, set_user_creds_override
from services.collector import Collector
from models.user import User
from flask import session

collect_bp = Blueprint("collect", __name__)

# 全局单例采集器
_collector = Collector()

MONTH_NAMES = ["一月", "二月", "三月", "四月", "五月", "六月",
               "七月", "八月", "九月", "十月", "十一月", "十二月"]


@collect_bp.route("/start", methods=["POST"])
def start_collect():
    """启动采集任务"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "请求体不能为空"}), 400

    school_names = data.get("schools", [])
    year = data.get("year", date.today().year)
    week_number = data.get("week_number")
    start_date_str = data.get("start_date")
    end_date_str = data.get("end_date")
    platforms = data.get("platforms")
    record_type = data.get("record_type", "weekly")
    month_number = data.get("month_number", "")
    data_source = data.get("data_source", "grafana")

    if not school_names:
        return jsonify({"error": "请选择至少一个学校"}), 400
    if not week_number:
        return jsonify({"error": "请填写周次/月次"}), 400
    if not start_date_str or not end_date_str:
        return jsonify({"error": "请提供时间范围"}), 400

    try:
        start_date = date.fromisoformat(start_date_str)
        end_date = date.fromisoformat(end_date_str)
    except ValueError:
        return jsonify({"error": "日期格式错误，请使用 YYYY-MM-DD"}), 400

    # 验证学校名称
    all_schools = {s["name"] for s in get_schools()}
    invalid = [n for n in school_names if n not in all_schools]
    if invalid:
        return jsonify({"error": f"未知的学校: {', '.join(invalid)}"}), 400

    # 验证月度模式的月次格式
    if record_type == "monthly" and month_number:
        if month_number not in MONTH_NAMES:
            return jsonify({"error": f"无效的月次格式: {month_number}，请使用'一月'~'十二月'"}), 400

    # 互斥检查
    if _collector.is_running:
        return jsonify({"error": "已有采集任务正在执行，请等待完成"}), 409

    # 设置用户凭证
    user_id = session.get("user_id")
    if user_id:
        user = User.get_by_id(user_id)
        if user:
            creds = {}
            if user.lida_username and user.lida_password:
                creds["lida"] = {"username": user.lida_username, "password": user.lida_password}
            if user.grafana_username and user.grafana_password:
                creds["grafana"] = {"username": user.grafana_username, "password": user.grafana_password}
            if user.main_site_username and user.main_site_password:
                creds["main_site"] = {"username": user.main_site_username, "password": user.main_site_password}
            set_user_creds_override(creds if creds else None)
        else:
            set_user_creds_override(None)
    else:
        set_user_creds_override(None)

    try:
        task_id = _collector.start_collect(
            school_names=school_names,
            year=year,
            week_number=week_number,
            start_date=start_date,
            end_date=end_date,
            platforms=platforms,
            record_type=record_type,
            month_number=month_number,
            user_id=user_id,
            data_source=data_source,
        )
        return jsonify({"task_id": task_id, "message": "采集任务已启动",
                        "record_type": record_type, "data_source": data_source})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409


@collect_bp.route("/status")
def get_status():
    """获取采集状态"""
    return jsonify({
        "running": _collector.is_running,
        "paused": _collector.is_paused,
        "task_id": _collector.current_task_id,
        "user_id": _collector.current_user_id,
    })


@collect_bp.route("/pause", methods=["POST"])
def pause_collect():
    """暂停采集"""
    if not _collector.is_running:
        return jsonify({"error": "没有正在执行的采集任务"}), 400
    if _collector.is_paused:
        return jsonify({"message": "已经处于暂停状态"})
    _collector.pause()
    return jsonify({"message": "采集已暂停"})


@collect_bp.route("/resume", methods=["POST"])
def resume_collect():
    """继续采集"""
    if not _collector.is_running:
        return jsonify({"error": "没有正在执行的采集任务"}), 400
    if not _collector.is_paused:
        return jsonify({"message": "采集未暂停"})
    _collector.resume()
    return jsonify({"message": "采集已继续"})


@collect_bp.route("/stream")
def stream_progress():
    """SSE 进度流（每个客户端独立订阅）"""
    sub_id, q = _collector.subscribe()

    def generate():
        try:
            while True:
                try:
                    event = q.get(timeout=5)
                    yield f"data: {event.to_json()}\n\n"
                    if event.platform == "system" and event.status == "completed":
                        break
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                    # 采集器已完成且队列为空，兜底退出
                    if not _collector.is_running and q.empty():
                        done = {"school": "", "platform": "system",
                                "status": "completed",
                                "message": "采集完成"}
                        yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
                        break
        finally:
            _collector.unsubscribe(sub_id)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
