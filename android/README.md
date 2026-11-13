# Android 本机单机版

这个 APK 在手机后台运行 zoffline，通过仅针对 `com.zwift.zwiftgame` 的本地 VPN 连接同一手机上的 Zwift。首版支持启动/停止、打开 Zwift、地图与幽灵骑手设置、实际游戏登录状态、日志和玩家存档导出。安装包包含 ARM64 和 x86_64，最低 Android 10；Android 17 的完整骑行与证书补丁仍需目标手机实测。

## 安装和使用

1. 安装 `dist/zoffline-android-solo.apk`。另外安装 Zwift 安卓客户端，并准备完整的游戏资源；本项目不包含 Zwift 本体和地图资源。
2. 完成适用于该 Zwift 版本的证书补丁。可以尝试 [ZofflineObb v1.5](https://github.com/fatsbrown/ZofflineObbAndroid/releases/tag/v1.5)。该版本更新到 API 36，但维护者没有确认 Android 17 兼容性。若无法在手机上修改 OBB，参考 [issue #505 的 Windows/ADB 方法](https://github.com/zoffline/zwift-offline/issues/505)。已有游戏存档在清理 Zwift 数据前应自行备份；不要为尝试补丁直接清除重要数据。
3. 打开 zoffline，点击“启动本机服务”，允许系统 VPN 授权和通知。已有其他 VPN 时，系统会切换 VPN。
4. 显示服务就绪后，可先打开“地图与幽灵骑手设置”并保存，再点击“打开 Zwift”。本机单机沿用原服务行为，可以使用任意邮箱和密码登录。
5. 主界面只有在收到成功的游戏登录请求后才显示“已收到 Zwift 游戏登录”。“服务已就绪”只能证明本地服务和 VPN 已启动。
6. 骑行结束后先在 Zwift 保存并退出，再停止 zoffline。切换回桌面不停止后台服务；可以从前台通知停止。系统关闭 VPN、应用强制停止或进程异常会终止本次服务。

在厂商系统里将 zoffline 电池策略设为允许后台运行，避免骑行期间被清理。蓝牙设备配对仍在 Zwift 中完成，本 APK 不申请蓝牙权限。完全断网骑行依赖 Zwift 已下载所需资源；首版关闭服务端 CDN 网络代理。

## Windows 模拟器

当前 Windows 工作区已经准备好 Android 16 x86_64 虚拟手机。双击仓库根目录的 `start-android-emulator.cmd`，或在终端运行：

```powershell
.\start-android-emulator.cmd
```

脚本使用 `build/android-tools/sdk` 和 `build/android-tools/avd`，自动打开模拟器、等待系统就绪并打开 zoffline；已经运行时复用同一虚拟手机。优先使用含 Google Play 和 ARM64 转译的 `zoffline-playstore`；若未创建则依次选择 Google APIs 虚拟手机 `zoffline-zwift` 或原有的 `zoffline-smoke`。前两者使用主机 GPU 加速，后者使用软件渲染。未安装 zoffline 时会安装 `dist/zoffline-android-solo.apk`，已有安装和数据会保留。更新 APK 可直接将文件拖入模拟器窗口。

要在模拟器里直接安装完整 Zwift，打开 Google Play，由本人登录谷歌账号，再安装开发者为 Zwift, Inc.、包名为 `com.zwift.zwiftgame` 的应用。此过程需要可访问 Google 登录和商店服务的网络。先让商店完成游戏资源下载，再按前文准备证书补丁并启动 zoffline VPN。各虚拟手机的数据分别保存，切换虚拟手机不会自动迁移应用和账号。

启动脚本默认让模拟器使用 `1.1.1.1,8.8.8.8` 作为 DNS；当前网络的默认 DNS 将 GetApps 的 `global.market.xiaomi.com` 错误地解析为 `127.0.0.1`。此设置仅作用于模拟器。需要其他 DNS 时，可在启动脚本前设置环境变量 `ZOFFLINE_ANDROID_DNS`；修改后关闭模拟器并重新启动生效。

SDK、系统镜像和虚拟手机数据属于本机环境，不随 Git 仓库分发。关闭模拟器窗口即可关闭虚拟手机；重新运行脚本可以再次启动。启动日志位于 `build/android-tools/emulator-launcher*.log`。这个环境用于测试本项目 APK，官方 Zwift 在 x86_64 模拟器中的兼容性、蓝牙设备及完整骑行仍需另行验证。

当前 Google APIs 环境已验证 Zwift 1.124.0（167761）ARM64 APK 可以安装并加载原生库。单独的 APK 不含游戏资源，启动会提示安装不完整，仍需同版本的 `main.167761.com.zwift.zwiftgame.obb`；尚未验证资源齐全后的游戏运行或完整骑行。原有 `zoffline-smoke` 虚拟手机及其数据保留。

## 存档

数据位于应用私有目录 `files/storage`，与 Windows 服务端的 storage 结构一致。相同应用 ID 和签名的覆盖更新保留存档，卸载会删除存档。停止服务后可使用“导出玩家存档”保存 ZIP；首版没有手机端导入入口。导出的文件可能包含已配置的第三方服务凭据，请按个人备份保管。

## 从源码构建

需要 JDK 17 或 21、Python 3.11、Android SDK，网络可访问 Google Maven、Maven Central、PyPI 和 Chaquopy。

SDK 安装组件：

```powershell
sdkmanager "platforms;android-36" "build-tools;35.0.0" "platform-tools"
```

在仓库根目录执行（SDK 路径按本机设置）：

```powershell
.\build-android.cmd --sdk "C:\Users\admin\AppData\Local\Android\Sdk"
```

也可以在 `android/local.properties` 设置 `sdk.dir`，再运行 `build-android.cmd`。脚本准备公共资源、运行 Java 单元测试和 Android Lint，构建并复制测试签名 APK 到 `dist/zoffline-android-solo.apk`，同时生成 SHA256 文件。Gradle Wrapper 固定到 8.11.1 并校验发行包 SHA256；首次构建会下载工具和依赖。

调试版使用构建者的 Android debug keystore。持续给同一批手机升级时，请保留签名。正式签名配置支持 `ZOFFLINE_ANDROID_KEYSTORE`、`ZOFFLINE_ANDROID_STORE_PASSWORD`、`ZOFFLINE_ANDROID_KEY_ALIAS`、`ZOFFLINE_ANDROID_KEY_PASSWORD`，随后在 android 目录构建 `assembleRelease`；不要将密钥提交进仓库。

## 实现和验证边界

- `android_runtime.py` / `runtime_paths.py` / `embedded_wsgi.py` 提供嵌入接口，桌面默认仍使用 gevent。
- Python 和 VPN 运行于独立的 `:offline` 服务进程，停止后结束该进程，下一次启动使用新的解释器。
- 本地服务绑定 `127.0.0.1`：HTTP 8080、HTTPS 8443、管理页 HTTP 8081、TCP 3025、UDP 3024。管理页通过回环 HTTP 访问，没有关闭 HTTPS 证书校验。
- hev-socks5-tunnel 2.19.3 提供 TUN TCP/IP 栈；Java SOCKS5 转发层将 Zwift 域名解析为 `198.18.0.1`，将对应端口映射到本地服务。DNS 为 `198.18.0.2`，仅 Zwift UID 进入 VPN，其他应用不经过该转发。
- `android/resources.txt` 和 `scripts/prepare_android.py` 使用公共资源/源码白名单，构建者的 storage、logs、账号及第三方登录状态不进入 APK。
- APK 中的开源许可证见资源 `runtime/runtime.zip` 内的 LICENSE 与 third-party；对应源码包括本仓库的 Android 工程和 Python 改动。

自动验证覆盖真实 HTTP/HTTPS 与 protobuf 登录、TCP/UDP 转发、DNS、重复启动/停止和端口释放。测试探针可用于 Android 模拟器检查实际 TUN 链路，但不能替代官方 Zwift 客户端、真实设备配对或完整骑行验证。当前交付的具体验证结果记录在技术规格的 Verification 中。
