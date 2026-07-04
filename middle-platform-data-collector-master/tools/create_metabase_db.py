"""创建空的 metabase.db，仅包含表结构（无数据）"""
import sqlite3
from pathlib import Path

db_path = Path(__file__).parent.parent / "data" / "metabase.db"
db_path.unlink(missing_ok=True)

conn = sqlite3.connect(str(db_path))

conn.executescript("""
CREATE TABLE IF NOT EXISTS teacher_base (
    teacher_id TEXT,
    school_id INTEGER,
    school_name TEXT,
    stage_names TEXT DEFAULT '',
    grade_names TEXT DEFAULT '',
    subject_names TEXT DEFAULT '',
    state INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS dws_ingress_teacher_day (
    tianli_school_id INTEGER,
    school_name TEXT,
    host TEXT,
    stat_date TEXT,
    pv_count INTEGER DEFAULT 0,
    tianli_user_id TEXT,
    stage_names TEXT DEFAULT '',
    grade_names TEXT DEFAULT '',
    subject_names TEXT DEFAULT '',
    url TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_teacher_school ON teacher_base(school_id);
CREATE INDEX IF NOT EXISTS idx_teacher_name ON teacher_base(school_name);
CREATE INDEX IF NOT EXISTS idx_ingress_date ON dws_ingress_teacher_day(stat_date);
CREATE INDEX IF NOT EXISTS idx_ingress_school ON dws_ingress_teacher_day(tianli_school_id);
CREATE INDEX IF NOT EXISTS idx_ingress_user ON dws_ingress_teacher_day(tianli_user_id);
""")

conn.commit()
conn.close()
print(f"OK: {db_path} 已创建（空结构，表已就绪）")
