# Quick Pick 当前业务合同（2026-09-10）

业务后台为同级 `fast-sort-global`；当前 App 为 `quick-pick-ios` 和 `quick-pick-android`。`rapid-sorting-global` 不作为新 App 的接口事实来源。

业务后台获取 TikTok 资料走 `GET /internal/rooms/{username}`，使用 `X-Live-Service-Key`，其值由部署环境 `LIVE_INTERNAL_API_KEY` 提供。用户名采用双方一致的规范，响应只包含当前资料字段（`username`、`nickname`、`avatarUrl`、`roomId`、`live`），不抓 HTML 或尝试旧字段。该接口只应向业务服务器内网开放。404 表示账号不存在，503 表示上游资料暂时不可用。

现有 `/ws/{username}` 地址保持不变。`CONNECTING` / `LIVING` 表示连接状态；`UPSTREAM_TIMEOUT` / `UPSTREAM_RECONNECTING` 表示由服务端处理的临时上游问题；`LIVE_CONNECT_ERROR`、`STOPPED`、`3` 表示终止。暂停为 `1`，恢复为 `2`。客户端不应在临时上游重连时反复创建房间订阅。

业务 Redis 标签使用 `orderUser:dy_room_id_user:{roomId}:{buyerId}`，黑名单使用 `black:{buyerId}`，内容为 UTF-8 普通 JSON。字段类型必须明确，`createdUsers` 为字符串数组，不能写入 Java `@class`、嵌套 ArrayList 包装或二次 JSON 编码。Redis 中的 `orderNameId` 必须匹配消息的 `danmuUserId`。

业务后台维护 `quick_pick:metadata_ready:v1`（值为 `1`，30 秒就绪租约），并在数据丢失后重建缓存。Redis 必须采用 `noeviction`，两服务共享同一业务逻辑库。每条评论在一个 Redis 事务中读取标签、黑名单和就绪标记；只有完整读取且就绪时，缺失买家键才表示“新买家/未拉黑”。不可用、正在重建、畸形数据、错误身份或 Redis 超时均使 `metadataAvailable=false`，不会发布部分标签或默认黑名单等级。评论仍继续发送，App 应暂停该评论的自动打印。状态变化另发 `METADATA_UNAVAILABLE` / `METADATA_READY` 供显示。

这是新三端同步采用的合同，不兼容旧后台缓存或旧移动端的隐式授权行为。部署后应分别检查业务后台、Python 服务和双端实际版本；代码更新不代表部署已完成。

本地测试：`python -m pytest -q`。`tests/local_contract_server.py` 是仅供本地联调的测试入口，使用生产 ASGI、房间管理和 Redis 补充链路，替换 TikTok 外部上游；`/__contract/comment` 与 `/__contract/config` 只存在于该测试入口，不能作为部署入口。完整本地链路及复跑命令见 `../fast-sort-global/docs/2026-09-10-cross-service-validation.md`。本轮测试不等于真实 TikTok 账号、Shopee 服务或打印硬件验收。
