"""SQLite 数据库连接管理与表结构初始化"""
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent.parent / "data")))
_DB_PATH = _DATA_DIR / "app.db"


def _ensure_dirs():
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_db_path() -> Path:
    return _DB_PATH


@contextmanager
def get_connection():
    """获取数据库连接的上下文管理器"""
    _ensure_dirs()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Migration: add platform_elapsed column if not exists
    for table in ('weekly_records', 'monthly_records'):
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not cols:
            continue  # table does not exist yet
        col_names = [c["name"] for c in cols]
        if 'platform_elapsed' not in col_names:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN platform_elapsed TEXT DEFAULT ''")
            conn.commit()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_WEEKLY_RECORDS_SCHEMA = """
    CREATE TABLE weekly_records (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        school_name         TEXT    NOT NULL,
        year                INTEGER NOT NULL,
        week_number         TEXT    NOT NULL,
        week_start_date     TEXT,
        week_end_date       TEXT,
        overall_usage_rate  TEXT,
        overall_jibei       TEXT,
        grade_jibei         TEXT,
        department_jibei    TEXT,
        homework_count      TEXT,
        weekly_active_teachers TEXT,
        weekly_total_teachers  TEXT,
        weekly_overall_activity TEXT,
        weekly_active_ratio TEXT,
        collected_at        TEXT    NOT NULL,
        status              TEXT    NOT NULL DEFAULT 'success',
        error_message       TEXT,
        UNIQUE(school_name, year, week_number)
    )
"""

_COLLECT_TASKS_SCHEMA = """
    CREATE TABLE collect_tasks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        year            INTEGER NOT NULL,
        week_number     TEXT    NOT NULL,
        schools         TEXT    NOT NULL,
        status          TEXT    NOT NULL DEFAULT 'pending',
        progress        TEXT,
        started_at      TEXT,
        finished_at     TEXT,
        result_summary  TEXT
    )
"""


def _migrate_if_needed(conn):
    """检测 week_number 是否为 INTEGER，如果是则迁移为 TEXT"""
    # 检查 weekly_records 表
    cols = conn.execute("PRAGMA table_info(weekly_records)").fetchall()
    if not cols:
        return  # 表不存在，跳过
    week_col = [c for c in cols if c["name"] == "week_number"]
    if not week_col or "TEXT" in week_col[0]["type"]:
        return  # 已是 TEXT，无需迁移

    # 迁移 weekly_records
    conn.execute("ALTER TABLE weekly_records RENAME TO weekly_records_old")
    conn.execute(_WEEKLY_RECORDS_SCHEMA)
    conn.execute("""
        INSERT INTO weekly_records (
            id, school_name, year, week_number, week_start_date, week_end_date,
            overall_usage_rate, overall_jibei, grade_jibei, department_jibei,
            homework_count, weekly_active_teachers, weekly_total_teachers,
            weekly_overall_activity, weekly_active_ratio, collected_at, status, error_message
        ) SELECT
            id, school_name, year, '第' || CAST(week_number AS TEXT) || '周',
            week_start_date, week_end_date,
            overall_usage_rate, overall_jibei, grade_jibei, department_jibei,
            homework_count, weekly_active_teachers, weekly_total_teachers,
            '' as weekly_overall_activity, weekly_active_ratio, collected_at, status, error_message
        FROM weekly_records_old
    """)
    conn.execute("DROP TABLE weekly_records_old")

    # 迁移 collect_tasks
    cols2 = conn.execute("PRAGMA table_info(collect_tasks)").fetchall()
    if cols2:
        week_col2 = [c for c in cols2 if c["name"] == "week_number"]
        if week_col2 and "INTEGER" in week_col2[0]["type"]:
            conn.execute("ALTER TABLE collect_tasks RENAME TO collect_tasks_old")
            conn.execute(_COLLECT_TASKS_SCHEMA)
            conn.execute("""
                INSERT INTO collect_tasks (
                    id, year, week_number, schools, status, progress,
                    started_at, finished_at, result_summary
                ) SELECT
                    id, year, '第' || CAST(week_number AS TEXT) || '周',
                    schools, status, progress,
                    started_at, finished_at, result_summary
                FROM collect_tasks_old
            """)
            conn.execute("DROP TABLE collect_tasks_old")






def _import_schools_if_empty(conn):
    """首次启动时从 config.yaml 导入学校数据到数据库"""
    count = conn.execute("SELECT COUNT(*) FROM schools").fetchone()[0]
    if count > 0:
        return  # 数据库已有学校数据，以数据库为准

    # 尝试从 config.yaml 读取学校配置
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    if not config_path.exists():
        logger.info("config.yaml 不存在，跳过学校导入")
        return

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("读取 config.yaml 失败: %s", e)
        return

    schools = cfg.get("schools")
    if not schools or not isinstance(schools, list):
        logger.info("config.yaml 中无学校配置，跳过导入")
        return

    from datetime import datetime
    now = datetime.now().isoformat()
    imported = 0
    for i, s in enumerate(schools):
        if not s.get("name"):
            continue
        try:
            conn.execute("""
                INSERT OR IGNORE INTO schools (
                    name, lida_name, grafana_name, main_site_name,
                    metabase_school_id,
                    xueduan, nianji, jibu, xuebu, sort_order,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["name"],
                s.get("lida_name", s["name"]),
                s.get("grafana_name", s["name"]),
                s.get("main_site_name", s["name"]),
                s.get("metabase_school_id", ""),
                s.get("xueduan", "") or "",
                s.get("nianji", "") or "",
                s.get("jibu", "") or "",
                s.get("xuebu", "") or "",
                i,
                now, now,
            ))
            imported += 1
        except Exception as e:
            logger.warning("导入学校 '%s' 失败: %s", s.get("name"), e)

    logger.info("从 config.yaml 导入了 %d 所学校到数据库", imported)


def _seed_users_from_excel(conn):
    """从 Excel 数据导入种子用户和学校负责人关联"""
    from datetime import datetime
    now = datetime.now().isoformat()

    # 负责人姓名 -> (手机号, 角色)
    # 角色: 超级管理员=李国浩, 管理员=康振荣/易佳雯, 其余=普通用户
    person_map = {
        "康振荣": ("15308204558", "admin"),
        "李国浩": ("13931822731", "super_admin"),
        "吕岳阳": ("18123431006", "user"),
        "易佳雯": ("18121920012", "admin"),
        "岳鹏": ("15387626186", "user"),
        "许杰": ("18728425128", "user"),
        "杨忠昂": ("15725936360", "user"),
        "孙蓉": ("18306385717", "user"),
        "秦凤": ("18582287338", "user"),
        "岑艳请": ("17301860996", "user"),
        "廖文静": ("18391469616", "user"),
        "黄乙梅": ("13076069720", "user"),
        "崔火云": ("19971188251", "user"),
        "樊庆梅": ("13708297752", "user"),
        "何阳": ("18308456997", "user"),
    }

    # 负责人 -> 所属学校列表
    person_schools = {
        "康振荣": ["彝良学校"],
        "李国浩": ["达州天立", "涪陵天立", "德阳天立", "内江天立"],
        "吕岳阳": ["宜春天立"],
        "易佳雯": ["资阳天立"],
        "岳鹏": ["苍溪天立", "剑阁学校", "广元天立"],
        "许杰": ["雅安天立", "西昌天立"],
        "杨忠昂": ["济宁天立", "来安天立", "日照学校"],
        "孙蓉": ["烟台天立", "东营天立", "潍坊学校", "威海天立"],
        "秦凤": ["乌兰察布西区", "保山学校", "楚雄天立"],
        "岑艳请": ["百色天立", "玉林天立"],
        "廖文静": ["宜宾天立", "洪湖天立"],
        "黄乙梅": ["合江天立", "泸州天立中学", "泸州小学", "泸州春雨"],
        "崔火云": ["铜仁天立", "遵义学校", "周口学校", "新乡天立"],
        "樊庆梅": ["郫都天立", "成都龙泉"],
        "何阳": ["兰州天立"],
    }

    # 创建用户
    user_ids = {}
    for name, (phone, role) in person_map.items():
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password, display_name, lida_username, lida_password, grafana_username, grafana_password, main_site_username, main_site_password, assigned_schools, is_admin, role, created_at, updated_at) VALUES (?, 'qiming123', ?, '', '', '', '', '', '', ?, ?, ?, ?, ?)",
            (phone, name, ",".join(person_schools.get(name, [])), 1 if role in ("super_admin", "admin") else 0, role, now, now)
        )
        row = conn.execute("SELECT id FROM users WHERE username=?", (phone,)).fetchone()
        if row:
            user_ids[name] = row["id"]

    # 优先级（独立字段，来自 Excel 第3列）
    school_priority = {
        "彝良学校": "高", "达州天立": "高", "涪陵天立": "高", "宜春天立": "高",
        "资阳天立": "高", "雅安天立": "高", "济宁天立": "高", "烟台天立": "高",
        "乌兰察布西区": "高", "百色天立": "高", "宜宾天立": "高",
        "苍溪天立": "中", "保山学校": "中", "来安天立": "中", "合江天立": "中",
        "洪湖天立": "中", "剑阁学校": "中", "广元天立": "中", "德阳天立": "中",
        "铜仁天立": "中", "西昌天立": "中", "内江天立": "中", "郫都天立": "中",
        "遵义学校": "中", "日照学校": "中", "东营天立": "中", "潍坊学校": "中",
        "兰州天立": "中", "玉林天立": "中", "周口学校": "中", "楚雄天立": "中",
        "成都龙泉": "低", "威海天立": "低", "新乡天立": "低",
        "泸州天立中学": "低", "泸州小学": "低", "泸州春雨": "低",
    }

    # 关联直营校 owner_id 和优先级（仅更新已存在的学校，不新建）
    for name, schools in person_schools.items():
        uid = user_ids.get(name)
        if uid:
            for sname in schools:
                pri = school_priority.get(sname, "中")
                # 通过 display_name 匹配 Metabase 导入的学校
                conn.execute(
                    "UPDATE schools SET owner_id=?, priority=? WHERE display_name=? AND type='直营校'",
                    (uid, pri, sname))

    logger.info("种子用户数据导入完成: %d 位用户", len(user_ids))


def init_db():
    """初始化数据库表结构"""
    with get_connection() as conn:
        _migrate_if_needed(conn)
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS weekly_records (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                school_name         TEXT    NOT NULL,
                year                INTEGER NOT NULL,
                week_number         TEXT    NOT NULL,
                week_start_date     TEXT,
                week_end_date       TEXT,
                overall_usage_rate  TEXT,
                overall_jibei       TEXT,
                grade_jibei         TEXT,
                department_jibei    TEXT,
                homework_count      TEXT,
                weekly_active_teachers TEXT,
                weekly_total_teachers  TEXT,
                weekly_overall_activity TEXT,
        weekly_active_ratio TEXT,
                collected_at        TEXT    NOT NULL,
                status              TEXT    NOT NULL DEFAULT 'success',
                error_message       TEXT,
                UNIQUE(school_name, year, week_number)
            );

            CREATE TABLE IF NOT EXISTS collect_tasks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                year            INTEGER NOT NULL,
                week_number     TEXT    NOT NULL,
                schools         TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'pending',
                progress        TEXT,
                started_at      TEXT,
                finished_at     TEXT,
                result_summary  TEXT
            );

            CREATE TABLE IF NOT EXISTS schools (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL UNIQUE,
                lida_name       TEXT NOT NULL,
                grafana_name    TEXT NOT NULL,
                main_site_name  TEXT NOT NULL,
                xueduan         TEXT DEFAULT '',
                nianji          TEXT DEFAULT '',
                jibu            TEXT DEFAULT '',
                xuebu           TEXT DEFAULT '',
                sort_order      INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS monthly_records (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                school_name         TEXT    NOT NULL,
                year                INTEGER NOT NULL,
                month_number        TEXT    NOT NULL,
                month_start_date    TEXT,
                month_end_date      TEXT,
                overall_usage_rate  TEXT,
                platform_usage      TEXT,
                platform_usage_hs   TEXT,
                platform_usage_ms   TEXT,
                platform_usage_ps   TEXT,
                overall_jibei       TEXT,
                jibei_hs            TEXT,
                jibei_ms            TEXT,
                jibei_ps            TEXT,
                zujuan              TEXT,
                zujuan_hs           TEXT,
                zujuan_ms           TEXT,
                zujuan_ps           TEXT,
                homework_count      TEXT,
                daily_active_ratio  TEXT,
                weekly_active_ratio TEXT,
                monthly_active_ratio TEXT,
                collected_at        TEXT    NOT NULL,
                status              TEXT    NOT NULL DEFAULT 'success',
                error_message       TEXT,
                UNIQUE(school_name, year, month_number)
            );

            CREATE TABLE IF NOT EXISTS users (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                username            TEXT NOT NULL UNIQUE,
                password            TEXT NOT NULL,
                lida_username       TEXT DEFAULT '',
                lida_password       TEXT DEFAULT '',
                grafana_username    TEXT DEFAULT '',
                grafana_password    TEXT DEFAULT '',
                main_site_username  TEXT DEFAULT '',
                main_site_password  TEXT DEFAULT '',
                assigned_schools    TEXT DEFAULT '',
                is_admin            INTEGER NOT NULL DEFAULT 0,
                role                TEXT NOT NULL DEFAULT 'user',
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            );
        """)

        # ── 增量迁移：为已有表添加缺失列 ──
        # record_type column for collect_tasks
        cols_t = conn.execute("PRAGMA table_info(collect_tasks)").fetchall()
        if cols_t:
            col_names = [c["name"] for c in cols_t]
            if "record_type" not in col_names:
                conn.execute("ALTER TABLE collect_tasks ADD COLUMN record_type TEXT NOT NULL DEFAULT 'weekly'")

        # weekly_overall_activity column for weekly_records
        cols_w = conn.execute("PRAGMA table_info(weekly_records)").fetchall()
        if cols_w:
            col_names_w = [c["name"] for c in cols_w]
            if "weekly_overall_activity" not in col_names_w:
                conn.execute("ALTER TABLE weekly_records ADD COLUMN weekly_overall_activity TEXT DEFAULT ''")


        # owner_id column for schools (记录创建者)
        cols_s = conn.execute("PRAGMA table_info(schools)").fetchall()
        if cols_s:
            col_names_s = [c["name"] for c in cols_s]
            if "owner_id" not in col_names_s:
                conn.execute("ALTER TABLE schools ADD COLUMN owner_id INTEGER DEFAULT NULL")
                conn.execute("UPDATE schools SET owner_id = 1 WHERE owner_id IS NULL")

        # metabase_school_id column for schools (Metabase学校数字ID)
        cols_s2 = conn.execute("PRAGMA table_info(schools)").fetchall()
        if cols_s2:
            col_names_s2 = [c["name"] for c in cols_s2]
            if "metabase_school_id" not in col_names_s2:
                conn.execute("ALTER TABLE schools ADD COLUMN metabase_school_id TEXT DEFAULT ''")
                # 将已有 lida_name 迁移到 metabase_school_id（首次迁移）
                conn.execute("UPDATE schools SET metabase_school_id = lida_name WHERE metabase_school_id = '' OR metabase_school_id IS NULL")

        # display_name column for schools (前端展示用简称)
        cols_s3 = conn.execute("PRAGMA table_info(schools)").fetchall()
        if cols_s3:
            col_names_s3 = [c["name"] for c in cols_s3]
            if "display_name" not in col_names_s3:
                conn.execute("ALTER TABLE schools ADD COLUMN display_name TEXT DEFAULT ''")
                conn.execute("UPDATE schools SET display_name = name WHERE display_name = '' OR display_name IS NULL")

        # type column for schools (直营校 / 托管校)
        cols_s4 = conn.execute("PRAGMA table_info(schools)").fetchall()
        if cols_s4:
            col_names_s4 = [c["name"] for c in cols_s4]
            if "type" not in col_names_s4:
                conn.execute("ALTER TABLE schools ADD COLUMN type TEXT DEFAULT ''")

        # priority column for schools (高/中/低)
        cols_p = conn.execute("PRAGMA table_info(schools)").fetchall()
        if cols_p:
            col_names_p = [c["name"] for c in cols_p]
            if "priority" not in col_names_p:
                conn.execute("ALTER TABLE schools ADD COLUMN priority TEXT DEFAULT '中'")

        # data_source column for weekly_records (数据来源: grafana/database)
        cols_w2 = conn.execute("PRAGMA table_info(weekly_records)").fetchall()
        if cols_w2:
            col_names_w2 = [c["name"] for c in cols_w2]
            if "data_source" not in col_names_w2:
                conn.execute("ALTER TABLE weekly_records ADD COLUMN data_source TEXT DEFAULT 'grafana'")

        # data_source column for monthly_records
        cols_m = conn.execute("PRAGMA table_info(monthly_records)").fetchall()
        if cols_m:
            col_names_m = [c["name"] for c in cols_m]
            if "data_source" not in col_names_m:
                conn.execute("ALTER TABLE monthly_records ADD COLUMN data_source TEXT DEFAULT 'grafana'")

        # role column for users (super_admin / admin / user)
        cols_r = conn.execute("PRAGMA table_info(users)").fetchall()
        if cols_r:
            col_names_r = [c["name"] for c in cols_r]
            if "role" not in col_names_r:
                conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
                conn.execute("UPDATE users SET role = 'super_admin' WHERE is_admin = 1 AND role = 'user'")

        # display_name column for users (真实姓名)
        cols_dn = conn.execute("PRAGMA table_info(users)").fetchall()
        if cols_dn:
            col_names_dn = [c["name"] for c in cols_dn]
            if "display_name" not in col_names_dn:
                conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT DEFAULT ''")

        # 创建默认管理员（如果 users 表为空）
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if user_count == 0:
            from datetime import datetime
            now = datetime.now().isoformat()
            conn.execute('''INSERT INTO users (username, password, display_name, is_admin, role, assigned_schools, lida_username, lida_password, grafana_username, grafana_password, main_site_username, main_site_password, created_at, updated_at) VALUES (?, ?, ?, 1, 'super_admin', '', '', '', '', '', '', '', ?, ?)''', ('admin', 'admin123', '管理员', now, now))
            logger.info("已创建默认管理员账户: admin / admin123")
            _seed_users_from_excel(conn)

        _import_schools_if_empty(conn)
