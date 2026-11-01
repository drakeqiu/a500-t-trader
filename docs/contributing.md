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
在已配置 AmazingData 账号和连接权限的环境中，可显式运行实时连接、`563360` 快照和分钟线检查：

```bash
uv run pytest -m live
```

实时测试不会跳过缺失配置、连接失败、权限不足或必要行情数据缺失的情况；这些问题会使测试失败。

运行 `npm run format` 可格式化 JSON、YAML 和 TOML 文件。

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

GitHub Actions 会在推送和拉取请求时运行代码检查、格式检查、严格模式的 mypy
和 pytest。持续集成会使用 `uv sync --locked` 安装锁定的依赖环境。

## 自动合并拉取请求

为目标分支是默认分支的拉取请求添加 `automerge` 标签，即可启用 GitHub 的
压缩提交合并（squash）。移除该标签会取消待处理的自动合并。

自动合并仅管理来自本仓库分支的拉取请求；来自派生仓库（fork）的拉取请求会被忽略。

要启用此功能，先在仓库设置中开启自动合并，再配置默认分支的保护规则或
规则集，要求所需的状态检查、至少一项审批，以及分支保持最新。GitHub
自动合并会等待这些条件满足后再执行合并。
