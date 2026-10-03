# 仓库 HTTPS 发版要求

## HTTPS 自动续期发版要求（2026-10-03）

自有 HTTPS/WSS 域名统一使用 Let's Encrypt 自动申请、自动续期及自动部署，完整要求见 [HTTPS 证书发版约定](docs/https-certificate-release.md)。TOS/CDN 等托管入口必须自动更新绑定并读回公网证书；发版需核验续期任务、部署钩子和失败告警。此规则是后续要求，不代表历史域名已经完成配置，也不授权改动本次任务之外的生产服务。
