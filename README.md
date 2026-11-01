# A500 T-Trader

用于查询标准化行情数据和订阅 tgw 推送的 Python 包。本项目不提供交易策略、下单功能，也没有可交互的命令行工具。
安装与配置、API 示例和数据语义请参阅[使用指南](docs/user-guide.md)。

## 开发

需要 Python 3.14 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --locked
```

运行全部质量检查并执行包入口命令：

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python -m a500_t_trader
```

运行冒烟测试：

```bash
uv run pytest
```

## 使用

- `AmazingDataProvider` 和 `MarketDataService` 提供按需快照及分钟线查询。
- `TgwPushProvider` 适配调用方已初始化的 tgw API 对象，接收快照、分钟线和逐笔成交推送。

这两种用法的配置要求和数据交付方式不同。安装前提、配置、示例、能力限制、错误处理和订阅清理方式请参阅[使用指南](docs/user-guide.md)。

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
