"""Flask 应用工厂"""
import logging
import os
import sys
from pathlib import Path

from flask import Flask, session, redirect, request, jsonify, render_template_string

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _setup_logging():
    handlers = [logging.StreamHandler()]
    # Vercel serverless 环境无文件写入权限，只用 stdout 日志
    if not os.environ.get("VERCEL"):
        log_dir = _PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "app.log", encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
    )


_LOGIN_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 - 中台分析平台</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "SF Pro Display",
                 "Helvetica Neue", "PingFang SC", "Hiragino Sans GB",
                 "Microsoft YaHei", sans-serif;
    background: linear-gradient(135deg, #0c4a6e 0%, #0e7490 15%, #0891b2 30%,
                #06b6d4 50%, #22d3ee 70%, #67e8f9 90%, #cffafe 100%);
    background-attachment: fixed;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    -webkit-font-smoothing: antialiased;
  }

  .login-wrapper {
    width: 100%;
    max-width: 400px;
    padding: 24px;
  }

  .login-brand {
    text-align: center;
    margin-bottom: 28px;
  }
  .login-brand-icon {
    width: 64px; height: 64px;
    border-radius: 18px;
    background: linear-gradient(135deg, #ffffff 0%, #e0f2fe 100%);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 12px 32px rgba(0,0,0,0.18);
    margin-bottom: 16px;
    font-size: 28px;
    font-weight: 700;
    color: #0e7490;
  }
  .login-brand h1 {
    font-size: 26px;
    font-weight: 700;
    color: #ffffff;
    letter-spacing: 0.02em;
    text-shadow: 0 2px 8px rgba(0,0,0,0.12);
  }
  .login-brand p {
    font-size: 14px;
    color: rgba(255,255,255,0.75);
    margin-top: 6px;
  }

  .login-card {
    background: #ffffff;
    border-radius: 16px;
    padding: 36px 32px;
    box-shadow: 0 16px 48px rgba(0,0,0,0.15), 0 1px 3px rgba(0,0,0,0.06);
  }

  .error {
    background: #fef2f2;
    color: #dc2626;
    border: 1px solid #fecaca;
    border-radius: 10px;
    padding: 10px 14px;
    font-size: 13px;
    margin-bottom: 20px;
    text-align: center;
    display: none;
    align-items: center;
    justify-content: center;
    gap: 6px;
  }
  .error.show { display: flex; }

  .form-group {
    margin-bottom: 16px;
  }
  .form-group label {
    display: block;
    font-size: 13px;
    font-weight: 600;
    color: #374151;
    margin-bottom: 6px;
    letter-spacing: 0.01em;
  }
  .input-wrap {
    position: relative;
    display: flex;
    align-items: center;
  }
  .input-wrap svg {
    position: absolute;
    left: 14px;
    width: 18px;
    height: 18px;
    color: #9ca3af;
    pointer-events: none;
    transition: color 0.2s;
  }
  .form-group input {
    width: 100%;
    padding: 12px 14px 12px 42px;
    border: 1.5px solid #e5e7eb;
    border-radius: 10px;
    font-size: 14px;
    color: #1d1d1f;
    background: #f9fafb;
    outline: none;
    transition: border-color 0.2s, box-shadow 0.2s, background 0.2s;
    -webkit-appearance: none;
    appearance: none;
  }
  .form-group input:focus {
    border-color: #0891b2;
    box-shadow: 0 0 0 3px rgba(8,145,178,0.12);
    background: #fff;
  }

  .login-btn {
    width: 100%;
    padding: 13px;
    margin-top: 8px;
    background: linear-gradient(135deg, #0891b2 0%, #0e7490 100%);
    color: white;
    border: none;
    border-radius: 10px;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    letter-spacing: 0.04em;
    transition: transform 0.15s, box-shadow 0.2s, opacity 0.2s;
    box-shadow: 0 4px 14px rgba(8,145,178,0.3);
  }
  .login-btn:hover {
    transform: translateY(-1px);
    box-shadow: 0 6px 20px rgba(8,145,178,0.35);
  }
  .login-btn:active {
    transform: translateY(0);
    opacity: 0.9;
  }

  .login-tip {
    text-align: center;
    margin-top: 16px;
    font-size: 12px;
    color: #9ca3af;
  }

  .login-footer {
    text-align: center;
    margin-top: 24px;
    font-size: 12px;
    color: rgba(255,255,255,0.55);
  }
</style>
</head>
<body>
  <div class="login-wrapper">
    <div class="login-brand">
      <div class="login-brand-icon">中</div>
      <h1>中台分析平台</h1>
      <p>学校数据采集与可视化系统</p>
    </div>

    <div class="login-card">
      <div class="error" id="loginError">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
        <span id="loginErrorMsg"></span>
      </div>
      <form id="loginForm">
        <input type="hidden" name="next" value="{{ next_url }}">
        <div class="form-group">
          <label>手机号</label>
          <div class="input-wrap">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>
            <input type="tel" id="phoneInput" name="username" placeholder="请输入手机号" autocomplete="tel" autofocus required>
          </div>
        </div>
        <div class="form-group">
          <label>密码</label>
          <div class="input-wrap">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
            <input type="password" id="passwordInput" name="password" placeholder="请输入密码" autocomplete="current-password" required>
          </div>
        </div>
        <button type="submit" class="login-btn" id="loginBtn">登 录</button>
      </form>
      <div class="login-tip">默认密码: qiming123</div>
    </div>

    <div class="login-footer">中台分析平台 v2.0</div>
  </div>
  <script>
  document.getElementById('loginForm').addEventListener('submit', async function(e) {
      e.preventDefault();
      var btn = document.getElementById('loginBtn');
      var errEl = document.getElementById('loginError');
      var errMsg = document.getElementById('loginErrorMsg');
      errEl.classList.remove('show');
      btn.disabled = true;
      btn.textContent = '登录中...';
      try {
          var resp = await fetch('/login', {
              method: 'POST',
              headers: {'Content-Type': 'application/x-www-form-urlencoded'},
              body: 'username=' + encodeURIComponent(document.getElementById('phoneInput').value.trim())
                  + '&password=' + encodeURIComponent(document.getElementById('passwordInput').value)
                  + '&next=' + encodeURIComponent(document.querySelector('input[name=next]').value)
          });
          var data = await resp.json();
          if (data.success) {
              window.location.href = data.redirect || '/';
          } else {
              errMsg.textContent = data.error || '登录失败';
              errEl.classList.add('show');
              btn.disabled = false;
              btn.textContent = '登 录';
          }
      } catch(err) {
          errMsg.textContent = '网络错误，请重试';
          errEl.classList.add('show');
          btn.disabled = false;
          btn.textContent = '登 录';
      }
  });
  </script>
</body>
</html>"""


def _setup_auth(app: Flask):
    """用户认证系统"""

    @app.before_request
    def check_auth():
        if request.endpoint in ("static", "login_page", "login_submit") or request.path == "/api/collect/sync-db":
            return None
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect(f"/login?next={request.path}")

    @app.route("/login", methods=["GET"])
    def login_page():
        error = request.args.get("error", "")
        next_url = request.args.get("next", "/")
        return render_template_string(_LOGIN_TEMPLATE, error=error, next_url=next_url)

    @app.route("/login", methods=["POST"])
    def login_submit():
        from models.user import User
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        next_url = request.form.get("next", "/")

        if not username:
            return jsonify({"success": False, "error": "请输入手机号"})
        if not password:
            return jsonify({"success": False, "error": "请输入密码"})

        user = User.get_by_username(username)
        if not user:
            return jsonify({"success": False, "error": "账号不存在，请检查手机号"})

        if user.password != password:
            return jsonify({"success": False, "error": "密码错误"})

        session["user_id"] = user.id
        session["username"] = user.username
        session["is_admin"] = user.is_admin
        session["role"] = user.role
        return jsonify({"success": True, "redirect": next_url or "/"})

    @app.route("/change-password", methods=["POST"])
    def change_password():
        if not session.get("user_id"):
            return jsonify({"success": False, "error": "请先登录"})
        from models.user import User
        old_password = request.form.get("old_password", "").strip()
        new_password = request.form.get("new_password", "").strip()
        if not old_password or not new_password:
            return jsonify({"success": False, "error": "请填写旧密码和新密码"})
        if len(new_password) < 6:
            return jsonify({"success": False, "error": "新密码至少6位"})
        user = User.get_by_id(session["user_id"])
        if not user:
            return jsonify({"success": False, "error": "用户不存在"})
        if user.password != old_password:
            return jsonify({"success": False, "error": "旧密码不正确"})
        user.password = new_password
        user.save()
        return jsonify({"success": True})

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.context_processor
    def inject_user():
        """注入当前用户信息到所有模板"""
        user_id = session.get("user_id")
        if user_id:
            from models.user import User
            user = User.get_by_id(user_id)
            if user:
                d = user.to_dict()
                d["can_manage_schools"] = user.can_manage_schools
                d["is_super_admin"] = user.is_super_admin
                return {"current_user": d}
        return {"current_user": None}


def create_app() -> Flask:
    _setup_logging()
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "weekmonth-data-collector-prod")
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # 开发阶段确保模板实时更新

    from models.database import init_db
    init_db()

    from web.routes.main import main_bp
    from web.routes.collect import collect_bp
    from web.routes.export import export_bp
    from web.routes.school import school_bp
    from web.routes.user import user_bp
    from web.routes.charts import charts_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(collect_bp, url_prefix="/api/collect")
    app.register_blueprint(export_bp, url_prefix="/api/export")
    app.register_blueprint(school_bp, url_prefix="/api/schools")
    app.register_blueprint(user_bp, url_prefix="/api/users")
    app.register_blueprint(charts_bp)

    _setup_auth(app)
    return app
