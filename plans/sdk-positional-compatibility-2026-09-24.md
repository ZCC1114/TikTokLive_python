# 待处理：合并后旧 SDK 位置参数不兼容

- 状态：**已复现，尚未修复**。按用户要求记录并交接，留给另一台电脑处理。
- 发现日期：2026-09-24。
- 问题所在提交：`c0e46b028f181541fea2de09d774fbb4dbc726f6`（合并到 `main`）。
- 合并前功能分支：`6623d7f9560c2302c18aee89ceb7ad952c154a43`。
- 合并前 `main`：`e9547493d7449505d1b54833fd036d4138b1f6d4`。
- 本交接只修改文档，没有修复代码或部署服务器。

## 影响范围

这里的“代理”是 HTTP 网络代理，不是业务代理商，也不是 PirateTok 的不同版本。问题是旧 SDK 的位置参数含义发生变化。

1. `TikTokLiveClient("creator", httpx.Proxy(...))` 原本把第二个参数作为 `web_proxy`；合并后第二个参数是 `platform`，连接时出现 `AttributeError: 'Proxy' object has no attribute 'value'`。
2. `await client.web.fetch_signed_websocket(7)` 原本把 `7` 作为 `room_id`；合并后第一个参数是 `platform`，出现 `AttributeError: 'int' object has no attribute 'value'`。
3. 更多位置参数也可能随之错位，不能只对上述两个例子做特判。

正式弹幕服务通过 `live_service/manager.py` 创建 `AnonymousLiveClient`，不走这两个旧签名 SDK 入口，因此已核查的正式调用路径不受此问题影响。手机端接口和业务消息格式无需因这项修复调整。这次复核没有证明该问题与弹幕重复或重连延迟有关，也没有重新执行线上直播长稳测试。

## 涉及代码及根因

- [TikTokLiveClient.__init__](../TikTokLive/client/client.py)
- [FetchSignedWebSocketRoute.__call__](../TikTokLive/client/web/routes/fetch_signed_websocket.py)
- [已有合并兼容测试](../tests/test_sdk_merge_compatibility.py)

合并保留了原 `main` 的 `WebcastPlatform` 支持，但没有兼容功能分支原有的位置参数顺序：

```text
构造函数，合并前功能分支：
  (unique_id, web_proxy, ws_proxy, web_kwargs, ws_kwargs, is_userid)
构造函数，合并后 main：
  (unique_id, platform, web_proxy, ws_proxy, web_kwargs, ws_kwargs, is_user_id,
   *, is_userid=None)

签名路由，合并前功能分支：
  (room_id, preferred_agent_ids, session_id, tt_target_idc, *, ...)
签名路由，合并后 main：
  (platform, room_id, session_id, tt_target_idc, *, preferred_agent_ids, ...)
```

以上只展示参数顺序，完整默认值以对应提交代码为准。`platform` 在合并前 `main` 就存在，修复时也必须保留这套调用方式。

## 本地复现（不连接真实 TikTok 或签名服务）

在已安装测试依赖和项目的虚拟环境中运行下面的脚本。签名 HTTP 请求已替换为 mock，代理地址只是占位示例，不需要搭建代理。

```python
import asyncio
from unittest.mock import AsyncMock

import httpx
from TikTokLive import TikTokLiveClient


async def reproduce():
    client = TikTokLiveClient("creator", httpx.Proxy("http://127.0.0.1:9"))
    request = AsyncMock(side_effect=httpx.ReadTimeout("mock request reached"))
    client.web.signer.client.get = request
    try:
        await client.start(room_id=7, fetch_live_check=False, sign_api_retries=0)
    except Exception as exc:
        print("constructor:", type(exc).__name__, str(exc))
        print("mock signing requests:", request.await_count)
    finally:
        await client.disconnect(close_client=True)

    client = TikTokLiveClient("creator")
    request = AsyncMock(side_effect=httpx.ReadTimeout("mock request reached"))
    client.web.signer.client.get = request
    try:
        await client.web.fetch_signed_websocket(7, retries=0)
    except Exception as exc:
        print("route:", type(exc).__name__, str(exc))
        print("mock signing requests:", request.await_count)
    finally:
        await client.disconnect(close_client=True)


asyncio.run(reproduce())
```

当前有问题版本的两个结果分别是上述 `Proxy` / `int` 的 `AttributeError`，mock 请求次数均为 `0`。修复后应正确解析参数并到达 mock，输出预设的 `ReadTimeout`；这只是确认到达请求边界，不代表真实上游连接成功。

## 建议修复方案

1. 为两个入口增加清晰的参数兼容层，识别显式 `WebcastPlatform` 和旧的代理／房间号参数，统一转换成内部关键字参数。支持合并前两边的合法调用方式。
2. 不要仅恢复旧参数顺序，否则会破坏原 `main` 的平台位置参数调用；也不要只吞掉异常或把错误参数强制转成默认平台。
3. 仓库内调用统一使用 `platform=...`、`web_proxy=...`、`room_id=...` 等关键字参数。仅改内部调用不能代替公共入口的兼容处理。
4. 明确处理默认参数、显式 `None`、多个位置参数、位置与关键字混用，以及重复赋值等非法调用。保留 `is_user_id` / `is_userid` 两种现有关键字的兼容行为。
5. 修复只涉及通用 SDK 的入口兼容，不更换正式无签名连接器，不改变手机端协议或业务流程。

## 测试与验收

- 补充上述两例的回归测试，断言最终参数含义正确，而不只是“不报错”。构造函数需确认代理落到 HTTP 客户端；路由需确认房间号和平台正确传给 mock 请求。
- 覆盖旧格式、平台格式、全关键字、多个位置参数、显式 `None`、混合传参及非法重复参数。
- 保持已有 WEB/MOBILE 平台测试、移动端会话要求、超时与重试、数字用户 ID 和 SuperFan 事件测试通过。
- 运行完整 `python -m pytest -q`、`python -m pip check` 和 [.github/workflows/tests.yml](../.github/workflows/tests.yml) 中的 Ruff 检查；确认 Python 3.10 / 3.11 / 3.12 CI 通过。
- 完成后更新本文状态，记录修复提交和实际验证结果。

合并提交曾通过 192 项本地测试及 [三版本 CI](https://github.com/ZCC1114/TikTokLive_python/actions/runs/36003356861)。这些测试未覆盖旧位置参数调用，所以通过不能证明这项兼容问题不存在。
