"""启动入口 - 教育平台数据自动采集系统"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web.app import create_app


def main():
    app = create_app()

    debug_mode = "--debug" in sys.argv

    if debug_mode:
        # 开发模式: Flask 内置服务器（支持热重载）
        print("\n  *** 开发模式 (Flask dev server) ***")
        app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
    else:
        # 生产模式: waitress WSGI 服务器（稳定、多线程、Windows 原生支持）
        try:
            from waitress import serve
            print("\n" + "=" * 50)
            print("  教育平台数据自动采集系统")
            print("  生产模式 (waitress WSGI server)")
            print("=" * 50)
            print(f"  本机访问:  http://localhost:5000")
            print(f"  局域网:    http://0.0.0.0:5000")
            print(f"  同事访问:  http://<你的IP>:5000")
            print("=" * 50)
            print("  按 Ctrl+C 停止服务\n")
            serve(app, host="0.0.0.0", port=5000, threads=8,
                  channel_timeout=120)
        except ImportError:
            print("\n  waitress 未安装，回退到 Flask dev server")
            print("  建议运行: pip install waitress")
            app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
