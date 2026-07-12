#!/usr/bin/env python3
"""
review_common.py — 复习自动化共享工具（v3：md 文件为最小单位）

重大变更（2026-07-06）：
  复习条目的最小单位从"讲次(讲义章节序号)"改为"知识点 md 文件"。
  新格式：
    - [ ] [[考研备考/.../指令流水线|指令流水线]] · 408·计组·中央处理器 🌱06-29 [D7] 到期07-06 ⏭️延1
  旧格式（v1）保留在归档区兼容读取，不再新增。

被 add-review.py / auto-review-mark.py / rollover-review.py / study-coach-api.py 共用。
"""

import os
import re
from datetime import datetime, timedelta

VAULT_PATH = "/root/obsidian-vault"
KAOYAN_ROOT = os.path.join(VAULT_PATH, "考研备考")
PROGRESS_DIR = os.path.join(KAOYAN_ROOT, "进度")
REVIEW_LIST_PATH = os.path.join(KAOYAN_ROOT, "复习清单.md")

# 艾宾浩斯节点
INTERVALS = [1, 3, 7, 15, 30]
NODE_NAMES = ["D1", "D3", "D7", "D15", "D30"]
WEAK_INTERVALS = [1, 2, 4, 8, 15]  # 薄弱点减半

# 顺延策略
MAX_ROLLOVER = 3
DAILY_CAP = 8

# 科目：顶级目录名 → 短名（用于 SUBJECT_HEADERS / API subject 字段）
SUBJECT_DIR_TO_KEY = {
    "数学二": "数学",
    "408": "408",
    "英语二": "英语",
    "政治": "政治",
}
SUBJECT_KEYS = ["数学", "408", "英语", "政治"]

SUBJECT_HEADERS = {
    "数学": "## 🟥 数学二",
    "408": "## 🟦 408",
    "英语": "## 🟩 英语二",
    "政治": "## 🟨 政治",
}

# 扫描知识点时要排除的文件（basename）
EXCLUDE_FILES = {"复习清单.md", "薄弱点.md", "今日任务.md", "home.md"}
# 排除目录名（basename）
EXCLUDE_DIR_NAMES = {"进度", "复盘", "资料笔记", "真题", "assets"}
# 目录 md 通常是"XX目录.md" 或 "目录.md" → 结尾匹配
EXCLUDE_FILE_SUFFIX_RE = re.compile(r"目录\.md$")

# 章节目录前缀数字剥离，用于人类可读显示（"02-计算机组成原理" → "计算机组成原理"）
DIR_PREFIX_NUM_RE = re.compile(r"^\d+[-_.]\s*")

# 章节缩写（用于生成 path_hint 中的科目简称）
CHAPTER_SHORT = {
    "计算机组成原理": "计组",
    "计算机网络": "计网",
    "操作系统": "OS",
    "数据结构": "数构",
}


# ============= 知识点扫描 =============

class KnowledgePoint:
    """
    一个 md 文件 = 一个知识点。

    属性：
      name       文件名（不含 .md）
      rel_path   相对 vault 根的路径，如 "考研备考/408/02-计算机组成原理/05-中央处理器/指令流水线.md"
      abs_path   绝对路径
      subject    "数学" / "408" / "英语" / "政治"
      chapter    大章（如 "计算机组成原理"，已剥前缀数字）
      section    小节（如 "中央处理器"，已剥前缀数字），可能为空
      path_hint  人类可读的分组标签，如 "408·计组·中央处理器"
      pinyin_head  拼音首字母（小写），如 "zllsx"
    """
    __slots__ = ("name", "rel_path", "abs_path", "subject",
                 "chapter", "section", "path_hint", "pinyin_head")

    def __init__(self, name, rel_path, abs_path, subject,
                 chapter, section, path_hint, pinyin_head):
        self.name = name
        self.rel_path = rel_path
        self.abs_path = abs_path
        self.subject = subject
        self.chapter = chapter
        self.section = section
        self.path_hint = path_hint
        self.pinyin_head = pinyin_head

    def to_dict(self):
        return {
            "name": self.name,
            "rel_path": self.rel_path,
            "subject": self.subject,
            "chapter": self.chapter,
            "section": self.section,
            "path_hint": self.path_hint,
            "pinyin_head": self.pinyin_head,
        }


def _strip_dir_prefix(seg):
    return DIR_PREFIX_NUM_RE.sub("", seg).strip()


def _compute_pinyin_head(text):
    try:
        from pypinyin import pinyin, Style
        parts = pinyin(text, style=Style.FIRST_LETTER, errors="ignore")
        return "".join(p[0] for p in parts if p).lower()
    except Exception:
        return ""


def _build_path_hint(subject, chapter, section):
    parts = [subject]
    if chapter:
        parts.append(CHAPTER_SHORT.get(chapter, chapter))
    if section and section != chapter:
        parts.append(section)
    return "·".join(parts)


def scan_knowledge_points():
    """
    扫描 考研备考/{数学二,408,英语二,政治}/ 下所有非排除的 .md。
    返回 KnowledgePoint 列表。
    """
    result = []
    if not os.path.isdir(KAOYAN_ROOT):
        return result

    for subj_dir, subj_key in SUBJECT_DIR_TO_KEY.items():
        subj_root = os.path.join(KAOYAN_ROOT, subj_dir)
        if not os.path.isdir(subj_root):
            continue

        for dirpath, dirnames, filenames in os.walk(subj_root):
            # 就地剪枝
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_NAMES]

            for fname in filenames:
                if not fname.endswith(".md"):
                    continue
                if fname in EXCLUDE_FILES:
                    continue
                if EXCLUDE_FILE_SUFFIX_RE.search(fname):
                    continue

                abs_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(abs_path, VAULT_PATH)
                name = fname[:-3]  # 去 .md

                # 拆章节：相对 subj_root 的路径分段
                rel_to_subj = os.path.relpath(abs_path, subj_root)
                parts = rel_to_subj.split(os.sep)
                # parts[-1] 是 filename，前面是目录段
                dir_parts = parts[:-1]

                chapter = _strip_dir_prefix(dir_parts[0]) if len(dir_parts) >= 1 else ""
                section = _strip_dir_prefix(dir_parts[1]) if len(dir_parts) >= 2 else ""

                path_hint = _build_path_hint(subj_key, chapter, section)
                py_head = _compute_pinyin_head(name)

                result.append(KnowledgePoint(
                    name=name,
                    rel_path=rel_path.replace(os.sep, "/"),
                    abs_path=abs_path,
                    subject=subj_key,
                    chapter=chapter,
                    section=section,
                    path_hint=path_hint,
                    pinyin_head=py_head,
                ))

    return result


def search_knowledge_points(query, subject=None, limit=30, all_kps=None):
    """
    模糊搜索。匹配策略（按优先级降序）：
      1. name 完全等于 query
      2. name 以 query 开头
      3. name 包含 query (子串)
      4. pinyin_head 完全等于 query
      5. pinyin_head 以 query 开头
      6. pinyin_head 包含 query

    subject 传入时按科目过滤。
    """
    if all_kps is None:
        all_kps = scan_knowledge_points()

    q = (query or "").strip().lower()
    if not q:
        pool = all_kps
        if subject:
            pool = [k for k in pool if k.subject == subject]
        return pool[:limit]

    scored = []
    for kp in all_kps:
        if subject and kp.subject != subject:
            continue
        name_low = kp.name.lower()
        py = kp.pinyin_head

        score = 0
        if name_low == q:
            score = 100
        elif name_low.startswith(q):
            score = 80
        elif q in name_low:
            score = 60
        elif py and py == q:
            score = 50
        elif py and py.startswith(q):
            score = 40
        elif py and q in py:
            score = 25

        if score > 0:
            # 短名称优先（越短相关度越高）
            score += max(0, 20 - len(kp.name))
            scored.append((score, kp))

    scored.sort(key=lambda t: (-t[0], t[1].name))
    return [kp for _, kp in scored[:limit]]


def find_kp_by_rel_path(rel_path, all_kps=None):
    """按精确 rel_path 查找。"""
    if all_kps is None:
        all_kps = scan_knowledge_points()
    rel_norm = rel_path.replace(os.sep, "/")
    for kp in all_kps:
        if kp.rel_path == rel_norm:
            return kp
    return None


# ============= 复习清单 v2 解析器 =============

class ReviewEntry:
    """
    一条复习条目。核心字段：
      checked   [x] / [ ]
      name      知识点显示名（双链的显示部分或文件名）
      rel_path  完整相对路径（双链的目标），主键
      path_hint 人类可读分组（写入文件用）
      subject   数学 / 408 / 英语 / 政治（内存字段，从 rel_path 推导）
      birth     🌱MM-DD
      done_marks [(D1, 06-30), ...]
      current   当前节点 [Dn]
      due       到期 MM-DD
      rolled    ⏭️延N
      weak      ⚠️
      archived_full_date  归档 ✅YYYY-MM-DD（仅归档区条目）
      raw       原始行（含 - [ ] ...）
      legacy_v1 若为旧版格式解析出来的兼容条目 → True
      legacy_unit_id / legacy_book  旧版字段
    """
    __slots__ = ("checked", "name", "rel_path", "path_hint",
                 "subject", "birth", "done_marks", "current", "due",
                 "rolled", "weak", "archived_full_date", "raw",
                 "legacy_v1", "legacy_unit_id", "legacy_book")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        if self.done_marks is None:
            self.done_marks = []
        if self.rolled is None:
            self.rolled = 0
        if self.checked is None:
            self.checked = False
        if self.weak is None:
            self.weak = False
        if self.legacy_v1 is None:
            self.legacy_v1 = False


# 新格式：- [ ] [[<rel_path>|<name>]] · <path_hint> 🌱06-29 [D7] 到期07-06
# 或   ：- [ ] [[<name>]] · <path_hint> 🌱06-29 [D7] 到期07-06     （无路径时按 name 反查）
ENTRY_V2_RE = re.compile(
    r"^- \[([ xX])\]\s+\[\[([^\]]+?)\]\]\s+·\s+(.+?)\s+🌱(\d{2}-\d{2})(.*)$"
)

# 旧格式（v1）：- [ ] 5.5 极限存在准则 · 武忠祥高数强化 🌱06-29 ...
ENTRY_V1_RE = re.compile(
    r"^- \[([ xX])\]\s+(\S+)\s+(.+?)\s+·\s+(.+?)\s+🌱(\d{2}-\d{2})(.*)$"
)


def _parse_tail(tail):
    """从 tail 中提取 done_marks / current / due / rolled / weak / archived_full_date"""
    done_marks = re.findall(r"✔(D\d+)\((\d{2}-\d{2})\)", tail)
    cur_m = re.search(r"\[(D\d+)\]", tail)
    current = cur_m.group(1) if cur_m else None
    due_m = re.search(r"到期(\d{2}-\d{2})", tail)
    due = due_m.group(1) if due_m else None
    roll_m = re.search(r"⏭️延(\d+)", tail)
    rolled = int(roll_m.group(1)) if roll_m else 0
    weak = "⚠️" in tail
    archived_m = re.search(r"✅(\d{4}-\d{2}-\d{2})", tail)
    archived = archived_m.group(1) if archived_m else None
    return {
        "done_marks": done_marks,
        "current": current,
        "due": due,
        "rolled": rolled,
        "weak": weak,
        "archived_full_date": archived,
    }


def _subject_from_rel_path(rel_path):
    """从 rel_path 首段推 subject key。默认 '408'。"""
    parts = rel_path.split("/")
    # 考研备考/<顶级目录>/...
    if len(parts) >= 2 and parts[0] == "考研备考":
        return SUBJECT_DIR_TO_KEY.get(parts[1], parts[1])
    return ""


def parse_link_content(link_body):
    """
    解析双链内部的字符串：
      "考研备考/.../指令流水线|指令流水线" → (rel_path_no_ext, alias)
      "指令流水线"                          → (None, "指令流水线")

    返回 (rel_path or None, alias)。rel_path 若不含 .md 会自动补。
    """
    if "|" in link_body:
        target, alias = link_body.split("|", 1)
    else:
        target = link_body
        alias = link_body

    target = target.strip()
    alias = alias.strip()

    # 如果 target 包含 / 视为路径
    if "/" in target:
        if not target.endswith(".md"):
            target = target + ".md"
        return target, alias
    return None, alias


def parse_review_line(line):
    """
    尝试解析一行。返回 ReviewEntry 或 None。
    """
    # 先试 v2
    m = ENTRY_V2_RE.match(line)
    if m:
        check, link_body, path_hint, birth, tail = m.groups()
        rel_path, name = parse_link_content(link_body)
        tail_info = _parse_tail(tail)
        subject = _subject_from_rel_path(rel_path) if rel_path else ""

        return ReviewEntry(
            checked=(check.lower() == "x"),
            name=name,
            rel_path=rel_path,  # 可能是 None（只写了 [[name]]）
            path_hint=path_hint.strip(),
            subject=subject,
            birth=birth,
            raw=line,
            legacy_v1=False,
            **tail_info,
        )

    # 再试 v1（兼容旧格式）
    m = ENTRY_V1_RE.match(line)
    if m:
        check, unit_id, title, book, birth, tail = m.groups()
        tail_info = _parse_tail(tail)
        return ReviewEntry(
            checked=(check.lower() == "x"),
            name=title.strip(),
            rel_path=None,
            path_hint=book.strip(),
            subject="",  # 旧格式没法直接推
            birth=birth,
            raw=line,
            legacy_v1=True,
            legacy_unit_id=unit_id,
            legacy_book=book.strip(),
            **tail_info,
        )

    return None


def render_review_entry(entry):
    """
    输出到文件的一行。始终使用 v2 格式（旧数据用户已同意清空）。
    """
    check = "x" if entry.checked else " "

    # 双链
    if entry.rel_path:
        rp = entry.rel_path
        if rp.endswith(".md"):
            rp = rp[:-3]
        link = f"[[{rp}|{entry.name}]]"
    else:
        link = f"[[{entry.name}]]"

    parts = [f"- [{check}] {link} · {entry.path_hint}", f"🌱{entry.birth}"]
    for node, date in entry.done_marks:
        parts.append(f"✔{node}({date})")
    if entry.current:
        parts.append(f"[{entry.current}]")
    if entry.due:
        parts.append(f"到期{entry.due}")
    if entry.rolled and entry.rolled > 0:
        parts.append(f"⏭️延{entry.rolled}")
    if entry.weak:
        parts.append("⚠️")
    if entry.archived_full_date:
        parts.append(f"✅{entry.archived_full_date}")
    return " ".join(parts)


# ============= 复习清单文件读写 =============

def read_review_list():
    if not os.path.exists(REVIEW_LIST_PATH):
        return ""
    with open(REVIEW_LIST_PATH, "r", encoding="utf-8") as f:
        return f.read()


def write_review_list(content):
    with open(REVIEW_LIST_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def parse_review_list(content):
    """
    按 section 解析。返回：
      {
        "header_lines": [...],
        "sections": {"数学": [entry, ...], "408": [...], ...},
        "archived_lines": [line, ...]   # 归档区原样保留（每行文本）
      }
    section 里的 entry.subject 若为空，用当前 section 名回填。
    """
    lines = content.split("\n")
    result = {
        "header_lines": [],
        "sections": {k: [] for k in SUBJECT_KEYS},
        "archived_lines": [],
    }
    section_by_header = {v: k for k, v in SUBJECT_HEADERS.items()}

    current_section = None
    in_archived = False

    for line in lines:
        if line.strip() == "## ✅ 已掌握":
            in_archived = True
            result["archived_lines"].append(line)
            continue
        if in_archived:
            result["archived_lines"].append(line)
            continue

        if line in section_by_header:
            current_section = section_by_header[line]
            continue

        if current_section is None:
            result["header_lines"].append(line)
        else:
            entry = parse_review_line(line)
            if entry:
                if not entry.subject:
                    entry.subject = current_section
                result["sections"][current_section].append(entry)
            # 空行/其他 → 忽略

    return result


def render_review_list(parsed):
    """把解析结果写回文本"""
    out = list(parsed["header_lines"])
    while out and out[-1] == "":
        out.pop()
    out.append("")

    for subj in SUBJECT_KEYS:
        out.append(SUBJECT_HEADERS[subj])
        out.append("")
        for entry in parsed["sections"][subj]:
            out.append(render_review_entry(entry))
        out.append("")

    out.append("---")
    out.append("")
    out.extend(parsed["archived_lines"])

    return "\n".join(out).rstrip() + "\n"


# ============= 便捷函数 =============

def make_entry_from_kp(kp, today, weak=False):
    """
    根据 KnowledgePoint 生成一条新的 D1 起始 entry。
    """
    return ReviewEntry(
        checked=False,
        name=kp.name,
        rel_path=kp.rel_path,
        path_hint=kp.path_hint,
        subject=kp.subject,
        birth=today.strftime("%m-%d"),
        done_marks=[],
        current="D1",
        due=(today + timedelta(days=1)).strftime("%m-%d"),
        rolled=0,
        weak=weak,
        archived_full_date=None,
        raw="",
        legacy_v1=False,
    )


def find_entry_by_rel_path(parsed, rel_path):
    """在活跃 sections 中查条目（rel_path 精确匹配）"""
    for subj in SUBJECT_KEYS:
        for e in parsed["sections"][subj]:
            if e.rel_path == rel_path:
                return subj, e
    return None, None


def find_entry_by_name(parsed, name):
    """在活跃 sections 中按 name 查（可能多条），返回列表 [(subj, entry), ...]"""
    hits = []
    for subj in SUBJECT_KEYS:
        for e in parsed["sections"][subj]:
            if e.name == name:
                hits.append((subj, e))
    return hits


if __name__ == "__main__":
    print("=== 知识点扫描 ===")
    kps = scan_knowledge_points()
    print(f"共 {len(kps)} 个知识点")
    print("\n各科目：")
    from collections import Counter
    for subj, cnt in Counter(k.subject for k in kps).items():
        print(f"  {subj}: {cnt}")

    print("\n=== 搜索 '流水线' ===")
    for kp in search_knowledge_points("流水线", limit=5):
        print(f"  {kp.name}  ({kp.path_hint})  → {kp.rel_path}")

    print("\n=== 拼音搜索 'zllsx' ===")
    for kp in search_knowledge_points("zllsx", limit=5):
        print(f"  {kp.name}  ({kp.path_hint})  → {kp.rel_path}")

    print("\n=== 复习清单解析 ===")
    parsed = parse_review_list(read_review_list())
    for subj in SUBJECT_KEYS:
        for e in parsed["sections"][subj]:
            tag = "[v1兼容]" if e.legacy_v1 else "[v2]"
            print(f"  {subj} {tag} {e.name} · {e.path_hint} [{e.current}] 到期{e.due}")
