"""用户模型"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

from models.database import get_connection


@dataclass
class User:
    username: str
    password: str
    lida_username: str = ""
    lida_password: str = ""
    grafana_username: str = ""
    grafana_password: str = ""
    main_site_username: str = ""
    main_site_password: str = ""
    assigned_schools: str = ""
    is_admin: bool = False
    role: str = "user"  # super_admin / admin / user
    display_name: str = ""  # 真实姓名
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def school_list(self) -> list[str]:
        if not self.assigned_schools:
            return []
        import re
        return [s.strip() for s in re.split(r'[,，、]', self.assigned_schools) if s.strip()]

    def get_credentials(self, platform: str) -> dict:
        if platform == "lida":
            return {"username": self.lida_username, "password": self.lida_password}
        elif platform == "grafana":
            return {"username": self.grafana_username, "password": self.grafana_password}
        elif platform == "main_site":
            return {"username": self.main_site_username, "password": self.main_site_password}
        return {}

    @property
    def is_super_admin(self) -> bool:
        return self.role == "super_admin"

    @property
    def can_manage_schools(self) -> bool:
        return self.role in ("super_admin", "admin")

    def save(self):
        now = datetime.now().isoformat()
        with get_connection() as conn:
            if self.id:
                conn.execute("UPDATE users SET username=?, password=?, display_name=?, lida_username=?, lida_password=?, grafana_username=?, grafana_password=?, main_site_username=?, main_site_password=?, assigned_schools=?, is_admin=?, role=?, updated_at=? WHERE id=?", (self.username, self.password, self.display_name, self.lida_username, self.lida_password, self.grafana_username, self.grafana_password, self.main_site_username, self.main_site_password, self.assigned_schools, int(self.is_admin), self.role, now, self.id))
            else:
                cursor = conn.execute("INSERT INTO users (username, password, display_name, lida_username, lida_password, grafana_username, grafana_password, main_site_username, main_site_password, assigned_schools, is_admin, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (self.username, self.password, self.display_name, self.lida_username, self.lida_password, self.grafana_username, self.grafana_password, self.main_site_username, self.main_site_password, self.assigned_schools, int(self.is_admin), self.role, now, now))
                self.id = cursor.lastrowid

    def delete(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (self.id,))

    @staticmethod
    def get_all() -> list["User"]:
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY is_admin DESC, username").fetchall()
            return [User._from_row(r) for r in rows]

    @staticmethod
    def get_by_id(user_id: int) -> "User | None":
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return User._from_row(row) if row else None

    @staticmethod
    def get_by_username(username: str) -> "User | None":
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            return User._from_row(row) if row else None

    @staticmethod
    def authenticate(username: str, password: str) -> "User | None":
        user = User.get_by_username(username)
        if user and user.password == password:
            return user
        return None

    @staticmethod
    def _from_row(row) -> "User":
        return User(
            id=row["id"], username=row["username"], password=row["password"],
            lida_username=row["lida_username"] or "",
            lida_password=row["lida_password"] or "",
            grafana_username=row["grafana_username"] or "",
            grafana_password=row["grafana_password"] or "",
            main_site_username=row["main_site_username"] or "",
            main_site_password=row["main_site_password"] or "",
            assigned_schools=row["assigned_schools"] or "",
            is_admin=bool(row["is_admin"]),
            role=row["role"] if "role" in row.keys() else ("super_admin" if bool(row["is_admin"]) else "user"),
            display_name=row["display_name"] if "display_name" in row.keys() else "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    def to_dict(self, include_passwords=False) -> dict:
        d = {
            "id": self.id, "username": self.username, "display_name": self.display_name,
            "assigned_schools": self.assigned_schools,
            "school_list": self.school_list,
            "is_admin": self.is_admin,
            "role": self.role,
            "has_lida_creds": bool(self.lida_username and self.lida_password),
            "has_grafana_creds": bool(self.grafana_username and self.grafana_password),
            "has_main_site_creds": bool(self.main_site_username and self.main_site_password),
        }
        if include_passwords:
            d.update({
                "password": self.password,
                "lida_username": self.lida_username, "lida_password": self.lida_password,
                "grafana_username": self.grafana_username, "grafana_password": self.grafana_password,
                "main_site_username": self.main_site_username, "main_site_password": self.main_site_password,
            })
        return d
