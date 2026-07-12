#!/usr/bin/env python3
"""
add-review.py — 登记一条知识点到复习清单

用法：
  python3 add-review.py "指令流水线"                # 按知识点名（含拼音首字母）搜索
  python3 add-review.py --path 考研备考/408/02-.../指令流水线.md   # 精确指定
  python3 add-review.py "指令流水线" --weak         # 薄弱化（用 D1/D2/D4/D8/D15）
  python3 add-review.py --list-only "流水线"       # 只列候选不写入
  python3 add-review.py "指令流水线" --dry-run

退出码：
  0  登记成功
  1  找不到匹配
  2  多个匹配，需要用户消歧（列表打印在 stdout，机器可读 JSON 见 --json）
  3  已在清单中（重复）
  4  参数错误

--json 参数会让脚本以 JSON 输出结果（供 API/agent 消费）。
"""
import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review_common import (  # noqa
    scan_knowledge_points, search_knowledge_points, find_kp_by_rel_path,
    read_review_list, write_review_list,
    parse_review_list, render_review_list,
    make_entry_from_kp, find_entry_by_rel_path,
    SUBJECT_KEYS,
)


def emit(payload, use_json):
    if use_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        # 简单文本
        if payload.get("status") == "ok":
            e = payload["entry"]
            print(f"✅ 登记成功: {e['name']}  ({e['path_hint']})  [D1] 到期{e['due']}")
        elif payload.get("status") == "not_found":
            print(f"❌ 找不到匹配: {payload['query']}")
        elif payload.get("status") == "ambiguous":
            print(f"⚠️  多个匹配 ({len(payload['candidates'])} 个)，请用 --path 精确指定：")
            for c in payload["candidates"]:
                print(f"  - {c['name']}  ({c['path_hint']})  → {c['rel_path']}")
        elif payload.get("status") == "duplicate":
            print(f"⚠️  已在复习清单: {payload['existing']['name']} · {payload['existing']['path_hint']} [{payload['existing']['current']}]")
        elif payload.get("status") == "listed":
            print(f"候选（{len(payload['candidates'])} 个）：")
            for c in payload["candidates"]:
                print(f"  - {c['name']}  ({c['path_hint']})  → {c['rel_path']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", help="知识点名或拼音首字母")
    ap.add_argument("--path", help="精确的 rel_path（考研备考/... 开头）")
    ap.add_argument("--subject", choices=SUBJECT_KEYS, help="限定科目")
    ap.add_argument("--weak", action="store_true", help="登记为薄弱点（D1/D2/D4/D8/D15）")
    ap.add_argument("--list-only", action="store_true", help="只列出候选，不写入")
    ap.add_argument("--limit", type=int, default=10, help="搜索候选数量上限")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    if not args.query and not args.path:
        ap.error("必须提供 query 或 --path")

    all_kps = scan_knowledge_points()

    # 1. 定位 KP
    kp = None
    candidates = []

    if args.path:
        kp = find_kp_by_rel_path(args.path, all_kps=all_kps)
        if not kp:
            emit({"status": "not_found", "query": args.path}, args.json)
            sys.exit(1)
    else:
        candidates = search_knowledge_points(
            args.query, subject=args.subject, limit=args.limit, all_kps=all_kps
        )
        if args.list_only:
            emit({
                "status": "listed",
                "candidates": [c.to_dict() for c in candidates],
            }, args.json)
            sys.exit(0)

        if not candidates:
            emit({"status": "not_found", "query": args.query}, args.json)
            sys.exit(1)
        if len(candidates) > 1 and candidates[0].name.lower() != (args.query or "").strip().lower():
            emit({
                "status": "ambiguous",
                "query": args.query,
                "candidates": [c.to_dict() for c in candidates],
            }, args.json)
            sys.exit(2)
        kp = candidates[0]

    # 2. 检查是否已在清单
    content = read_review_list()
    parsed = parse_review_list(content)
    subj, existing = find_entry_by_rel_path(parsed, kp.rel_path)
    if existing is not None:
        emit({
            "status": "duplicate",
            "existing": {
                "name": existing.name,
                "path_hint": existing.path_hint,
                "current": existing.current,
                "due": existing.due,
            },
        }, args.json)
        sys.exit(3)

    # 3. 生成新条目并追加
    today = date.today()
    new_entry = make_entry_from_kp(kp, today, weak=args.weak)
    parsed["sections"].setdefault(kp.subject, []).append(new_entry)

    # 4. 写回
    if args.dry_run:
        emit({
            "status": "ok",
            "dry_run": True,
            "entry": {
                "name": new_entry.name,
                "path_hint": new_entry.path_hint,
                "rel_path": new_entry.rel_path,
                "subject": new_entry.subject,
                "current": new_entry.current,
                "due": new_entry.due,
                "birth": new_entry.birth,
                "weak": new_entry.weak,
            },
        }, args.json)
        sys.exit(0)

    new_content = render_review_list(parsed)
    write_review_list(new_content)

    emit({
        "status": "ok",
        "entry": {
            "name": new_entry.name,
            "path_hint": new_entry.path_hint,
            "rel_path": new_entry.rel_path,
            "subject": new_entry.subject,
            "current": new_entry.current,
            "due": new_entry.due,
            "birth": new_entry.birth,
            "weak": new_entry.weak,
        },
    }, args.json)
    sys.exit(0)


if __name__ == "__main__":
    main()
