# 考研看板

2027 考研进度追踪 + Obsidian 知识库同步。`hyh36.xyz/kanban/`。

## 功能

- 进度追踪（数二/408/英二/政治）
- 今日任务勾选回写 Obsidian
- 艾宾浩斯复习清单（D1/D3/D7/D15/D30）
- 知识点搜索
- OSS 双向同步（ossutil）
- 状态页 `/kanban/status/`（同步日志、服务健康）

## 架构

- **后端**：`backend/study-coach-api.py`（Python stdlib，`http.server`）
- **端口**：127.0.0.1:8791
- **服务**：systemd `study-coach-api.service`
- **前端**：`frontend/index.html`（原生 JS + Mermaid + KaTeX）
- **数据源**：`/root/obsidian-vault/考研备考/`（Obsidian vault）

## 相关脚本

`backend/` 里附带：
- `scan-progress.py` — 扫进度文件（供 API 调用）
- `add-review.py` / `rollover-review.py` / `auto-review-mark.py` — 复习清单维护
- `review_common.py` — 复习清单解析共用模块

## 关联的 skill

`~/.hermes/profiles/general/skills/study-coach/` — coordinator。里面的 `scripts/` 是软链，指向 `/root/projects/study-coach-kanban/backend/`。

## 同步 cron

`0 * * * *` → `/root/bin/vault-with-lock.sh`（flock 120s，与 API 共享 `/var/lock/study-coach.lock`）

## 备份

`.backups/` — 历史 index.html 和 API 备份。
