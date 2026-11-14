# 仓库脚本不全 / 缺参数怎么补

症状（任一）：

- 跑阶段 3/5 报 `unrecognized arguments: --limit-merge` 或 `--split-by-section`。
- `bootstrap.py --check` 报缺 `multi_agent/chapter_pipeline.py`、`md/知识点总结提示词V5.1.md` 等。
- 流水线起不来，提示 `multi_agent.prompts` 或某个 md 提示词不存在。

原因：skill 只带了自己的 `scripts/`，仓库侧脚本（`multi_agent/`、`md/` 提示词、`补题/` 脚本、阶段 2 转换脚本）在对方仓库里可能缺失或版本偏旧。包里带了 `repo-addons/`，一条命令补齐。

## 1. 先看差异（不改盘）

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\install.py --dry-run
```

输出 `ADD`（缺）、`REPLACE`（版本不同）、`SAME`（已一致）三类清单。

## 2. 确认后执行

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\install.py
```

- 被替换的原文件先备份到 `.book-to-question-backup\install-<时间戳>\`，保持仓库相对路径。
- 幂等：再跑一次应全为 `SAME`。
- `--with-env` 可在目标缺 `.env` 时从 `.env.example` 生成一份（仍需自己填 key）。

## 3. 装完自检 / 取命令

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\preflight.py
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\preflight.py --emit detail
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\preflight.py --emit compact
```

`--emit` 只拼**本仓库实际支持**的参数：老流水线不会带 `--limit-merge`，省得你再撞 `unrecognized arguments`。

## 4. KaTeX 渲染检查（可选依赖）

`check_katex.py`（阶段 3 详细版、阶段 4 题目都会用到）需要 node 和 KaTeX：

```bash
npm i katex
```

也可以把已有包目录写进 `KATEX_PATH` 或传 `--katex-path`。找不到时脚本以 2 退出，批量流程会打印「跳过 KaTeX 渲染检查」，不影响其他校验。

解析错误的模型兜底 `fix_render_llm.py` 还需要 `.env` 里的 `DEEPSEEK_API_KEY`（可选 `DEEPSEEK_API_BASE`、`DEEPSEEK_MODEL`，默认 `deepseek-v4-flash`）；没有 key 时会直接退出，不会动文件。

## 回滚

把 `.book-to-question-backup\install-<时间戳>\` 里的文件按原路径拷回去即可。只想回滚某个文件就只拷那一个。
