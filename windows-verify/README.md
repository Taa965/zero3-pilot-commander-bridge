# Windows 验证通道（windows-verify）

网页 GPT 等外部 AI 指挥官可通过本通道要求 **Windows 开发机**（Hermes 所在机器）执行环境验证任务，并回读结构化结果。

## 目录布局（本仓库内，与 commands/ 平行，互不干扰）

```
windows-verify/
  pending/<id>.json        # 指挥官写入：待执行命令
  running/<id>.json        # 执行器写入：已认领（原子移动）
  done/<id>.json           # 执行器写入：已完结（含结果）
  results/<id>.json        # 执行器写入：完整结果信封
  events/audit.jsonl       # 追加式审计日志（每动作一行，先写后执行）
```

## 命令信封（指挥官写入 pending/<id>.json）

```json
{
  "id": "wv-20260901-001",
  "type": "shell",
  "command": "python -m pytest tests/ -x -q",
  "workdir": "C:/Users/aaaa/Documents/zero3-pilot",
  "timeout_s": 300,
  "requested_by": "gpt-web",
  "created_at": "2026-09-01T12:00:00+08:00"
}
```

字段规则：
- `id`：全局唯一，建议格式 `wv-YYYYMMDD-序号`；重复 id 会被拒绝（结果中 status=error）。
- `type`（白名单，四选一）：
  - `shell` — 执行 `command`（无 shell 注入顾虑，Windows 上经 bash 执行，POSIX 语法）
  - `file_check` — 检查 `paths` 数组中每个路径的存在性/大小/mtime
  - `git_status` — 对 `workdir` 输出 `git status --porcelain` 与最近 5 条 `git log --oneline`
  - `flutter_analyze` — 在 `workdir` 执行 `flutter analyze`（机器已装 Flutter）
- `timeout_s`：上限 1800，超出按 timeout 结束并记录。
- `workdir`：必须位于 **允许目录白名单** 内（见下）。

## 允许目录白名单

| 前缀 | 用途 |
|---|---|
| `C:/Users/aaaa/Documents/` | 主要项目与仓库 |
| `C:/Users/aaaa/Desktop/0/` | 桌面项目快照 |

白名单之外的 `workdir` 一律拒绝（status=error, reason=workdir_not_allowed）。

## 结果信封（执行器写入 results/<id>.json，并在 done/ 留同 id 标记）

```json
{
  "id": "wv-20260901-001",
  "status": "ok",
  "exit_code": 0,
  "stdout_tail": "…最后 4000 字符…",
  "stderr_tail": "…",
  "started_at": "2026-09-01T12:01:00+08:00",
  "finished_at": "2026-09-01T12:01:23+08:00",
  "duration_s": 23.4,
  "artifacts": [],
  "executor": "hermes-windows-1",
  "notes": ""
}
```

- `status`：`ok` / `fail`（exit_code≠0）/ `timeout` / `error`（信封不合法、白名单拒绝等，附 `reason`）。
- `stdout_tail`/`stderr_tail` 各截断至 4000 字符，完整输出不上传（太大时执行器存本机并在 `notes` 注明路径）。

## 指挥官如何取结果

直接读 `windows-verify/results/<id>.json`（GitHub 网页或 API 均可）。`running/` 有文件 = 已认领执行中；`done/` 有文件 = 已完结。

## 执行器纪律（Hermes Windows 侧）

1. 先写审计日志，后执行（durable-first）。
2. 单条命令独立子进程 + 超时强杀。
3. 不使用 force-push；push 冲突时 fetch/rebase 重试，仍失败则保留本地结果等下一轮。
4. 凭证只走本机 gh CLI 登录态，绝不写进仓库。
5. 每 3 分钟一轮（cronjob），空闲轮零提交零通知。
