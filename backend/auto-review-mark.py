#!/usr/bin/env python3
"""
auto-review-mark.py — 扫描复习清单勾选态，打完成标记

流程：
  1. 读复习清单
  2. 对每个 [x] 条目：
     - 追加 ✔{current}({today})
     - 若 current == D30 → 移到 ✅ 已掌握区
     - 否则 → 计算下一节点，重置 [ ]，更新 [current] + 到期日
  3. 写回

用法：
  python3 auto-review-mark.py                # 处理今天
  python3 auto-review-mark.py --dry-run
"""

import sys, os
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review_common import (  # noqa
    NODE_NAMES, INTERVALS, WEAK_INTERVALS,
    read_review_list, write_review_list,
    parse_review_list, render_review_list, render_review_entry,
    SUBJECT_KEYS,
)


def next_node(current, weak):
    """返回 (next_node_name, days_from_previous_node) 或 None（已是最后一个）"""
    if current not in NODE_NAMES:
        return None
    idx = NODE_NAMES.index(current)
    if idx + 1 >= len(NODE_NAMES):
        return None
    intervals = WEAK_INTERVALS if weak else INTERVALS
    days_from_prev = intervals[idx + 1] - intervals[idx]
    return (NODE_NAMES[idx + 1], days_from_prev)


def process(today, dry_run=False):
    content = read_review_list()
    parsed = parse_review_list(content)

    today_short = today.strftime("%m-%d")
    today_full = today.strftime("%Y-%m-%d")
    marked = 0
    archived = 0
    advanced = 0
    archive_lines_new = []

    for subj in SUBJECT_KEYS:
        keep = []
        for e in parsed["sections"][subj]:
            if not e.checked:
                keep.append(e)
                continue

            cur = e.current
            if not cur:
                e.checked = False
                keep.append(e)
                continue

            # 追加完成标记
            e.done_marks.append((cur, today_short))
            marked += 1

            nxt = next_node(cur, e.weak)
            if nxt is None:
                # D30 完成，归档
                e.checked = True
                e.current = None
                e.due = None
                e.rolled = 0
                e.archived_full_date = today_full
                arch_line = render_review_entry(e)
                archive_lines_new.append(arch_line)
                archived += 1
                continue

            next_name, days_from_prev = nxt
            new_due = today + timedelta(days=days_from_prev)
            e.checked = False
            e.current = next_name
            e.due = new_due.strftime("%m-%d")
            e.rolled = 0  # 顺延次数重置
            advanced += 1
            keep.append(e)

        parsed["sections"][subj] = keep

    # 归档新行插到 "## ✅ 已掌握" 之后
    if archive_lines_new:
        arch = parsed["archived_lines"]
        idx = 0
        for i, line in enumerate(arch):
            if line.strip() == "## ✅ 已掌握":
                idx = i + 1
                break
        new_arch = arch[:idx] + [""] + archive_lines_new + arch[idx:]
        parsed["archived_lines"] = new_arch

    print(f"📝 复习打标 ({today})")
    print(f"  完成打标: {marked}")
    print(f"    ├─ 进入下一节点: {advanced}")
    print(f"    └─ 归档掌握区: {archived}")

    if not dry_run and marked > 0:
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
