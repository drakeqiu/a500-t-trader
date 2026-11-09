# 开发与贡献

## 开发环境

需要 Python 3.14、Node.js 24、[uv](https://docs.astral.sh/uv/) 和 GitHub CLI
（`gh`）。首次设置开发环境时，先按[项目依赖说明](dependencies.md)下载所需
wheel。

下载完成后运行以下命令安装锁定的开发环境：

```bash
uv sync --locked
```

安装 Node.js 工具依赖：

```bash
npm ci
```

运行质量检查并执行包入口命令：

```bash
uv run ruff check .
uv run ruff format --check .
uv run yamllint --strict .github
npm run format:check
npx --yes markdownlint-cli2
npx --yes prettier --check "**/*.md"
uv run mypy
uv run pytest
uv run python -m a500_t_trader
```

普通 `uv run pytest` 会排除需要真实 provider 的 `live` 测试，fixture 驱动的测试仍会正常运行。
数据库相关 Python 测试需设置 `TEST_DATABASE_URL`，指向可用的 PostgreSQL 测试数据库；每个测试会创建并删除独立 schema。前端 Playwright 使用本地 OIDC 与内存 REST/SSE API mock，不启动业务后端，也不连接 PostgreSQL 或 Redis。
Starlette 测试客户端使用开发依赖 `httpx2`；`uv sync --locked` 会一并安装。
在已配置 AmazingData 账号和连接权限的环境中，可显式运行实时连接、`563360` 快照和分钟线检查。将 `AD_USERNAME`、`AD_PASSWORD`、`AD_HOST`、`AD_PORT` 配置在受保护的环境或仓库外 `.env` 文件中后运行：

```bash
uv run pytest -m live
```

实时测试不会跳过缺失配置、连接失败、权限不足或必要行情数据缺失的情况；这些问题会使测试失败。

运行 `npm run format` 可格式化 JSON、YAML 和 TOML 文件。

GitHub Actions 分别按 `frontend/bun.lock` 和 `uv.lock` 缓存前端与 Python
Playwright 浏览器；锁文件变更会触发重新下载。每次运行仍会检查和安装系统依赖，
缓存未命中时会自动安装 Chromium。

## Git 提交规范

本仓库使用 Conventional Commits 提交模板。执行 `git commit` 时，Git 会读取
`.gitmessage`：

```text
type(scope): description
```

常见类型包括 `feat`、`fix`、`docs`、`refactor`、`test`、`chore` 和 `ci`。
`scope`、正文和脚注均为可选项。可在冒号前添加 `!`，或使用
`BREAKING CHANGE:` 脚注标记不兼容变更。可按需修改模板；以 `#` 开头的行仅作
提示，不会写入提交消息。

在新克隆的仓库中启用此模板：

```bash
git config --local commit.template .gitmessage
```

此配置仅对当前克隆生效，不会通过仓库共享。

## 持续集成

GitHub Actions 将 Python/文档检查与前端检查并行运行；前端 job 执行 lint、
格式检查、Vitest 单元测试和 Playwright E2E 测试。推送新提交会取消同一分支或
拉取请求中已过期的 CI 运行。CI 使用 `uv sync --locked` 和 Bun 锁文件安装依赖。

Compose E2E 将 RabbitMQ 行情验收和 Keycloak 设备授权验收放在独立 runner 上并行执行，
不缩减验收覆盖。两者都成功后，汇总检查 `Market data and Keycloak E2E` 才通过；
任一失败、取消或跳过都不会放行。Compose E2E 显式使用官方 APT/PyPI 源，避免脚本继承
本地开发的镜像默认值；两个 job 复用 uv 下载缓存。

## 自动合并拉取请求

面向 `main` 的拉取请求在创建、重新打开、推送新提交或修改目标分支时，会自动
添加 `automerge` 标签。重新打开或推送后会重新评估自动合并；移除标签会取消
待处理的自动合并，下一次重新打开或推送会再次启用。

自动合并仅管理来自本仓库分支的非草稿拉取请求；fork PR 只添加标签，不自动合并。
工作流只读取 GitHub 元数据，不检出或执行 PR 代码。每次事件都以当前 PR 状态为准，
并要求同一 HEAD 的 Python/文档检查、前端检查和测试、Market data and Keycloak
E2E 三项检查全部成功，才请求 GitHub 启用压缩提交自动合并（squash）。
失败、取消、跳过、缺失或仍在运行的检查都会阻止启用；检查完成后通过
`workflow_run` 再次评估，不再占用 runner 长时间轮询。

仍应在默认分支的保护规则或规则集中要求这些状态检查、至少一项审批和分支保持最新。
这样可确保 GitHub 只在保护规则满足时合并，并防止仓库设置变化绕过所需检查。
