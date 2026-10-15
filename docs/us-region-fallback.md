# TikTok 美国地区解析兜底

当前部分公开账号在印尼或其他地区的用户名查询返回 `19881007 / user_not_found`，美国出口能得到真实资料。共享采集保持原查询优先；仅此错误触发可选美国解析，返回的昵称、头像和实时状态沿用现有内部接口。

## 代码与配置

- `live_service/upstream/regional_bootstrap.py` 管理独立地区会话。默认路径成功、已下播、HTTP 403、权限限制、429 或网络错误不触发地区轮换。
- 配置 `LIVE_TIKTOK_US_PROXY_URL` 才启用；不配置时直接使用原 `AnonymousBootstrap`。
- 两次查询和原会话准备共用原 8 秒预算。美国会话的 Retry-After 只影响需要该地区的房间，原出口的全局限流继续保留。
- 美国响应只提供当前资料和场次；WebSocket 沿用原出口及原匿名 Cookie。进入直播仍重新检查直播状态，不固化历史场次、不用账号登录 Cookie。
- 代理凭据只保存到服务器私有配置；日志、内部解析响应和 Git 均不得包含代理口令或匿名 Cookie。

## 美国出口

可通过固定 HTTP/HTTPS/SOCKS5 代理配置。当前接入方案使用用户已授权的现有美国节点，在生产机运行独立 Mihomo；只绑定 `127.0.0.1:17897`，启用本地代理鉴权，关闭 TUN、控制接口及 DNS 接管，仅允许 TikTok 和出口地区验证地址，其余目标拒绝。TLS 验证保持开启。

`deploy/tiktok-us-egress.service` 使用 DynamicUser、systemd credentials 和独立状态目录。节点配置位于 `/etc/tiktoklive-us-egress/proxy.json`，解析环境位于 `/etc/tiktoklive-us-egress/resolver.env`，均为 root 私有文件。节点过期或订阅变化需要维护此独立配置，不依赖维护人员电脑开机，也不会改变主机其他服务的出口。

Mihomo 采用已验证的 v1.19.17 官方 Linux amd64 发行件：压缩件 SHA-256 为 `aebf089577047a957bacac5891ca7a7e2bb07d9b12f7f21a91ee0a73dd8d8606`。不得以关闭 TLS 校验处理节点连接失败。

## 发布与回退

必须蓝绿发布。旧活动槽持续服务，候选在备用槽加载该环境文件；核验节点出口国家、目标账号资料、对照直播实时弹幕、原出口故障隔离、内部接口鉴权及 FP/LT 与快拣两系统的入口后才切换 Nginx 上游。Nginx 平滑 reload 后旧 WebSocket 仍由旧实例处理，确认连接排空后才可停止旧实例；保留旧制品及旧环境回退。

回退先确认旧槽健康，再将 `quick_pick_tiktok` 上游切回旧槽并平滑 reload。候选仍有连接时不提前停止。不要修改两个业务数据库、业务鉴权或 Redis 归属。

手机端和业务后端使用既有接口，无需客户端发版。Windows/macOS 桌面端本地采集不包含在此改动内。资料解析通过不等于目标实时弹幕验收通过。

2026-10-15 14:30:59（北京时间）已在共享生产采集蓝绿切至 green / 8766，应用源码 `0fb2adf`。14:19 目标 `77beans0914` 真实开播时，候选实例收到 7 条评论，其中 1 条为抵达时间差不超过 2 秒的新消息，其余 6 条为历史消息；之后目标下播，解析正确返回资料与 `live=false`，正式业务机的添加解析路径 HTTP 200。两轮切换/回退共 1,800 次 HTTP 探测零失败，已有公网 WebSocket 在切换后继续响应心跳。旧 blue 仍有订阅，保留运行；不得强停以完成排空。

私有回执及可执行回退在 `/var/backups/tiktoklive/us-fallback-20261015-0fb2adf/`。首次验收脚本因误用 `NOT_LIVING` 预期而自动切回；按现有 `LIVE_CONNECT_ERROR` 加内部 `offline` 状态修正验证后完成最终切换，应用协议未改。完整上线记录同步至 FP/LT `fast-sort-global/docs/tiktok-us-fallback-release-20261015.md` 及快拣海外服务器清单。

## 验证

`python -m pytest -q tests` 覆盖原服务协议、去重、重连、账号解析鉴权，以及地区兜底的超时、取消、限流与会话隔离。测试必须使用隔离环境，不连接生产 SQL/Redis。当前部署源码基线已从实际生产制品恢复到主线历史，发布时只将本功能相对基线的代码差异应用到新制品。
