#!/usr/bin/env python3
"""探索 Metabase Dashboard 6 的所有卡片"""
import asyncio, json, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))

from config.config_loader import get_credentials
import aiohttp


async def main():
    creds = get_credentials("metabase")
    url = creds["url"].rstrip("/")
    dashboard_id = creds.get("dashboard_id", 6)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        # 登录
        print("=" * 60)
        print("1. 登录 Metabase")
        async with session.post(
            f"{url}/api/session",
            json={"username": creds["username"], "password": creds["password"]},
        ) as resp:
            data = await resp.json()
            session_id = data["id"]
            print(f"   ✅ session_id: {session_id[:16]}...")

        headers = {"X-Metabase-Session": session_id}

        # 获取 Dashboard 信息
        print(f"\n{'=' * 60}")
        print(f"2. 获取 Dashboard {dashboard_id} 详情")
        async with session.get(f"{url}/api/dashboard/{dashboard_id}", headers=headers) as resp:
            dash = await resp.json()
            # 打印所有顶层 key
            print(f"   顶层 keys: {list(dash.keys())}")

            # 获取 ordered_cards (dashcards)
            dashcards = dash.get("ordered_cards", [])
            print(f"   ordered_cards 数量: {len(dashcards)}")

            # 也尝试 dashcards 字段
            if not dashcards:
                dashcards = dash.get("dashcards", [])
                print(f"   dashcards 数量: {len(dashcards)}")

            if not dashcards:
                # 打印完整响应（截取前 3000 字符）
                print(f"   完整响应: {json.dumps(dash, ensure_ascii=False)[:3000]}")

        print(f"\n{'=' * 60}")
        print("3. 解析卡片列表")
        cards_info = []
        for dc in dashcards:
            card = dc.get("card", {})
            cid = card.get("id") or dc.get("card_id", "?")
            name = card.get("name", "?")
            display = card.get("display", "table")
            viz_type = card.get("visualization_settings", {}).get("virtual_card", {}).get("display", "")
            desc = card.get("description", "")

            # 参数映射信息
            param_mappings = dc.get("parameter_mappings", [])
            params = []
            for pm in param_mappings:
                params.append(f"{pm.get('target', [None])[-1]}=>{pm.get('parameter_id', '?')}")

            cards_info.append({
                "id": cid,
                "name": name,
                "display": display,
                "desc": (desc or "")[:60],
                "params": params,
            })

        for ci in cards_info:
            cid = ci["id"]
            is_skip = cid in {247, 250}
            flag = " ⚠️ SKIP" if is_skip else ""
            print(f"\n   Card #{ci['id']}: {ci['name']} [{ci['display']}]{flag}")
            if ci["desc"]:
                print(f"      描述: {ci['desc']}")
            if ci["params"]:
                print(f"      参数: {', '.join(ci['params'])}")

        # 4. 对每个非 skip 的卡片试查询
        print(f"\n{'=' * 60}")
        print("4. 逐个卡片查询（不带参数）")
        for ci in cards_info:
            cid = ci["id"]
            if cid in {247, 250, "?"}:
                print(f"\n   Card #{cid}: ⏭️ 跳过")
                continue

            async with session.post(
                f"{url}/api/card/{cid}/query",
                json={"parameters": []},
                headers=headers,
            ) as resp:
                if resp.status in (200, 202):
                    result = await resp.json()
                    data_info = result.get("data", {})
                    cols = data_info.get("cols", [])
                    rows = data_info.get("rows", [])
                    col_names = [c.get("display_name", c.get("name", "")) for c in cols]
                    print(f"\n   Card #{cid}: {ci['name']}")
                    print(f"      列({len(cols)}): {col_names}")
                    print(f"      行: {len(rows)}")
                    if rows:
                        print(f"      第1行: {rows[0]}")
                else:
                    text = await resp.text()
                    print(f"\n   Card #{cid}: ❌ HTTP {resp.status} - {text[:100]}")


if __name__ == "__main__":
    asyncio.run(main())
