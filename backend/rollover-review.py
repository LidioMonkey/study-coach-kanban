#!/usr/bin/env python3
"""
rollover-review.py — 顺延未完成条目

流程：
  1. 扫复习清单未勾选的条目
  2. 若 due < today：
     - rolled += 1
     - due 改为 today
  3. 若 rolled >= 3：标 ⚠️（薄弱化）

用法：
  python3 rollover-review.py
  python3 rollover-review.py --dry-run
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review_common import (  # noqa
    MAX_ROLLOVER,
    read_review_list, write_review_list,
    parse_review_list, render_review_list,
    SUBJECT_KEYS,
)


def parse_short_date(short, ref_year):
    """MM-DD → date，年份用参考年"""
    try:
        mm, dd = short.split("-")
        return date(ref_year, int(mm), int(dd))
    except Exception:
        return None


def process(today, dry_run=False):
    content = read_review_list()
    parsed = parse_review_list(content)

    today_short = today.strftime("%m-%d")
    rolled = 0
    degraded = 0
    degrade_records = []

    for subj in SUBJECT_KEYS:
        for e in parsed["sections"][subj]:
            if e.checked or not e.due:
                continue

            due_d = parse_short_date(e.due, today.year)
            if due_d is None:
                continue
            # 跨年：due 看起来在很远未来 → 认为是去年
            if (due_d - today).days > 180:
                due_d = due_d.replace(year=today.year - 1)

            if due_d >= today:
                continue

            e.rolled += 1
            e.due = today_short
            rolled += 1

            if e.rolled >= MAX_ROLLOVER and not e.weak:
                e.weak = True
                degraded += 1
                degrade_records.append((e.name, e.rel_path))

    print(f"⏭️  顺延处理 ({today})")
    print(f"  顺延: {rolled}")
    print(f"  降级为薄弱点: {degraded}")

    if degrade_records:
        print("\n降级明细：")
        for name, rp in degrade_records:
            print(f"  {name}  → {rp}")

    if not dry_run and rolled > 0:
        new_content = render_review_list(parsed)
        write_review_list(new_content)
        print("\n✅ 已写入 复习清单.md")
    elif dry_run:
        print("\n(dry-run，未写入)")


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    process(date.today(), dry_run=dry)


if __name__ == "__main__":
    main()
