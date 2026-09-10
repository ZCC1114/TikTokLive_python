# 国际版 TikTok 弹幕免第三方签名方案研究

后续实验更新：当日首次实测卡在匿名 Cookie 获取阶段，随后通过 TikTok 自身的匿名注册接口修复，本机与雅加达服务器均已取得真实弹幕。见 [连接修复与实测](../deploy/piratetok-repair-2026-09-09.md)，以及保留的 [修复前失败记录](../deploy/piratetok-experiment-2026-09-09.md)。下文保留研究阶段的源码结论和原定验证方案，不代表最新实测状态。

核查日期：2026-09-09。范围为 `tiktok.com` 国际版直播弹幕，判断标准为保留现有业务字段、消息去重语义和连接体验。本轮完成公开资料检索与关键源码核查，没有切换生产采集器，也没有执行候选方案的真实直播连接测试。

**结论：存在有源码支撑的无显式 URL 签名直连候选，也存在由真实浏览器负责连接的方案。当前项目依赖 Euler Stream，不代表所有接入方式都必须依赖它。** 最值得先验证的是 PirateTok 的直连路径；浏览器原始 WebSocket 帧采集适合作为第二条路线。两者目前都没有在本项目服务器上通过验收。

“不用填写 API Key”“不调用第三方签名服务”“请求完全不带签名”是三个不同条件。浏览器可以自己完成签名和会话管理；第三方托管弹幕服务也可能只是把签名藏在服务内部。

| 方案 | 独立第三方签名服务 | 显式请求签名 | 当前证据 | 本项目判断 |
| --- | --- | --- | --- | --- |
| PirateTok 直连 | 不需要 | 已核连接 URL 不含签名参数；仍使用匿名设备 Cookie | Python、JS、Rust 连接源码，近期使用者 PR | 第一优先的隔离验证候选 |
| 真实浏览器的原始 WS 帧采集 | 可不使用 | 网站自己处理；不能称底层完全无签名 | 开源扩展实现、Playwright 官方接口 | 第二优先，可争取保留原始字段 |
| 浏览器页面文字采集 | 可不使用 | 网站自己处理 | Social Stream Ninja 文档与 DOM 源码 | 不适合直接替代现有业务协议 |
| 浏览器本地签名 | 不使用 Euler | 仍然签名 | Social Stream Ninja 功能说明与作者技术说明 | 备选，完整实现未独立核验 |
| 切换主流语言 SDK | 默认仍使用 | 默认仍签名 | TikTokLive、TikTok-Live-Connector 文档 | 不能仅靠换语言解决依赖 |
| 其他托管弹幕 API | 由提供方处理 | 可能由提供方隐藏 | 服务方接口文档 | 是换供应商，不是证实完全免签名 |

**PirateTok：最接近“真正不签名”的候选**

已审读 Python `ee836405ef122d7e2dd3403d99b91cb0d967977e`、JavaScript `ad822caecf91c494580102e3c1ded6f98ace71be`、Rust `092930f7090650f161d85e13dce222bcc12b765e` 的连接相关源码。三个实现都直接访问 TikTok，未发现连接流程调用 Euler 或其他签名提供方。[Python 项目](https://github.com/PirateTok/live-py)、[JavaScript 项目](https://github.com/PirateTok/live-js)、[Rust 项目](https://github.com/PirateTok/live-rs)

Python 实现先取得匿名 `ttwid` Cookie 与直播间 ID，再自行构造 `/webcast/im/ws_proxy/ws_reuse_supplement/` 的 WSS 地址。该 URL 没有 `X-Bogus`、`X-Gnarly`、`signature` 参数，浏览器属性、房间 ID 等参数在本地拼接。这里的“无签名”指不额外生成这些 URL 签名，并不表示不使用 Cookie 或其他连接参数。[固定版本 URL 构造](https://github.com/PirateTok/live-py/blob/ee836405ef122d7e2dd3403d99b91cb0d967977e/piratetok_live/connection/url.py)、[匿名 Cookie 获取](https://github.com/PirateTok/live-py/blob/ee836405ef122d7e2dd3403d99b91cb0d967977e/piratetok_live/auth/ttwid.py)

它包含入房、心跳、ACK、gzip 与 Protobuf 解析代码，因此不只是返回直播视频地址或房间元信息的项目。不过，源码证明的是实现方式，不能单独证明当前所有地域、IP 和房间都能连通。[连接与收包实现](https://github.com/PirateTok/live-py/blob/ee836405ef122d7e2dd3403d99b91cb0d967977e/piratetok_live/connection/wss.py)

2026-08-19 提交的 Rust PR 报告匿名 Cookie 获取间歇失败，作者样本约五次成功一次；采用缓存等改动后，后续连接约 3 秒。核查时该 PR 仍未合并。这是使用者自述，不是本项目的测量，但明确暴露出新的关键依赖：TikTok 是否正常发放匿名设备 Cookie。[PR #2](https://github.com/PirateTok/live-rs/pull/2)

项目的历史帧回放数据可以验证消息解码的一致性，不能替代当前网络上的握手、长连接及消息完整性测试。[回放数据仓库](https://github.com/PirateTok/live-testdata)

源码审查还发现 Python 客户端不适合整包替换现有服务：异步连接函数内调用同步 HTTP，握手之前发送 connected 事件，默认收到数据超时为 60 秒，停止标记不能立即打断正在等待的收包。这些会削弱目前已经实现的连接预算和取消行为。[客户端生命周期](https://github.com/PirateTok/live-py/blob/ee836405ef122d7e2dd3403d99b91cb0d967977e/piratetok_live/client.py)、[WSS 实现](https://github.com/PirateTok/live-py/blob/ee836405ef122d7e2dd3403d99b91cb0d967977e/piratetok_live/connection/wss.py)

**浏览器原始帧：可以摆脱 Euler，保留结构化消息的机会更大**

真实浏览器访问直播页，TikTok 网页负责自己的连接；采集器旁路读取浏览器已收到的数据。`nglmercer/wshook-extension` 有包装 WebSocket、解压并解析 TikTok 消息的代码先例，聊天事件保留原始消息 ID、用户对象及内容。该版本同时干预 ACK、重连，不能原样作为生产组件。[扩展实现](https://github.com/nglmercer/wshook-extension/blob/main/injected.js)

本项目可采用被动读取方式：监听页面 WebSocket 的 `framereceived`，将二进制数据交给现有解码层；由页面负责自己的 ACK 和心跳，采集器不重复发送。Playwright 官方明确提供收到帧、连接关闭和 socket 错误事件。该架构是基于接口与先例的工程建议，尚未完成本项目原型验证。[Playwright WebSocket 接口](https://playwright.dev/python/docs/api/class-websocket)

这条路线仍受正常页面可用性、登录状态、验证页面和浏览器进程故障影响。Social Stream Ninja 对页面模式明确列出了 CAPTCHA、重定向、聊天不可见等问题。浏览器还增加 CPU、内存与页面加载成本，现有 2 核 4 GiB 服务器能稳定承担多少房间必须实际测量。[页面模式说明](https://socialstream.ninja/docs/tiktok-app-modes-guide.html)

**页面文字采集与本地签名要分别看待**

Social Stream Ninja 的 Standard 模式直接读取直播页面；Local Signer 则在本地 TikTok 窗口中生成签名。其 Auto、Polling 模式仍使用 Euler，切成 Polling 并不能自动去掉该依赖。[签名模式说明](https://socialstream.ninja/docs/tiktok-signing.html)

已核的 DOM 采集代码输出显示姓名、文本、头像、徽章等数据，没有直接保留本项目需要的原始消息 ID 与用户唯一 ID。代码说明页面的 `data-index` 是循环使用的节点槽位；其普通评论去重按姓名与正文判断，可能误删同一人真实重复发送的相同内容。不能以这个策略替换现有按源消息 ID 的去重。[DOM 采集源码](https://github.com/steveseguin/social_stream/blob/main/sources/tiktok.js)

Local Signer 的作者技术说明描述了 TikTok 页面环境内生成签名的机制，但所引用完整 Electron 签名模块未在本次公开源码核查中取得。因此只能认定为有产品与作者文档支持的路线，不能声称已独立审计其完整实现。[作者技术说明](https://github.com/steveseguin/social_stream/blob/main/docs/agents/08-platform-sources/tiktok-standalone-app.md)

**容易误认成替代方案的结果**

- 主流 TikTok-Live-Connector 仍将连接签名交给 Euler；其迁移说明还删除了旧轮询选项。旧教程中的“免登录”“HTTP 轮询”不能作为当前免签名连接的保证。[项目文档](https://github.com/zerodytrash/TikTok-Live-Connector)、[迁移说明](https://github.com/zerodytrash/TikTok-Live-Connector/blob/ts-rewrite/BREAKING.md)
- All-Chat 文档虽然提供 `self` / `shadow` 选项，但明确写明自有签名器未实现时仍回退 Euler。其已验证的免 Euler 测试针对房间解析与开播查询，不是完整弹幕连接。[All-Chat 文档](https://github.com/caesarakalaeii/all-chat/blob/main/services/tiktok-listener/README.md)
- Tik.Tools 的接口明确列有签名及预签名凭据功能。接入其托管数据服务属于替换供应方，不能据此认定 TikTok 连接完全无签名；本轮未验证其服务质量。[提供方 API 文档](https://github.com/tiktool/docs/blob/main/rest-api/overview.mdx)
- 返回主播、在线状态、观众数或 HLS/FLV 地址，只解决元信息或视频流，不证明能取得持续弹幕。搜索结果中的国内抖音项目也不能直接证明国际版可用。
- 本轮未在 TikTok 公开开发者目录找到适用于任意直播间实时弹幕的通用接口。这个结论只限公开资料，不排除合作方或特定业务计划的专用接口。[TikTok 开发者目录](https://developers.tiktok.com/docs/en/welcome)

**对现有项目的建议与验收顺序**

优先做独立的无签名接入原型，借鉴 PirateTok 的连接协议，继续使用现有 Python 解码、队列、消息 ID 去重、Redis 业务转换与 WSS 输出。无需先重写全部架构。协议兼容性仍要用真实字段检查确认，尤其是 `dyMsgId`、`dyRoomId`、用户唯一标识、昵称及正文。

原型与当前 Euler 采集器先并行观察同一房间，但只有现有采集器输出给业务客户端。这样可以比较来源消息集合而不制造双路重复转发；两个采集器都没有收到的消息仍无法由这种对照发现，不能把交集比较等同绝对完整率。

建议按以下顺序验收，这些是未来验收目标而非本轮测试结果：

1. 在当前雅加达服务器及至少另一个正常网络上，以少量正常公开直播间确认无签名握手和持续新弹幕；验证不向 Euler 或其他签名提供方发请求。
2. 分别测首次匿名 Cookie 获取、已有有效 Cookie 的连接、明确断线后的恢复。记录成功率及 P50/P95；遇到平台拒绝、验证或限流如实分类，不靠高频重试掩盖。
3. 对照原始消息 ID 检查历史重放、漏收、重复及业务字段；同文本不同消息 ID 必须都保留。
4. 逐步做 30 分钟、2 小时、24 小时运行验证，观察真实新消息、内存、CPU、任务数量与恢复次数。高并发容量另外测，不能由单房间稳定推断。
5. 符合业务与延迟目标后，按房间小范围切换并保留回退开关。回退仍须遵守所选提供方限流；同一时刻只允许一个采集来源向业务层发出事件。

如无签名路径在目标环境不能稳定通过，再验证浏览器原始帧路线。DOM 文字抓取不作为默认降级，因为缺失唯一标识可能违反“不改变原业务”的要求。

当前结论支持投入小规模原型验证，不支持直接宣称已找到永久免签、无限制、秒连且可立即替换生产的方案。
