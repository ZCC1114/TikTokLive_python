# PirateTok 无签名方案实际实验

> 后续更新：当晚已通过 TikTok 匿名注册接口解决 Cookie 获取问题，本机与服务器均收到真实弹幕。最新结果见 [连接修复与实测](piratetok-repair-2026-09-09.md)。下文保留修复前的失败记录。

2026-09-09，北京时间约 22:26–22:35。结论：**当前环境没有跑通 PirateTok 的匿名接入流程，尚不具备替换生产采集器的条件。卡点在取得匿名 `ttwid` Cookie，未能开始收取弹幕，因此没有得到持续连接或断线恢复的稳定性样本。**

实验使用国际版 TikTok 正常公开直播 `tv_asahi_news`。本机和雅加达服务器都确认其在线，直播间 ID 为 `7683069193373207304`。此前测试的 `shalphoke69`、`y277902` 当时已下播，没有把下播结果计入方案失败。

**实际结果**

| 测试 | 直播间解析 | 匿名 Cookie | WebSocket / 弹幕 | 结果 |
| --- | --- | --- | --- | --- |
| 雅加达服务器，正式探针 | 0.414 秒完成 | 失败；总计 0.618 秒终止 | 未进入握手；0 条弹幕 | `bootstrap_failed` |
| 本机网络，正式探针 | 1.085 秒完成 | 失败；总计 3.836 秒终止 | 未进入握手；0 条弹幕 | `bootstrap_failed` |
| 服务器，独立 Cookie 诊断 | 不适用 | 主播主页及首页均未返回 `ttwid` | HTTP 200、响应 1462 字节、`X-TT-System-Error: 3`，仅 `ak_bmsc` Cookie | HTTP 200 不能视为正常页面或可连接凭据 |
| 服务器，不带 Cookie 的直接握手诊断 | 成功 | 刻意省略，仅用于定位 | HTTP 200，`InvalidStatus`，0 帧 | WebSocket 未升级成功；此项不是原版 PirateTok 完整流程 |

正式探针各请求最多 30 秒观察窗口，但在必要的初始化失败后立即退出。0.618 / 3.836 秒是失败所用时间，不是连接成功耗时。另一项先行服务器握手烟测也在 Cookie 阶段失败（0.701 秒），与正式探针方向一致。

固定源码的 Cookie 获取逻辑会依次尝试主播主页与 `@tiktok` 主页；本轮没有自动轮换代理、IP、身份或使用登录 Cookie。独立诊断的异常页面与该源码描述的拦截情形相符，但仅凭这些响应不能进一步断言究竟由 IP、地域、请求特征还是临时平台策略造成。

**本轮能确认与不能确认的事项**

- 能确认：直播间在线且可解析，失败发生在匿名凭据获取阶段，不是主播下播、我们的域名证书、Nginx 或业务 JSON 转换导致。
- 能确认：本机和当前服务器的这两次正式尝试均没有开始收取弹幕，没有成功样本可用于计算重复率、恢复延迟或长连接可用率。
- 不能确认：取得有效匿名 Cookie 后，这条无签名 WebSocket 路径是否在当前环境可用、能持续多久、是否比 Euler 方案稳定。
- 不能据此宣称 PirateTok 在所有网络都不可用；同样不能用其源码和其他人的成功记录替代当前服务器验收。
- 10 分钟长测与强制断线测试没有执行，因为尚未建立可测试的连接；没有将计划时长写成已完成的测试时长。

**实验隔离与复现**

使用 PirateTok Python 固定版本 `ee836405ef122d7e2dd3403d99b91cb0d967977e` 的房间解析、匿名 Cookie、URL 构造及 Protobuf 定义。探针自行监督连接生命周期，避免原 SDK 在实际握手前发出 connected 事件。

依赖固定为 `betterproto==2.0.0b7`、`websockets==15.0.1`、`curl_cffi==0.13.0`；完整依赖见 [requirements.lock](reports/piratetok-20260909/requirements.lock)。探针源码为 [piratetok_probe.py](piratetok_probe.py)。探针编译、Ruff 和离线模拟握手/故障/消息解析检查已通过；离线检查不属于 TikTok 真实连接成功证据。

服务器实验目录为 `/opt/tiktoklive/experiments/piratetok-20260909`，使用独立虚拟环境，以 `tiktoklive` 用户执行。未修改正式服务的运行依赖、systemd 配置、Nginx、DNS 或消息输出。未导入账户 API Key、登录 Cookie，也未发聊天、点赞或礼物。记录只包含计数、阶段、耗时、错误类型和源码摘要，不保存评论正文、观众身份、Cookie 值或完整握手 URL。

服务器上复测初始化：

```bash
runuser -u tiktoklive -- \
  /opt/tiktoklive/experiments/piratetok-20260909/venv/bin/python \
  /opt/tiktoklive/experiments/piratetok-20260909/piratetok_probe.py \
  --source-root /opt/tiktoklive/experiments/piratetok-20260909/source \
  --room tv_asahi_news --seconds 30 \
  --output /opt/tiktoklive/experiments/piratetok-20260909/reports/recheck.json
```

先确认主播当时仍在线。只有初始化通过且持续收到新弹幕后，才有意义改成 `--seconds 600 --fault-at 180 --fault-at 360` 测断线恢复。此命令只操作自己的测试连接。

**后续判断**

应先解决正常匿名会话的可获得性，再决定是否值得做长测。真实浏览器能否正常打开同一直播间并获得可用会话，是另一个可验证方向；即使浏览器可用，也需要单独评估无人值守、资源占用和会话维护成本。当前生产服务继续保留原采集器。

原始证据：[服务器正式探针](reports/piratetok-20260909/piratetok-smoke-server.json)、[本机正式探针](reports/piratetok-20260909/piratetok-smoke-local.json)、[Cookie 响应诊断](reports/piratetok-20260909/cookie-diagnostic.json)、[不带 Cookie 的握手诊断](reports/piratetok-20260909/no-cookie-diagnostic.json)、[先行烟测](reports/piratetok-20260909/handshake-smoke.json)。

实验结束后，生产服务仍为 active，PID 13966、重启计数 0；Nginx 与证书续期定时器正常，公网 HTTPS `/healthz` 返回 200。实验进程均已结束。源码、实际烟测脚本与最终维护脚本的摘要，以及服务状态，见 [实验状态记录](reports/piratetok-20260909/experiment-state.json)。
