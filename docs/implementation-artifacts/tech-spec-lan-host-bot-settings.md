---
title: 局域网服务端配置跟骑机器人
type: bugfix
created: 2026-11-13
status: done
baseline_commit: e5447dd2f6650e9c4074ad4fc992684b5cd6aee0
context: []
---

# 局域网服务端配置跟骑机器人

<frozen-after-approval reason="用户已选择由服务端指定一名玩家，沿用一支跟骑车队">

## Intent

**Problem:** 桌面机器人编辑器只允许单机模式，保存还会强制绑定单机资料；创建局域网无法为实际远端账号配置机器人。

**Approach:** 建房端在启动前和运行中复用机器人编辑器，增加明确的跟随玩家 ID，保存至原配置并由现有多人服务实时读取。纯服务器与本机参与骑行均适用，附近玩家共享可见。

## Boundaries & Constraints

**Always:** 单机仍自动选择原单机资料；局域网保留并允许修改所选玩家，目标未骑行时不生成位置；继续原子保存、字段校验和扩展参数保留。启动/授权、停止/还原和退出期间禁止新开编辑器，已打开草稿可留存。

**Ask First:** 自动给每位玩家建立独立车队，或改变现有跟随/广播语义。

**Never:** 允许加入端修改服务器配置；新增远程管理接口；改动用户已有 `.gitignore`、账号和实际系统配置；提交或推送代码。

## I/O & Edge-Case Matrix

| 场景 | 输入/状态 | 结果/错误处理 |
| --- | --- | --- |
| 建房前保存 | 本机骑行开/关，指定玩家 47 | 显示人数和绑定，重开仍为 47 |
| 建房运行中保存 | 改人数/名称/玩家 ID、启停 | 下一轮检测更新，旧机器人移除 |
| 远端账号骑行 | 服务器无本机 Zwift | 只跟随目标玩家，附近其他玩家可见 |
| 无效玩家 ID | 空值、布尔、非整数、非正数、超出 int64 | 定位并提示错误，文件和草稿保留 |
| 加入模式 | 启动前或运行中 | 不显示/开放本地机器人编辑 |
| 配置损坏 | 建房启动或编辑 | 显示错误，原文件不被覆盖 |
| 编辑时停止 | 原草稿尚未保存 | 保留草稿，保存提示下次服务器启动使用 |

</frozen-after-approval>

## Code Map

- `windows_gui.py`：模式区域、配置弹窗、会话状态与摘要。
- `windows_bot_config.py`：轻量配置加载、校验和原子写入。
- `standalone.py`、`solo_blob.py`：已支持多人玩家状态输入、跟随和共享机器人注册表。

## Tasks & Acceptance

**Execution:**
- [x] `windows_bot_config.py`：增加显式玩家绑定保存入口，默认保留单机规则。
- [x] `windows_gui.py`：建房可见入口及目标 ID 编辑、模式对应摘要与保存提示；建房启动校验配置。
- [x] `tests/test_windows_bot_config.py`、`tests/test_windows_gui.py`：覆盖显式绑定、非法 ID、建房前/运行中/停止状态、取消及加入隔离。
- [x] `tests/test_solo_blob_live_config.py`：用两个玩家真实状态验证远端绑定、切换跟随及实时启停。
- [x] `README.md`、`运行.md`：说明建房配置、ID 来源、纯服务器与共享可见条件。

**Acceptance Criteria:**
- Given 建房端绑定非单机资料的玩家，When 保存并启动多人服务，Then 服务加载目标账号和所选人数。
- Given 两名玩家持续骑行，When 运行中切换绑定并保存，Then 车队跟随新目标且不残留旧机器人。
- Given 单机或加入模式，When 使用原有启动流程，Then 单机绑定和加入连接行为继续成立。

## Verification

- `.venv-build/Scripts/python.exe -m unittest discover -s tests`：原 158 项与新增回归通过。
- 独立临时目录和替代 loopback 端口运行真实打包多人服务，检查 HTTPS 状态、热更新、关闭及父连接退出。
- 检查隐藏 Tk 的入口、目标字段、保存回显与布局；构建最新 EXE，保留实际游戏端显示验证的边界。

## Spec Change Log

## Results

- 基线：158 项测试通过；原有关闭辅助连接测试偶发后台 BrokenPipeError，未改变测试结果。
- 实现后：175 项测试通过，新增 17 项；原有后台连接关闭诊断与基线一致。
- 三项独立审查（Blind Hunter、Edge Case Hunter、Acceptance Auditor）无剩余可行动发现。
- 通过标准构建脚本生成 `dist/zoffline.exe`，大小 34,665,295 字节；SHA-256：`0e41c20477f13e762b21a601375ae9348e77bc8bd799210e20bd317dfee649e7`。
- 从最终 EXE 提取实际归档的配置、GUI、引擎模块，运行 92 项相关回归全部通过且无跳过；归档不含玩家 storage/logs。报告：`build/lan-bots-bundle-report.json`。
- 临时中文空格路径、替代 loopback 端口运行最终 EXE 的真实多人 worker：可信 HTTPS 健康检查、纯服务器、两名加密 UDP 模拟客户端共享同一车队及读取中文资料、非目标独自骑行不生成车队、目标 2→1 与人数/名称实时修改、关闭/开启及父连接断开退出均通过。首次配置更新观测 0.203 秒。报告：`build/lan-bot-server-report.json`，脚本：`build/smoke_lan_bots_server.py`。
- 已检查建房运行中入口和目标字段布局，截图：`build/lan-bots-main.png`、`build/lan-bots-settings.png`。未启动实际 Zwift 游戏，未修改实际 hosts/系统证书/防火墙，未验证真实跨机游戏显示。
