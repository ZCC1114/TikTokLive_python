# 独立域名、HTTPS/WSS 与自动续期验收

日期：2026-09-09。本记录在此前代码优化及本机直播测试之后补充，说明实际域名接入结果。与 [整体优化结果](../plans/optimization-result-2026-09-09.md) 中的 2.13/3.39 秒故障恢复样本分开统计。

## 已完成配置

| 项目 | 实际结果 |
| --- | --- |
| 主域名 | `quick-pick.net` |
| 新建子域名 | `live.quick-pick.net` |
| DNS 托管 | 火山引擎 TrafficRoute DNS，当前 Daoheng 道亨科技账号 |
| DNS 记录 | `live`，A，`163.7.2.127`，默认线路，TTL 600 秒 |
| HTTPS 健康入口 | `https://live.quick-pick.net/healthz` |
| 业务 WSS 入口 | `wss://live.quick-pick.net/ws/{live_id}` |
| 免费证书 | Let's Encrypt 正式证书，签发成功 |
| 当前证书到期时间 | 2026-12-08 08:46:41 UTC，即北京时间 16:46:41 |
| 自动续期 | `certbot.timer` enabled、active |
| 续期验收 | `renew --dry-run`、实际 deploy hook 成功回执、hook 后 TLS 复查全部通过 |

本次只新建该子域名记录。Nginx 保留原 `/ws/{live_id}` 路径、文本 `ping`/`pong` 和业务消息字段；免费证书更新后先执行 `nginx -t`，通过后 reload，无需手工更换证书文件。HTTP-01 验证目录与公网 TCP 80 保留用于后续续期，业务访问跳转 HTTPS。

## 公网入口验证

| 检查 | 结果 | 含义 |
| --- | --- | --- |
| HTTPS `/healthz` | HTTP 200 | 域名、TLS 与应用健康代理可达 |
| HTTP 业务与 `/ws/` 路径 | HTTP 308 跳转 HTTPS | 明文入口转向加密入口；客户端正式使用 `wss://` |
| 公网 `/readyz` | HTTP 403 | 预期访问控制，详细运行指标只允许内部读取 |
| 真实公网 WSS | 两客户端保持连接并收到弹幕 | TLS/WebSocket 代理与原业务协议可用 |

证书信任链、域名匹配及 TLS 连接已检查。定时器运行和试续期均通过，不只是安装了 Certbot 或生成了证书文件。

## 本轮真实 WSS 测试

原始报告：[probe-public-wss.json](reports/probe-public-wss.json)。测试开始于 2026-09-09 09:45:41 UTC，约 90 秒，直播间 `@shalphoke69`，两个公网客户端。

| 项目 | 客户端 0 | 客户端 1 |
| --- | --- | --- |
| WSS 建立 | 1.081 秒 | 1.654 秒 |
| 收到 LIVING | 70.031 秒 | 69.993 秒 |
| 弹幕数量 / 重复源 ID | 9 / 0 | 9 / 0 |
| 首条 / 最后弹幕 | 70.032 / 85.842 秒 | 69.999 / 85.794 秒 |
| 文本 pong 次数 | 9 | 9 |
| pong 中位数 / 最大值 | 205.015 / 305.777 ms | 177.749 / 290.544 ms |
| 客户端连接异常 | 0 | 0 |

**本次上游首次可用等待约 70 秒，不能称为快速连接已经全面达标。** 服务日志显示第一次上游尝试遇到 `InvalidStatusCode`，第二次遇到 `SignatureRateLimitError`，上游要求 `Retry-After: 53` 秒。服务遵守冷却后重试成功，两个公网 WSS 在等待期间保持连接，继续响应 ping/pong，并在约第 70 秒开始收到弹幕。正式签名配额及限流策略仍是接入体验的重要边界；不能通过无视 Retry-After 来承诺恢复速度。

报告中的 `health_samples.error=JSONDecodeError` 来自探针请求公网 `/readyz` 后尝试把预期的 403 页面解析成 JSON，是探针与内部指标访问策略不匹配，不代表应用健康检查失败或 WSS 故障。本次公网健康以独立的 HTTPS `/healthz` 200 验收。

应用切到回环监听后，另做 [75 秒公网 WSS 复验](reports/probe-public-wss-loopback.json)：一个客户端，WSS 在 1.483 秒建立，首次签名 8 秒超时，下一次尝试被要求冷却约 51 秒，最终第 65.336 秒进入 LIVING。收到 6 条首批弹幕、0 重复，8 次 pong，中位数 191.133 ms，客户端连接异常为 0。首条至最后一条仅为 65.338–65.340 秒；最后约 10 秒没有新评论，不能称该次又观察到了后续实时评论。此前 90 秒测试首条至最后弹幕为 70.032–85.842 秒，确实观察到初批之后的新到达。

此前 2.131/3.386 秒是已建立直播连接遭明确 transport 中断后的恢复测试；本轮约 70 秒及收口后约 65 秒是首次上游连接遇到拒绝、超时和限流后的结果。两次长等待不能被受控恢复的 2–3 秒样本覆盖，**正式签名配额及长期稳定性仍未解决**。这些样本不能混成同一个重连时延指标，也不能证明 24 小时稳定或高负载容量。

## 8765 收口与最终状态

- 云安全组的 TCP 8765 入站规则已删除。
- 主机 UFW 的 8765 规则已删除，80/443 对公网开放，22 保留管理出口白名单。
- systemd 服务 active，进程 13966，重启计数 0；`ss` 确认应用只监听 `127.0.0.1:8765`。
- 云安全组界面确认仅保留公网 IPv4 的 80/443 和管理出口 `/32` 的 22 入站规则；Redis 仍只监听本机。
- 修改后公网 HTTPS `/healthz` 仍为 200，75 秒 WSS 复验如上，Nginx 代理能够访问回环应用。

最终状态证据见 [domain-https-state.json](reports/domain-https-state.json)，记录时间为 2026-09-09 09:49:39 UTC。本轮 Nginx/证书相关 29 项测试在本地和服务器均通过；上一轮完整功能回归为 116 项，本轮没有重新执行全部功能测试，不能称完整 128 项或更多用例已通过。

## 日常检查

在服务器执行：

```bash
systemctl status nginx tiktok-live certbot.timer --no-pager
systemctl list-timers certbot.timer --no-pager
certbot certificates
curl --fail https://live.quick-pick.net/healthz
curl --fail http://127.0.0.1:8765/readyz
```

需要人工检查续期链路时：

```bash
certbot renew --cert-name live.quick-pick.net --dry-run --run-deploy-hooks --no-random-sleep-on-renew
```

不要把证书私钥、PEM 登录密钥、Cookie 或签名密钥写入日志和报告。实际业务 Redis 数据及正式签名账号配置尚未因域名上线而自动迁移，仍以部署记录中的明确配置为准。
