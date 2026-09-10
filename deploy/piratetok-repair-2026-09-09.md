# PirateTok 匿名连接修复与实测

2026-09-09。此前不能连接的直接原因已定位：固定版本 PirateTok 从主播主页和 `@tiktok` 页面获取 `ttwid`，但当前网络返回 HTTP 200 的短验证页，只设置 `ak_bmsc`，没有可用于连接的匿名 `ttwid`。房间本身在线且可解析。

同一匿名会话向 TikTok 自身的 `https://www.tiktok.com/ttwid/register/` 正常注册后，可以获得 `ttwid`，并直接建立不带签名参数的 WebSocket，持续收到新评论。此路径不依赖浏览器、登录 Cookie 或第三方签名服务。

## 修复内容

- [piratetok_bootstrap.py](piratetok_bootstrap.py)：固定 HTTP 会话和 UA，匿名注册后解析房间，整个初始化共用 8 秒预算；对拒绝、限流、下播和异常响应给出明确原因。限制响应体和 Cookie 大小，检查 Cookie 作用域，诊断信息不记录 Cookie、评论正文或身份。
- [piratetok_probe.py](piratetok_probe.py)：新增显式 `--bootstrap registered` 路径，保留 `profile` 作为原方案对照。首次注册的 Cookie 在受控重连间复用；使用 TikTok Protobuf 心跳和 ACK，关闭可能导致误断开的标准 Ping/Pong 超时要求。
- [test_piratetok_bootstrap.py](../tests/test_piratetok_bootstrap.py)：17 项针对性验证通过，包括真实本地 HTTP 传输的超大响应中止；这些离线用例不计作 TikTok 实连证据。

## 真实样本与证据口径

测试对象是公开直播 `tv_asahi_news`。本机使用整理后的模块；服务器先沿用既有探针，通过独立 [registered-runtime.py 的存档](reports/piratetok-20260909/registered-runtime.py.txt) 替换匿名注册入口与 Ping 设置。两者验证同一注册修复，但初始化顺序、请求数和 UA 有差异，不能当成字节相同的部署。

本机 30 秒整理版样本：初始化至连接成功 2.751 秒，握手本身 1.166 秒；39 帧、8 条评论，其中 6 条被上游标记为历史、2 条符合新评论判定。见 [本机烟测](reports/piratetok-20260909/repair-local-smoke.json)。

服务器 45 秒先行样本：初始化至连接成功 0.561 秒；58 帧、8 条评论，其中 6 条历史、2 条符合新评论判定。没有观察到意外断开。见 [云助手输出摘录](reports/piratetok-20260909/repair-server-smoke-observation.json)，原任务为 `ivk-yeupwr61vjlmv6aebtha`。

两处均完成约 10 分钟观察，在第 180、360 秒主动中断实验连接：

| 指标 | 雅加达服务器 | 本机整理版 |
| --- | --- | --- |
| 实际观察时间 | 600.088 秒 | 600.251 秒 |
| 启动至首次握手成功 | 0.537 秒 | 3.563 秒 |
| 收到的二进制/协议帧总数 | 779 | 772 |
| 评论到达总数（含回放） | 62 | 61 |
| 符合新评论判定的数量 | 44 | 43 |
| 重连回放的重复评论 | 12；每次重连 6 条 | 12；每次重连 6 条 |
| 两次主动断开后的握手恢复 | 0.340 / 0.404 秒 | 1.121 / 1.576 秒 |
| 两次恢复至首个上游消息 | 0.443 / 0.479 秒 | 1.380 / 1.888 秒 |
| 两次恢复至首条合格新评论 | 18.987 / 21.197 秒 | 6.176 / 28.542 秒 |
| 非计划断开 | 0 | 0 |
| 进程 RSS 峰值 | 48.94 MiB | 49.75 MiB |
| 事件循环延迟 P95 | 1.112 毫秒 | 4.141 毫秒 |

上述三个恢复指标均从主动断开时刻计算。收到的首条消息可能是历史或其他事件，不能冒充新评论；等待新评论还受直播间发言频率影响。本轮没有消息集合对照，不能据此保证 15 秒内必有新评论或声称零漏收。三代连接均在建立 5 秒后继续收到独立评论，两处都运行到预定截止，没有因解析或上游拒绝耗尽重试预算。

两次重连各收到 6 条原生 ID 已见的评论，明确验证了历史回放是重复来源之一。探针在计数层识别这些重复并排除出 `fresh_comments`；它没有向业务用户输出数据，不能将此说成生产改造已经完成。

原始计数与阶段数据：[服务器长测](reports/piratetok-20260909/repair-server-600s.json)、[本机长测](reports/piratetok-20260909/repair-local-600s.json)。服务器文件来自云助手任务 `ivk-yeupxlrm7tilu1did6yy` 的 `FINAL_REPORT`，为控制输出长度仅省略了 `source_sha256` 和 `resource_samples`；完整报告仍在服务器实验目录。测试后生产服务 PID 13966、重启次数 0、状态 active，HTTPS `/healthz` 返回 `{"status":"ok"}`。

## 复现整理后的代码

使用之前固定的 PirateTok Python 源码提交 `ee836405ef122d7e2dd3403d99b91cb0d967977e`，依赖见 [requirements.lock](reports/piratetok-20260909/requirements.lock)。本机命令：

```bash
/private/tmp/tiktok-piratetok-experiment/venv/bin/python \
  deploy/piratetok_probe.py \
  --source-root /private/tmp/piratetok-research-live-py \
  --room tv_asahi_news --bootstrap registered \
  --seconds 600 --fault-at 180 --fault-at 360 \
  --output deploy/reports/piratetok-20260909/repair-local-600s.json
```

`comments` 是上游到达总数，包含历史和重复；`fresh_comments` 要求有效原生 ID、未重复、非历史标记，且消息时间不早于当前握手前 2 秒。时间允许误差，需结合连接建立 5 秒后的独立评论和持续消息流判断。

`transport.abort()` 测的是可立即感知的连接断开。握手恢复与首条新评论到达必须分别报告，后者也受直播间发言频率影响。该实验没有测试静默丢包时的断线检测，也没有独立消息源用于评估漏收率。10 分钟样本不能证明全天可用。

## 业务边界

当前修复位于独立实验层，没有替换生产采集器。当前业务对外转发评论与连接/直播状态，正式接入必须保持用户账号名、Redis 标签/黑名单键、消息字段、按订阅者去重和背压语义，并完善 Cookie 过期与静默断网的生命周期处理。最小改动方向是替换匿名初始化与 WSS 传输，保留现有 SDK 的事件模型与业务转换；不能原样接入 PirateTok 提前触发的 connected 事件或同步 dict 回调。

礼物和点赞目前没有业务输出分支；将来若增加，需要单独验证连击终态、重复回放、点赞增量和累计值，不能由本轮评论样本推断其正确性。已有 Nginx、HTTPS、Redis 和业务消息格式保持现状。
