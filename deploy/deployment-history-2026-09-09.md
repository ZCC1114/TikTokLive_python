# 新服务器部署记录

部署日期：2026-09-09。**`live.quick-pick.net` 的 DNS、免费 HTTPS/WSS 和自动续期已经验收，8765 已收回为仅本机入口**，见 [域名接入与本轮公网测试](domain-https-2026-09-09.md)。本轮代码已切换到 `20260909-02`，去重/恢复实测见 [优化结果与方案](../plans/optimization-result-2026-09-09.md)。初始实测保留在 `validation-2026-09-09.md`，后续控制事件修复见 [控制事件修复记录](control-event-fix.md)；这些历史样本与本轮公网测试分开统计。

## 实例与服务

| 项目 | 配置 |
| --- | --- |
| 实例 | ECS-live-global / i-yeup1j0hkwz5gcnn36xa |
| 地域 | 火山引擎雅加达，可用区 A |
| 公网 / 私网 | 163.7.2.127 / 172.31.13.15 |
| 系统 | Ubuntu 24.04 x86_64，Python 3.12 |
| 资源 | 2 vCPU、4 GiB、50 GiB PL0 |
| 发布目录 | `/opt/tiktoklive/releases/20260909-02` |
| 当前版本 | `/opt/tiktoklive/current` 符号链接 |
| 进程管理 | `tiktok-live.service`，开机启动，失败后 5 秒重启 |
| 运行用户 | `tiktoklive`，禁止交互登录 |
| 环境配置 | `/etc/tiktoklive/service.env`，root 持有，权限 0600 |
| 日志 | `/var/log/tiktoklive/server.log`，按日轮转，保留 30 份 |
| 业务入口 | `examples.fastapi_ws_server:app`，单 worker，仅 `127.0.0.1:8765` |
| 公网加密入口 | `wss://live.quick-pick.net/ws/{live_id}` |
| 证书与续期 | Let's Encrypt 免费证书；`certbot.timer` 已启用，续期 dry-run 和 deploy hook 验收通过 |

保留 `/ws/{主播账号}`、文本 `ping` / `pong` 及原有业务消息字段。不要直接增加 worker 数：房间复用和去重记录都在进程内。

## 网络与管理

新建并绑定专用安全组 `tiktok-live-global`（`sg-z2lrf3bh20ao2xpagjxzpwqf`）。原 Default 安全组保留，但已从该实例网卡解绑。

| 方向 | 协议 / 端口 | 来源或目的 |
| --- | --- | --- |
| 入站 | TCP 80、443 | 0.0.0.0/0，HTTP 验证/HTTPS 入口 |
| 入站 | TCP 22 | 104.28.211.105/32 |
| 入站 | TCP 8765 | 云安全组和 UFW 规则均已删除，应用仅本机监听 |
| 出站 | 全部 | 0.0.0.0/0 |

主机 UFW 默认拒绝入站、允许出站，80/443 对外放通，SSH 保留原管理出口白名单；8765 规则已删除且应用只在回环监听，最终检查见域名验收记录。Redis 仅监听 `127.0.0.1:6379` 和 `[::1]:6379`，不开放公网。SSH 禁止密码登录，root 仅允许密钥认证。保留云助手作为不依赖入站 SSH 的管理入口。

当前管理网络存在多个公网出口。SSH 白名单只保证匹配该出口的连接可达；其他出口会被拦截。更换管理出口时需同步更新云安全组和 UFW。业务客户端改用公网 WSS 域名，不再通过开放 8765 接入。不要以单次 SSH 成功推断其他端口、其他出口也可达。

TrafficRoute DNS 已新建 `live.quick-pick.net` A 记录指向本机，默认线路、TTL 600 秒。Let's Encrypt 正式证书已签发，当前到期为 2026-12-08 08:46:41 UTC；自动续期 timer、dry-run、deploy hook 回执和 TLS 复查均通过。公网 HTTPS `/healthz` 返回 200，HTTP 业务入口返回 308，公网详细 `/readyz` 返回预期 403。真实公网 WSS 已收取弹幕；两轮首次上游连接因拒绝/超时及签名冷却分别等约 70 秒和 65 秒，正式签名配额及稳定性仍未解决，不能以此前受控恢复的 2–3 秒覆盖该结果。应用回环绑定后已验证 HTTPS/WSS；详细结果见 [验收记录](domain-https-2026-09-09.md)，部署脚本见 `configure_nginx.py`。

维护时仍可以使用 SSH 隧道访问服务器本机入口：

```bash
ssh -i /Users/muleng/Downloads/quick-pick-global.pem \
  -o IdentitiesOnly=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
  -N -L 127.0.0.1:18765:127.0.0.1:8765 root@163.7.2.127
```

保持终端运行，客户端连接 `ws://127.0.0.1:18765/ws/主播账号`。SSH 隧道本身仍要求管理出口符合 22 端口白名单。首次连接应核对主机指纹；本次连接记录的 ED25519 指纹为 `SHA256:DjSOyz/1cKjVkccyATVgTCgRc8FR7vsODXvCn3gpcnw`。部署过程中记录的主机公钥没有写入用户全局 SSH 配置。用户提供的 PEM 原文件权限已收紧为 0600，内容未改动。

## 依赖与配置

Ubuntu 默认的 `mirrors.ivolces.com` Python 镜像源缺少锁定版本 `annotated-doc==0.0.5`，本次改用官方 PyPI 安装，未修改锁文件版本：

```bash
cd /opt/tiktoklive/current
.venv/bin/python -m pip install --index-url https://pypi.org/simple -r requirements.lock
.venv/bin/python -m pip install --index-url https://pypi.org/simple --no-deps -e '.[server]'
.venv/bin/python -m pip check
```

当前环境配置使用 `LOG_DIR=/var/log/tiktoklive`、`REDIS_URL=redis://127.0.0.1:6379/0`、`DEBUG_TIKTOK_RAW_COMMENT_EVENT=0`。

更正（2026-09-10）：用户确认这是新项目，没有历史订单或黑名单数据。本机 Redis 为空是正常初始状态，不需要数据迁移。此前将代码中已有的查询逻辑误解为存在历史数据，相关迁移待办不成立。

现有 `EULERSTREAM_API_KEY` 没有上传。本次连接测试在未配置该密钥的情况下完成；未验证该模式的正式配额或长期可用性。配置正式签名账号时，将已授权的密钥写入受限环境文件，然后重启服务，不要将其写入代码、命令行参数或报告。

上传的初始代码包 SHA-256：`4c5bce28889f5473923aef37a7dc9ea0b4230b8103cb99f5aa82273c238f214f`。包只包含运行代码、锁文件、文档、测试和新 systemd 文件，排除了旧 service 配置和私钥。部署包中的 `live_service/redis_store.py` 已去除旧的硬编码 Redis 密码默认值，实际通过上述 `REDIS_URL` 配置连接；其余运行代码对应本次优化后的工作区。后续启用了 `web_signer.py` 的 TLS 证书校验，并补充实测脚本和报告，这些更新不在初始包的校验范围内。

## 日常检查

以下命令在服务器执行：

```bash
systemctl status tiktok-live --no-pager
systemctl show tiktok-live -p NRestarts -p MemoryCurrent
journalctl -u tiktok-live -n 60 --no-pager
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS http://127.0.0.1:8765/readyz
curl -fsS https://live.quick-pick.net/healthz
systemctl status nginx certbot.timer --no-pager
```

`healthz` 只检查进程响应；`readyz` 可看房间、连接、队列、重放抑制及 Redis 降级计数。`ready=true` 不代表主播在线或签名配额充足。检查业务是否正常时，应同时观察 `LIVING` 状态和后续新弹幕。

`deploy/live_probe.py` 用于有限时长实测，只保存统计和字段名，不保存弹幕正文、观众账号、签名或 Cookie：

```bash
cd /opt/tiktoklive/current
.venv/bin/python deploy/live_probe.py 主播账号 --seconds 600 --clients 2 \
  --output /var/lib/tiktoklive/probe.json
```

## 更新与回退

新版本先安装到新的 `/opt/tiktoklive/releases/<版本>` 目录，检查依赖、回归测试和环境配置，再切换 `current` 并重启 `tiktok-live`。保留先前发布目录及虚拟环境；出现问题时，把 `current` 指回先前版本并重启。当前保留完整 `20260909-01` 和 `20260909-02` 两个发布版本。新版本已经通过实际直播测试，必要时可以回退到 01；回退也会恢复 01 的同代重复策略。

配置变更前保留受限权限的备份。当前部署未做数据库结构迁移，也没有切换已有生产前端流量。
