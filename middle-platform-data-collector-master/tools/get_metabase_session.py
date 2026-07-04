"""用 Playwright 登录 Lida，通过点击菜单访问使用概览"""
import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        all_mb_reqs = []
        
        async def on_request(req):
            url = req.url
            if 'metabase' in url:
                all_mb_reqs.append({'method': req.method, 'url': url, 'headers': dict(req.headers)})
                print(f'[MB] {req.method} {url[:200]}')
            elif '/api/' in url:
                print(f'[API] {req.method} {url[:200]}')
        
        async def on_response(resp):
            url = resp.url
            if 'metabase' in url:
                try:
                    body = await resp.json()
                    if 'id' in body:
                        print(f'[MB-RESP] Session: {json.dumps(body, ensure_ascii=False)[:200]}')
                    else:
                        print(f'[MB-RESP] {resp.status} {url[:100]}')
                except:
                    pass
        
        page.on('request', on_request)
        page.on('response', on_response)
        
        # 1. 登录
        print('1. 登录...')
        await page.goto('https://lida.qimingdaren.com/#/workbench', timeout=30000)
        await page.wait_for_timeout(2000)
        
        content = await page.content()
        if 'login' in content.lower():
            await page.fill('input[type="text"]', '13931822731', timeout=5000)
            await page.fill('input[type="password"]', 'tjQoH66O', timeout=5000)
            await page.click('button:has-text("登录")', timeout=5000)
            await page.wait_for_timeout(5000)
        
        # 2. 查找所有导航元素并点击
        print('2. 查找菜单...')
        
        # 获取所有可点击元素
        all_clickables = await page.evaluate("""() => {
            const elements = document.querySelectorAll('a, button, [role="button"], .el-menu-item, .tab, [class*="menu"], [class*="nav"], [class*="tab"], span[class*="title"]');
            const results = [];
            elements.forEach(el => {
                const text = el.innerText?.trim();
                if (text && text.length > 0 && text.length < 30) {
                    results.push({tag: el.tagName, text: text, class: el.className?.substring(0, 80)});
                }
            });
            return results;
        }""")
        
        for item in all_clickables:
            print(f'  {item["tag"]} [{item["class"]}]: "{item["text"]}"')
        
        # 3. 点击"教学教研"（它可能在顶部是一个下拉菜单）
        print('\n3. 尝试点击"教学教研"...')
        try:
            els = await page.query_selector_all('text="教学教研"')
            for el in els:
                parent = await el.evaluate("el => el.closest('a, button, div, li').outerHTML.substring(0, 200)")
                print(f'  父元素: {parent}')
            if els:
                await els[0].click()
                await page.wait_for_timeout(2000)
                print(f'  点击后 URL: {page.url}')
        except Exception as e:
            print(f'  失败: {e}')
        
        # 4. 尝试直接打开 Lida 的完整 URL (不是 hash)
        print('\n4. 尝试完整 URL...')
        await page.goto('https://lida.qimingdaren.com/', timeout=30000)
        await page.wait_for_timeout(2000)
        await page.evaluate("window.location.hash = '#/data/CollectivePreparationStats/UseageOverview'")
        await page.wait_for_timeout(5000)
        print(f'  URL: {page.url}')
        
        # 截图
        await page.screenshot(path='/tmp/lida_usage.png', full_page=True)
        
        # 5. 统计
        print(f'\nMetabase 请求: {len(all_mb_reqs)}')
        for r in all_mb_reqs:
            print(f"  {r['method']} {r['url'][:200]}")
            for h in ['x-metabase-session', 'X-Metabase-Session']:
                if h in r.get('headers', {}):
                    print(f"    {h}: {r['headers'][h][:80]}...")
        
        await browser.close()

asyncio.run(main())
