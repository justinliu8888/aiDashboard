# AGENTS.md

本文件是本仓库的 agent 约定。工程技能（`to-spec`、`to-tickets`、`triage`、`tdd`、`code-review` 等）会读取这里描述的配置。

## Agent skills

### Issue tracker

Issues 与规格存放在本仓库的 GitHub Issues，用 `gh` CLI 操作。See `docs/agents/issue-tracker.md`.

### Triage labels

五个分类角色对应标签 `needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。See `docs/agents/triage-labels.md`.

### Domain docs

单上下文布局：`GLOSSARY.md` 与 `docs/adr/` 都在仓库根。See `docs/agents/domain.md`.
