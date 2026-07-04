"""API 端点发现脚本

用 Playwright 打开浏览器，依次登录三个平台，拦截网络请求，
把 API 端点和配置保存到 config/api_endpoints.json。

运行方式: cd E:\\project-child\\weekmonth_data && python tools/discover_apis.py
"""
import asyncio
import json
import sys
import os
from pathlib import Path
from datetime import date, datetime, timedelta

# 将项目根目录加入 path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright
from config.config_loader import load_config, get_credentials, get_schools


OUTPUT_PATH = PROJECT_ROOT / "config" / "api_endpoints.json"


class ApiDiscovery:
    """API 端点发现器"""

    def __init__(self):
        self.config = load_config()
        self.results = {
            "discovered_at": datetime.now().isoformat(),
            "grafana": {},
            "lida": {},
            "main_site": {},
        }

    async def run(self):
        """主入口"""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False, slow_mo=100)

            print("\n" + "=" * 60)
            print("  API 端点发现脚本")
            print("=" * 60)

            # ── 平台 1: Grafana ──
            print("\n[1/3] 发现 Grafana API 端点...")
            try:
                await self._discover_grafana(browser)
                print("  ✓ Grafana 发现完成")
            except Exception as e:
                print(f"  ✗ Grafana 发现失败: {e}")

            # ── 平台 2: LIDA / Metabase ──
            print("\n[2/3] 发现 LIDA/Metabase API 端点...")
            try:
                await self._discover_lida(browser)
                print("  ✓ LIDA/Metabase 发现完成")
            except Exception as e:
                print(f"  ✗ LIDA/Metabase 发现失败: {e}")

            # ── 平台 3: 主站 ──
            print("\n[3/3] 发现主站 API 端点...")
            try:
                await self._discover_main_site(browser)
                print("  ✓ 主站发现完成")
            except Exception as e:
                print(f"  ✗ 主站发现失败: {e}")

            await browser.close()

        # 保存结果
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)
        print(f"\n{'=' * 60}")
        print(f"  发现结果已保存到: {OUTPUT_PATH}")
        print(f"{'=' * 60}")
        self._print_summary()

    # ──────────────────────────────────────────────
    #  Grafana 发现
    # ──────────────────────────────────────────────
    async def _discover_grafana(self, browser):
        creds = get_credentials("grafana")
        ctx = await browser.new_context()
        page = await ctx.new_page()

        captured = {
            "dashboard_uids": {},
            "panels": {},
            "api_responses": [],
            "query_targets": {},
            "base_url": "https://grafana.qimingdaren.com",
            "auth": {},
        }

        async def on_response(resp):
            url = resp.url
            if "/api/dashboards/uid/" in url:
                try:
                    body = await resp.json()
                    captured["api_responses"].append({
                        "url": url, "type": "dashboard_json",
                        "body_preview": json.dumps(body, ensure_ascii=False)[:5000],
                    })
                    dash = body.get("dashboard", {})
                    uid = dash.get("uid", "")
                    title = dash.get("title", "")
                    if uid:
                        captured["dashboard_uids"][title] = uid
                        panels = []
                        for p in dash.get("panels", []):
                            panel_info = {
                                "id": p.get("id"),
                                "title": p.get("title", ""),
                                "type": p.get("type", ""),
                                "targets": p.get("targets", []),
                                "fieldConfig": p.get("fieldConfig", {}),
                                "transformations": p.get("transformations", []),
                                "datasource": p.get("datasource", {}),
                            }
                            panels.append(panel_info)
                            if p.get("targets"):
                                captured["query_targets"][str(p["id"])] = {
                                    "title": p.get("title", ""),
                                    "targets": p.get("targets", []),
                                    "datasource": p.get("datasource", {}),
                                }
                        captured["panels"][uid] = panels
                except Exception:
                    pass
            elif "/api/ds/query" in url or "/api/tsdb/query" in url:
                try:
                    body = await resp.json()
                    captured["api_responses"].append({
                        "url": url, "type": "ds_query",
                        "body_preview": json.dumps(body, ensure_ascii=False)[:3000],
                    })
                except Exception:
                    pass
            elif "/api/datasources" in url:
                try:
                    body = await resp.json()
                    captured["api_responses"].append({
                        "url": url, "type": "datasources",
                        "body_preview": json.dumps(body, ensure_ascii=False)[:3000],
                    })
                except Exception:
                    pass

        page.on("response", on_response)

        # 登录
        print("    登录 Grafana...")
        await page.goto(creds["url"], wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        if "/login" in page.url:
            username = creds.get("username", "")
            password = creds.get("password", "")
            if not username or username == "your_username":
                lida_creds = get_credentials("lida")
                username = lida_creds["username"]
                password = lida_creds["password"]
            await page.fill('input[name="user"]', username)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(5000)

        captured["auth"] = {
            "method": "api_token" if creds.get("api_token") else "basic_auth",
            "has_api_token": bool(creds.get("api_token")),
            "username": creds.get("username", ""),
        }

        # 进入每个 dashboard
        dashboard_names = ["中台周报表", "中台使用统计"]
        for name in dashboard_names:
            print(f"    进入 dashboard: {name}...")
            await page.goto("https://grafana.qimingdaren.com/dashboards",
                            wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            clicked = await page.evaluate(f"""() => {{
                const links = document.querySelectorAll('a, [role="link"]');
                for (const el of links) {{
                    if (el.textContent.trim().includes('{name}')) {{
                        el.click();
                        return true;
                    }}
                }}
                return false;
            }}""")
            if clicked:
                await page.wait_for_timeout(5000)
                print(f"    已进入 {name}, URL: {page.url[:100]}")
            else:
                print(f"    未找到 {name}")

        # 获取数据源列表
        try:
            ds_resp = await page.evaluate("""async () => {
                const resp = await fetch('/api/datasources');
                return await resp.json();
            }""")
            captured["datasources"] = [
                {"id": d.get("id"), "name": d.get("name"), "type": d.get("type"),
                 "uid": d.get("uid")}
                for d in (ds_resp if isinstance(ds_resp, list) else [])
            ]
        except Exception as e:
            print(f"    获取数据源失败: {e}")

        await ctx.close()
        self.results["grafana"] = captured

    # ──────────────────────────────────────────────
    #  LIDA / Metabase 发现
    # ──────────────────────────────────────────────
    async def _discover_lida(self, browser):
        creds = get_credentials("lida")
        ctx = await browser.new_context()
        page = await ctx.new_page()

        captured = {
            "lida_base_url": creds["url"],
            "metabase_base_url": "",
            "metabase_session_id": "",
            "iframe_url": "",
            "dashboard_ids": [],
            "card_ids": [],
            "api_responses": [],
            "auth": {"username": creds.get("username", ""), "login_url": creds["url"]},
        }

        async def on_response(resp):
            url = resp.url
            if "/api/" in url and "qimingdaren" in url:
                try:
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        body_text = await resp.text()
                        captured["api_responses"].append({
                            "url": url, "status": resp.status,
                            "type": "lida_api", "body_preview": body_text[:2000],
                        })
                except Exception:
                    pass
            if "metabase" in url and "/api/" in url:
                try:
                    body_text = await resp.text()
                    resp_type = "metabase_api"
                    if "/api/session" in url:
                        resp_type = "metabase_session"
                    elif "/api/dashboard" in url:
                        resp_type = "metabase_dashboard"
                    elif "/api/card" in url:
                        resp_type = "metabase_card"
                    captured["api_responses"].append({
                        "url": url, "status": resp.status,
                        "type": resp_type, "body_preview": body_text[:3000],
                    })
                    if "/api/session" in url and resp.status == 200:
                        try:
                            data = json.loads(body_text)
                            captured["metabase_session_id"] = data.get("id", "")
                        except Exception:
                            pass
                    if "/api/dashboard" in url and resp.status == 200:
                        try:
                            data = json.loads(body_text)
                            if isinstance(data, dict) and "id" in data:
                                captured["dashboard_ids"].append({
                                    "id": data["id"], "name": data.get("name", ""),
                                    "url": url,
                                })
                            elif isinstance(data, list):
                                for d in data:
                                    if isinstance(d, dict) and "id" in d:
                                        captured["dashboard_ids"].append({
                                            "id": d["id"], "name": d.get("name", ""),
                                        })
                        except Exception:
                            pass
                    if "/api/card" in url and resp.status == 200:
                        try:
                            data = json.loads(body_text)
                            if isinstance(data, dict) and "id" in data:
                                captured["card_ids"].append({
                                    "id": data["id"], "name": data.get("name", ""),
                                    "url": url,
                                })
                        except Exception:
                            pass
                except Exception:
                    pass

        page.on("response", on_response)

        # 登录 LIDA
        print("    登录 LIDA...")
        await page.goto(creds["url"], wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        if "/login" in page.url:
            await page.locator('input.el-input__inner:visible').first.fill(creds["username"])
            await page.locator('input[type="password"]:visible').first.fill(creds["password"])
            await page.locator('button.el-button--primary:visible').first.click()
            await page.wait_for_timeout(5000)

        # 切换到学校端
        try:
            hs = await page.query_selector('.header-switch')
            if hs:
                text = await hs.text_content()
                if "学校" not in text:
                    await hs.click()
                    await page.wait_for_timeout(2000)
        except Exception:
            pass

        # 导航到使用概览
        print("    导航到使用概览页面...")
        await page.goto(
            "https://lida.qimingdaren.com/#/data/CollectivePreparationStats/UseageOverview",
            wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        # 找到 Metabase iframe
        print("    查找 Metabase iframe...")
        for frame in page.frames:
            if "metabase" in frame.url:
                parts = frame.url.split("/")
                captured["metabase_base_url"] = parts[0] + "//" + parts[2]
                captured["iframe_url"] = frame.url
                print(f"    Metabase URL: {frame.url[:100]}")
                try:
                    await frame.wait_for_load_state("networkidle", timeout=30000)
                except Exception:
                    pass
                await page.wait_for_timeout(5000)
                break

        if not captured["iframe_url"]:
            print("    未找到 Metabase iframe，记录所有 frames...")
            for frame in page.frames:
                furl = frame.url
                if furl and furl != "about:blank" and "lida" not in furl:
                    captured.setdefault("other_frames", []).append(furl)

        # 尝试直接用 LIDA 凭证登录 Metabase
        if captured["metabase_base_url"]:
            print("    尝试登录 Metabase API...")
            try:
                mb_login = await page.evaluate(f"""async () => {{
                    try {{
                        const resp = await fetch('{captured["metabase_base_url"]}/api/session', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/json'}},
                            body: JSON.stringify({{
                                username: '{creds["username"]}',
                                password: '{creds["password"]}'
                            }})
                        }});
                        const data = await resp.json();
                        return {{status: resp.status, data: data}};
                    }} catch(e) {{
                        return {{error: e.message}};
                    }}
                }}""")
                captured["metabase_login_attempt"] = mb_login
                if mb_login and mb_login.get("status") == 200:
                    sid = mb_login.get("data", {}).get("id", "")
                    if sid:
                        captured["metabase_session_id"] = sid
                        print(f"    Metabase 登录成功, session: {sid[:20]}...")
            except Exception as e:
                print(f"    Metabase 登录尝试失败: {e}")

        # 如果有 session，获取 dashboard 列表
        if captured["metabase_session_id"] and captured["metabase_base_url"]:
            print("    获取 Metabase dashboard 列表...")
            try:
                dashboards = await page.evaluate(f"""async () => {{
                    const resp = await fetch('{captured["metabase_base_url"]}/api/dashboard/', {{
                        headers: {{'X-Metabase-Session': '{captured["metabase_session_id"]}'}}
                    }});
                    return await resp.json();
                }}""")
                if isinstance(dashboards, list):
                    captured["metabase_dashboards"] = [
                        {"id": d.get("id"), "name": d.get("name", ""),
                         "collection": d.get("collection", {})}
                        for d in dashboards[:20]
                    ]
                    print(f"    找到 {len(dashboards)} 个 dashboards")
            except Exception as e:
                print(f"    获取 dashboard 列表失败: {e}")

        await ctx.close()
        self.results["lida"] = captured

    # ──────────────────────────────────────────────
    #  主站发现
    # ──────────────────────────────────────────────
    async def _discover_main_site(self, browser):
        creds = get_credentials("main_site")
        ctx = await browser.new_context()
        page = await ctx.new_page()

        captured = {
            "base_url": creds["url"],
            "ops_url": "https://operation.qimingdaren.com",
            "login_api": {},
            "exam_apis": [],
            "school_apis": [],
            "all_api_responses": [],
            "auth": {"username": creds.get("username", "")},
        }

        async def on_response(resp):
            url = resp.url
            if any(skip in url for skip in [
                '.js', '.css', '.png', '.jpg', '.svg', '.ico',
                '.woff', '.ttf', 'google', 'baidu', 'analytics',
            ]):
                return
            if "/api/" in url or "/rest/" in url or ".json" in url:
                try:
                    ct = resp.headers.get("content-type", "")
                    body_preview = ""
                    if "json" in ct:
                        body_text = await resp.text()
                        body_preview = body_text[:2000]
                    captured["all_api_responses"].append({
                        "url": url, "method": resp.request.method,
                        "status": resp.status, "content_type": ct,
                        "body_preview": body_preview,
                    })
                except Exception:
                    pass
            if any(kw in url.lower() for kw in ["exam", "school", "account", "login", "auth"]):
                try:
                    body_text = await resp.text()
                    entry = {
                        "url": url, "method": resp.request.method,
                        "status": resp.status, "body_preview": body_text[:2000],
                    }
                    if "exam" in url.lower():
                        captured["exam_apis"].append(entry)
                    elif "school" in url.lower() or "account" in url.lower():
                        captured["school_apis"].append(entry)
                    elif "login" in url.lower() or "auth" in url.lower():
                        captured["login_api"] = entry
                except Exception:
                    pass

        page.on("response", on_response)

        # 登录
        print("    登录主站...")
        await page.goto(creds["url"], wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        if "/login" in page.url:
            await page.locator('input.el-input__inner:visible').first.fill(creds["username"])
            await page.locator('input[type="password"]:visible').first.fill(creds["password"])
            await page.locator('button:has-text("登录")').first.click()
            for _ in range(30):
                await page.wait_for_timeout(500)
                if "/login" not in page.url:
                    break
            await page.wait_for_timeout(3000)
            print(f"    登录成功: {page.url[:80]}")

        # 导航到学校运维
        print("    导航到学校运维...")
        await page.goto("https://operation.qimingdaren.com/#/account/school",
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        # 搜索学校触发 API
        schools = get_schools()
        if schools:
            school_name = schools[0].get("main_site_name", schools[0]["name"])
            print(f"    搜索学校: {school_name}...")
            try:
                search_input = page.locator('input.el-input__inner:visible').first
                await search_input.click()
                await search_input.fill(school_name)
                await page.wait_for_timeout(500)
                try:
                    await page.locator('button:has-text("搜索")').first.click(timeout=3000)
                except Exception:
                    await page.keyboard.press("Enter")
                await page.wait_for_timeout(3000)
            except Exception as e:
                print(f"    搜索学校失败: {e}")

        # 检查 Vue 应用
        print("    检查 Vue 应用 API 调用...")
        try:
            vue_info = await page.evaluate("""() => {
                const info = {hasVmApp: false, methods: [], data: {}};
                if (typeof vmApp !== 'undefined') {
                    info.hasVmApp = true;
                    for (const key of Object.keys(vmApp.$options.methods || {})) {
                        info.methods.push(key);
                    }
                    for (const key of Object.keys(vmApp.$data || {})) {
                        const val = vmApp.$data[key];
                        if (typeof val === 'string' && (val.includes('http') || val.includes('/api/'))) {
                            info.data[key] = val;
                        }
                        if (typeof val === 'object' && val !== null) {
                            for (const k2 of Object.keys(val)) {
                                if (typeof val[k2] === 'string' &&
                                    (val[k2].includes('http') || val[k2].includes('/api/'))) {
                                    info.data[key + '.' + k2] = val[k2];
                                }
                            }
                        }
                    }
                }
                return info;
            }""")
            captured["vue_app"] = vue_info
        except Exception as e:
            print(f"    Vue 检查失败: {e}")

        # 注入 XHR 拦截器
        await page.evaluate("""() => {
            window.__captured_requests = [];
            const origOpen = XMLHttpRequest.prototype.open;
            const origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url) {
                this.__url = url; this.__method = method;
                return origOpen.apply(this, arguments);
            };
            XMLHttpRequest.prototype.send = function(body) {
                const xhr = this;
                xhr.addEventListener('load', function() {
                    if (xhr.__url && !xhr.__url.includes('.js') && !xhr.__url.includes('.css')) {
                        window.__captured_requests.push({
                            url: xhr.__url, method: xhr.__method,
                            status: xhr.status,
                            body: body ? String(body).substring(0, 500) : null,
                            response: xhr.responseText ? xhr.responseText.substring(0, 1000) : '',
                        });
                    }
                });
                return origSend.apply(this, arguments);
            };
            const origFetch = window.fetch;
            window.fetch = function(url, opts) {
                return origFetch.apply(this, arguments).then(resp => {
                    const u = typeof url === 'string' ? url : url.url;
                    if (!u.includes('.js') && !u.includes('.css')) {
                        resp.clone().text().then(text => {
                            window.__captured_requests.push({
                                url: u, method: (opts && opts.method) || 'GET',
                                status: resp.status,
                                body: (opts && opts.body) ? String(opts.body).substring(0, 500) : null,
                                response: text.substring(0, 1000),
                            });
                        }).catch(() => {});
                    }
                    return resp;
                });
            };
        }""")

        # 一键登录
        print("    一键登录...")
        try:
            await page.locator('text=一键登录').first.click(timeout=5000)
            await page.wait_for_timeout(1000)
            msgbox = page.locator('.el-message-box__wrapper')
            await msgbox.wait_for(state="visible", timeout=5000)
            confirm_btn = msgbox.locator('.el-button--primary')
            try:
                async with page.context.expect_page(timeout=15000) as new_page_info:
                    await confirm_btn.click()
                new_page = await new_page_info.value
                await new_page.wait_for_load_state("domcontentloaded")
                await new_page.wait_for_timeout(3000)
                page = new_page
                print(f"    新标签页: {page.url[:80]}")
            except Exception:
                await page.wait_for_timeout(5000)
                print(f"    同页面: {page.url[:80]}")
        except Exception as e:
            print(f"    一键登录失败: {e}")

        # 导航到考试管理
        print("    导航到考试管理...")
        try:
            async with page.context.expect_page(timeout=15000) as new_page_info:
                await page.locator('text=考试阅卷系统').first.click(timeout=10000)
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("domcontentloaded")
            page = new_page
            await page.wait_for_timeout(3000)
        except Exception:
            await page.wait_for_timeout(5000)

        try:
            await page.locator('text=考试管理').first.click(timeout=10000)
            await page.wait_for_timeout(5000)
        except Exception as e:
            print(f"    导航考试管理失败: {e}")

        # 触发考试查询
        print("    触发考试查询 API...")
        try:
            today = date.today()
            week_ago = today - timedelta(days=7)
            await page.evaluate(f"""() => {{
                if (typeof vmApp !== 'undefined' && vmApp.examSearch) {{
                    if (vmApp.$set) {{
                        vmApp.$set(vmApp.examSearch, 'startDate', '{week_ago.isoformat()}');
                        vmApp.$set(vmApp.examSearch, 'endDate', '{today.isoformat()}');
                    }}
                }}
            }}""")
            try:
                hw_btn = page.locator('.search-btns.s4 span.li:has-text("作业")').first
                await hw_btn.click(timeout=5000)
            except Exception:
                pass
            try:
                await page.locator('button:visible:has-text("搜索")').first.click(timeout=5000)
            except Exception:
                await page.evaluate("""
                    if (typeof vmApp !== 'undefined' && vmApp.searchExam) vmApp.searchExam();
                """)
            await page.wait_for_timeout(5000)
        except Exception as e:
            print(f"    触发查询失败: {e}")

        # 收集 XHR 拦截结果
        try:
            xhr_captured = await page.evaluate("window.__captured_requests || []")
            captured["xhr_intercepted"] = xhr_captured
            print(f"    XHR 拦截器捕获 {len(xhr_captured)} 个请求")
        except Exception:
            pass

        await ctx.close()
        self.results["main_site"] = captured

    def _print_summary(self):
        """打印发现结果摘要"""
        print("\n" + "=" * 60)
        print("  发现结果摘要")
        print("=" * 60)

        g = self.results.get("grafana", {})
        print(f"\n  Grafana:")
        print(f"    Dashboard UIDs: {len(g.get('dashboard_uids', {}))} 个")
        for name, uid in g.get("dashboard_uids", {}).items():
            print(f"      - {name}: {uid}")
        print(f"    面板配置: {sum(len(v) for v in g.get('panels', {}).values())} 个面板")
        print(f"    查询目标: {len(g.get('query_targets', {}))} 个")
        print(f"    数据源: {len(g.get('datasources', []))} 个")

        l = self.results.get("lida", {})
        print(f"\n  LIDA/Metabase:")
        print(f"    Metabase URL: {l.get('metabase_base_url', '未找到')}")
        print(f"    iframe URL: {l.get('iframe_url', '未找到')[:80] if l.get('iframe_url') else '未找到'}")
        print(f"    Session ID: {'有' if l.get('metabase_session_id') else '无'}")
        print(f"    Dashboard IDs: {len(l.get('dashboard_ids', []))} 个")
        print(f"    Card IDs: {len(l.get('card_ids', []))} 个")
        print(f"    API 响应: {len(l.get('api_responses', []))} 个")
        if l.get("metabase_dashboards"):
            print(f"    Metabase Dashboards:")
            for d in l["metabase_dashboards"][:5]:
                print(f"      - [{d['id']}] {d['name']}")

        m = self.results.get("main_site", {})
        print(f"\n  主站:")
        print(f"    Login API: {'有' if m.get('login_api') else '无'}")
        print(f"    Exam APIs: {len(m.get('exam_apis', []))} 个")
        print(f"    School APIs: {len(m.get('school_apis', []))} 个")
        print(f"    XHR 拦截: {len(m.get('xhr_intercepted', []))} 个")
        print(f"    所有 API: {len(m.get('all_api_responses', []))} 个")
        vue = m.get("vue_app", {})
        if vue.get("hasVmApp"):
            print(f"    Vue 方法: {vue.get('methods', [])[:10]}")


if __name__ == "__main__":
    asyncio.run(ApiDiscovery().run())
