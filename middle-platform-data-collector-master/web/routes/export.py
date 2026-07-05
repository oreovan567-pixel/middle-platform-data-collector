"""数据导出 API"""
import base64
import io
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from io import BytesIO

from flask import Blueprint, request, jsonify, send_file, session

from models.weekly_record import WeeklyRecord
from models.monthly_record import MonthlyRecord
from services.exporter import export_weekly, export_monthly

logger = logging.getLogger(__name__)
export_bp = Blueprint("export", __name__)

# PPT 会话存储：{session_id: {"dir": Path, "start_date": str, "end_date": str, "school_name": str, "created_at": float}}
_PPT_SESSIONS = {}
_PPT_SESSION_TTL = 600  # 10 分钟过期


def _safe_filename(label: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]', '_', label)


def _get_allowed_schools():
    """获取当前用户可见的学校列表"""
    user_id = session.get("user_id")
    if not user_id:
        return None
    from models.user import User
    user = User.get_by_id(user_id)
    if not user:
        return None
    if user.is_admin or not user.assigned_schools:
        return None
    return user.school_list


@export_bp.route("/weekly")
def export_weekly_data():
    """导出单周数据"""
    allowed = _get_allowed_schools()
    year = request.args.get("year", type=int)
    week_number = request.args.get("week_number", type=str) or ""
    school_name = request.args.get("school_name", "")
    month_prefix = request.args.get("month_prefix", "")

    if not year:
        return jsonify({"error": "请提供 year 参数"}), 400

    # 非管理员如果选择了不在自己范围内的学校，拒绝
    if allowed is not None and school_name and school_name not in allowed:
        return jsonify({"error": "无权访问该学校数据"}), 403

    records = WeeklyRecord.query_flexible(year, week_number, school_name, month_prefix)
    # 过滤记录
    if allowed is not None:
        records = [r for r in records if r.school_name in allowed]
    if not records:
        return jsonify({"error": "没有找到对应数据"}), 404

    filepath = export_weekly(records, year, week_number)
    from services.exporter import _build_export_name
    _period = week_number or (month_prefix if month_prefix else "")
    dl_name = _build_export_name(year, _period) + ".xlsx"
    return send_file(
        filepath,
        as_attachment=True,
        download_name=dl_name,
    )


@export_bp.route("/preview")
def preview_data():
    """预览数据（JSON）"""
    allowed = _get_allowed_schools()
    year = request.args.get("year", type=int)
    week_number = request.args.get("week_number", type=str) or ""
    school_name = request.args.get("school_name", "")
    month_prefix = request.args.get("month_prefix", "")

    if not year:
        return jsonify({"error": "请提供 year 参数"}), 400

    # 非管理员如果选择了不在自己范围内的学校，返回空
    if allowed is not None and school_name and school_name not in allowed:
        return jsonify({"records": []})

    records = WeeklyRecord.query_flexible(year, week_number, school_name, month_prefix)
    if allowed is not None:
        records = [r for r in records if r.school_name in allowed]
    return jsonify({"records": [r.to_dict() for r in records]})


@export_bp.route("/monthly")
def export_monthly_data():
    """导出月度数据"""
    allowed = _get_allowed_schools()
    year = request.args.get("year", type=int)
    month_number = request.args.get("month_number", "")
    school_name = request.args.get("school_name", "")

    if not year:
        return jsonify({"error": "请提供 year 参数"}), 400

    if allowed is not None and school_name and school_name not in allowed:
        return jsonify({"error": "无权访问该学校数据"}), 403

    records = MonthlyRecord.query_flexible(year, month_number, school_name)
    if allowed is not None:
        records = [r for r in records if r.school_name in allowed]
    if not records:
        return jsonify({"error": "没有找到对应数据"}), 404

    filepath = export_monthly(records, year, month_number)
    from services.exporter import _build_export_name
    dl_name = _build_export_name(year, month_number) + ".xlsx"
    return send_file(
        filepath,
        as_attachment=True,
        download_name=dl_name,
    )

@export_bp.route("/distinct_weeks")
def distinct_weeks():
    """查询指定年份已有的不重复周标签"""
    year = request.args.get("year", type=int)
    if not year:
        return jsonify({"error": "请提供 year 参数"}), 400
    weeks = WeeklyRecord.query_distinct_weeks(year)
    return jsonify({"weeks": weeks})


# ── PPT 模板常量 ──
_PPT_BLUE = "1A73E8"       # 天立主色
_PPT_DARK = "0D47A1"       # 深蓝
_PPT_LIGHT_BG = "F0F4FA"   # 浅蓝背景
_PPT_WHITE = "FFFFFF"

# PPT 内容说明文字
_PPT_DESC = {
    "kpi": "本页展示选定时间范围内全校总览的核心KPI指标，包括学校总数、日/周/月活跃人数及作业提交情况。",
    "bar": "各校使用率排名（柱状图），反映各直营校在平台上的整体活跃度差异。",
    "donut_radar": "左图：优先级分层占比（环形图），展示高/中/低优先级学校分布。右图：全板块均衡度分析（雷达图），覆盖六大维度。",
    "trend": "日活人数趋势（折线图），按学段拆分展示各学段每日活跃教师人数变化。",
    "school_kpi": "本页展示指定学校在选定时间范围内的核心KPI指标，包含平台使用率、日/周/月活跃人数及作业数据。",
    "school_trend": "本校周期使用率趋势，按学段（高中/初中/小学）拆分展示每日使用率变化。",
    "school_module": "本校8大业务板块使用率拆解（柱状图），展示各板块活跃教师占比。",
}


def _make_pptx(screenshots, start_date, end_date, school_name):
    """使用 python-pptx 生成 PPT 文件"""
    from pptx import Presentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.enum.shapes import MSO_SHAPE

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    SW = prs.slide_width
    SH = prs.slide_height
    blue = RGBColor(0x1A, 0x73, 0xE8)
    dark = RGBColor(0x0D, 0x47, 0xA1)
    white = RGBColor(0xFF, 0xFF, 0xFF)
    grey = RGBColor(0x66, 0x66, 0x66)
    light_bg = RGBColor(0xF0, 0xF4, 0xFA)

    def _add_bg(slide, color=None):
        """添加纯色背景"""
        bg = slide.background
        fill = bg.fill
        fill.solid()
        fill.fore_color.rgb = color if color else white

    def _add_title_bar(slide, title_text, subtitle_text=None):
        """顶部蓝色标题栏"""
        bar = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), SW, Inches(1.0)
        )
        bar.fill.solid()
        bar.fill.fore_color.rgb = blue
        bar.line.fill.background()
        tf = bar.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = title_text
        p.font.size = Pt(26)
        p.font.color.rgb = white
        p.font.bold = True
        p.alignment = PP_ALIGN.LEFT
        tf.margin_left = Inches(0.6)
        tf.margin_top = Inches(0.15)
        if subtitle_text:
            p2 = tf.add_paragraph()
            p2.text = subtitle_text
            p2.font.size = Pt(13)
            p2.font.color.rgb = RGBColor(0xCC, 0xDD, 0xFF)
            p2.alignment = PP_ALIGN.LEFT

    def _add_footer(slide, page_num):
        """底部页码"""
        txBox = slide.shapes.add_textbox(Inches(0.5), SH - Inches(0.45), Inches(12), Inches(0.35))
        tf = txBox.text_frame
        p = tf.paragraphs[0]
        p.text = f"天立国际 · 数据看板  |  {page_num}"
        p.font.size = Pt(9)
        p.font.color.rgb = grey
        p.alignment = PP_ALIGN.RIGHT

    def _add_image_slide(slide, img_path, desc_text, top_offset=Inches(1.1), page_num=""):
        """在 slide 上放置截图 + 文字说明"""
        from pptx.util import Inches as I
        max_w = I(11.8)
        max_h = I(5.0)
        # 放置图片
        left = I(0.75)
        pic = slide.shapes.add_picture(img_path, left, top_offset, max_w, max_h)
        # 文字说明
        txBox = slide.shapes.add_textbox(I(0.75), top_offset + max_h + I(0.1), max_w, I(0.8))
        tf = txBox.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = desc_text
        p.font.size = Pt(12)
        p.font.color.rgb = grey
        p.alignment = PP_ALIGN.LEFT
        _add_footer(slide, page_num)

    # ── Slide 1: 封面 ──
    slide1 = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    _add_bg(slide1, blue)
    # 装饰条
    deco = slide1.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0), Inches(3.0), SW, Inches(0.06)
    )
    deco.fill.solid(); deco.fill.fore_color.rgb = white; deco.line.fill.background()
    # 标题
    txBox = slide1.shapes.add_textbox(Inches(1.5), Inches(1.2), Inches(10), Inches(1.6))
    tf = txBox.text_frame; tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = "教育平台数据看板"
    p.font.size = Pt(44); p.font.color.rgb = white; p.font.bold = True
    p.alignment = PP_ALIGN.CENTER
    # 副标题
    txBox2 = slide1.shapes.add_textbox(Inches(1.5), Inches(3.3), Inches(10), Inches(1.0))
    tf2 = txBox2.text_frame
    p2 = tf2.paragraphs[0]
    p2.text = f"数据周期: {start_date} ~ {end_date}"
    p2.font.size = Pt(20); p2.font.color.rgb = RGBColor(0xCC, 0xDD, 0xFF)
    p2.alignment = PP_ALIGN.CENTER
    # 天立标识
    txBox3 = slide1.shapes.add_textbox(Inches(1.5), Inches(4.5), Inches(10), Inches(0.6))
    tf3 = txBox3.text_frame
    p3 = tf3.paragraphs[0]
    p3.text = "天立国际  TIANLI INTERNATIONAL  |  01773.HK"
    p3.font.size = Pt(14); p3.font.color.rgb = RGBColor(0x99, 0xBB, 0xEE)
    p3.alignment = PP_ALIGN.CENTER
    # 日期
    txBox4 = slide1.shapes.add_textbox(Inches(1.5), Inches(5.6), Inches(10), Inches(0.5))
    tf4 = txBox4.text_frame
    p4 = tf4.paragraphs[0]
    p4.text = datetime.now().strftime("%Y年%m月%d日")
    p4.font.size = Pt(13); p4.font.color.rgb = RGBColor(0x99, 0xBB, 0xEE)
    p4.alignment = PP_ALIGN.CENTER

    page = 1

    slide_keys = [
        ("kpi", "整体使用情况 — 核心KPI"),
        ("bar", "整体使用情况 — 各校使用率排名"),
        ("donut_radar", "整体使用情况 — 优先级分层与均衡度"),
        ("trend", "整体使用情况 — 日活人数趋势"),
    ]
    for key, title in slide_keys:
        page += 1
        img = screenshots.get(key)
        if not img:
            continue
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        _add_bg(slide, white)
        _add_title_bar(slide, title, f"{start_date} ~ {end_date}")
        _add_image_slide(slide, img, _PPT_DESC.get(key, ""), page_num=str(page))

    # 本校使用情况
    if school_name and screenshots.get("school_kpi"):
        page += 1
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        _add_bg(slide, white)
        _add_title_bar(slide, f"本校使用情况 — {school_name}", f"核心KPI · {start_date} ~ {end_date}")
        _add_image_slide(slide, screenshots["school_kpi"], _PPT_DESC["school_kpi"], page_num=str(page))

    if school_name and screenshots.get("school_trend"):
        page += 1
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        _add_bg(slide, white)
        _add_title_bar(slide, f"本校使用情况 — {school_name}", f"周期趋势 · {start_date} ~ {end_date}")
        _add_image_slide(slide, screenshots["school_trend"], _PPT_DESC["school_trend"], page_num=str(page))

    if school_name and screenshots.get("school_module"):
        page += 1
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        _add_bg(slide, white)
        _add_title_bar(slide, f"本校使用情况 — {school_name}", f"业务板块拆解 · {start_date} ~ {end_date}")
        _add_image_slide(slide, screenshots["school_module"], _PPT_DESC["school_module"], page_num=str(page))

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def _capture_screenshots(base_url, start_date, end_date, school_id, school_name, dest_dir=None):
    """使用 Playwright 截取页面截图"""
    from playwright.sync_api import sync_playwright

    screenshots = {}
    if dest_dir:
        tmp_dir = Path(dest_dir)
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="ppt_export_"))
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True, args=["--disable-gpu", "--no-sandbox", "--disable-extensions"])
            context = browser.new_context(viewport={"width": 1920, "height": 1080})
            page = context.new_page()

            # ── 0. 自动登录 ──
            logger.info("[PPT] 自动登录...")
            # 先访问登录页（无需认证），拿到同源上下文
            page.goto(f"{base_url}/login", wait_until="domcontentloaded", timeout=20000)
            login_result = page.evaluate("""
                async () => {
                    const resp = await fetch('/login', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: 'username=13931822731&password=qiming123&next=%2F'
                    });
                    return await resp.json();
                }
            """)
            if not login_result.get("success"):
                logger.error("[PPT] 登录失败: %s", login_result)
                raise Exception(f"自动登录失败: {login_result.get('error', '未知错误')}")
            # 登录后跳转首页让 session 生效
            page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)  # 等待 JS 初始化
            logger.info("[PPT] 登录成功")

            # ── 1. 首页 KPI ──
            home_url = f"{base_url}/?start_date={start_date}&end_date={end_date}"
            logger.info("[PPT] 采集首页: %s", home_url)
            page.goto(home_url, wait_until="load", timeout=30000)
            time.sleep(1)
            # 等待 KPI 数据加载
            try:
                page.wait_for_function(
                    "document.getElementById('kpiSchoolCount') && document.getElementById('kpiSchoolCount').textContent !== '--'",
                    timeout=15000,
                )
                time.sleep(1.5)  # 等动画完成
            except Exception:
                time.sleep(3)
            kpi_path = str(tmp_dir / "home_kpi.png")
            try:
                el = page.query_selector("#kpiGrid")
                if el:
                    el.screenshot(path=kpi_path)
                    screenshots["kpi"] = kpi_path
            except Exception as e:
                logger.warning("[PPT] KPI 截图失败: %s", e)

            # ── 2. 首页柱状图 ──
            bar_path = str(tmp_dir / "home_bar.png")
            try:
                el = page.query_selector("#chartGrid .chart-card:nth-child(1)")
                if el:
                    el.screenshot(path=bar_path)
                    screenshots["bar"] = bar_path
            except Exception as e:
                logger.warning("[PPT] 柱状图截图失败: %s", e)

            # ── 3. 环形图 + 雷达图（合并）──
            donut_radar_path = str(tmp_dir / "home_donut_radar.png")
            try:
                el = page.query_selector("#chartGrid")
                if el:
                    el.screenshot(path=donut_radar_path)
                    # 覆盖 bar，用整张图表区
                    screenshots["donut_radar"] = donut_radar_path
            except Exception as e:
                logger.warning("[PPT] 环形图截图失败: %s", e)

            # ── 4. 首页趋势图 ──
            trend_path = str(tmp_dir / "home_trend.png")
            try:
                # 趋势图是 chartGrid 最后一个全宽卡片
                trend_el = page.query_selector("#chartGrid .chart-card:last-child")
                if trend_el:
                    trend_el.screenshot(path=trend_path)
                    screenshots["trend"] = trend_path
            except Exception:
                pass

            # ── 5. 单校详情页 ──
            if school_id:
                school_url = f"{base_url}/school/{school_id}?start_date={start_date}&end_date={end_date}"
                logger.info("[PPT] 采集单校: %s", school_url)
                page.goto(school_url, wait_until="load", timeout=30000)
                time.sleep(1)
                try:
                    page.wait_for_function(
                        "document.getElementById('kpiUsage') && document.getElementById('kpiUsage').textContent !== '--'",
                        timeout=15000,
                    )
                    time.sleep(1.5)
                except Exception:
                    time.sleep(3)

                # 学校 KPI 卡片
                skpi_path = str(tmp_dir / "school_kpi.png")
                try:
                    el = page.query_selector(".school-kpi-grid")
                    if el:
                        el.screenshot(path=skpi_path)
                        screenshots["school_kpi"] = skpi_path
                except Exception as e:
                    logger.warning("[PPT] 单校KPI截图失败: %s", e)

                # 趋势图
                strend_path = str(tmp_dir / "school_trend.png")
                try:
                    el = page.query_selector("#trendChart")
                    if el:
                        # 截图包含趋势canvas的父容器
                        parent = page.query_selector(".trend-canvas")
                        if parent:
                            parent.screenshot(path=strend_path)
                            screenshots["school_trend"] = strend_path
                except Exception as e:
                    logger.warning("[PPT] 单校趋势截图失败: %s", e)

                # 业务板块图表
                smod_path = str(tmp_dir / "school_module.png")
                try:
                    el = page.query_selector("#moduleChart")
                    if el:
                        parent = page.query_selector(".module-canvas")
                        if parent:
                            parent.screenshot(path=smod_path)
                            screenshots["school_module"] = smod_path
                except Exception as e:
                    logger.warning("[PPT] 单校模块截图失败: %s", e)

            browser.close()
    except Exception as e:
        logger.error("[PPT] 截图过程出错: %s", e, exc_info=True)

    return screenshots, tmp_dir


def _img_to_base64(img_path, max_width=400):
    """将图片转为 base64 缩略图"""
    try:
        from PIL import Image
        img = Image.open(img_path)
        w, h = img.size
        if w > max_width:
            ratio = max_width / w
            img = img.resize((max_width, int(h * ratio)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        with open(img_path, "rb") as f:
            return base64.b64encode(f.read()).decode()


def _cleanup_session(session_id):
    """清理过期或已使用的会话"""
    info = _PPT_SESSIONS.pop(session_id, None)
    if info:
        try:
            shutil.rmtree(str(info["dir"]), ignore_errors=True)
        except Exception:
            pass


def _cleanup_expired():
    """清理所有过期会话"""
    now = time.time()
    expired = [sid for sid, info in _PPT_SESSIONS.items()
               if now - info["created_at"] > _PPT_SESSION_TTL]
    for sid in expired:
        _cleanup_session(sid)


@export_bp.route("/ppt-screenshots", methods=["POST"])
def ppt_screenshots():
    """Step 1: 截取 PPT 所需截图，返回预览数据"""
    data = request.get_json(silent=True) or {}
    start_date = data.get("start_date", "") or request.args.get("start_date", "")
    end_date = data.get("end_date", "") or request.args.get("end_date", "")
    school_id = data.get("school_id", "") or request.args.get("school_id", "")

    if not start_date or not end_date:
        return jsonify({"error": "时间范围为必填项"}), 400
    if not school_id:
        return jsonify({"error": "请指定学校"}), 400

    # 获取学校名称
    school_name = ""
    try:
        from models.school import School as LocalSchool
        s = LocalSchool.get_by_id(school_id)
        if s:
            school_name = s.name or s.school_name or ""
    except Exception:
        pass

    # 清理过期会话
    _cleanup_expired()

    # 创建会话
    session_id = uuid.uuid4().hex[:12]
    dest_dir = Path(tempfile.gettempdir()) / f"ppt_export_{session_id}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    port = os.environ.get("PORT", "5001")
    base_url = f"http://127.0.0.1:{port}"

    logger.info("[PPT-Preview] 开始截屏: school=%s, %s ~ %s, session=%s",
                school_id, start_date, end_date, session_id)

    screenshots, tmp_dir = _capture_screenshots(
        base_url, start_date, end_date, school_id, school_name,
        dest_dir=str(dest_dir),
    )

    if not screenshots:
        shutil.rmtree(str(dest_dir), ignore_errors=True)
        return jsonify({"error": "截图失败，请检查服务是否正常运行"}), 500

    # 保存会话信息
    _PPT_SESSIONS[session_id] = {
        "dir": dest_dir,
        "start_date": start_date,
        "end_date": end_date,
        "school_name": school_name,
        "created_at": time.time(),
    }

    # 构建预览数据
    slide_order = [
        ("cover", "封面", "天立国际 · 数据看板"),
    ]
    slide_keys_def = [
        ("kpi", "整体使用情况", "核心KPI指标"),
        ("bar", "整体使用情况", "各校使用率排名"),
        ("donut_radar", "整体使用情况", "优先级分层与均衡度"),
        ("trend", "整体使用情况", "日活人数趋势"),
    ]
    for key, section, label in slide_keys_def:
        if screenshots.get(key):
            slide_order.append((key, f"{section} — {label}", _PPT_DESC.get(key, "")))

    if school_name:
        school_slides = [
            ("school_kpi", f"本校 — {school_name}", "核心KPI"),
            ("school_trend", f"本校 — {school_name}", "周期趋势"),
            ("school_module", f"本校 — {school_name}", "业务板块拆解"),
        ]
        for key, section, label in school_slides:
            if screenshots.get(key):
                slide_order.append((key, f"{section} — {label}", _PPT_DESC.get(key, "")))

    slides = []
    for key, title, desc in slide_order:
        slide_data = {"key": key, "title": title, "desc": desc}
        if key == "cover":
            slide_data["isCover"] = True
        else:
            img_path = screenshots.get(key)
            if img_path and os.path.exists(img_path):
                slide_data["preview"] = _img_to_base64(img_path)
        slides.append(slide_data)

    return jsonify({
        "session_id": session_id,
        "school_name": school_name,
        "start_date": start_date,
        "end_date": end_date,
        "slides": slides,
    })


@export_bp.route("/ppt-download/<session_id>")
def ppt_download(session_id):
    """Step 2: 根据 session_id 生成并下载 PPT 文件"""
    _cleanup_expired()

    info = _PPT_SESSIONS.get(session_id)
    if not info:
        return jsonify({"error": "会话已过期，请重新截屏"}), 404

    dest_dir = info["dir"]
    if not dest_dir.exists():
        _cleanup_session(session_id)
        return jsonify({"error": "临时文件已丢失，请重新截屏"}), 404

    # 重新读取截图
    screenshots = {}
    for key in ["kpi", "bar", "donut_radar", "trend", "school_kpi", "school_trend", "school_module"]:
        candidates = [
            dest_dir / f"home_{key}.png",
            dest_dir / f"{key}.png",
        ]
        for c in candidates:
            if c.exists():
                screenshots[key] = str(c)
                break

    start_date = info["start_date"]
    end_date = info["end_date"]
    school_name = info.get("school_name", "")

    logger.info("[PPT-Download] 生成 PPT: session=%s, school=%s", session_id, school_name)

    try:
        pptx_buf = _make_pptx(screenshots, start_date, end_date, school_name)
    except Exception as e:
        logger.error("[PPT] PPT 生成失败: %s", e, exc_info=True)
        return jsonify({"error": f"PPT 生成失败: {str(e)}"}), 500

    # 清理
    _cleanup_session(session_id)

    dl_name = f"数据看板_{start_date}_{end_date}_{school_name or 'export'}.pptx"
    return send_file(
        pptx_buf,
        as_attachment=True,
        download_name=dl_name,
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
