# 当前部署

正式采集器已切换到匿名无签名实现，版本为 `20260910-unsigned-01`。运行入口不调用签名服务，也没有自动切回签名方案的分支。业务说明见 [SERVICE.md](../SERVICE.md)，实施步骤见 [落地计划](../plans/unsigned-production-rollout.md)。

| 项目 | 当前配置 |
| --- | --- |
| 服务器 | 火山引擎雅加达 ECS-live-global，163.7.2.127 |
| 实例 | i-yeup1j0hkwz5gcnn36xa，Ubuntu 24.04，2 vCPU / 4 GiB |
| 发布目录 | `/opt/tiktoklive/releases/20260910-unsigned-01` |
| 当前链接 | `/opt/tiktoklive/current` |
| 上一版本 | `/opt/tiktoklive/releases/20260909-02`，保留以便回退 |
| 服务 | `tiktok-live.service`，用户 `tiktoklive`，单 worker |
| 公网入口 | `wss://live.quick-pick.net/ws/{live_id}` |
| 本机入口 | `127.0.0.1:8765`；Redis `127.0.0.1:6379` |
| 配置 | `/etc/tiktoklive/service.env`，root 0600 |
| 日志 | `/var/log/tiktoklive/server.log` |
| HTTPS | Let's Encrypt 免费证书，Certbot 自动续期及 Nginx reload hook |

接口保留评论字段、标签字段、控制码及文本 ping/pong。超时状态从 `SIGN_API_TIMEOUT` 改为 `UPSTREAM_TIMEOUT`，前端如按旧字符串处理需要同步替换。

安全组和 UFW 仅公开 80/443；22 限管理来源 `104.28.211.105/32`、`142.91.109.185/32`。应用及 Redis 不公开，SSH 禁止密码登录。公网 `/healthz` 提供最小健康信息，详细 `/readyz` 只允许本机访问。域名 A 记录仍指向本机，证书有效期截至 2026-12-08 08:46:41 UTC。证书续期配置沿用已验证版本，部署没有重新签发证书。

这是新项目，服务器本机 Redis 为空是正常初始状态，没有历史订单、黑名单或数据迁移待办。代码中已有标签和黑名单查询逻辑，其空数据及异常处理已通过真实 Redis 回归测试；后续按新项目的业务流程写入数据即可。Redis 凭证只从环境变量读取，采集服务不需要签名 Key。

## 部署复现

在项目根目录构建白名单文件包：

```bash
python3 deploy/build_release.py /absolute/path/release.tar.gz
```

生成包包含运行代码、依赖锁、测试夹具和 SHA-256 清单，排除环境文件、私钥、日志和虚拟环境。上传至服务器后，先解压到新的版本目录，创建独立虚拟环境；禁止在当前线上目录直接安装或覆盖运行文件。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple -r requirements-test.txt
.venv/bin/python -m pip install --index-url https://pypi.org/simple --no-deps -e '.[server]'
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
.venv/bin/ruff check live_service tests
```

火山默认 pip 镜像缺少部分锁定版本，因此显式使用官方 PyPI；没有降低或替换已验证依赖。预发布使用 `deploy.quality_probe`，只监听 127.0.0.1:8766，以独立 systemd 临时任务运行并加载受限配置文件。预发布通过后，原子替换 current 符号链接并重启正式服务，验证 `/readyz` 中 `collector=anonymous_unsigned`，然后进行公网 WSS 验收。

本次发布包 SHA-256 为 `5f8b86bd4d0a05b87ea028d8db40fcd108f43be0a30590326b74ee26a89b0114`；服务器 `RELEASE-MANIFEST.json` 记录 85 个文件的哈希。完成预发布后只更新部署探针和说明，没有改变被验证的运行时代码。

## 检查和回退

```bash
systemctl status tiktok-live nginx certbot.timer --no-pager
curl -fsS http://127.0.0.1:8765/readyz
curl -fsS https://live.quick-pick.net/healthz
journalctl -u tiktok-live -n 50 --no-pager
```

回退当前发布版本时，在服务器执行以下命令。临时链接已存在时命令会停止，需先检查，不覆盖未知文件。

```bash
ln -s /opt/tiktoklive/releases/20260909-02 /opt/tiktoklive/.rollback-previous
mv -Tf /opt/tiktoklive/.rollback-previous /opt/tiktoklive/current
systemctl restart tiktok-live
curl -fsS http://127.0.0.1:8765/healthz
```

没有数据库结构迁移，回退不改 Redis 数据。上一版本仍依赖签名方案，回退会恢复其原有连接限制，应只用于处理新版本异常。

首次部署、旧签名版本测试和当时限制保留在 [历史记录](deployment-history-2026-09-09.md)。最新验收结果见 [无签名上线报告](unsigned-2026-09-10.md)。
