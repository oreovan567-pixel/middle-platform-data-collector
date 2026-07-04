#!/usr/bin/env python3
"""测试 Metabase 登录凭证"""
import asyncio
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))

from config.config_loader import load_config, get_credentials


async def main():
    creds = get_credentials("metabase")
    print(f"Metabase URL: {creds.get('url')}")
    print(f"Username:    {creds.get('username')}")
    print(f"Password:    {'***' if creds.get('password') else '(empty)'}")
    print(f"Dashboard:   {creds.get('dashboard_id', 6)}")
    print()

    import aiohttp
    url = creds["url"].rstrip("/")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        # 1. 登录
        print("正在登录 Metabase...")
        async with session.post(
            f"{url}/api/session",
            json={"username": creds["username"], "password": creds["password"]},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                session_id = data.get("id", "")
                print(f"✅ 登录成功! session_id = {session_id[:16]}...")
            else:
                text = await resp.text()
                print(f"❌ 登录失败: HTTP {resp.status}")
                print(f"   响应: {text[:500]}")
                return

        # 2. 测试查询 Dashboard 6 的卡片 244（平台总体数据）
        print("\n正在查询卡片 244（平台总体数据）...")
        headers = {"X-Metabase-Session": session_id}
        async with session.post(
            f"{url}/api/card/244/query",
            json={"parameters": []},
            headers=headers,
        ) as resp:
            if resp.status in (200, 202):
                result = await resp.json()
                print(f"✅ 查询成功!")
                data_info = result.get("data", {})
                cols = data_info.get("cols", [])
                rows = data_info.get("rows", [])
                col_names = [c.get("display_name", c.get("name", "")) for c in cols]
                print(f"   列: {col_names}")
                print(f"   数据行数: {len(rows)}")
                if rows:
                    print(f"   第一行: {rows[0]}")
            else:
                text = await resp.text()
                print(f"❌ 查询失败: HTTP {resp.status}")
                print(f"   响应: {text[:500]}")

        # 3. 列出 Dashboard 6 的所有卡片
        print("\n正在获取 Dashboard 6 的卡片列表...")
        async with session.get(
            f"{url}/api/dashboard/6",
            headers=headers,
        ) as resp:
            if resp.status == 200:
                dash = await resp.json()
                cards = dash.get("ordered_cards", [])
                print(f"✅ Dashboard 6 共有 {len(cards)} 个卡片:")
                for card in cards:
                    cid = card.get("card_id") or card.get("card", {}).get("id", "?")
                    cname = card.get("card", {}).get("name", "") or card.get("name", "")
                    ctype = card.get("card", {}).get("display", "") or card.get("visualization_settings", {}).get("virtual_card", {}).get("display", "")
                    size = f"{card.get('col', 0)}x{card.get('row', 0)}"
                    print(f"   Card #{cid}: {cname or '(无名称)'} [{ctype}] @ {size}")
            else:
                text = await resp.text()
                print(f"❌ 获取失败: HTTP {resp.status}")
                print(f"   响应: {text[:500]}")


if __name__ == "__main__":
    asyncio.run(main())
