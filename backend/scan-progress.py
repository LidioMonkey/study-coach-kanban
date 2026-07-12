#!/usr/bin/env python3
"""
scan-progress.py — 扫描所有进度文件，输出结构化 JSON

供 Hermes study-coach skill 调用，快速获取当前学习状态。

用法：
  python3 scan-progress.py                    # 输出所有书籍摘要
  python3 scan-progress.py --detail           # 输出每个单元的详细信息
  python3 scan-progress.py --book "660高数"   # 按书名过滤
  python3 scan-progress.py --completed        # 只输出已完成项
  python3 scan-progress.py --remaining        # 只输出未完成项

输出格式：JSON（stdout）
"""

import os
import re
import json
import sys
from datetime import datetime

VAULT_PATH = "/root/obsidian-vault"
PROGRESS_DIR = os.path.join(VAULT_PATH, "考研备考", "进度")


def parse_progress_file(filepath):
    """解析单个进度文件"""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 提取书名
    book_name = ""
    for line in content.split("\n"):
        if line.startswith("# "):
            book_name = line[2:].strip()
            break

    # 提取 tags
    tags_match = re.search(r"tags:\s*\[([^\]]*)\]", content)
    tags = []
    if tags_match:
        tags = [t.strip().strip('"').strip("'") for t in tags_match.group(1).split(",")]

    # 提取备注
    note = ""
    for line in content.split("\n"):
        if line.startswith("- 备注:") or line.startswith("- 备注："):
            note = line.split(":", 1)[1].strip() if ":" in line else ""
            break

    # 解析单元行
    unit_pattern = re.compile(r"- \[([ xX])\]\s+(\S+)\s+(.*)")
    units = []
    total = 0
    completed_count = 0
    weak_count = 0

    for line in content.split("\n"):
        m = unit_pattern.match(line)
        if not m:
            continue

        is_done = m.group(1).lower() == "x"
        unit_id = m.group(2).strip()
        rest = m.group(3).strip()

        # 清理标题（去掉 ✅日期 和 ⚠️标记）
        title = re.sub(r"✅\d{4}-\d{2}-\d{2}", "", rest)
        title = re.sub(r"⚠️薄弱.*", "", title).strip()
        if not title:
            title = unit_id

        # 提取完成日期
        date_match = re.search(r"✅(\d{4}-\d{2}-\d{2})", line)
        completed_date = date_match.group(1) if date_match else None

        # 薄弱标记
        is_weak = "⚠️" in line
        weak_reason = ""
        weak_date = None
        if is_weak:
            weak_m = re.search(r"⚠️薄弱：(.*?)(?:（标记于(\d{4}-\d{2}-\d{2})）)?", rest)
            if weak_m:
                weak_reason = weak_m.group(1).strip()
                weak_date = weak_m.group(2)

        # 提取页码
        page_match = re.search(r"\(P(\d+[+-]?\d*|N/A)\)", rest)
        page = page_match.group(0)[1:-1] if page_match else ""

        units.append({
            "id": unit_id,
            "title": title,
            "page": page,
            "done": is_done,
            "completed_date": completed_date,
            "weak": is_weak,
            "weak_reason": weak_reason,
            "weak_date": weak_date,
        })

        total += 1
        if is_done:
            completed_count += 1
        if is_weak:
            weak_count += 1

    return {
        "filename": os.path.basename(filepath),
        "book_name": book_name,
        "tags": tags,
        "note": note,
        "total_units": total,
        "completed": completed_count,
        "remaining": total - completed_count,
        "weak_count": weak_count,
        "progress_pct": round(completed_count / total * 100, 1) if total > 0 else 0,
        "units": units,
    }


def main():
    args = sys.argv[1:]

    detail_mode = "--detail" in args
    filter_book = None
    only_completed = "--completed" in args
    only_remaining = "--remaining" in args

    for i, arg in enumerate(args):
        if arg == "--book" and i + 1 < len(args):
            filter_book = args[i + 1]

    if not os.path.isdir(PROGRESS_DIR):
        print(json.dumps({"error": f"Progress dir not found: {PROGRESS_DIR}"}, ensure_ascii=False))
        sys.exit(1)

    all_books = []
    for fname in sorted(os.listdir(PROGRESS_DIR)):
        if not fname.endswith(".md"):
            continue
        filepath = os.path.join(PROGRESS_DIR, fname)
        try:
            book_data = parse_progress_file(filepath)
            if filter_book and filter_book.lower() not in book_data["book_name"].lower():
                continue
            if not detail_mode:
                book_data.pop("units")
            all_books.append(book_data)
        except Exception as e:
            all_books.append({"filename": fname, "error": str(e)})

    summary = {
        "total_books": len(all_books),
        "total_units": sum(b.get("total_units", 0) for b in all_books),
        "total_completed": sum(b.get("completed", 0) for b in all_books),
        "total_weak": sum(b.get("weak_count", 0) for b in all_books),
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "books": all_books,
    }

    if only_completed:
        for b in all_books:
            b["units"] = [u for u in b.get("units", []) if u["done"]]
    elif only_remaining:
        for b in all_books:
            b["units"] = [u for u in b.get("units", []) if not u["done"]]

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
