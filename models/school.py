"""学校配置数据模型"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

from models.database import get_connection


@dataclass
class School:
    name: str
    lida_name: str
    grafana_name: str
    main_site_name: str
    metabase_school_id: str = ""
    display_name: str = ""
    type: str = ""  # "直营校" / "托管校"
    xueduan: str = ""
    nianji: str = ""
    jibu: str = ""
    xuebu: str = ""
    id: int | None = None
    sort_order: int = 0
    owner_id: int | None = None
    priority: str = "中"  # 高/中/低
    created_at: str = ""
    updated_at: str = ""

    def save(self):
        """插入或更新记录（按 name 去重）"""
        now = datetime.now().isoformat()
        with get_connection() as conn:
            if self.id:
                conn.execute("""
                    UPDATE schools SET
                        name=?, lida_name=?, grafana_name=?, main_site_name=?,
                        metabase_school_id=?, display_name=?, type=?,
                        xueduan=?, nianji=?, jibu=?, xuebu=?,
                        sort_order=?, priority=?, updated_at=?
                    WHERE id=?
                """, (
                    self.name, self.lida_name, self.grafana_name,
                    self.main_site_name, self.metabase_school_id,
                    self.display_name or self.name, self.type or "",
                    self.xueduan, self.nianji,
                    self.jibu, self.xuebu, self.sort_order,
                    self.priority or "中", now, self.id,
                ))
            else:
                conn.execute("""
                    INSERT INTO schools (
                        name, lida_name, grafana_name, main_site_name,
                        metabase_school_id, display_name, type,
                        xueduan, nianji, jibu, xuebu, sort_order,
                        owner_id, priority, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        lida_name=excluded.lida_name,
                        grafana_name=excluded.grafana_name,
                        main_site_name=excluded.main_site_name,
                        metabase_school_id=excluded.metabase_school_id,
                        display_name=excluded.display_name,
                        type=excluded.type,
                        xueduan=excluded.xueduan,
                        nianji=excluded.nianji,
                        jibu=excluded.jibu,
                        xuebu=excluded.xuebu,
                        sort_order=excluded.sort_order,
                        priority=excluded.priority,
                        updated_at=excluded.updated_at
                """, (
                    self.name, self.lida_name, self.grafana_name,
                    self.main_site_name, self.metabase_school_id,
                    self.display_name or self.name, self.type or "",
                    self.xueduan, self.nianji,
                    self.jibu, self.xuebu, self.sort_order,
                    self.owner_id, self.priority or "中", now, now,
                ))

    def delete(self):
        """删除记录"""
        with get_connection() as conn:
            conn.execute("DELETE FROM schools WHERE id=?", (self.id,))

    @staticmethod
    def get_all() -> list["School"]:
        """获取所有学校"""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM schools ORDER BY sort_order, name"
            ).fetchall()
            return [School._from_row(r) for r in rows]

    @staticmethod
    def get_by_owner(owner_id: int) -> list["School"]:
        """获取指定用户创建的学校"""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM schools WHERE owner_id=? ORDER BY sort_order, name",
                (owner_id,)
            ).fetchall()
            return [School._from_row(r) for r in rows]

    @staticmethod
    def get_by_name(name: str) -> "School | None":
        """按名称查找学校"""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM schools WHERE name=?", (name,)
            ).fetchone()
            return School._from_row(row) if row else None

    @staticmethod
    def get_by_id(school_id: int) -> "School | None":
        """按 ID 查找学校"""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM schools WHERE id=?", (school_id,)
            ).fetchone()
            return School._from_row(row) if row else None

    @staticmethod
    def _from_row(row) -> "School":
        return School(
            id=row["id"],
            name=row["name"],
            lida_name=row["lida_name"],
            grafana_name=row["grafana_name"],
            main_site_name=row["main_site_name"],
            metabase_school_id=row["metabase_school_id"] if "metabase_school_id" in row.keys() else "",
            display_name=row["display_name"] if "display_name" in row.keys() else "",
            type=row["type"] if "type" in row.keys() else "",
            xueduan=row["xueduan"] or "",
            nianji=row["nianji"] or "",
            jibu=row["jibu"] or "",
            xuebu=row["xuebu"] or "",
            sort_order=row["sort_order"] or 0,
            owner_id=row["owner_id"] if "owner_id" in row.keys() else None,
            priority=row["priority"] if "priority" in row.keys() else "中",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    def to_dict(self) -> dict:
        """返回与旧 YAML dict 完全兼容的字典"""
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "type": self.type or "",
            "lida_name": self.lida_name,
            "grafana_name": self.grafana_name,
            "main_site_name": self.main_site_name,
            "metabase_school_id": self.metabase_school_id,
            "xueduan": self.xueduan,
            "nianji": self.nianji,
            "jibu": self.jibu,
            "xuebu": self.xuebu,
            "priority": self.priority or "中",
        }

    @staticmethod
    def get_by_type(school_type: str) -> list["School"]:
        """按类型获取学校（直营校 / 托管校）"""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM schools WHERE type=? ORDER BY sort_order, name",
                (school_type,)
            ).fetchall()
            return [School._from_row(r) for r in rows]
