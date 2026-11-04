# TGW 运行与恢复能力验证

## 证据范围

结论依据仓库 `wheels/tgw-1.0.9.2-py3-none-any.whl`、当前 adapter `src/a500_t_trader/market_data/providers/tgw_push.py` 及 `tests/test_subscriptions.py`。未使用真实 TGW 账号、证书、授权或行情连接；mock 测试不证明生产环境能力。

## 可确认能力

- wheel metadata 标注 TGW 1.0.9.2、依赖 pandas、许可标识 `chinastock_tgw_sdk_python`，但未包含许可条款、授权办法或网络配置。SDK wheel 提供 Linux Python 3.14 x86_64 对应绑定；通用 wheel 标签不代表任意容器镜像兼容。
- SDK `Login(config, api_mode, path='')` 调用 `IGMDApi_Init`，返回 True/False。异步登录失败原因需由 log SPI 查看；`SetLogSpi` 可配置此观察面。`TmpPushSpi.OnEvent(level, code, event_msg, len)` 将事件转交日志 SPI。
- SDK 事件码包含 UMS/channel TCP 连接或登录成功/失败、heartbeat timeout、session closed、reconnect failed 等状态。它们提供候选观测信号，不证明 SDK 会自动重连、事件必达或订阅自动恢复。
- `Subscribe` 返回错误码；adapter 将抛错或非零返回视为订阅调用失败。订阅调用成功不能证明登录完成或行情回调已恢复。
- TGW `TGWTickExecution` 暴露 `channel_no`、`appl_seq_num`、`md_stream_id`、`biz_index` 等字段；SDK 代码未承诺其唯一范围、重连连续性或不复用。当前项目 `Trade` adapter 未保留这些字段，故契约没有已证实的可靠来源事件 ID。若以后暴露候选去重键，必须标为尽力而为，直到供应商文档或实测证实其语义。

## 未验证的运行约束

尚未验证 TGW 容器镜像的系统库兼容性、运行时授权校验、服务地址/端口、防火墙/代理/专线规则、证书挂载与续期、账号权限与订阅上限，也未验证断线后自动重连、订阅保留、遗漏行情补发或可靠逐笔标识。SDK 的平台绑定与 Python 3.14 支持不足以构成容器可运行保证。部署不得将 TGW 采集器容器化标为已验证能力。

## 生命周期实现约束

采集角色应注册日志/事件 SPI，明确记录 SDK 初始化/登录/连接事件与订阅调用结果。连接状态与订阅状态分别跟踪；只有连接/登录已确认、所有配置订阅意图重新提交成功且收到至少一个行情回调，才报告数据流已恢复。采集器在订阅调用成功但尚未收到行情回调时报告 `awaiting_data`，收到回调后才将订阅状态报告为 `subscribed`。断线/心跳超时应使行情状态失效，重连采用限速退避；SDK 是否自动重连及订阅保持能力未确认前，不得把“已连接”当作订阅或数据流恢复。重启仅恢复配置中的订阅意图，不恢复进程内易失行情状态。

要把未验证项提升为部署保证，需由有 TGW 账号、许可证、证书和网络权限的环境完成真实容器连通性，以及登录、断线、心跳超时、恢复和逐笔序号跨重连行为验证。
