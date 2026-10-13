# 问卷自动填写工具

Python **3.10+**，仅标准库，无需 pip 安装。Python 代码在项目根目录，测试在 `tests/`。

仅用于你有权使用的问卷、微信账号及姓名数据。随机答案不代表真实受访者意见，请确认问卷方允许这种填答方式。不会伪造微信身份或绕过服务端限制。

## 直接运行

**现在直接运行 `main.py` 会使用当前账号自动尝试完成本轮，默认最多 10 次，每次成功后等待 301 秒再继续。** 已将本次抓包登录返回的 openid 填入本地 `local_settings.py`。不需要先导入账号、不需要传参：

```powershell
python main.py
```

启动后每轮先查该账号历史、检查可填写状态和问卷结构，再发送提交。默认目标是本轮最多 **10 次**：每次收到明确“提交成功”后，从完成时等待 **301 秒**，再检查并提交下一次；到账号十次总额度、达到本轮十次、按 Ctrl+C 或遇到异常时停止，不会无限循环。若启动时账号仍在冷却，默认先等待剩余时间再继续。

### 换微信号只改一行

在 `local_settings.py` 修改：

```python
OPENID = "新账号在这个小程序中的真实openid"
```

保存后重新运行 `python main.py` 即可。此配置已被 Git 忽略，避免泄露身份；`local_settings.example.py` 是可提交的配置示例。代码不缓存配置，每次启动重新读取。

- **不同 openid 没有共享五分钟冷却**，新账号若自身满足额度和服务端条件，可立即提交。
- 同一 openid 累计最多 **10 次**，每次间隔至少 **300 秒**。切回原账号时保留原来的次数和冷却，不会重置。
- 每账号独立并发保护：同一账号的 pending 会阻止重复提交，不影响其他账号。
- 不需要删除状态库，也不要通过删除状态库重置次数。

### 可选命令

```powershell
# 只预览，绝不提交
python main.py preview
python main.py --dry-run

# 临时指定 openid，不改配置（也支持放在 submit 子命令之后）
python main.py --openid "新账号openid"

# 查询默认当前账号的历史，不提交
python main.py status --refresh

# 默认就是最多10次、自动等待301秒；以下写法等价
python main.py --count 10 --wait

# 只提交一次，或遇冷却不自动等待
python main.py --count 1
python main.py --no-wait

# 原有子命令仍支持；submit 默认真实执行，--execute 仅兼容旧命令
python main.py submit --count 10 --wait
python main.py submit --openid "新账号openid" --dry-run
```

`--openid` 优先于默认配置，也可选用 `--account 别名`；没有二者时才用 `local_settings.py` 的 `OPENID`。请勿同时指定不同的 openid 和账号别名。显式空 openid 会报错，不会悄悄使用旧身份。

`--capture`、`--names` 放在子命令前，例如：

```powershell
python main.py --capture capture/new_Full.txt --names names.json preview
```

默认文件路径相对脚本所在项目根目录，不受启动目录影响；显式传入的相对路径相对启动目录。

## 是否只需要 openid？

**就这份抓包观察到的业务请求来说，是的：唯一需要随微信账号更换的身份字段是 `userId=openid`。** 程序已经保留服务器地址、Referer、问卷标识等公共参数，使用者只需更换该小程序内的真实 openid。

抓包链路：

1. 小程序 AppID：`wxfacea951a0cbea7c`。
2. `wx.login()` 获取临时 code。
3. `POST /captcha/wxlogin?code=...`，请求体 `{}`，业务服务器返回 `openid` 和 `session_key`。
4. 问卷查询和提交传 `userId=openid`；**未观察到 Authorization、Cookie、签名或 session_key**。

这只是单份抓包的观测，不能保证服务端永远没有额外风控或授权要求。openid 不是微信昵称/微信号，也不能从昵称推导；必须使用新账号在**同一个小程序**内的登录结果。被服务端拒绝时程序停止，不会绕过限制。

不需要重新提交问卷来获取身份。在新微信账号打开小程序并触发登录，抓取 `/captcha/wxlogin` 成功响应即可获取 openid；也保留自动解析到本地别名的功能：

```powershell
python main.py import-account --account wx2 --from-capture capture/wx2_Full.txt
python main.py --account wx2
python main.py accounts
```

导入别名不会改写 `local_settings.py` 的默认账号；如果之后希望继续无参运行，仍需修改配置中的 `OPENID`。不保存 session_key，不重放抓包中已使用的 code，不自动读取微信进程内存或切换客户端账号。

如果有权在开发/调试环境获取尚未使用的新 code，可用：

```powershell
python main.py login-code --account wx2
# 或从已忽略的本地文件读取，避免写入命令历史
python main.py login-code --account wx2 --code-file .local/wx-code.txt
```

官方文档：[`wx.login` code 有效期五分钟](https://developers.weixin.qq.com/miniprogram/dev/api/open-api/login/wx.login.html)，[临时 code 只能使用一次，session_key 不应公开](https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/login.html)。code 有效期与问卷提交间隔是两回事；已经成功交换的旧 code 不应再次使用。

## 填答规则

- 抓包模板为 **45 题：42 单选，3 多选**。
- 从 `names.json` 的 **316 个去重姓名**等概率选一个；单选从该题的实际选项随机选择。
- 第 **14、15、30** 题的多选对象、`optionIds` 及顺序保持抓包一致。
- 每次真实提交前检查线上题目与模板一致；结构变化、未知题型或联动规则都停止。
- 保留抓包中的问卷公共参数。手机号/凭证为空，位置字段为“经度/纬度/地理位置”占位文字；未来模板若出现真实坐标、手机号或凭证，拒绝跨姓名/账号重放。

重新预处理姓名：

```powershell
python prepare_names.py
```

只提取 `data.campusResumes[].resume.name` 和 `data.socialResumes[].resume.name`，去空白、去重、排序，输出纯字符串数组 **`names.json`（不被 Git 忽略）**。不输出手机号、照片地址、简历 ID、学校等源资料；纯姓名仍可能是个人信息，公开前确认分享权限。

## 次数、冷却与异常

- 每个账号固定最多十次、服务端/本地最小间隔至少五分钟；自动循环使用 **301 秒**，多留一秒边界余量。不每天或重启自动重置。
- 日志均带本地时区时间、级别和本轮序号：开始检查、历史/剩余额度、等待及下次检查时间、每次成功、异常与最终汇总都会立即输出。日志不打印原始 openid，只显示账号指纹。
- 任何异常都会显示 ERROR 日志并停止，不自动重试异常请求；已发起但结果不确定的尝试仍保守计次。
- SQLite `.local/submissions.sqlite3` 保存账号散列、次数、时间、尝试结果。保留旧版状态库，旧版全局冷却不再参与是否可提交的判断。
- 每次同步 `queryTreUserWjByUserId` 的历史，和本地数量取保守较大值。该接口仅返回问卷名称、记录 ID、时间，没有问卷 ID，因此保守计入返回的全部历史，不按名称猜测过滤。服务端范围和未公开分页不能由一个抓包确认，服务端资格检查仍是必要条件。
- 历史时间按 UTC+8 解析并加一秒覆盖秒级精度。冷却从请求结束计算，时间回拨不提前放行。
- 预留并发起请求后即保守消耗一次。网络中断、未知返回、业务拒绝不自动退款/重试，避免已写入却没收到响应导致重复提交。
- HTTP 200 本身不是成功；只有业务 `status:200` 且 `message:"提交成功"` 才记成功。
- 不跟随重定向，不禁用 TLS 证书检查，不打印含 code/openid 的完整请求 URL 或原始响应。
- 硬退出留下的 pending 只阻止对应账号。下次启动会先查询服务端历史；若历史次数和时间明确覆盖该 pending，会自动确认其已经成功并继续冷却，绝不会重发这一笔。没有足够证据时仍保持阻止。确认相关提交程序都已停止，在小程序核实结果后可显式恢复：

```powershell
python main.py recover-pending --confirm-stopped
```

恢复所有遗留 pending 为 unknown，保留次数，并让这些账号从恢复时重新冷却五分钟，不影响其他账号。

其他设备的手动填答无法共享本地锁，以历史接口和服务端限制为最终约束。不要人为改系统时钟；文件/数据库损坏会停止，不自动清空。

## 一次真实测试结果

经用户明确授权，已于 **2026-10-13 12:26:05（UTC+8）** 执行一次无参数 `python main.py`：服务端返回“提交成功”。提交前历史 **1 条**，提交后独立查询为 **2 条**；查询时账号剩余 **8 次**（这是测试时快照，以最新状态为准）。此次仅发送一份问卷，没有重复试交。仍按此前需求随机姓名/单选，保留抓包多选及公共参数。

## 测试与文件

```powershell
python -m unittest discover -s tests -v
```

测试用临时数据库、合成问卷、模拟 HTTP 和时钟。包括**无参 main() 模拟完成10次、9次301秒等待及日志**、异常日志与停止、配置改 openid、切号立即可填、原账号仍冷却、十次额度、并发、重启、不确定结果与离线预览等；测试本身不发真实请求。可选真实抓包集成测试只读文件，缺失时跳过。

| 文件 | 用途 |
| --- | --- |
| `main.py` | 直接运行入口、可选 CLI、提交循环 |
| `local_settings.py` | 本地默认 OPENID，换号修改这里（Git 忽略） |
| `local_settings.example.py` | 可提交的配置示例 |
| `questionnaire.py` | 构造答案、校验结构 |
| `capture_parser.py` | Full 抓包解析 |
| `wechat_client.py` | 微信业务 API、本地账号别名 |
| `submission_state.py` | 每账号次数、间隔及并发保护 |
| `prepare_names.py` / `names.json` | 姓名预处理 / 纯姓名列表 |
| `tests/` | 离线测试 |

`.gitignore` 忽略原始 `capture/`、`name-source/`、本地配置、`.local/` 和数据库。原始身份配置是本地明文，请保护文件权限。Git 已初始化，未自动提交或推送。
