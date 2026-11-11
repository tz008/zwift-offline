---
title: Windows 单文件构建与自动启动
type: feature
created: '2026-11-11'
status: complete
baseline_commit: f07743b41ddf7f36c08522dc035519ae55183a4e
context: [运行.md, README.md]
---

# Windows 单文件构建与自动启动

## Intent

现有项目为 Python 服务端，旧 PyInstaller 配置依赖签名工具，启动游戏需另开 Zwift Launcher。提供一个包含项目运行依赖的 EXE：发现并校验 Zwift，临时配置 hosts 和证书，在服务端完成监听后自动启动原版 Launcher，继续既有地图选择与游戏流程；退出后恢复启动前的配置。

## Boundaries & Constraints

保留源码服务端及跨平台用途、端口、业务参数和玩家数据；新增 Windows 专用入口。正常使用不要求 Python 或手填路径。Zwift 本体单独安装。按用户 2026-11-11 的追加要求，EXE 自动临时配置 Windows hosts、受信任证书及 Zwift data/cacert.pem，必要时触发 UAC，关闭后恢复运行前状态。预先存在的映射和证书不能被误删；备份、日志和个人数据不进入构建产物。只打包明确列出的项目资源和库，保留项目服务用 SSL 证书与密钥。

## I/O & Edge-Case Matrix

| 状态 | 行为 |
| --- | --- |
| 有效安装 | 目录需含 ZwiftLauncher.exe、ZwiftApp.exe、data/cacert.pem；服务监听成功后用参数列表和安装目录作为 cwd 打开 Launcher |
| 没有有效安装 | 显示“请先安装Zwift”的原生窗口，非零退出，导入服务前停止 |
| 无效缓存或注册表记录 | 跳过，继续候选；忽略大小写去重，按固定优先级选第一个有效安装 |
| 权限不足等访问错误 | 尽力检查其他候选；仍无有效结果时显示具体检查错误，不伪装成未安装 |
| 端口占用或启动失败 | 显示可见错误，安全退出并终止本次进程的服务线程 |
| 空格、中文或不同工作目录 | 资源按模块/EXE 绝对目录读取，数据写到 EXE 同级，正确传参 |
| 用户关闭窗口或 Ctrl+C | 撤销本次 hosts/cacert 变更，仅删除本次新增的受信任证书 |
| 主进程崩溃或被结束 | 提权辅助进程监测原用户进程连接，独立隐藏恢复进程监测辅助进程；任一方退出后还原，断电或全部进程被结束时保留恢复记录，下次启动先恢复 |
| 第二个实例、配置中途失败 | 同一台电脑只允许一个临时配置会话；应用失败回滚，既有用户文件不被覆盖 |
| 运行期间外部修改配置 | 保留无关 hosts 名称的有效映射；尽可能仅撤销本次变更并保留其他修改，冲突/还原失败明确提示且保留备份，独立尝试其余恢复项 |

## Code Map

- `standalone.py`：原服务顶层入口，保留原执行顺序。
- `zwift_offline.py`：HTTPS 服务与资源路径。
- `standalone.spec`：旧单文件构建配置。
- `scripts/configure_client.bat`：原安装路径缓存来源。

## Tasks & Acceptance

- [x] `windows_launcher.py`、`zwift_installation.py`：标准库完成入口及有界发现。缓存优先，其次 HKCU/HKLM 的 32/64 位 App Paths/卸载信息，再常见目录和固定本地磁盘明确候选；不递归扫描。
- [x] `standalone.py`、`zwift_offline.py`：可选就绪回调，在真正绑定 HTTPS 后调用；修正 protobuf 路径以模块为基准；原源码启动不自动打开游戏。
- [x] `build.cmd`、`scripts/build_windows.py`、`requirements-windows-build.txt`：64 位 Python 3.11/3.12 的隔离环境与固定构建依赖，单命令构建，失败非零退出。
- [x] `standalone.spec`、`scripts/windows_resources.txt`：现代 PyInstaller 单文件配置，显式资源清单，包含 garth/Discord 集成依赖，不强制签名或 UPX；输出 `dist/zoffline.exe`。
- [x] `.github/workflows/pyinstaller.yaml`、`.gitignore`：复用构建命令并忽略构建环境。
- [x] `tests/test_windows_startup.py`：安装存在/缺失、无效和不可访问候选、稳定优先级、注册表发现、路径及错误显示、启动顺序。
- [x] `README.md`、`运行.md`：构建、分发、首次客户端配置、日常启动与验证限制。
- [x] `windows_client_config.py`、Windows 入口及相关测试：持久化回滚记录、配置事务、系统证书 API、独立恢复进程、UAC、单实例互斥；常规关闭及主进程终止后恢复。

验收：Given 本机已有有效 Zwift，When 运行 EXE，Then 服务就绪并自动打开原 Launcher。Given 没有安装，When 双击 EXE，Then 精确可见提示并退出。Given 仅复制 EXE 至中文和空格目录，When 从其他目录启动，Then 内部资源及服务仍正常、数据保存在 EXE 同级。

## Verification

执行 `python -m unittest discover -s tests -v`、`build.cmd`、构建归档内容核查及实际 EXE 冒烟。优先在本机真实安装验证启动，并在不修改系统安装信息的隔离测试中覆盖未安装结果。区分实际验证与未提供的干净 Windows 环境/完整骑行验证。

## Spec Change Log

- 2026-11-11：用户追加“启动这个exe的时候自动hosts/证书配置，关闭exe就还原hosts/证书配置”，将首次手工配置改为每次 EXE 运行期间的临时配置。保留此前有效安装检测、单文件构建、资源白名单和服务就绪回调。
- 2026-11-11：独立审查确认需保护异常分支的 traceback 输出、避免 Unicode casefold 合并不同 Windows 路径，并容错注册表枚举期间删除条目；作为实现修补纳入。
