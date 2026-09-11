# Quick Pick 海外蓝绿部署（2026-09-12）

运行于雅加达 `live-global`：`163.7.2.127` / `172.31.13.15`。公网入口 `wss://live.quick-pick.net/ws/{uniqueId}`，业务 API 在同域名 `/api/` 经私网转业务主机。业务私网 `172.31.13.16` 调用本机 `9000/internal/rooms/`，并校验独立服务密钥。

`/opt/tiktoklive/releases/20260912-03` 对应源码 `dab5062047a2f7f4fbfa3829ec340496c6dd2a5f`。`slots/blue` / `slots/green` 为版本链接；systemd `tiktok-live@blue/green` 监听回环 8765 / 8766，每槽一个 worker。活动 blue，两槽均已验证；原 `tiktok-live.service` 在订阅数为零后停止并禁用。

配置由 `/etc/tiktoklive/overseas.env` 与 `blue.env` / `green.env` 提供，文件受限可读，不进 Git。日志目录为 `/var/log/tiktoklive/blue|green`，通过 systemd `LogsDirectory` 和 `LOG_DIR` 提供可写位置，代码目录只读。

```bash
# root on live-global; target must already contain an installed version
quick-pick-switch tiktok green
quick-pick-switch tiktok blue
journalctl -u tiktok-live@blue -n 100 --no-pager
curl --fail http://127.0.0.1:8765/readyz
```

切换工具安装于 `/usr/local/sbin/quick-pick-switch`：互斥锁、连续三次 readiness、原子 Nginx 上游更新、配置校验与 reload、失败恢复上游。原连接留在旧进程，不主动断开。复用非活动槽时，先确认该槽 `subscribers=0`，停止后原子更新版本链接，启动并核验，再切换。仍有订阅者时必须等待排空。

本轮 184 项测试通过；真实 `@weathernewslive` 单连接运行 605 秒，收到 10 条评论、58 个 pong、0 元数据失败。green → blue → green 期间原连接保持；原生 iOS/Android 均通过真实业务与弹幕链路。该结果不代表最大容量、多日长稳或物理打印验收。

完整服务器、产物哈希、共享 Redis 权限、备份与跨端证据见业务仓库 [海外生产部署](https://github.com/daoheng1/quick-pick-server/blob/main/docs/overseas-production-deployment.md)。只向 `codex/piratetok-unsigned-runtime` 推送，不向 main / master 合并。
