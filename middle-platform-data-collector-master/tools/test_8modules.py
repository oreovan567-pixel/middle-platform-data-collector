#!/usr/bin/env python3
"""测试 8 模块数据采集，对比郫都天立期望值"""
import asyncio, json, sys
from datetime import date
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))

from config.config_loader import get_schools
from scrapers.api_lida import ApiLidaScraper, CARD_NAMES, ALL_MODULE_CARDS


async def main():
    # 查找郫都天立
    schools = get_schools()
    pindu = None
    for s in schools:
        if "郫都" in s.get("name", "") or "天立" in s.get("name", ""):
            pindu = s
            break

    if not pindu:
        print("未找到郫都天立学校，列出所有学校：")
        for s in schools:
            print(f"  {s['name']}: metabase_school_id={s.get('metabase_school_id', 'N/A')}")
        return

    print(f"学校: {pindu['name']}")
    print(f"  metabase_school_id: {pindu.get('metabase_school_id', 'N/A')}")
    print(f"  xueduan: {pindu.get('xueduan', 'N/A')}")
    print(f"  nianji: {pindu.get('nianji', 'N/A')}")
    print()

    # 期望值（来自 Lida 平台日志）
    expected = {
        "244": "31.54%",   # 平台总体数据
        "353": "36.59%",   # 平台使用数据（内部员工）
        "245": "1.17%",    # 个备访问数据
        "246": "2.34%",    # 集备访问数据
        "248": "2.1%",     # 组卷访问数据
        "249": "0.23%",    # 手阅作业访问数据
        "250": "0.23%",    # 学情分析访问数据
        "247": "0.23%",    # 错题本访问数据
    }

    # 使用上一周的数据作为日期范围（需要与实际数据对齐）
    # 先用今天的日期作为参考
    today = date.today()
    # 假设采集的是最近一周数据
    end_date = today
    start_date = today.replace(day=1)  # 当月1号

    print(f"日期范围: {start_date} ~ {end_date}")
    print()

    async with ApiLidaScraper() as scraper:
        print("=" * 60)
        print("查询所有 8 个模块（整体）...")
        modules = await scraper.scrape_all_modules(pindu, (start_date, end_date))

        if modules is None:
            print("❌ 采集失败!")
            return

        print()
        print("=" * 60)
        print(f"{'模块':<25} {'实测值':<12} {'期望值':<12} {'匹配':<8}")
        print("-" * 60)

        all_match = True
        for cid in ALL_MODULE_CARDS:
            key = str(cid)
            actual = modules.get(key, "")
            exp = expected.get(key, "")
            name = CARD_NAMES.get(cid, str(cid))
            match = "✅" if actual == exp else "❌"
            if actual != exp and exp:
                all_match = False
            print(f"  {name:<23} {actual:<12} {exp:<12} {match:<8}")

        print("-" * 60)
        if all_match:
            print("🎉 所有数据 100% 匹配!")
        else:
            print("⚠️ 存在不匹配项，可能需要调整日期范围或其他参数")

        # 也按学段查询
        print()
        print("=" * 60)
        for stage in ["高中", "初中", "小学"]:
            stage_modules = await scraper.scrape_all_modules(pindu, (start_date, end_date), stage=stage)
            if stage_modules:
                print(f"\n{stage}:")
                for cid in [244, 246, 248, 245, 247, 249, 250, 353]:
                    name = CARD_NAMES.get(cid, str(cid))
                    val = stage_modules.get(str(cid), "")
                    if val:
                        print(f"  {name}: {val}")


if __name__ == "__main__":
    asyncio.run(main())
