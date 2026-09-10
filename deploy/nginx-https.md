# TikTok LIVE 独立域名与 HTTPS/WSS

**实际站点 `live.quick-pick.net` 已完成 DNS、Let's Encrypt 免费证书、HTTPS/WSS 与续期验收**，详见 [2026-09-09 域名接入记录](domain-https-2026-09-09.md)。应用已仅监听 `127.0.0.1:8765`，云安全组和 UFW 的 8765 规则均已删除；修改后 HTTPS/WSS 复验通过。该记录保留两轮公网测试因签名问题等待约 70/65 秒的结果，不能与此前 2–4 秒明确故障恢复样本混用；正式签名配额及长期稳定性仍未解决。

脚本 `configure_nginx.py` 供 `163.7.2.127` 的 Ubuntu 服务器以 root 身份运行。它使用免费 Let's Encrypt 证书、Ubuntu 的 Certbot 与 systemd 定时器，保留业务入口 `/ws/{live_id}`。下文是可复用的操作说明；`live.example.com` 为示例，当前实际子域名是 `live.quick-pick.net`。

## 运行前

1. 确定子域名，例如 `live.example.com`，在域名的 DNS 服务商添加唯一 A 记录，指向 `163.7.2.127`。示例不是实际域名。此服务器当前只配置了公网 IPv4，不应保留指向其他机器的同名 A 或 AAAA 记录。
2. 如果 DNS 服务商支持代理，初次签发先使用仅 DNS 解析。脚本会检查服务器解析器返回的地址集合，必须只有上述 IPv4；最终公网 HTTP-01 可达性由证书颁发机构验证。
3. 云安全组与主机防火墙对公网开放 TCP 80、443。80 用于证书首次签发和日后续期，正常业务跳转 HTTPS；不要在签发之后关掉它。
4. 已安装 Ubuntu `nginx`、`certbot` 包，业务服务可从 `127.0.0.1:8765` 访问。不要把 Redis 端口开放到公网。8765 可在 WSS 验证成功后收回公网访问。

先只输出配置供检查，不需要 root、DNS 或公网访问：

```bash
python3 deploy/configure_nginx.py live.example.com --print-config
```

正式执行（替换成自己的实际子域名和联系邮箱）：

```bash
sudo python3 deploy/configure_nginx.py live.example.com --email admin@example.com
```

邮箱可省略，Certbot 会使用无邮箱方式注册 ACME 账户。脚本把所有外部命令以参数列表传给 `subprocess`，不执行拼接的 Shell 命令；域名只接受严格验证的完整 ASCII DNS 名称，国际化域名需要其 punycode 名称。

## 生成的配置

- `/etc/nginx/conf.d/tiktok-live.conf`：只管理该独立站点，不删除默认站点和其他站点；发现同名文件未带本工具管理标记时拒绝覆盖。
- `/var/www/tiktoklive-acme/.well-known/acme-challenge/`：HTTP-01 验证文件目录，保留给自动续期使用。
- `/etc/letsencrypt/live/<子域名>/`：由 Certbot 管理的证书及私钥。脚本不输出私钥内容，也不修改其权限。
- `/etc/letsencrypt/renewal-hooks/deploy/tiktok-live-nginx`：仅当此站点的证书成功续期时，先执行 `nginx -t`，通过后 reload Nginx。
- `/run/tiktoklive-nginx-renewal-hook.json`：上述 hook 测试和重载成功后的时间及证书目录回执，不含密钥，用于防止 Certbot 本身成功但 hook 失败时误报验收通过。
- `/var/backups/tiktoklive-nginx/<时间戳>/`：修改前的受管配置、hook 和文件存在性/权限清单。目录只有 root 可访问。

WSS 入口为 `wss://<子域名>/ws/<live_id>`。Nginx 使用 HTTP/1.1 转发 Upgrade/Connection，关闭响应与请求缓冲，保留原路径及查询参数。上游读取空闲超时为 3600 秒，客户端原有文本 `ping`/`pong` 仍适用；该超时不意味着关闭了掉线检测。

公网 `/healthz` 只代理应用现有的最小存活响应。包含连接数及内部诊断的 `/readyz` 只允许来自回环地址的请求。其他 HTTPS 路径返回 404。

保护阈值只针对 `/ws/` 握手入口：每个来源 IP 每秒 10 次，允许 30 次瞬时突发；每 IP 最多 100 个连接，整个站点最多 500 个连接，超限返回 HTTP 429。已升级的 WebSocket 内部弹幕帧不受握手请求速率限制。若正式业务由同一出口集中转发超过 100 个连接，应依据实际压测调整这些值；反向代理/CDN 接入后也需重新确认来源 IP 的可信处理，不能直接信任客户端自行填写的转发头。

## 自动续期与失败行为

脚本执行 `certbot certonly --webroot`，明确使用 Let's Encrypt 正式签发接口；重复运行会保留尚不需要续期的证书。首次安装先启用 HTTP 验证，并实际探测验证目录是否可读；尚无证书时其他路径临时返回 503，避免跳转到尚未就绪的 HTTPS。重跑时若现有证书可用，则保留 HTTPS 在线。证书生成后测试并加载完整 HTTPS 配置，通过回环 TLS 连接验证证书信任链、域名及应用 `/healthz` 响应，再启用 `certbot.timer`，最后运行指定证书的 `renew --dry-run --run-deploy-hooks --no-random-sleep-on-renew`。本次手工验收关闭随机等待，不改变日常定时任务的调度。脚本还检查本次试续期的 hook 成功回执并再次验证 HTTPS。只有这些验证与定时器状态均通过才输出成功结果。

每次变更 Nginx 配置前保留上一份配置；`nginx -t`、同名虚拟主机冲突检查、加载或加载后的可达性验证失败时，恢复上一份配置并尝试重新加载。探测会短暂重试，避免 Nginx reload 返回后新 worker 尚未就绪造成误判。脚本不删除已签发证书。若证书签发或试续期失败，脚本以非零状态退出；可能已生成证书并运行 HTTPS，此时不能把“当前能访问”视作自动续期已验收。根据错误修复 DNS、公网端口或 ACME 可达性后，可重跑脚本。

运维检查：

```bash
systemctl status nginx certbot.timer --no-pager
systemctl list-timers certbot.timer --no-pager
certbot certificates
certbot renew --cert-name live.quick-pick.net --dry-run --run-deploy-hooks
curl --fail https://live.quick-pick.net/healthz
```

参考官方说明：[Nginx WebSocket 代理](https://nginx.org/en/docs/http/websocket.html)、[Nginx 代理缓冲与读取超时](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)、[Certbot webroot、续期与 hooks](https://eff-certbot.readthedocs.io/en/stable/using.html)。
