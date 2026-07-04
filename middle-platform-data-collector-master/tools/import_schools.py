#!/usr/bin/env python3
"""从 Metabase Card #251 导入全部学校到数据库，并标记直营校/托管校"""
import asyncio, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))

import aiohttp
from config.config_loader import get_credentials

# 直营校：(数据库全名 → 用户简称)
ZHIYING_MAP = {
    "东营天立学校": "东营天立",
    "乌兰察布天立学校": "乌兰察布西区",
    "保山市天立学校": "保山学校",
    "兰州天立学校": "兰州天立",
    "内江天立学校": "内江天立",
    "剑阁天立学校": "剑阁学校",
    "泸州合江天立学校": "合江天立",
    "周口天立学校": "周口学校",
    "威海天立学校": "威海天立",
    "宜宾天立学校": "宜宾天立",
    "宜春天立学校": "宜春天立",
    "广元天立学校": "广元天立",
    "彝良县天立学校": "彝良学校",
    "德阳天立学校": "德阳天立",
    "成都龙泉天立学校": "成都龙泉",
    "新乡天立": "新乡天立",
    "日照天立学校": "日照学校",
    "来安天立": "来安天立",
    "楚雄天立": "楚雄天立",
    "泸州天立": "泸州天立中学",
    "泸州龙马潭区天立小学": "泸州小学",
    "泸州天立春雨学校": "泸州春雨",
    "洪湖天立": "洪湖天立",
    "济宁天立学校": "济宁天立",
    "涪陵天立学校": "涪陵天立",
    "潍坊天立学校": "潍坊学校",
    "烟台天立": "烟台天立",
    "玉林天立": "玉林天立",
    "百色天立学校": "百色天立",
    "苍溪天立学校": "苍溪天立",
    "西昌天立学校": "西昌天立",
    "资阳天立学校": "资阳天立",
    "达州天立学校": "达州天立",
    "遵义天立学校": "遵义学校",
    "成都郫都天立学校": "郫都天立",
    "铜仁天立学校": "铜仁天立",
    "雅安天立学校": "雅安天立",
}

# 测试学校（3所）
TEST_SCHOOL_MAP = {
    "启鸣达人学校": "启鸣达人学校",
    "启鸣达人测试学校": "启鸣达人测试学校",
    "立达学校": "立达学校",
}


async def main():
    creds = get_credentials("metabase")
    url = creds["url"].rstrip("/")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        # 登录
        async with session.post(
            f"{url}/api/session",
            json={"username": creds["username"], "password": creds["password"]},
        ) as resp:
            data = await resp.json()
            sid = data["id"]
        headers = {"X-Metabase-Session": sid}

        # 查询 Card #251
        print("正在获取学校列表...")
        async with session.post(
            f"{url}/api/card/251/query",
            json={"parameters": []},
            headers=headers,
        ) as resp:
            result = await resp.json()
            rows = result.get("data", {}).get("rows", [])
            print(f"获取到 {len(rows)} 所学校")

        # 导入基础数据
        from models.school import School
        added = 0
        updated = 0
        for row in rows:
            sid = str(row[0])
            sname = str(row[1])
            if not sid or not sname:
                continue

            existing = School.get_by_name(sname)
            if existing:
                if not existing.metabase_school_id or existing.metabase_school_id != sid:
                    existing.metabase_school_id = sid
                    existing.save()
                    updated += 1
            else:
                s = School(
                    name=sname,
                    lida_name=sname,
                    grafana_name=sname,
                    main_site_name=sname,
                    metabase_school_id=sid,
                )
                s.save()
                added += 1

        print(f"基础导入 — 新增: {added}, 更新: {updated}")

        # ── 标记直营校 / 测试学校 / 托管校 ──
        zhiying_count = 0
        test_count = 0
        tuoguan_count = 0
        all_schools = School.get_all()
        for s in all_schools:
            if s.name in ZHIYING_MAP:
                s.display_name = ZHIYING_MAP[s.name]
                s.type = "直营校"
                s.save()
                zhiying_count += 1
            elif s.name in TEST_SCHOOL_MAP:
                s.display_name = TEST_SCHOOL_MAP[s.name]
                s.type = "测试学校"
                s.save()
                test_count += 1
            elif s.metabase_school_id:  # 有 school_id 但不在以上列表 → 托管校
                s.display_name = s.name
                s.type = "托管校"
                s.save()
                tuoguan_count += 1

        print(f"分类完成 — 直营校: {zhiying_count}, 测试学校: {test_count}, 托管校: {tuoguan_count}")

        # 汇总
        all_schools = School.get_all()
        print(f"\n数据库共有: {len(all_schools)} 所学校")
        for t in ("直营校", "测试学校", "托管校"):
            typed = [s for s in all_schools if s.type == t]
            print(f"  {t}: {len(typed)} 所")
            for s in typed[:3]:
                print(f"    {s.display_name} ← {s.name}")
            if len(typed) > 3:
                print(f"    ... 等 {len(typed)} 所")


if __name__ == "__main__":
    asyncio.run(main())
