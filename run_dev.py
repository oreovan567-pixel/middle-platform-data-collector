"""开发模式启动（端口 5001，避免 AirPlay 冲突）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web.app import create_app

app = create_app()
print("\n" + "=" * 50)
print("  教育平台数据自动采集系统")
print("  开发模式 (Flask dev server)")
print("=" * 50)
print("  本机访问:  http://localhost:5001")
print("=" * 50 + "\n")
app.run(host="0.0.0.0", port=5001, debug=True, threaded=True)
