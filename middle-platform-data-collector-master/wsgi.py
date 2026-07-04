"""生产环境 WSGI 入口（供 waitress / gunicorn 加载）"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web.app import create_app

app = create_app()
