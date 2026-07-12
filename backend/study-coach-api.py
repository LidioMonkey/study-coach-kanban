#!/usr/bin/env python3
"""Study Coach API — serves progress data from Obsidian progress files.

Read endpoints:
- GET /api/progress      → 总体进度
- GET /api/today-tasks   → 今日任务（含 file/line/raw 回写坐标）
- GET /api/reviews       → 复习清单

Write endpoints (require X-Token header):
- POST /api/sync-tasks   → 批量勾选/取消勾选，写回 vault 并推 OSS
- POST /api/pull         → 手动触发 OSS→本地 拉取

Status endpoints (require X-Token header):
- GET /api/status/system    → CPU/内存/磁盘/uptime
- GET /api/status/services  → API/nginx/radicale/cron 状态
- GET /api/status/vault     → vault/OSS 摘要
- GET /api/status/logs?type=... → 日志 tail
"""

import datetime
import fcntl
import glob
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import time

VAULT_ROOT = "/root/obsidian-vault"
VAULT = os.path.join(VAULT_ROOT, "考研备考")
PROGRESS_DIR = os.path.join(VAULT, "进度")
TASKS_FILE = os.path.join(VAULT, "今日任务.md")
REVIEW_FILE = os.path.join(VAULT, "复习清单.md")

# 看板自管理配置：key 是 progress 文件名（不含 .md），value 是 limit
BOOK_CONFIG_FILE = "/root/study-coach-active-books.json"
DEFAULT_LIMIT = 5

TOKEN_FILE = "/root/.hermes/study-coach/api-token"
LOCK_FILE = "/var/lock/study-coach.lock"
SYNC_LOG = "/root/.hermes/study-coach/sync.log"
PULL_LOG = "/root/obsidian-vault-pull.log"
API_LOG = "/root/.hermes/study-coach/api.log"

SYNC_SH = "/root/obsidian-backups/sync.sh"
PULL_SH = "/root/obsidian-vault-pull.sh"
BACKUP_SH = "/root/obsidian-backups/backup.sh"
# 单文件快速同步（勾选写回专用，避免全量 sync 3~4s 开销）
PULL_ONE_SH = "/root/obsidian-vault-pull-one.sh"
SYNC_ONE_SH = "/root/obsidian-backups/sync-one.sh"

SUBJECT_TITLES = {
    "数学二": "## 🟥 数学二",
    "408": "## 🟦 408",
    "英语二": "## 🟩 英语二",
    "政治": "## 🟨 政治",
}

# --------- token ---------

def load_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

API_TOKEN = load_token()


def log_sync(msg):
    os.makedirs(os.path.dirname(SYNC_LOG), exist_ok=True)
    with open(SYNC_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}\n")


# --------- vault lock (shared with pull cron) ---------

class VaultLock:
    def __init__(self):
        self.fh = None
    def __enter__(self):
        self.fh = open(LOCK_FILE, "w")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self
    def __exit__(self, *a):
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()


# --------- progress scan ---------

def parse_tags(line):
    return line.strip().replace("tags: [", "").replace("]", "").replace(" ", "").split(",") if "tags:" in line else []


def scan_progress():
    books = []
    if not os.path.isdir(PROGRESS_DIR):
        return books

    for pf in sorted(glob.glob(os.path.join(PROGRESS_DIR, "*.md"))):
        name = os.path.splitext(os.path.basename(pf))[0]
        subject = "其他"
        total = 0
        done_total = 0
        chapters = []

        with open(pf, "r", encoding="utf-8") as f:
            in_frontmatter = False
            frontmatter_lines = []
            for line in f:
                if line.startswith("---"):
                    in_frontmatter = not in_frontmatter
                    if not in_frontmatter:
                        continue
                if in_frontmatter:
                    frontmatter_lines.append(line)
                    continue

                m = re.match(r"^- \[([ x])\] (.+)", line)
                if m:
                    total += 1
                    is_done = m.group(1) == "x"
                    text = m.group(2).strip()

                    cid = ""
                    title = text
                    page = ""
                    date = ""

                    id_match = re.match(r"(\S+)\s+(.*)", text)
                    if id_match:
                        cid = id_match.group(1)
                        title = id_match.group(2)

                    page_match = re.search(r"\(P?(\d+)(-P?\d+)?\)", title)
                    if page_match:
                        page = page_match.group(0)
                        title = title.replace(page_match.group(0), "").strip()

                    date_match = re.search(r"✅(\d{4}-\d{2}-\d{2})", title)
                    if date_match:
                        date = date_match.group(1)
                        title = title.replace("✅" + date_match.group(1), "").strip()

                    if is_done:
                        done_total += 1

                    chapters.append({
                        "id": cid,
                        "title": title,
                        "page": page,
                        "done": is_done,
                        "date": date,
                    })

        tags = []
        for l in frontmatter_lines:
            tags.extend(parse_tags(l))
        for tag in tags:
            if tag in SUBJECT_TITLES:
                subject = tag
                break

        if total > 0:
            pct = round(done_total / total * 100, 1)
            books.append({
                "name": name,
                "subject": subject,
                "total": total,
                "done": done_total,
                "pct": pct,
                "chapters": chapters,
            })
    return books


# --------- today tasks ---------

# 旧白名单（保留仅作为首次初始化默认值；运行时以 BOOK_CONFIG_FILE 为准）
_LEGACY_ACTIVE_BOOKS_INIT = {
    # 进度文件名(不含.md) -> limit
    "武忠祥高等数学辅导讲义强化篇":        5,
    "武忠祥高等数学辅导讲义配套严选题":    5,
    "基础过关660-高数篇":                  5,
    "王道操作系统":                        5,
    "王道计算机网络":                      5,
    "英语红宝书2027":                      3,
    "刘晓艳-58篇基础阅读":                 3,
    "刘晓艳-88句":                         3,
    "刘晓艳-考研英语核心词":               3,
}


def _load_book_config():
    """返回 { active: {name:limit,...}, updated_at: str }"""
    if os.path.exists(BOOK_CONFIG_FILE):
        try:
            with open(BOOK_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # 首次初始化
    cfg = {"active": dict(_LEGACY_ACTIVE_BOOKS_INIT),
           "updated_at": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        with open(BOOK_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return cfg


def _save_book_config(cfg):
    cfg["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = BOOK_CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BOOK_CONFIG_FILE)
    return cfg


def parse_today_tasks():
    """扫 /进度/*.md，按 BOOK_CONFIG.active 配置取每本前 N 条未完成条目。

    与旧版差异：
    - 教材识别改用「进度文件名(basename)」；不再依赖 #tag 匹配
    - 增删教材通过看板 POST /api/book-config 完成，无需改 API 代码
    - 保留 file/line/raw 三元组，双向写回逻辑不变
    """
    today = datetime.date.today().isoformat()
    tasks = {"数学二": [], "408": [], "英语二": [], "政治": []}

    if not os.path.isdir(PROGRESS_DIR):
        return None, today

    cfg = _load_book_config()
    active = cfg.get("active", {}) or {}
    if not active:
        return tasks, today

    task_pat = re.compile(r"^- \[([ xX])\]\s+(.+)$")
    tag_pat = re.compile(r"#([^\s#]+)")
    date_pat = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")

    for pf in sorted(glob.glob(os.path.join(PROGRESS_DIR, "*.md"))):
        basename = os.path.splitext(os.path.basename(pf))[0]
        if basename not in active:
            continue
        limit = int(active[basename]) if active[basename] else DEFAULT_LIMIT
        limit = max(1, min(50, limit))

        rel_path = os.path.relpath(pf, VAULT_ROOT)
        with open(pf, "r", encoding="utf-8") as f:
            all_lines = f.readlines()

        # 抽 subject（frontmatter tags 里找 SUBJECT_TITLES 命中的第一个）
        subject = None
        in_fm = False
        fm_lines = []
        for raw_line in all_lines:
            stripped = raw_line.rstrip("\n")
            if stripped.startswith("---"):
                if in_fm:
                    break
                in_fm = True
                continue
            if in_fm:
                fm_lines.append(stripped)
        for l in fm_lines:
            for t in parse_tags(l):
                if t in SUBJECT_TITLES:
                    subject = t
                    break
            if subject:
                break
        if not subject or subject not in tasks:
            # 无法归类的教材（如"其他"）跳过；避免污染 4 科分栏
            continue

        # 扫任务体（记录最近的 ### 章节标题作为 title 前缀）
        picked = []
        in_fm = False
        cur_chapter = ""
        for idx, raw_line in enumerate(all_lines):
            stripped = raw_line.rstrip("\n")
            if stripped.startswith("---"):
                in_fm = not in_fm
                continue
            if in_fm:
                continue

            # 章节标题（### / ## / #）：记录为当前上下文
            heading_m = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
            if heading_m:
                level = len(heading_m.group(1))
                heading_txt = heading_m.group(2).strip()
                # 忽略 h1（整本书的主标题）；h2/h3/h4 都当章节前缀
                if level >= 2:
                    # 跳过元信息式标题（📋 章节进度 / 📎 相关 / 📝 备注 等），它们不是真正的章节
                    if re.match(r"^[\U0001F300-\U0001FAFF📋📎📝📌📊🔖]", heading_txt) \
                       or "章节进度" in heading_txt or "相关" in heading_txt \
                       or "备注" in heading_txt or "统计" in heading_txt:
                        cur_chapter = ""
                    else:
                        cur_chapter = heading_txt
                continue

            m = task_pat.match(stripped.strip())
            if not m:
                continue
            if m.group(1).lower() == "x":
                continue

            text = m.group(2).strip()
            title = date_pat.sub("", text)
            title = tag_pat.sub("", title)
            title = re.sub(r"\s+", " ", title).strip()

            # 章节与标题拆开返回：前端可分别渲染 tag 与主标题
            # 兼容旧客户端：title 字段仍旧保持"第3章 XXX · 一、概念题"格式
            full_title = f"{cur_chapter} · {title}" if cur_chapter else title

            picked.append({
                "title": full_title,
                "title_short": title,      # 不含章节前缀的纯任务名
                "chapter": cur_chapter,    # 章节字符串（无章节时为 ""）
                "book": basename,
                "book_name": basename,
                "done": False,
                "file": rel_path,
                "line": idx,
                "raw": stripped,
            })
            if len(picked) >= limit:
                break

        tasks[subject].extend(picked)

    return tasks, today


# --------- reviews ---------

REVIEW_NODES = [1, 3, 7, 15, 30]

def parse_reviews():
    """新格式（v2, md 文件为最小单位）解析。同时兼容旧 v1 行。
       输出字段：
         name, rel_path, path_hint, subject, stage, stage_desc,
         seed_date, due_date, done_marks, rolled, weak, checked,
         file, line, raw, overdue, legacy_v1
    """
    import sys as _sys
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in _sys.path:
        _sys.path.insert(0, _here)
    from review_common import parse_review_line, SUBJECT_HEADERS  # noqa

    today = datetime.date.today()
    result = {"today": [], "upcoming": {}, "stats": {"active": 0, "mastered": 0}}

    if not os.path.exists(REVIEW_FILE):
        return result

    with open(REVIEW_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    subj_by_header = {v: k for k, v in SUBJECT_HEADERS.items()}
    subj_display = {"数学": "数学二", "408": "408", "英语": "英语二", "政治": "政治"}

    current_subj = None
    in_mastered = False

    for line_no, raw_line in enumerate(lines):
        stripped = raw_line.rstrip("\n").rstrip()

        if stripped.strip().startswith("## ✅ 已掌握"):
            in_mastered = True
            current_subj = None
            continue
        if stripped in subj_by_header:
            current_subj = subj_by_header[stripped]
            in_mastered = False
            continue

        if in_mastered:
            if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
                result["stats"]["mastered"] += 1
            continue

        entry = parse_review_line(stripped)
        if not entry:
            continue
        if not current_subj:
            continue

        due_short = entry.due
        if not due_short:
            continue
        try:
            mm, dd = due_short.split("-")
            due_date = datetime.date(today.year, int(mm), int(dd))
            if (due_date - today).days > 180:
                due_date = due_date.replace(year=today.year - 1)
        except Exception:
            continue

        item = {
            "name": entry.name,
            "rel_path": entry.rel_path,
            "path_hint": entry.path_hint,
            "subject": subj_display.get(current_subj, current_subj),
            "stage": entry.current or "?",
            "stage_desc": f"第{len(entry.done_marks) + 1}次",
            "seed_date": entry.birth,
            "due_date": due_date.isoformat(),
            "done_marks": entry.done_marks,
            "rolled": entry.rolled,
            "weak": entry.weak,
            "checked": entry.checked,
            "legacy_v1": entry.legacy_v1,
            # 回写坐标
            "file": "考研备考/复习清单.md",
            "line": line_no,
            "raw": stripped,
        }
        # 旧格式兼容：如果是 v1，补上 title/book/unit_id 供前端 fallback
        if entry.legacy_v1:
            item["title"] = entry.name
            item["book"] = entry.legacy_book or ""
            item["unit_id"] = entry.legacy_unit_id or ""

        result["stats"]["active"] += 1
        delta = (due_date - today).days
        if delta <= 0:
            item["overdue"] = delta < 0
            result["today"].append(item)
        elif delta <= 7:
            key = due_date.isoformat()
            result["upcoming"].setdefault(key, []).append(item)

    return result


# ============================================================
# 复习条目 → 知识点文件匹配（2026-07-06 新：按大纲知识点文件直接匹配）
# ============================================================

# 书名关键词（优先匹配）→ 笔记目录
NOTE_DIR_BY_KEYWORD = [
    # 408
    ("数据结构",     "408/01-数据结构"),
    ("计算机组成",   "408/02-计算机组成原理"),
    ("计组",         "408/02-计算机组成原理"),
    ("操作系统",     "408/03-操作系统"),
    ("计算机网络",   "408/04-计算机网络"),
    ("计网",         "408/04-计算机网络"),
    ("高数",         "数学二/01-高数"),
    ("武忠祥",       "数学二/01-高数"),
    ("线代",         "数学二/02-线代"),
    ("线性代数",     "数学二/02-线代"),
    ("李永乐",       "数学二/02-线代"),
    ("政治",         "政治"),
    ("徐涛",         "政治"),
    ("英语",         "英语二"),
    ("红宝书",       "英语二"),
    ("刘晓艳",       "英语二"),
]

CROSS_SUBJECT_KEYWORDS = {
    "同步与互斥": ["408/03-操作系统", "408/02-计算机组成原理"],
    "存储系统":   ["408/02-计算机组成原理", "408/03-操作系统"],
    "进程调度":   ["408/03-操作系统", "408/02-计算机组成原理"],
    "文件组织":   ["408/03-操作系统", "408/01-数据结构"],
}


def _find_note_file_v2(title, book):
    """新匹配逻辑（2026-07-06）：按知识点文件名直接匹配。

    策略（优先级从高到低）：
      1. 书名关键词直接定位目录 → 目录内精确匹配文件名
      2. 跨科目关键词 → 依次搜索多个可能目录
      3. 全 vault 模糊搜索文件名包含 title 的文件
      4. 全 vault 模糊搜索文件内容（heading）包含 title 的文件
      5. 兜底：在书名关键词定位的目录中，找任意一个相关文件

    返回 (abs_path, matched_precision: str) 或 (None, None)
    matched_precision: "exact" | "contains" | "cross" | "content" | "fallback"
    """
    import unicodedata

    def norm(s):
        """归一化：去括号/符号/空格/编号前缀，便于匹配"""
        s = re.sub(r'[\[\]【】（）()《》""\'\']', '', s)
        s = re.sub(r'^[\d.]+\s*', '', s)          # 去掉编号前缀
        s = re.sub(r'\s+', '', s)
        return s.lower()

    norm_title = norm(title)

    # Step 1: 书名关键词定位目录
    search_dirs = []
    for kw, rel in NOTE_DIR_BY_KEYWORD:
        if kw in (book or "") or kw in title:
            full = os.path.join(VAULT_ROOT, rel)
            if os.path.isdir(full):
                search_dirs.append(full)

    # 去重但保留顺序
    seen = set()
    ordered_dirs = []
    for d in search_dirs:
        if d not in seen:
            seen.add(d)
            ordered_dirs.append(d)
    search_dirs = ordered_dirs
    
    # Step 2: 跨科目关键词
    for kw, dirs in CROSS_SUBJECT_KEYWORDS.items():
        if kw in title or kw in (book or ""):
            for rel in dirs:
                full = os.path.join(VAULT_ROOT, rel)
                if full not in seen and os.path.isdir(full):
                    search_dirs.append(full)
    
    if not search_dirs:
        return None, None

    def _is_old_chapter_file(fname):
        """判断是否为旧章节文件（命名如'第1章-行列式.md'），新知识点文件应优先。"""
        return bool(re.match(r'^第.+章.*\.md$', fname))

    def _list_knowledge_files(root_dir):
        """递归列出目录下的所有 .md 文件（排除 SKILL.md 和旧章节文件）。"""
        results = []
        for root, dirs, files in os.walk(root_dir):
            for fname in files:
                if fname.endswith('.md') and fname != 'SKILL.md' and not _is_old_chapter_file(fname):
                    results.append(os.path.join(root, fname))
        return results

    # Step 3: 精确匹配（递归搜索子目录）
    for d in search_dirs:
        for fpath in _list_knowledge_files(d):
            fname_base = os.path.basename(fpath)[:-3]
            if norm(fname_base) == norm_title:
                return fpath, "exact"

    # Step 4: 文件名包含匹配（递归搜索子目录）
    for d in search_dirs:
        for fpath in _list_knowledge_files(d):
            fname_base = os.path.basename(fpath)[:-3]
            if norm_title in norm(fname_base) or norm(fname_base) in norm_title:
                return fpath, "contains"

    # Step 5: 全 vault 模糊搜索（只搜索 search_dirs 内的目录，跳过旧章节文件）
    best_path, best_score = None, 0
    for d in search_dirs:
        for fpath in _list_knowledge_files(d):
            fname_base = os.path.basename(fpath)[:-3]
            score = 0
            if norm(fname_base) == norm_title:
                score = 3
            elif norm_title in norm(fname_base) or norm(fname_base) in norm_title:
                score = 2
            elif any(part in fname_base for part in norm_title.split() if len(part) >= 3):
                score = 1
            if score > best_score:
                best_score = score
                best_path = fpath

    if best_path:
        return best_path, "content" if best_score == 1 else "contains"

    # Step 6: 兜底 → 第一个 search_dir 的第一个新知识点文件（跳过旧章节）
    for d in search_dirs:
        files = _list_knowledge_files(d)
        if files:
            return files[0], "fallback"

    return None, None


def _extract_section_from_knowledge_file(md_path, title):
    """从知识点文件（## 小节结构）中提取内容。
    
    策略：
      1. 标题精确匹配 ## 标题
      2. heading 编号+标题包含
      3. 全文模糊匹配关键词
      4. 兜底：前 800 字
    """
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None, None, False

    def norm_heading(h):
        h = re.sub(r'^#{1,3}\s*', '', h).strip()
        h = re.sub(r'^[\d.]+\s*', '', h).strip()
        return re.sub(r'\s+', '', h).lower()

    norm_t = norm_heading(title)

    # 拆成 sections
    lines = content.split("\n")
    sections = []
    current = None
    for line in lines:
        if re.match(r'^## ', line):
            if current:
                sections.append(current)
            current = (line, [])
        elif current:
            current[1].append(line)
    if current:
        sections.append(current)

    if not sections:
        return None, content[:800], False

    # 1. 精确匹配
    for h, body in sections:
        if norm_heading(h) == norm_t:
            return h.strip(), "\n".join(body).strip(), True

    # 2. 包含匹配
    for h, body in sections:
        hs = norm_heading(h)
        if norm_t in hs or hs in norm_t:
            return h.strip(), "\n".join(body).strip(), True

    # 3. 模糊
    keywords = [p for p in norm_t if len(p) >= 2]
    for h, body in sections:
        hs = norm_heading(h)
        if any(kw in hs for kw in keywords):
            return h.strip(), "\n".join(body).strip(), False

    # 4. 兜底
    first_h, first_body = sections[0]
    body_txt = "\n".join(first_body).strip()
    return first_h.strip(), body_txt[:800], False


def get_review_note_v2(unit_id, title, book):
    """新公开接口（2026-07-06）：按知识点文件名直接匹配。
    
    复习条目格式（新版）：
      "同步与互斥 · 操作系统"
      "指令流水线 · 计算机组成原理"
    
    返回 {"ok", "matched", "file", "heading", "body", "precision", "reason"}
    """
    result = {"ok": False, "matched": False, "precision": None,
              "file": None, "heading": None, "body": None, "reason": None}
    
    if not title and not book:
        result["reason"] = "title 和 book 均为空"
        return result
    
    # 新型条目格式："同步与互斥 · 操作系统" 或直接是知识点名
    # 优先从 title 中提取知识点名（去掉书名部分）
    knowledge_name = title
    if "·" in title:
        parts = title.split("·")
        if len(parts) >= 2:
            # 取第一个非空且非标签的部分
            for p in parts:
                p = p.strip()
                if p and not p.startswith("#"):
                    knowledge_name = p.strip()
                    break
    elif "#" in title:
        knowledge_name = re.sub(r'\s*#\S+', '', title).strip()
    
    if not knowledge_name:
        knowledge_name = title
    
    matched_file, precision = _find_note_file_v2(knowledge_name, book)
    
    if not matched_file:
        result["reason"] = f"未找到知识点文件: {knowledge_name}"
        return result
    
    heading, body, exact = _extract_section_from_knowledge_file(matched_file, knowledge_name)
    rel = os.path.relpath(matched_file, VAULT_ROOT)
    
    result.update({
        "ok": True,
        "matched": exact or precision in ("exact", "contains"),
        "precision": precision,
        "file": rel,
        "heading": heading,
        "body": body or "",
    })
    return result


def _find_note_dir_for_review(item):
    """根据复习条目的 book 名反查笔记目录。返回绝对路径或 None。"""
    book = (item.get("book") or "").strip()
    if not book:
        return None
    for kw, rel in NOTE_DIR_BY_KEYWORD:
        if kw in book:
            full = os.path.join(VAULT_ROOT, rel)
            if os.path.isdir(full):
                return full
    return None


def _find_chapter_file(note_dir, chapter_num):
    """在 note_dir 里找 '第N章-*.md'。"""
    if not note_dir or not os.path.isdir(note_dir):
        return None
    prefix = f"第{chapter_num}章"
    for fname in os.listdir(note_dir):
        if fname.startswith(prefix) and fname.endswith(".md"):
            return os.path.join(note_dir, fname)
    return None


def _extract_section(md_path, unit_id, title):
    """从 md_path 里定位小节内容（保留旧逻辑，向后兼容）。

    策略：
      1. 找 `## {unit_id} xxx` 精确匹配（进度文件编号与笔记编号一致时）
      2. 用 title 关键词（去掉编号）在 `##` 里模糊匹配
      3. 兜底：返回文件第一个 `##` 到下一个 `##` 之间的前 800 字

    返回 (section_title, section_body, matched_precise: bool)
    """
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None, None, False

    lines = content.split("\n")

    # 拆成 sections：每个 ## 到下一个 ##
    sections = []  # [(heading_line, [body_lines])]
    current = None
    for line in lines:
        if line.startswith("## ") and not line.startswith("### "):
            if current:
                sections.append(current)
            current = (line, [])
        elif current:
            current[1].append(line)
    if current:
        sections.append(current)

    if not sections:
        return None, None, False

    # 1. 优先按标题关键词精确匹配（进度和笔记的编号可能不一致，但标题应该一样）
    keyword = re.sub(r"^[\d.]+\s*", "", title).strip()
    if len(keyword) >= 2:
        # 1a. 完全相等
        for heading, body in sections:
            h = heading[3:].strip()
            h_stripped = re.sub(r"^[\d.]+\s*", "", h).strip()
            if keyword == h_stripped:
                return h, "\n".join(body).strip(), True

    # 2. 按 unit_id 编号精确匹配 `## 5.6 xxx`
    for heading, body in sections:
        h = heading[3:].strip()
        if h.startswith(unit_id + " ") or h.startswith(unit_id + "."):
            return h, "\n".join(body).strip(), True

    # 3. 关键词包含匹配（更宽松，作为兜底前）
    if len(keyword) >= 2:
        for heading, body in sections:
            h = heading[3:].strip()
            h_stripped = re.sub(r"^[\d.]+\s*", "", h).strip()
            if keyword in h_stripped or h_stripped in keyword:
                return h, "\n".join(body).strip(), True

    # 4. 兜底：返回第一节
    first_h, first_body = sections[0]
    body_txt = "\n".join(first_body).strip()
    return first_h[3:].strip(), body_txt[:800], False


def get_review_note(unit_id, title, book, rel_path=None):
    """公开接口：优先用 rel_path 直连；否则用知识点文件名匹配；再降级走旧逻辑。

    rel_path: v2 新格式条目直接给出的知识点文件路径（相对 vault 根）
    """
    # v2：有 rel_path 直接读
    if rel_path:
        abs_path = os.path.join(VAULT_ROOT, rel_path)
        if os.path.isfile(abs_path):
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    content = f.read()
                # 提取 h1
                heading = None
                for line in content.split("\n"):
                    if line.startswith("# ") and not line.startswith("## "):
                        heading = line[2:].strip()
                        break
                # body：去 frontmatter，完整返回（前端有 max-height 滚动兜底）
                body = re.sub(r"^---.*?---\s*", "", content, count=1, flags=re.DOTALL)
                return {
                    "ok": True,
                    "matched": True,
                    "precision": "rel_path_direct",
                    "file": rel_path,
                    "heading": heading or (title or ""),
                    "body": body,
                    "reason": None,
                }
            except Exception as e:
                return {"ok": False, "reason": f"读取失败: {e}", "file": rel_path,
                        "matched": False, "precision": None, "heading": None, "body": None}

    result = get_review_note_v2(unit_id, title, book)
    if result["ok"]:
        return result
    # 降级：旧逻辑（unit_id 章节匹配，兼容旧格式）
    old_result = {"ok": False, "note": None, "file": None, "matched": False}
    item = {"unit_id": unit_id, "title": title, "book": book}
    note_dir = _find_note_dir_for_review(item)
    if not note_dir:
        old_result["reason"] = f"未找到该书对应笔记目录（book={book}）"
        return old_result

    ch_m = re.match(r"(?:第)?(\d+)", unit_id)
    if not ch_m:
        old_result["reason"] = f"无法从 unit_id={unit_id} 提取章号"
        return old_result
    chapter = int(ch_m.group(1))

    md_path = _find_chapter_file(note_dir, chapter)
    if not md_path:
        old_result["reason"] = f"未找到第{chapter}章笔记文件"
        old_result["dir"] = note_dir
        return old_result

    heading, body, matched = _extract_section(md_path, unit_id, title)
    rel = os.path.relpath(md_path, VAULT_ROOT)

    old_result.update({
        "ok": True,
        "matched": matched,
        "file": rel,
        "chapter": chapter,
        "heading": heading,
        "body": body or "",
        "precision": "chapter_legacy",
    })
    return old_result


# --------- sync-tasks (write) ---------

# --------- knowledge points & reviews add/remove ---------

_KP_CACHE = {"ts": 0.0, "items": None}
_KP_CACHE_TTL = 60  # 秒

def get_knowledge_points_cached():
    """带 60s 缓存的知识点扫描。"""
    import sys as _sys
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in _sys.path:
        _sys.path.insert(0, _here)
    from review_common import scan_knowledge_points  # noqa

    now = time.time()
    if _KP_CACHE["items"] is not None and now - _KP_CACHE["ts"] < _KP_CACHE_TTL:
        return _KP_CACHE["items"]
    items = scan_knowledge_points()
    _KP_CACHE["ts"] = now
    _KP_CACHE["items"] = items
    return items


def api_knowledge_points(subject=None, query=None, limit=None):
    """返回知识点列表。可选 subject / query 过滤。"""
    import sys as _sys
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in _sys.path:
        _sys.path.insert(0, _here)
    from review_common import search_knowledge_points  # noqa

    all_kps = get_knowledge_points_cached()

    if query:
        results = search_knowledge_points(query, subject=subject,
                                          limit=limit or 200, all_kps=all_kps)
    else:
        results = [k for k in all_kps if (not subject or k.subject == subject)]
        if limit:
            results = results[:limit]

    # 附带"是否已在复习清单"字段
    review_paths = _get_active_review_paths()
    items = []
    for kp in results:
        d = kp.to_dict()
        d["in_review"] = kp.rel_path in review_paths
        items.append(d)
    return items


def _get_active_review_paths():
    """当前活跃复习条目的 rel_path 集合。"""
    import sys as _sys
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in _sys.path:
        _sys.path.insert(0, _here)
    from review_common import (  # noqa
        read_review_list, parse_review_list, SUBJECT_KEYS,
    )
    parsed = parse_review_list(read_review_list())
    s = set()
    for subj in SUBJECT_KEYS:
        for e in parsed["sections"][subj]:
            if e.rel_path:
                s.add(e.rel_path)
    return s


def api_add_review(rel_path, weak=False):
    """通过 rel_path 精确登记一条到复习清单。走 vault lock，拉 OSS → 备份 → 写 → 推 OSS。"""
    import sys as _sys
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in _sys.path:
        _sys.path.insert(0, _here)
    from review_common import (  # noqa
        find_kp_by_rel_path, read_review_list, write_review_list,
        parse_review_list, render_review_list,
        make_entry_from_kp, find_entry_by_rel_path,
    )

    result = {"ok": False}

    if not rel_path:
        result["error"] = "rel_path 必填"
        return result

    with VaultLock():
        rc, _ = run_cmd(["bash", PULL_SH], timeout=120)
        if rc != 0:
            result["error"] = "OSS 拉取失败"
            return result

        all_kps = get_knowledge_points_cached()
        # cache 可能过期，force 一次
        _KP_CACHE["ts"] = 0
        all_kps = get_knowledge_points_cached()

        kp = find_kp_by_rel_path(rel_path, all_kps=all_kps)
        if not kp:
            result["error"] = f"知识点不存在: {rel_path}"
            return result

        content = read_review_list()
        parsed = parse_review_list(content)
        subj, existing = find_entry_by_rel_path(parsed, kp.rel_path)
        if existing:
            result["error"] = "已在复习清单"
            result["existing"] = {
                "name": existing.name,
                "path_hint": existing.path_hint,
                "current": existing.current,
                "due": existing.due,
            }
            return result

        today = datetime.date.today()
        new_entry = make_entry_from_kp(kp, today, weak=weak)
        parsed["sections"].setdefault(kp.subject, []).append(new_entry)

        run_cmd(["bash", BACKUP_SH], timeout=60)
        write_review_list(render_review_list(parsed))

        rc_p, out_p = run_cmd(["bash", SYNC_SH], timeout=120)
        result["ok"] = True
        result["push_rc"] = rc_p
        result["entry"] = {
            "name": new_entry.name,
            "rel_path": new_entry.rel_path,
            "path_hint": new_entry.path_hint,
            "subject": new_entry.subject,
            "current": new_entry.current,
            "due": new_entry.due,
            "birth": new_entry.birth,
            "weak": new_entry.weak,
        }
        log_sync(f"add-review {kp.rel_path}")
        return result


def api_remove_review(rel_path):
    """从活跃 sections 中移除一条（不影响归档区）。"""
    import sys as _sys
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in _sys.path:
        _sys.path.insert(0, _here)
    from review_common import (  # noqa
        read_review_list, write_review_list,
        parse_review_list, render_review_list,
        SUBJECT_KEYS,
    )

    result = {"ok": False}
    if not rel_path:
        result["error"] = "rel_path 必填"
        return result

    with VaultLock():
        rc, _ = run_cmd(["bash", PULL_SH], timeout=120)
        if rc != 0:
            result["error"] = "OSS 拉取失败"
            return result

        content = read_review_list()
        parsed = parse_review_list(content)

        removed = None
        for subj in SUBJECT_KEYS:
            new_list = []
            for e in parsed["sections"][subj]:
                if e.rel_path == rel_path and removed is None:
                    removed = {"name": e.name, "subject": subj,
                               "path_hint": e.path_hint, "current": e.current}
                    continue
                new_list.append(e)
            parsed["sections"][subj] = new_list

        if not removed:
            result["error"] = "未找到对应条目"
            return result

        run_cmd(["bash", BACKUP_SH], timeout=60)
        write_review_list(render_review_list(parsed))
        rc_p, _ = run_cmd(["bash", SYNC_SH], timeout=120)

        result["ok"] = True
        result["removed"] = removed
        result["push_rc"] = rc_p
        log_sync(f"remove-review {rel_path}")
        return result


# --------- sync-tasks (write) ---------

def apply_toggle(raw_line, want_done, is_review_list=False):
    """把 `- [ ] xxx #tag` <-> `- [x] xxx ✅日期 #tag` 相互切换。返回 (new_line, changed_bool).

    is_review_list=True 时走复习清单专用逻辑：
      - 勾选 → 只把 [ ] 改为 [x]（后续由 auto-review-mark 补 ✔Dn 并推进）
      - 取消 → 把 [x] 改为 [ ]（也不去动 ✔ 标记，用户可能只是误勾）
    """
    m = re.match(r"^(\s*- \[)([ xX])(\]\s+)(.+)$", raw_line)
    if not m:
        return raw_line, False
    prefix, box, mid, rest = m.groups()
    is_done_now = box.lower() == "x"
    if is_done_now == want_done:
        return raw_line, False

    if is_review_list:
        # 复习清单：只切换勾选态，其他内容不动
        new_box = "x" if want_done else " "
        return f"{prefix}{new_box}{mid}{rest}", True

    if want_done:
        # 未完成 → 完成：在最末尾（tag 前）加 ✅ 日期
        today = datetime.date.today().isoformat()
        tag_match = re.search(r"\s(#\S+)", rest)
        if tag_match:
            i = tag_match.start()
            new_rest = rest[:i] + f" ✅ {today}" + rest[i:]
        else:
            new_rest = rest.rstrip() + f" ✅ {today}"
        return f"{prefix}x{mid}{new_rest}", True
    else:
        new_rest = re.sub(r"\s*✅\s*\d{4}-\d{2}-\d{2}", "", rest)
        new_rest = re.sub(r"\s+", " ", new_rest).strip()
        return f"{prefix} {mid}{new_rest}", True


def line_matches_target(current, want_done):
    """检查当前行是否已经处于目标状态（用于识别 idempotent 重放）。"""
    m = re.match(r"^\s*- \[([ xX])\]\s+", current)
    if not m:
        return False
    is_done = m.group(1).lower() == "x"
    return is_done == want_done


def run_cmd(cmd, timeout=90):
    """跑外部命令，返回 (rc, output)."""
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except Exception as e:
        return -2, str(e)


def sync_tasks(changes):
    """批量写回 vault 并推 OSS。返回结果字典。"""
    if not isinstance(changes, list) or not changes:
        return {"ok": False, "error": "changes must be non-empty list"}

    result = {"ok": True, "applied": 0, "conflicts": [], "pull": None, "push": None}

    with VaultLock():
        # 收集本次要动的文件相对路径（用于单文件同步）
        target_files = sorted({ch.get("file", "") for ch in changes if ch.get("file")})
        target_files = [f for f in target_files if f and not f.startswith("/") and ".." not in f]

        # 1. 拉最新——仅拉目标文件（旧的整仓 sync 3~4s，单文件 <0.2s）
        #    整仓一致性由 15min OSS pull cron 兜底
        if target_files:
            rc, out = run_cmd(["bash", PULL_ONE_SH, *target_files], timeout=30)
        else:
            rc, out = 0, ""
        result["pull"] = {"rc": rc}
        if rc != 0:
            # 单文件 pull 失败通常是"对象不存在"（例如新增文件）——不阻断写回
            # 真正的网络故障会在下面 push 阶段暴露
            log_sync(f"pull-one soft-fail rc={rc} files={target_files}")

        # 2. 备份
        rc_b, _ = run_cmd(["bash", BACKUP_SH], timeout=60)

        # 3. 按 file 分组
        by_file = {}
        for i, ch in enumerate(changes):
            by_file.setdefault(ch["file"], []).append((i, ch))

        for rel_path, items in by_file.items():
            full = os.path.join(VAULT_ROOT, rel_path)
            if not os.path.isfile(full):
                for _, ch in items:
                    result["conflicts"].append({**ch, "reason": "文件不存在"})
                continue

            is_review = rel_path.endswith("复习清单.md")

            with open(full, "r", encoding="utf-8") as f:
                lines = f.readlines()

            modified = False
            for _, ch in items:
                line_no = ch.get("line")
                expected = ch.get("expected", "")
                want_done = bool(ch.get("done"))
                if line_no is None or line_no < 0 or line_no >= len(lines):
                    result["conflicts"].append({**ch, "reason": "行号超范围"})
                    continue
                current = lines[line_no].rstrip("\n")
                if current.strip() != expected.strip():
                    # 若当前行已经处于目标状态，视为幂等重放，静默计为成功
                    if line_matches_target(current, want_done):
                        result["applied"] += 1
                        continue
                    result["conflicts"].append({**ch, "reason": "行内容已变（手机端可能已改）", "actual": current})
                    continue
                new_line, changed = apply_toggle(current, want_done, is_review_list=is_review)
                if not changed:
                    # 目标状态已达到，视为成功
                    result["applied"] += 1
                    continue
                lines[line_no] = new_line + "\n"
                modified = True
                result["applied"] += 1

            if modified:
                with open(full, "w", encoding="utf-8") as f:
                    f.writelines(lines)

        # 3b. 复习清单被改过 → 立即跑一次 auto-review-mark（追加 ✔Dn 并推进）
        review_changed = any(f.endswith("复习清单.md") for f in by_file.keys())
        if review_changed:
            rc_m, _ = run_cmd(
                ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto-review-mark.py")],
                timeout=30,
            )
            result["review_mark_rc"] = rc_m

        # 4. 推 OSS——仅推被真实修改的目标文件（旧的整仓 sync 4s，单文件 <0.2s）
        push_files = list(target_files)
        if review_changed:
            # auto-review-mark 会改动复习清单.md 本身，确保它被推上去
            for f in target_files:
                if f.endswith("复习清单.md"):
                    break
            else:
                # 兜底：找 vault 里的复习清单.md 相对路径
                for root_dir, _, files in os.walk(VAULT_ROOT):
                    if "复习清单.md" in files:
                        rel = os.path.relpath(os.path.join(root_dir, "复习清单.md"), VAULT_ROOT)
                        if rel not in push_files:
                            push_files.append(rel)
                        break

        if push_files:
            rc_p, out_p = run_cmd(["bash", SYNC_ONE_SH, *push_files], timeout=60)
        else:
            rc_p, out_p = 0, ""
        result["push"] = {"rc": rc_p}
        if rc_p != 0:
            result["ok"] = False
            result["error"] = "OSS推送失败（本地已保存，重试会自动同步）"

    log_sync(f"applied={result['applied']} conflicts={len(result['conflicts'])} pull_rc={result['pull']['rc']} push_rc={result['push']['rc']}")
    return result


def do_pull():
    """手动触发 OSS → 本地。"""
    with VaultLock():
        rc, out = run_cmd(["bash", PULL_SH], timeout=120)
    log_sync(f"manual pull rc={rc}")
    return {"ok": rc == 0, "rc": rc}


# --------- status endpoints ---------

def status_system():
    def read_proc(path, default=""):
        try:
            with open(path) as f:
                return f.read()
        except Exception:
            return default

    # uptime
    up = read_proc("/proc/uptime").split()
    uptime_s = int(float(up[0])) if up else 0

    # load
    load = read_proc("/proc/loadavg").split()
    load_1_5_15 = load[:3] if len(load) >= 3 else ["?", "?", "?"]

    # meminfo
    mem = {}
    for line in read_proc("/proc/meminfo").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            mem[k.strip()] = v.strip().split()[0]  # kB
    mem_total_kb = int(mem.get("MemTotal", 0))
    mem_avail_kb = int(mem.get("MemAvailable", 0))
    mem_used_kb = mem_total_kb - mem_avail_kb

    # cpu count
    cpu_count = os.cpu_count()

    # disk /
    disk_root = shutil.disk_usage("/")
    disk_vault = None
    try:
        disk_vault = shutil.disk_usage(VAULT_ROOT)
    except Exception:
        pass

    return {
        "hostname": socket.gethostname(),
        "now": datetime.datetime.now().isoformat(timespec="seconds"),
        "tz": time.strftime("%Z"),
        "uptime_seconds": uptime_s,
        "load": load_1_5_15,
        "cpu_count": cpu_count,
        "mem_total_kb": mem_total_kb,
        "mem_used_kb": mem_used_kb,
        "mem_avail_kb": mem_avail_kb,
        "disk_root_total": disk_root.total,
        "disk_root_used": disk_root.used,
        "disk_root_free": disk_root.free,
        "disk_vault_total": disk_vault.total if disk_vault else None,
        "disk_vault_used": disk_vault.used if disk_vault else None,
        "disk_vault_free": disk_vault.free if disk_vault else None,
    }


def _systemctl_active(name):
    rc, out = run_cmd(["systemctl", "is-active", name], timeout=5)
    return {"name": name, "active": out.strip() == "active", "state": out.strip()}


def _proc_by_pattern(pattern):
    rc, out = run_cmd(["pgrep", "-a", "-f", pattern], timeout=5)
    lines = [l for l in out.splitlines() if l.strip()]
    if not lines:
        return {"running": False}
    first = lines[0]
    pid = first.split()[0] if first else None
    return {"running": True, "pid": pid, "pids": [l.split()[0] for l in lines], "count": len(lines)}


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except Exception:
        return False


def status_services():
    return {
        "study_coach_api": {**_proc_by_pattern("study-coach-api.py"), "port_8791": _port_open(8791)},
        "nginx": _systemctl_active("nginx"),
        "radicale": {**_systemctl_active("radicale"), "port_5232": _port_open(5232)},
        "cron_pull_last": _cron_pull_last(),
    }


def _cron_pull_last():
    if not os.path.exists(PULL_LOG):
        return None
    try:
        # 找最后一行 END pull
        with open(PULL_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            m = re.match(r"\[([\d\- :]+)\] END pull rc=(-?\d+)", line)
            if m:
                return {"time": m.group(1), "rc": int(m.group(2))}
    except Exception:
        pass
    return None


def status_vault():
    # 统计 vault
    total_files = 0
    total_size = 0
    latest_mtime = 0
    for root, dirs, files in os.walk(VAULT_ROOT):
        for name in files:
            fp = os.path.join(root, name)
            try:
                st = os.stat(fp)
                total_files += 1
                total_size += st.st_size
                if st.st_mtime > latest_mtime:
                    latest_mtime = st.st_mtime
            except Exception:
                pass

    trash_dir = "/root/oss-pull-trash"
    trash_count = 0
    if os.path.isdir(trash_dir):
        for root, dirs, files in os.walk(trash_dir):
            trash_count += len(files)

    # 备份
    backups = []
    bdir = "/root/obsidian-backups"
    if os.path.isdir(bdir):
        for n in os.listdir(bdir):
            fp = os.path.join(bdir, n)
            if n.startswith("vault") and (n.endswith(".tar.gz") or n.endswith(".tgz") or os.path.isdir(fp)):
                try:
                    st = os.stat(fp)
                    backups.append({"name": n, "size": st.st_size, "mtime": st.st_mtime})
                except Exception:
                    pass
        backups.sort(key=lambda x: x["mtime"], reverse=True)

    return {
        "vault_root": VAULT_ROOT,
        "file_count": total_files,
        "total_size": total_size,
        "latest_mtime": latest_mtime,
        "trash_count": trash_count,
        "backup_count": len(backups),
        "latest_backup": backups[0] if backups else None,
    }


def status_logs(log_type, tail=80):
    paths = {
        "sync": SYNC_LOG,
        "pull": PULL_LOG,
        "api": API_LOG,
    }
    p = paths.get(log_type)
    if not p or not os.path.exists(p):
        return {"log": log_type, "lines": [], "exists": False}
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 32768))
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()[-tail:]
    except Exception as e:
        lines = [f"[log-read-error] {e}"]
    return {"log": log_type, "path": p, "lines": lines, "exists": True}


# --------- vault browse / search / read (for status page) ---------

# 可读取的文本扩展名（安全白名单）
_VAULT_TEXT_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".org", ".markdown"}
_VAULT_MAX_READ_BYTES = 256 * 1024  # 单文件最多返回 256KB
_VAULT_SEARCH_MAX = 80             # 搜索结果最多返回 80 条


def _vault_safe_path(rel_path):
    """把外部传入的 rel_path 规范化并校验一定落在 VAULT_ROOT 下。
    返回 (abs_path, rel_norm) 或抛 ValueError。"""
    rel = (rel_path or "").strip().lstrip("/").replace("\\", "/")
    # 空 rel 代表 vault 根
    abs_path = os.path.realpath(os.path.join(VAULT_ROOT, rel))
    root_real = os.path.realpath(VAULT_ROOT)
    if abs_path != root_real and not abs_path.startswith(root_real + os.sep):
        raise ValueError("path escapes vault root")
    rel_norm = "" if abs_path == root_real else os.path.relpath(abs_path, root_real)
    return abs_path, rel_norm


def vault_browse(rel_path=""):
    """列指定目录。返回子目录 + 文件（不含隐藏项和 .git）。"""
    try:
        abs_path, rel_norm = _vault_safe_path(rel_path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.isdir(abs_path):
        return {"ok": False, "error": "not a directory", "rel_path": rel_norm}

    dirs = []
    files = []
    try:
        for name in sorted(os.listdir(abs_path)):
            if name.startswith(".") or name == "node_modules":
                continue
            full = os.path.join(abs_path, name)
            try:
                st = os.stat(full)
            except Exception:
                continue
            entry_rel = os.path.join(rel_norm, name) if rel_norm else name
            if os.path.isdir(full):
                # 统计子目录内 md 数量（浅一层，避免递归开销大）
                try:
                    md_count = sum(1 for n in os.listdir(full)
                                   if n.endswith(".md") and not n.startswith("."))
                except Exception:
                    md_count = 0
                dirs.append({
                    "name": name, "rel_path": entry_rel,
                    "mtime": st.st_mtime, "md_count": md_count,
                })
            else:
                ext = os.path.splitext(name)[1].lower()
                files.append({
                    "name": name, "rel_path": entry_rel,
                    "size": st.st_size, "mtime": st.st_mtime,
                    "readable": ext in _VAULT_TEXT_EXT,
                })
    except Exception as e:
        return {"ok": False, "error": f"list failed: {e}"}

    # 面包屑
    crumbs = [{"name": "vault", "rel_path": ""}]
    if rel_norm:
        acc = ""
        for part in rel_norm.split(os.sep):
            acc = os.path.join(acc, part) if acc else part
            crumbs.append({"name": part, "rel_path": acc})

    return {
        "ok": True, "rel_path": rel_norm, "crumbs": crumbs,
        "dirs": dirs, "files": files,
    }


def vault_search(query, limit=_VAULT_SEARCH_MAX):
    """按文件名 / 目录名做子串匹配（大小写不敏感），返回相对路径列表。"""
    q = (query or "").strip().lower()
    if not q:
        return {"ok": False, "error": "empty query"}
    if len(q) < 1:
        return {"ok": False, "error": "query too short"}

    results = []
    root_real = os.path.realpath(VAULT_ROOT)
    try:
        for cur, dirs, files in os.walk(root_real):
            # 剪枝隐藏目录
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]

            for d in dirs:
                if q in d.lower():
                    full = os.path.join(cur, d)
                    rel = os.path.relpath(full, root_real)
                    try:
                        st = os.stat(full)
                    except Exception:
                        continue
                    results.append({
                        "type": "dir", "name": d, "rel_path": rel,
                        "mtime": st.st_mtime,
                    })
                    if len(results) >= limit:
                        break

            if len(results) >= limit:
                break

            for f in files:
                if f.startswith("."):
                    continue
                if q in f.lower():
                    full = os.path.join(cur, f)
                    rel = os.path.relpath(full, root_real)
                    try:
                        st = os.stat(full)
                    except Exception:
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    results.append({
                        "type": "file", "name": f, "rel_path": rel,
                        "size": st.st_size, "mtime": st.st_mtime,
                        "readable": ext in _VAULT_TEXT_EXT,
                    })
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
    except Exception as e:
        return {"ok": False, "error": f"walk failed: {e}"}

    # 排序：目录在前，再按 mtime 倒序
    results.sort(key=lambda x: (0 if x["type"] == "dir" else 1, -x["mtime"]))
    return {
        "ok": True, "query": query, "count": len(results),
        "truncated": len(results) >= limit,
        "results": results,
    }


def vault_read_file(rel_path):
    """读取 vault 内的文本文件（白名单扩展名，限制 256KB）。"""
    try:
        abs_path, rel_norm = _vault_safe_path(rel_path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.isfile(abs_path):
        return {"ok": False, "error": "not a file", "rel_path": rel_norm}
    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in _VAULT_TEXT_EXT:
        return {"ok": False, "error": f"not readable ext: {ext}", "rel_path": rel_norm}

    try:
        st = os.stat(abs_path)
        with open(abs_path, "rb") as f:
            data = f.read(_VAULT_MAX_READ_BYTES + 1)
        truncated = len(data) > _VAULT_MAX_READ_BYTES
        content = data[:_VAULT_MAX_READ_BYTES].decode("utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"read failed: {e}"}

    return {
        "ok": True, "rel_path": rel_norm, "size": st.st_size,
        "mtime": st.st_mtime, "content": content, "truncated": truncated,
        "ext": ext,
    }



# --------- HTTP handler ---------

class Handler(http.server.BaseHTTPRequestHandler):
    def _check_token(self):
        got = self.headers.get("X-Token", "")
        if not API_TOKEN or got != API_TOKEN:
            self._send_json({"ok": False, "error": "unauthorized"}, code=401)
            return False
        return True

    def do_GET(self):
        p = self.path.split("?", 1)
        path = p[0]
        query = p[1] if len(p) > 1 else ""

        if path == "/api/progress":
            books = scan_progress()
            gt = sum(b["total"] for b in books)
            gd = sum(b["done"] for b in books)
            self._send_json({"ok": True, "books": books, "total": gt, "done": gd,
                             "progress": round(gd / gt * 100, 1) if gt else 0})
        elif path == "/api/today-tasks":
            tasks, date = parse_today_tasks()
            if tasks is None:
                self._send_json({"ok": True, "tasks": {"数学二": [], "408": [], "英语二": [], "政治": []},
                                 "date": None, "source": "进度目录不存在"})
                return
            books = scan_progress()
            total = sum(b["total"] for b in books)
            done = sum(b["done"] for b in books)
            self._send_json({"ok": True, "tasks": tasks, "date": date,
                             "progress": {"total": total, "done": done,
                                          "pct": round(done / total * 100, 1) if total else 0},
                             "source": "进度/*.md"})
        elif path == "/api/book-config":
            cfg = _load_book_config()
            self._send_json({"ok": True, **cfg})
        elif path == "/api/reviews":
            data = parse_reviews()
            self._send_json({"ok": True, **data, "source": "复习清单.md"})
        elif path == "/api/quote":
            quote_file = "/root/study-coach-quote.txt"
            text = ""
            updated = None
            try:
                import os as _os
                if _os.path.exists(quote_file):
                    with open(quote_file, "r", encoding="utf-8") as _fq:
                        text = _fq.read().strip()
                    updated = _os.path.getmtime(quote_file)
            except Exception as _e:
                pass
            self._send_json({"ok": True, "text": text, "updated_at": updated})
        elif path == "/api/knowledge-points":
            from urllib.parse import unquote
            params = {}
            for kv in query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    params[k] = unquote(v)
            subject = params.get("subject") or None
            q = params.get("q") or None
            limit = None
            if "limit" in params:
                try: limit = max(1, min(500, int(params["limit"])))
                except: pass
            items = api_knowledge_points(subject=subject, query=q, limit=limit)
            self._send_json({"ok": True, "items": items, "count": len(items)})
        elif path == "/api/review-note":
            # 参数：unit_id, title, book, rel_path
            params = {}
            for kv in query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    from urllib.parse import unquote
                    params[k] = unquote(v)
            data = get_review_note(
                params.get("unit_id", ""),
                params.get("title", ""),
                params.get("book", ""),
                rel_path=params.get("rel_path") or None,
            )
            self._send_json(data)
        elif path == "/api/status/system":
            if not self._check_token(): return
            self._send_json({"ok": True, **status_system()})
        elif path == "/api/status/services":
            if not self._check_token(): return
            self._send_json({"ok": True, **status_services()})
        elif path == "/api/status/vault":
            if not self._check_token(): return
            self._send_json({"ok": True, **status_vault()})
        elif path == "/api/status/logs":
            if not self._check_token(): return
            log_type = "sync"
            tail = 80
            for kv in query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    if k == "type":
                        log_type = v
                    elif k == "tail":
                        try: tail = min(500, max(10, int(v)))
                        except: pass
            self._send_json({"ok": True, **status_logs(log_type, tail)})
        elif path == "/api/vault/browse":
            if not self._check_token(): return
            from urllib.parse import unquote
            rel = ""
            for kv in query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    if k == "path":
                        rel = unquote(v)
            self._send_json(vault_browse(rel))
        elif path == "/api/vault/search":
            if not self._check_token(): return
            from urllib.parse import unquote
            q = ""
            lim = _VAULT_SEARCH_MAX
            for kv in query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    if k == "q":
                        q = unquote(v)
                    elif k == "limit":
                        try: lim = max(1, min(_VAULT_SEARCH_MAX, int(v)))
                        except: pass
            self._send_json(vault_search(q, limit=lim))
        elif path == "/api/vault/file":
            if not self._check_token(): return
            from urllib.parse import unquote
            rel = ""
            for kv in query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    if k == "path":
                        rel = unquote(v)
            self._send_json(vault_read_file(rel))
        else:
            self._send_json({"ok": False, "error": "not found"}, code=404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path in ("/api/sync-tasks", "/api/pull", "/api/reviews/add", "/api/reviews/remove", "/api/book-config"):
            if not self._check_token(): return
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body.decode("utf-8") or "{}")
            except Exception as e:
                self._send_json({"ok": False, "error": f"bad json: {e}"}, code=400)
                return
            if path == "/api/sync-tasks":
                res = sync_tasks(data.get("changes") or [])
                self._send_json(res)
            elif path == "/api/reviews/add":
                res = api_add_review(data.get("rel_path"), weak=bool(data.get("weak")))
                self._send_json(res)
            elif path == "/api/reviews/remove":
                res = api_remove_review(data.get("rel_path"))
                self._send_json(res)
            elif path == "/api/book-config":
                # body: {name:str, active:bool, limit?:int}
                name = (data.get("name") or "").strip()
                if not name:
                    self._send_json({"ok": False, "error": "name required"}, code=400)
                    return
                cfg = _load_book_config()
                cfg.setdefault("active", {})
                if data.get("active"):
                    limit = data.get("limit")
                    try:
                        limit = int(limit) if limit is not None else DEFAULT_LIMIT
                    except: limit = DEFAULT_LIMIT
                    limit = max(1, min(50, limit))
                    cfg["active"][name] = limit
                else:
                    cfg["active"].pop(name, None)
                _save_book_config(cfg)
                self._send_json({"ok": True, **cfg})
            else:
                res = do_pull()
                self._send_json(res)
        else:
            self._send_json({"ok": False, "error": "not found"}, code=404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {self.address_string()} {fmt % args}"
        try:
            os.makedirs(os.path.dirname(API_LOG), exist_ok=True)
            with open(API_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        print(line, flush=True)


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 8791), Handler)
    print("Study Coach API listening on :8791 (writeback enabled)", flush=True)
    server.serve_forever()
