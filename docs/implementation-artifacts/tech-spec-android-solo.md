---
title: Android 本机单机 APK
type: feature
created: '2026-11-12'
status: done
baseline_commit: e5447dd
context: [README.md, 运行.md]
---

# Android 本机单机 APK

## Intent

在 Android 手机上安装独立 APK，后台运行当前 Python 服务端，并将同一设备上的 Zwift 安卓客户端连接到本地服务。用户已要求按上一轮方案实现本机单机，不加入局域网功能。

## Boundaries & Constraints

复用现有 Flask、SQLite、HTTPS、TCP/UDP 骑行协议；保留 Windows 和源码启动。Android UI 与 VPN/服务进程分离；只有 Zwift 的流量进入 VPN。本地服务绑定回环，玩家存档位于应用私有目录。APK 只包含显式允许的公共代码、数据和证书，不包含构建者的 storage、账号或游戏资源。Zwift 本体和 OBB 补丁由用户准备；本 APK 显示引导和实际通信状态，不声称已自动完成第三方应用证书配置。Android 17 真机兼容性需实测。构建使用固定版本工具和依赖，产物可以直接侧载。

## I/O & Edge-Case Matrix

| 场景 | 行为 |
| --- | --- |
| 启动 | 请求系统 VPN 授权，启动前台通知和独立进程，Python 与 SOCKS 就绪后建立仅针对 Zwift 的 VPN |
| 拒绝 VPN / Zwift 未安装 | 显示具体原因，停止本次服务，不残留 VPN |
| 网络 | 合成 Zwift 域名解析，转发 TCP 80/443 与 UDP/TCP 中继，非 Zwift 服务的联网请求保留正常路径 |
| 证书未配置 | 显示需配置说明，只有真实游戏登录后显示已连接 |
| 停止 / VPN 撤销 / 启动失败 | 释放 TUN、网络转发、监听、唤醒锁和通知，允许再次启动；不得清除玩家数据 |
| 返回桌面 / 打开游戏 | 前台服务继续工作，UI 重连能恢复状态 |
| 更新 APK | 相同应用 ID 与签名保留私有数据；卸载前提醒备份 |

## Code Map

- `standalone.py`、`zwift_offline.py`、`solo_blob.py`：当前服务、协议和资源路径。
- `scripts/windows_resources.txt`：参考公共资源白名单，Android 使用独立清单。
- `android/`：新增 Gradle 工程、原生界面、VPN、SOCKS/DNS 转发和资源生成。
- `runtime_paths.py`、`embedded_wsgi.py`、`android_runtime.py`：嵌入式运行兼容层。

## Tasks & Acceptance

- [x] Python 路径注入、嵌入式 HTTPS/HTTP、就绪状态和停止接口。
- [x] Android 工程、固定依赖、公共资源打包、后台生命周期和本机设置页。
- [x] 本地 VPN、成熟 TUN TCP/IP 组件、SOCKS5 TCP/UDP 和域名重定向。
- [x] 原生启动/停止、Zwift 检查与启动、证书准备说明、实际连接状态和日志。
- [x] 构建脚本、APK、协议与生命周期验证、中文使用说明。

验收：Given 手机已安装 Zwift 并完成适用的证书补丁，When 用户授权 VPN 并启动服务和 Zwift，Then 手机可连接本机服务、完成骑行并保存进度。Given 启动失败或停止，When 再次启动，Then 不存在本次遗留监听或 VPN，原存档保留。Given 干净构建环境，When 执行文档构建命令，Then 生成含 ARM64 原生库和 Python 公共资源的签名 APK。

## Design Notes

Chaquopy 嵌入 Python；Android 使用原生控件，减少首版 UI 依赖。HTTPS 改用标准库线程化 WSGI，仅在显式选择 embedded 后端时启用。VPN 使用已发布的 hev-socks5-tunnel AAR，通过 SOCKS5 将客户端原始端口映射到本机高端口；避免自写 TCP 协议栈。独立进程正常停止监听后结束，消除 Python 全局线程和解释器重启问题。构建工具下载位于忽略目录，资源清单不包含用户文件。

## Verification

运行 Python 回归与隔离真实 HTTP/HTTPS/UDP 集成测试、Java SOCKS/DNS 测试、Gradle assembleDebug/lint。检查 APK 的 ABI、签名、权限和资源内容。记录未完成的真机 VPN、Android 17 后台存活、完整骑行和证书补丁验证，不能用桌面测试替代。

2026-11-12 验证结果：

- 162 项 Python 测试、10 项 Java 网络集成测试通过；Gradle `testDebugUnitTest lintDebug assembleDebug` 构建成功。
- Android 16 / API 36 x86_64 模拟器安装和启动 APK，真实前台服务、独立 Python 进程及 hev TUN 运行成功。
- 使用仅在 build 目录的临时协议探针（不是官方 Zwift 客户端）验证仅针对 Zwift 包名的实际 VPN 链路：三个域名解析到 198.18.0.1、原始 HTTP 80 资源获取、原始 HTTPS 443 证书和主机名校验、protobuf 游戏登录均成功。
- 模拟器验证设置页正常加载和保存、停止并重新启动后使用新服务进程；运行时切到另一应用，前台服务继续提供协议响应。停止后经系统文件选择器导出 ZIP，SQLite `integrity_check` 为 ok。
- APK 包含 ARM64 / x86_64，约 32 MiB；签名 v2 验证通过。检查主包和内嵌归档，无 storage、logs 或玩家数据库；232 个 ELF 文件加载段均满足 16 KB 对齐。
- 未验证：用户 Android 17 真机、官方 Zwift 版本和 OBB 补丁、真实蓝牙设备、完整骑行及长时间后台存活。代码交付完成，端到端骑行验收需这些实际条件。

## Review Record

完成独立差异、边界和验收审查。修复启动与清理并发获取 VPN 资源、旧服务进程退出前允许重启、存档导出与启动并发的问题；为 TCP/UDP 中继的基础端口和实际端口提供兼容映射。设置页限定为本机表单，移除桌面操作和外部样式表依赖。最低系统版本根据实际原生库约束确定为 Android 10。安卓 pycryptodome 固定为可用的 3.21.0，桌面依赖不变。
