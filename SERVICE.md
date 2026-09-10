# TikTok 弹幕服务运行说明

当前正式服务使用 PirateTok 路线的匿名、无签名连接器：向 TikTok 注册匿名会话，查询公开直播间，再直接连接 TikTok WSS。运行时不加载签名客户端，不调用 EulerStream，不需要 API Key、登录 Cookie 或浏览器。此实现仅面向国际版 TikTok。

[落地计划](plans/unsigned-production-rollout.md)记录迁移步骤；[部署目录](deploy/README.md)记录实际上线范围。仓库内保留的通用 `TikTokLiveClient` 是旧 SDK 兼容入口，正式服务不通过它采集。README 后半部分的旧 SDK 签名参数不适用于本服务。

## 业务与接口

- 启动入口保持 `examples.fastapi_ws_server:app`，单进程、端口 8765。
- 订阅 `wss://live.quick-pick.net/ws/{live_id}`，主播名可带 `@`。发送文本 `ping`，回复文本 `pong`。
- 状态保持 `CONNECTING`、`LIVING`、`LIVE_CONNECT_ERROR`。**超时状态由 `SIGN_API_TIMEOUT` 改为 `UPSTREAM_TIMEOUT`**；前端如按旧字符串判断，需要替换。不能将 `LIVING` 解释为一定已经有新评论。
- 控制事件保持原有结束 `3`、暂停 `1`、恢复 `2`、其余 `0` 的映射和顺序。
- 评论仍输出 `msgId`、`dyMsgId`、`danmuUserId`、`username`、`danmuUserName`、`danmuContent`、`dyRoomId`、`fansStatus` 和标签字段；字符串 ID、UUID、中文及原有缺省类型保持兼容。
- Redis 键仍为 `orderUser:dy_room_id_user:{dyRoomId}:{danmuUserId}` 和 `black:{danmuUserId}`。每条评论重新查询，保留旧解析规则，不缓存业务标签。查询失败只保留已成功取得的字段。
- 首次接入保留 TikTok 返回的历史评论；最后一个前端离开后立即停止采集。明确下播、未开播、账号不存在或访问被拒绝时停止重试。

## 架构

`ConnectionManager` 为每个规范化主播名维护唯一 `RoomSession`。多个前端共享一条上游连接；房间监督任务负责建立、故障恢复和关闭。

| 模块 | 职责 |
| --- | --- |
| `live_service/upstream/bootstrap.py` | 异步匿名注册、公共房间查询、共享 Cookie 缓存、HTTP 总时限和限流 |
| `live_service/upstream/protocol.py` | 无签名 WSS 参数、进入房间、心跳、ACK、有界 protobuf/gzip 解码 |
| `live_service/upstream/client.py` | 握手、收包和心跳监督、静默失联检测、取消与关闭 |
| `live_service/manager.py` | 房间唯一所有权、代次隔离、有界事件队列、重连和全局限流冷却 |
| `live_service/enrichment.py` | 异步 Redis pipeline、超时和故障降级 |
| `live_service/protocol.py` | 原业务字段与控制状态适配 |
| `live_service/delivery.py` | 每前端独立发送任务、去重、队列容量/字节数/年龄限制 |
| `live_service/app.py` | WebSocket、健康接口、启动与关闭 |

匿名 HTTP 会话在进程内共享，注册采用单次并发执行；房间查询可并发进行。Cookie 仅保留在内存，短暂重连复用，不写日志。缓存到期通过相同公开注册流程更新。429 遵守数字或 HTTP 日期格式的 `Retry-After`，缺失时冷却 60 秒；不通过轮换身份规避拒绝或限流。

握手实际完成并成功发出进入房间和心跳后才发送 `LIVING`。每 3 秒发送业务心跳，连续 8 秒没有上游帧即判定静默失联；这是传输活性检测，不以有没有观众发评论作为依据。正常明确断线首轮随机等待 0–0.2 秒；重复短连接使用指数退避。TCP keepalive 和 Linux TCP_USER_TIMEOUT 作为补充，不能代替应用层收包超时。

短断线优先复用房间 ID；缓存房间被拒绝时最多回到公开查询刷新一次。每次重连建立新客户端，旧代次事件不能写入新连接。HTTP、WSS 握手、发送、收包、清理均设边界；心跳任务失败会结束读任务，读任务失败也会取消心跳。

HTTP 响应上限 2 MiB；WSS 帧和解压后数据各限制 8 MiB。ACK 保留上游不透明 `internal_ext` 字节，不能按 UTF-8 文本重新编码。只将当前业务需要的评论和控制事件交给业务层。

## 重复弹幕、背压与恢复边界

已实测 TikTok 每次重连可能再次下发已见历史 ID。每个前端维护最多 4096 个 ID、最长 3600 秒的去重记录，同代重复和跨代重放都按房间与正数源消息 ID 抑制。相同内容但不同 ID 仍是不同评论；没有有效源 ID 的消息不去重。新前端拥有独立记录，仍能收到历史批次。

评论按房间顺序完成标签查询及序列化，再分发给前端。慢前端只阻塞自己的发送任务；超出队列、字节数或消息年龄限制时以 1013 关闭该连接，其他前端继续接收。房间队列和 HTTP 连接数有界，避免无上限创建后台任务。

去重记录不是持久化消息账本。前端重新连接、服务重启、缓存淘汰后仍可能看到历史消息；上游未重放的消息也可能在故障期间缺失。ACK 表示采集协议应答，不代表业务端已经处理。本实现不承诺端到端恰好一次或绝对零丢失。

## 安装和配置

生产环境为 Ubuntu 24.04、Python 3.12、单 worker、Nginx TLS 终止、回环地址上的应用和 Redis。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple -r requirements.lock
.venv/bin/python -m pip install --index-url https://pypi.org/simple --no-deps -e '.[server]'
.venv/bin/python -m pip check
REDIS_URL=redis://127.0.0.1:6379/0 .venv/bin/python -m uvicorn examples.fastapi_ws_server:app --host 127.0.0.1 --port 8765 --workers 1
```

凭证只通过服务器环境传入。`REDIS_URL` 优先；未设置时使用 `REDIS_HOST=127.0.0.1`、`REDIS_PORT=6379` 和可选 `REDIS_PASSWORD`，没有硬编码密码默认值。`EULERSTREAM_API_KEY` 和 `LIVE_SIGN_TIMEOUT` 已从正式运行路径移除，旧环境中的这两个值不会启用签名回退。

| 环境变量 | 默认值与含义 |
| --- | --- |
| `LIVE_CONNECT_TIMEOUT` | 12 秒，完整连接预算，不含并发排队和全局冷却 |
| `LIVE_BOOTSTRAP_TIMEOUT` | 8 秒，匿名注册及房间查询总预算，包含会话锁等待 |
| `LIVE_ANONYMOUS_COOKIE_TTL` | 900 秒，并以 Cookie 实际到期时间为上限 |
| `LIVE_UPSTREAM_OPEN_TIMEOUT` | WSS 握手 3 秒 |
| `LIVE_UPSTREAM_HEARTBEAT_INTERVAL` | 3 秒 |
| `LIVE_UPSTREAM_IDLE_TIMEOUT` | 8 秒，必须大于心跳间隔 |
| `LIVE_UPSTREAM_SEND_TIMEOUT` | 2 秒 |
| `LIVE_FAST_RECONNECT_TIMEOUT`、`LIVE_FAST_RECONNECT_DELAY` | 5 秒、0–0.2 秒首轮重连预算/等待 |
| `LIVE_ROOM_ID_CACHE_TTL` | 60 秒 |
| `LIVE_RETRY_INITIAL`、`LIVE_RETRY_MAX`、`LIVE_RETRY_RESET_AFTER` | 1、30、30 秒；稳定后重置退避 |
| `LIVE_CONNECT_CONCURRENCY` | 4 |
| `LIVE_MAX_ROOMS`、`LIVE_MAX_SUBSCRIBERS`、`LIVE_MAX_SUBSCRIBERS_PER_ROOM` | 50、1000、200 |
| `LIVE_IDLE_GRACE` | 0 秒 |
| `LIVE_ROOM_QUEUE_SIZE`、`LIVE_SUBSCRIBER_QUEUE_SIZE` | 1024、512 条 |
| `LIVE_SUBSCRIBER_QUEUE_BYTES`、`LIVE_SUBSCRIBER_MAX_AGE` | 4194304 字节、30 秒 |
| `LIVE_REPLAY_DEDUP_SIZE`、`LIVE_REPLAY_DEDUP_TTL` | 每前端 4096 个 ID、3600 秒；容量 0 关闭去重 |
| `LIVE_SEND_TIMEOUT`、`LIVE_CLEANUP_TIMEOUT`、`LIVE_DRAIN_TIMEOUT` | 5、10、5 秒 |
| `LIVE_REDIS_TIMEOUT`、`LIVE_REDIS_FAILURE_COOLDOWN` | 1、2 秒 |
| `REDIS_MAX_CONNECTIONS`、`LOG_DIR` | 32、`logs` |

只运行一个 worker；直接增加 worker 会导致同一主播建立多条上游。需要扩容时按主播分片，再引入独立共享采集层。当前先保留单进程，避免引入业务去重与订阅路由的跨进程复杂度。

## 观测、验证与运维

`/healthz` 只返回存活状态。`/readyz` 限回环访问，返回匿名会话注册/缓存命中数、房间与前端数量、排队量、重复抑制数、Redis 失败数，以及各房间阶段、重连耗时、上游帧数、心跳/ACK 数、最近收包间隔和评论到达年龄桶。年龄基于上游秒级时间戳，不能等同于精确网络延迟。旧源诊断中的 `signer_*` 字段为兼容保留，正常应为 0。

`ready=true` 表示服务接受订阅，不保证主播在线、TikTok 可用或 Redis 查询必然成功。日志按日轮转，默认不保存完整评论、Cookie、签名 URL。测试报告只记录计数、时间和字段集合。

```bash
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check live_service tests
.venv/bin/python -m deploy.quality_probe tv_asahi_news --seconds 600 --silent-fault --output quality.json
```

质量探针只监听 `127.0.0.1:8766`，创建独立服务与两个客户端；第一处故障中断其上游 socket，第二处暂停应用收包，检查恢复和重复。暂停收包模拟应用读黑洞，不等同于真实运营商丢包。端口占用时失败退出，不停止已有服务。回归测试覆盖业务快照、真实 Redis、房间共享、背压、去重、静默超时、取消、HTTP 限流、匿名会话缓存及真实压缩协议帧。

HTTPS 使用免费 Let's Encrypt 证书，Certbot 定时续期及 Nginx reload hook 已配置。仅 80、443 对外；22 限管理来源；8765 和 Redis 仅回环。部署使用独立版本目录、独立虚拟环境和原子 `current` 切换，保留旧版本回退，无数据库结构迁移。
