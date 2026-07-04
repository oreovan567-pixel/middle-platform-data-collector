"""Vercel Serverless 入口 - 将 Flask 应用暴露为 serverless 函数"""
import sys
import os
import shutil

# 确保项目根目录在 sys.path 中
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Vercel 兼容：将数据库复制到 /tmp（可写区域）──
_TMP_DATA = "/tmp/data"
_SRC_DATA = os.path.join(_ROOT, "middle-platform-data-collector-master", "data")

if not os.path.isdir(_SRC_DATA):
    _SRC_DATA = os.path.join(_ROOT, "data")

if not os.path.exists(_TMP_DATA):
    os.makedirs(_TMP_DATA, exist_ok=True)

if os.path.isdir(_SRC_DATA):
    for fname in os.listdir(_SRC_DATA):
        if fname.endswith(".db"):
            src = os.path.join(_SRC_DATA, fname)
            dst = os.path.join(_TMP_DATA, fname)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)

os.environ["DATA_DIR"] = _TMP_DATA
os.environ["VERCEL"] = "1"

from web.app import create_app

app = create_app()
