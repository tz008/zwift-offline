---
title: Sandboxie Plus 最小双开试验
type: feature
created: 2026-11-12
status: done
baseline_commit: af1f678
context:
  - 运行.md
---

# Sandboxie Plus 最小双开试验

<frozen-after-approval reason="用户已要求按 Sandboxie Plus 最小修改并尝试可行性">

## Intent

现有桌面程序已提供多人服务器及临时客户端配置，但尚无隔离启动第二份 Zwift 的入口。本次仅验证同一 Windows 桌面的双开，不扩展正式 GUI。

使用现有 EXE 的“创建局域网 + 本机也参与骑行”启动服务和第一份普通客户端；等其进入地图后，通过独立脚本在一个预先创建的标准 Sandboxie Plus 沙箱中启动第二份客户端。

## Boundaries & Constraints

始终复用现有安装发现、可信 HTTPS 多人健康检查与全局配置生命周期。每个实例使用不同的服务器本地账号，依次进入地图。辅助脚本只查询 Sandboxie 配置并启动指定沙箱，不创建/修改/清空沙箱，不操作 hosts、证书、服务、安装程序或已有游戏进程。

用户请求限于最小可行性试验；安装驱动和人工登录、选图保留为明确的实际环境操作。当前机器有 `F:\Zwift`，未发现 Sandboxie Plus；不能将模拟启动验证写成真实双开成功。

## I/O & Edge-Case Matrix

| 场景 | 行为 |
| --- | --- |
| 参数 | 指定服务器 IPv4，默认沙箱名 `ZofflineZwift2`，支持安装目录覆盖与只检查模式 |
| 缺少 Sandboxie/Zwift | 显示具体缺少项及安装/参数提示，非零退出，不启动游戏 |
| 沙箱不存在、禁用或不可访问 | 提示在 Sandboxie Plus 新建专用标准沙箱，非零退出 |
| 指定沙箱已有进程 | 拒绝重复启动，保留沙箱和进程，提示先正常退出 |
| 服务不可达、单机模式或客户端域名未指向服务器 | 非零退出，不启动游戏，不修改全局设置 |
| `--check` | 完成同样前置检查并打印命令，不启动程序、不写配置 |
| 正常启动 | 参数列表调用官方 `Start.exe /box:<name>`，安装目录作为 cwd；退出码非零时明确失败；仅报告启动请求，双开进图需手动确认 |

</frozen-after-approval>

## Code Map

- `zwift_installation.py`：复用 Zwift 安装发现。
- `windows_network.py`：复用服务器地址校验、TCP 与可信 HTTPS 多人健康检查。
- `windows_client_config.py`：复用需要验证的客户端域名清单，只读。
- `windows_launcher.py` / `windows_session.py`：维持现有 GUI 和全局配置锁，不改动。

## Tasks & Acceptance

- [x] `scripts/launch_zwift_sandboxie.py`：新增标准库试验入口，检测 Sandboxie 工具、可用沙箱、运行进程、服务器及域名配置；只检查或启动第二份游戏。
- [x] `sandboxie.cmd`：优先使用项目 Python 环境，转发参数及退出码，让用户不必挑选解释器。
- [x] `运行.md`：补充安装链接、专用标准沙箱、第一份普通客户端与第二份沙箱客户端的操作、退出顺序、缓存证书说明和实际验证范围。

Given 多人服务和第一份游戏已就绪且沙箱为空，When 运行试验入口，Then 仅在指定沙箱内发出第二份启动请求，现有服务与游戏不被停止。

Given 任一前置条件失败或只检查模式，When 运行入口，Then 不启动游戏、不修改配置，缺少条件返回非零。

## Verification

验证 CLI 帮助、本机缺依赖路径、中文空格目录与参数传递；用临时假的 Sandboxie CLI 和隔离服务器/模拟探针覆盖只检查、缺失/禁用/运行中沙箱、错误服务、域名不匹配和启动失败。不为低影响辅助脚本添加永久测试套件；在可用环境执行实际双开，缺少 Sandboxie 时明确记录未完成。

## Implementation Record

实现完成：服务端、正式启动器、GUI 和单文件产物保持原有实现，新增独立试验入口，无需重新构建 EXE。只检查模式会运行官方只读查询；真实启动仅通过参数列表请求指定沙箱打开 Launcher。

独立审查确认 `SbieIni.exe query /boxes *` 与 `Start.exe /box:<name> /listpids` 的官方协议。修复显式选择另一安装时未验证其证书包、CMD 继承延迟展开以及不可用 Python 启动器阻断回退三个问题；证书按 DER 比较，支持 LF/CRLF。补充全新沙箱仍可能继承宿主机登录缓存的操作说明。试验命令按文档逐次执行；没有增加并发启动管理或 IPv6 网络支持。

执行 `python -B build/sandboxie-smoke.py`：26 项临时检查通过，覆盖仅检查不启动、缺失/禁用/运行中沙箱、错误进程协议、查询失败/超时、DNS 不匹配、服务验证失败、启动非零、中文空格参数、缺失/损坏/CRLF 证书、从其他工作目录执行 CMD 和退出码传递。真实 hosts 与 Zwift 的 cacert 文件哈希保持一致。该检查脚本位于忽略的 build 目录，不增加永久测试套件。

执行 `python -B -m unittest discover -s tests -p test_windows_network.py -v`：现有 11 项网络测试通过。Python AST 解析及补丁空白检查通过。审查者复测了 `cmd /v:on` 下的字面感叹号参数及证书分支，未发现新的需修复边界。

实际环境：自动发现 `F:\Zwift`；实际 `--check` 返回 1，明确报告缺少 Sandboxie Plus。TCP 443 当时被 `Steam++.Accelerator` 监听，客户端域名仍解析到官方公网地址。没有安装驱动、停止该进程、启动 Zwift 或修改系统配置。真实双开、独立账号、地图、UDP 同步及设备连接仍需安装 Sandboxie、释放端口并启动现有 GUI 后验证。

## Live Trial Record — 2026-11-12

用户提供 `D:\environment\Sandboxie-Plus` 后，识别 Plus 1.18.8，完成首次初始化并创建专用标准沙箱 `ZofflineZwift2`。用户释放 443 后，在 `build\sandboxie-live\runtime` 下复制既有 EXE，使用独立服务器数据启动多人模式；原版 Zwift 安装为 `F:\Zwift`，服务器地址为 `172.16.12.22`。临时 hosts、证书及防火墙均通过原有 GUI 生命周期处理。真实只读前置检查通过，沙箱命令执行及虚拟化文件落盘通过。

普通客户端使用测试账号 A，沙箱客户端使用账号 B。初始沙箱 Launcher 隐藏/黑屏，给该箱设置 `HideHostProcess=ZwiftApp.exe` 并仅重启试验沙箱后登录页恢复，账号 B 成功启动游戏。两份 Zwift 1.0.168455 同时进入 Watopia Seafoam Circuit；原生 PID 3580、沙箱 PID 15696 均响应，服务端显示玩家 1/2 在线，沙箱截图可见 A/B。稳定后沙箱 UDP 每分钟约 60 次收发、错误 0。证据保存于 `build\sandboxie-live\report.json` 和 `sandbox-game-live.png`。

第二份游戏首次蓝牙初始化在 15:26:25–15:31:26 间阻塞约 301 秒，随后恢复渲染与 UDP。原生客户端相同步骤当秒完成；官方兼容模板 `OpenBluetooth` 已补入试验箱。用户确认双开成功并要求自行关闭，因此未重启验证模板效果，也未继续验证活动保存、单实例退出后的存活或本次系统配置还原；所有游戏与服务保持运行交由用户操作。
