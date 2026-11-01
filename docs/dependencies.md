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
