# 项目依赖

项目运行时直接依赖 `AmazingData` 和 `tgw`，通过仓库 GitHub Release 附件提供
wheel；无需额外安装其他项目依赖，也不提供独立的 `pip install` 安装流程。
`uv sync --locked` 会根据锁文件安装这两个 wheel 及其所需依赖。

首次同步前，在仓库根目录创建 `wheels/` 并从
[`deps-v1` Release](https://github.com/drakeqiu/a500-t-trader/releases/tag/deps-v1)
下载 wheel。需要安装 GitHub CLI（`gh`）：

```bash
mkdir -p wheels
gh release download deps-v1 \
  --repo drakeqiu/a500-t-trader \
  --pattern "*.whl" \
  --dir wheels
```

构建项目 wheel（每次修改后端代码后重新构建）：

```bash
uv build --wheel
```

生成的 `dist/a500_t_trader-*.whl` 会由 `Dockerfile` 安装。镜像内使用
`pip install` 安装项目 wheel 和其声明的运行依赖；这些依赖不会被自动打包进
项目 wheel。供应商 wheel 从 `wheels/` 查找，其他依赖仍从 PyPI（或配置的镜像源）
下载。镜像构建前必须先生成项目 wheel。

直接执行 `docker compose build` 或 `podman-compose build` 时，必须先运行
`uv build --wheel`；`scripts/start-local.py` 和 `scripts/start-lan-replay.py`
会自动执行该命令。

本地启动脚本和 Compose E2E 脚本会默认使用 Aliyun PyPI 镜像（`UV_INDEX_URL`）
和 BFSU APT 镜像（`APT_MIRROR`）；非空环境变量可覆盖默认值，空值仍回退到默认镜像。
