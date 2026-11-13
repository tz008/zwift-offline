---
title: 按姓名选择局域网机器人跟随玩家
type: bugfix
created: 2026-11-13
status: done
baseline_commit: e5447dd2f6650e9c4074ad4fc992684b5cd6aee0
baseline_snapshot: build/player-picker-baseline
context: []
---

# 按姓名选择局域网机器人跟随玩家

<frozen-after-approval reason="用户反馈无法从玩家 ID 判断实际玩家">

## Intent

**Problem:** 建房端只能填写玩家 ID，用户不知道数字对应谁，无法可靠选择跟随目标。

**Approach:** 将目标输入框改为可编辑的玩家下拉列表，显示服务器账号姓名和 ID，并给出对应登录账号以区分重名；保留直接输入 ID 和原有实时保存行为。

## Boundaries & Constraints

**Always:** 从当前 EXE/源码运行数据目录的同一数据库只读查询账号；姓名包含中文时正确显示；列表按数字 ID 排序并区分重名。缺少数据库或账号时不创建数据，提示先注册并刷新；列表读取失败时保留草稿及此前可选列表，并允许手填 ID。

**Ask First:** 创建或修改账号、自动指定其他玩家、远程管理其他服务器。

**Never:** 查询密码或凭据列；引入服务端/protobuf 导入副作用；修改引擎跟随语义或用户工作区的其他变更；提交或推送。

## I/O & Edge-Case Matrix

| 场景 | 输入/状态 | 结果 |
| --- | --- | --- |
| 已有账号 | 中文/英文姓名和非连续 ID | 列出姓名与 ID，当前保存的目标回显姓名，选中后保存数字 ID |
| 重名 | 相同姓名、不同账号 | 可查看登录账号并分别选择，绑定正确 ID |
| 新玩家注册/改名 | 编辑器仍打开 | 点刷新或展开列表重新读取，原目标 ID 不变 |
| 手动 ID | 目标尚未注册、无效草稿 | 有效数字继续可保存；无效值定位提示且原文件不变 |
| 空列表 | 无库或无 user 表/账号 | 不创建文件，不擅自换目标，提示注册和刷新 |
| 坏库/锁库 | 无法读取 | 用户看见具体错误，已选玩家和草稿保留，界面及时返回 |
| 单机/加入 | 原模式逻辑 | 单机不显示玩家选择，加入仍由建房端配置 |

</frozen-after-approval>

## Code Map

- `windows_bot_config.py`：轻量标准库配置辅助，适合增加只读账号查询。
- `windows_gui.py`：局域网配置弹窗、目标保存和主窗口摘要。
- `zwift_offline.py`：`User` 表含 `player_id`、`first_name`、`last_name`、`username`；游戏改名同步此表。

## Tasks & Acceptance

**Execution:**
- [x] `windows_bot_config.py`：只读查询玩家姓名/账号/ID，明确处理空库、schema 尚未建立、文件/锁错误，短超时且连接总能关闭。
- [x] `windows_gui.py`：可编辑下拉列表、刷新、账号提示，姓名→ID 通过显式映射保存，刷新保留目标与无效草稿；摘要有姓名时显示姓名。
- [x] `tests/test_windows_bot_config.py`、`tests/test_windows_gui.py`：真实临时 SQLite 与隐藏 Tk 验证列表、重名、刷新、回显、保存、空库/锁库与错误不写盘。
- [x] `README.md`、`运行.md`：用姓名选择玩家及新玩家刷新说明。

**Acceptance Criteria:**
- Given 两名同名账号，When 逐一选择并保存，Then 显示的登录账号可区分两人，文件绑定各自正确数字 ID。
- Given 当前目标被改名或又注册账号，When 刷新列表，Then 能看到最新信息且当前跟随目标及其他草稿不变。
- Given 服务器运行中，When 选择有姓名的玩家并保存，Then 继续使用现有热更新和服务器统一车队。

## Verification

- 运行全量 unittest 和差异检查；独立审查本轮增量，不重复评审已有建房修复。
- 检查带姓名/重名/空列表的实际 Tk 布局；重新构建 EXE，运行归档相关回归和真实多人 worker 的双模拟客户端验证。
- 打包保留本轮开始前已完成的默认装备解锁功能；不启动实际游戏或更改系统连接配置。

## Spec Change Log

- 审查将账号查询函数说明明确为“通过只读连接读取既有数据库”，避免误解为抑制 SQLite 自身正常的 WAL 辅助文件；账号查询与目标选择逻辑未改变。

## Results

- 全量 191 项测试通过，本轮新增 16 项；原有辅助连接关闭测试的后台 BrokenPipeError 与上轮基线一致。
- 三项独立增量审查无剩余可行动问题；真实 SQLite 覆盖中文和 URI 特殊路径、缺库缺表、坏库、锁库、禁止读取密码列与连接释放，Tk 验证重名、刷新、改名、删除、手填和运行状态。
- 初次标准构建因 `dist/zoffline.exe` 被占用无法覆盖，保留原文件，通过临时规格按同一资源清单构建 `dist/zoffline-player-picker.exe`；大小 34,666,525 字节，SHA-256：`dbc4b1fb9313eade443c58bbe4cdc82a2e959ce71241539e3cee0a01901dc48d`。
- 实际归档的配置、GUI、引擎模块通过 108 项相关测试，无跳过，归档不含 storage/logs。报告：`build/player-picker-bundle-report.json`。
- 最终 EXE 的真实多人 worker 在中文空格临时目录和替代 loopback 端口运行：两个独立账号通过加密 UDP 共享车队，使用最终包内 GUI 按姓名由 LAN 2 选择为 LAN 1 并保存，实际进程热更新人数和名称；关闭/开启、父连接断开退出检查均通过，首次配置更新观测 0.312 秒。报告：`build/player-picker-server-report.json`。
- 截图：`build/player-picker-main.png`、`build/player-picker-settings.png`，局部示例 `build/player-picker-choice.png`。未启动实际 Zwift 游戏或修改系统连接配置。
