"""学校配置 CRUD API"""
from flask import Blueprint, request, jsonify, session

from models.school import School

school_bp = Blueprint("school", __name__)

_REQUIRED_FIELDS = ["name", "grafana_name", "main_site_name"]


def _to_api_dict(school):
    d = school.to_dict()
    d["id"] = school.id
    d["owner_id"] = school.owner_id
    d["priority"] = school.priority or "中"
    # 附上负责人姓名和账号
    owner_name = ""
    owner_phone = ""
    if school.owner_id:
        from models.user import User
        owner = User.get_by_id(school.owner_id)
        if owner:
            owner_name = owner.display_name or owner.username
            owner_phone = owner.username
    d["owner_name"] = owner_name
    d["owner_phone"] = owner_phone
    return d


def _is_admin():
    return bool(session.get("is_admin")) or session.get("role") in ("super_admin", "admin")


def _is_super_admin():
    return session.get("role") == "super_admin"


def _current_user_id():
    return session.get("user_id")


def _get_user_school_names():
    """获取当前用户被分配的学校名称列表，管理员返回 None"""
    if _is_admin():
        return None
    user_id = _current_user_id()
    if not user_id:
        return []
    from models.user import User
    user = User.get_by_id(user_id)
    if not user:
        return []
    return user.school_list


def _get_visible_schools():
    allowed = _get_user_school_names()
    if allowed is None:
        return School.get_all()
    return [s for s in School.get_all() if s.name in allowed]


@school_bp.route("", methods=["GET"])
def list_schools():
    schools = _get_visible_schools()
    return jsonify({"schools": [_to_api_dict(s) for s in schools]})


@school_bp.route("", methods=["POST"])
def create_school():
    data = request.get_json()
    if not data:
        return jsonify({"error": "\u8bf7\u6c42\u4f53\u4e0d\u80fd\u4e3a\u7a7a"}), 400

    for field in _REQUIRED_FIELDS:
        if not data.get(field, "").strip():
            return jsonify({"error": f"\'{field}\' \u4e3a\u5fc5\u586b\u9879"}), 400

    name = data["name"].strip()
    existing = School.get_by_name(name)
    if existing:
        return jsonify({"error": f"\u5b66\u6821 \'{name}\' \u5df2\u5b58\u5728"}), 409

    school = School(
        name=name,
        lida_name=data.get("lida_name", name).strip(),
        grafana_name=data["grafana_name"].strip(),
        main_site_name=data["main_site_name"].strip(),
        metabase_school_id=data.get("metabase_school_id", "").strip(),
        xueduan=data.get("xueduan", "").strip(),
        nianji=data.get("nianji", "").strip(),
        jibu=data.get("jibu", "").strip(),
        xuebu=data.get("xuebu", "").strip(),
        owner_id=_current_user_id(),
    )
    school.save()

    # 自动将新学校加入当前用户的 assigned_schools
    if not _is_admin():
        user_id = _current_user_id()
        if user_id:
            from models.user import User
            user = User.get_by_id(user_id)
            if user:
                current = [s.strip() for s in (user.assigned_schools or "").split(",") if s.strip()]
                if name not in current:
                    current.append(name)
                    user.assigned_schools = ",".join(current)
                    user.save()

    saved = School.get_by_name(name)
    return jsonify({"school": _to_api_dict(saved)}), 201


@school_bp.route("/<int:school_id>", methods=["PUT"])
def update_school(school_id):
    school = School.get_by_id(school_id)
    if not school:
        return jsonify({"error": "\u5b66\u6821\u4e0d\u5b58\u5728"}), 404

    if not _is_admin():
        user_school_names = _get_user_school_names() or []
        if school.name not in user_school_names:
            return jsonify({"error": "\u53ea\u80fd\u7f16\u8f91\u5206\u914d\u7ed9\u60a8\u7684\u5b66\u6821"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "\u8bf7\u6c42\u4f53\u4e0d\u80fd\u4e3a\u7a7a"}), 400

    for field in _REQUIRED_FIELDS:
        val = data.get(field, "").strip() if data.get(field) else ""
        if not val:
            return jsonify({"error": f"\'{field}\' \u4e3a\u5fc5\u586b\u9879"}), 400

    new_name = data["name"].strip()
    if new_name != school.name:
        existing = School.get_by_name(new_name)
        if existing:
            return jsonify({"error": f"\u5b66\u6821 \'{new_name}\' \u5df2\u5b58\u5728"}), 409

    school.name = new_name
    school.lida_name = data.get("lida_name", school.lida_name).strip()
    school.grafana_name = data["grafana_name"].strip()
    school.main_site_name = data["main_site_name"].strip()
    school.metabase_school_id = data.get("metabase_school_id", "").strip()
    school.xueduan = data.get("xueduan", "").strip()
    school.nianji = data.get("nianji", "").strip()
    school.jibu = data.get("jibu", "").strip()
    school.xuebu = data.get("xuebu", "").strip()
    if "sort_order" in data:
        school.sort_order = data["sort_order"]
    school.save()

    updated = School.get_by_id(school_id)
    return jsonify({"school": _to_api_dict(updated)})


@school_bp.route("/<int:school_id>", methods=["DELETE"])
def delete_school(school_id):
    school = School.get_by_id(school_id)
    if not school:
        return jsonify({"error": "\u5b66\u6821\u4e0d\u5b58\u5728"}), 404

    if not _is_admin():
        user_school_names = _get_user_school_names() or []
        if school.name not in user_school_names:
            return jsonify({"error": "\u53ea\u80fd\u5220\u9664\u5206\u914d\u7ed9\u60a8\u7684\u5b66\u6821"}), 403

    school.delete()
    return "", 204


@school_bp.route("/batch-owner", methods=["PUT"])
def batch_update_owner():
    """批量更新学校负责人 — 仅超级管理员可操作
    接收: [{school_id, owner_name, owner_phone}]
    按手机号匹配用户，同时更新用户 display_name
    """
    if not _is_super_admin():
        return jsonify({"error": "仅超级管理员可操作"}), 403

    data = request.get_json()
    if not data or not isinstance(data, list):
        return jsonify({"error": "请求体必须为数组"}), 400

    from models.user import User

    updated = 0
    for item in data:
        sid = item.get("school_id")
        owner_name = (item.get("owner_name") or "").strip()
        owner_phone = (item.get("owner_phone") or "").strip()

        school = School.get_by_id(sid)
        if not school:
            continue

        # 如果手机号或姓名为空，则清空负责人
        if not owner_phone and not owner_name:
            school.owner_id = None
            school.save()
            updated += 1
            continue

        # 按手机号查找用户
        user = None
        if owner_phone:
            user = User.get_by_username(owner_phone)

        # 如果按手机号没找到，按姓名查找
        if not user and owner_name:
            from models.database import get_connection
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT id FROM users WHERE display_name = ? LIMIT 1",
                    (owner_name,)
                ).fetchone()
                if row:
                    user = User.get_by_id(row["id"])

        if user:
            # 更新用户 display_name
            if owner_name and user.display_name != owner_name:
                user.display_name = owner_name
                user.save()
            school.owner_id = user.id
            school.save()
            updated += 1

    return jsonify({"updated": updated})


@school_bp.route("/batch-priority", methods=["PUT"])
def batch_update_priority():
    """批量更新学校优先级"""
    if not _is_admin():
        return jsonify({"error": "仅管理员可操作"}), 403

    data = request.get_json()
    if not data or not isinstance(data, list):
        return jsonify({"error": "请求体必须为数组"}), 400

    updated = 0
    for item in data:
        sid = item.get("school_id")
        priority = item.get("priority", "中")
        if priority not in ("高", "中", "低"):
            continue
        school = School.get_by_id(sid)
        if school:
            school.priority = priority
            school.save()
            updated += 1

    return jsonify({"updated": updated})
