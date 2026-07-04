"""Vercel Serverless 入口 - 将 Flask 应用暴露为 serverless 函数"""
import sys
import os

# 确保项目根目录在 sys.path 中
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web.app import create_app

app = create_app()
