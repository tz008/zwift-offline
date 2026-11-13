@echo off
setlocal EnableExtensions
chcp 65001 >nul
title zoffline Android Emulator

set "ZOFFLINE_ROOT=%~dp0"
set "ANDROID_SDK_ROOT=%ZOFFLINE_ROOT%build\android-tools\sdk"
set "ANDROID_AVD_HOME=%ZOFFLINE_ROOT%build\android-tools\avd"
set "ZOFFLINE_EMULATOR=%ANDROID_SDK_ROOT%\emulator\emulator.exe"
set "ZOFFLINE_ADB=%ANDROID_SDK_ROOT%\platform-tools\adb.exe"
set "ZOFFLINE_SERIAL=emulator-5580"
set "ZOFFLINE_APK=%ZOFFLINE_ROOT%dist\zoffline-android-solo.apk"
if not defined ZOFFLINE_ANDROID_DNS set "ZOFFLINE_ANDROID_DNS=1.1.1.1,8.8.8.8"
set "ZOFFLINE_AVD=zoffline-smoke"
set "ZOFFLINE_GPU=swiftshader"
if exist "%ANDROID_AVD_HOME%\zoffline-zwift.ini" (
    set "ZOFFLINE_AVD=zoffline-zwift"
    set "ZOFFLINE_GPU=host"
)
if exist "%ANDROID_AVD_HOME%\zoffline-playstore.ini" (
    set "ZOFFLINE_AVD=zoffline-playstore"
    set "ZOFFLINE_GPU=host"
)

if not exist "%ZOFFLINE_EMULATOR%" (
    echo [错误] 找不到模拟器：%ZOFFLINE_EMULATOR%
    echo 请先准备 Android SDK 和 Android 16 x86_64 系统镜像。
    goto failed
)
if not exist "%ZOFFLINE_ADB%" (
    echo [错误] 找不到 adb：%ZOFFLINE_ADB%
    goto failed
)
if not exist "%ANDROID_AVD_HOME%\%ZOFFLINE_AVD%.ini" (
    echo [错误] 找不到虚拟手机配置：%ANDROID_AVD_HOME%\%ZOFFLINE_AVD%.ini
    goto failed
)

"%ZOFFLINE_ADB%" start-server >nul 2>&1
if errorlevel 1 (
    echo [错误] 无法启动 adb 服务。
    goto failed
)
"%ZOFFLINE_ADB%" -s "%ZOFFLINE_SERIAL%" get-state 2>nul | findstr /x /c:"device" /c:"offline" >nul
if not errorlevel 1 (
    echo [信息] 模拟器已运行，复用当前虚拟手机。
    goto wait_boot
)

echo [信息] 正在打开 Android 16 模拟手机窗口：%ZOFFLINE_AVD%……
powershell.exe -NoProfile -NonInteractive -Command "Start-Process -FilePath $env:ZOFFLINE_EMULATOR -ArgumentList @('-avd',$env:ZOFFLINE_AVD,'-port','5580','-no-snapshot','-no-boot-anim','-gpu',$env:ZOFFLINE_GPU,'-memory','4096','-scale','0.4','-dns-server',$env:ZOFFLINE_ANDROID_DNS) -WorkingDirectory $env:ZOFFLINE_ROOT -WindowStyle Hidden -RedirectStandardOutput (Join-Path $env:ZOFFLINE_ROOT 'build\android-tools\emulator-launcher.log') -RedirectStandardError (Join-Path $env:ZOFFLINE_ROOT 'build\android-tools\emulator-launcher-error.log') -ErrorAction Stop"
if errorlevel 1 (
    echo [错误] 无法启动模拟器，请查看 build\android-tools 下的 emulator-launcher 日志。
    goto failed
)

:wait_boot
echo [信息] 等待安卓系统启动，首次可能需要 1 到 3 分钟……
set /a ZOFFLINE_BOOT_ATTEMPT=0 >nul
:poll_boot
"%ZOFFLINE_ADB%" -s "%ZOFFLINE_SERIAL%" shell getprop sys.boot_completed 2>nul | findstr /x /c:"1" >nul
if not errorlevel 1 goto boot_ready
set /a ZOFFLINE_BOOT_ATTEMPT+=1 >nul
if %ZOFFLINE_BOOT_ATTEMPT% geq 90 (
    echo [错误] 等待启动超时，请查看模拟器窗口和 build\android-tools 下的日志。
    goto failed
)
powershell.exe -NoProfile -NonInteractive -Command "Start-Sleep -Seconds 2"
goto poll_boot

:boot_ready
"%ZOFFLINE_ADB%" -s "%ZOFFLINE_SERIAL%" emu avd name 2>nul | findstr /x /c:"%ZOFFLINE_AVD%" >nul
if errorlevel 1 (
    echo [错误] 端口 5580 被其他虚拟手机占用，请关闭占用该端口的模拟器后重试。
    goto failed
)
"%ZOFFLINE_ADB%" -s "%ZOFFLINE_SERIAL%" shell pm path com.zoffline.solo 2>nul | findstr /b /c:"package:" >nul
if not errorlevel 1 goto open_app
if not exist "%ZOFFLINE_APK%" (
    echo [信息] 模拟手机已就绪。构建 APK 后，可将文件拖入模拟器安装。
    exit /b 0
)
echo [信息] 首次安装 zoffline APK……
"%ZOFFLINE_ADB%" -s "%ZOFFLINE_SERIAL%" install -r "%ZOFFLINE_APK%"
if errorlevel 1 (
    echo [错误] APK 安装失败。
    goto failed
)

:open_app
"%ZOFFLINE_ADB%" -s "%ZOFFLINE_SERIAL%" shell input keyevent KEYCODE_WAKEUP >nul 2>&1
"%ZOFFLINE_ADB%" -s "%ZOFFLINE_SERIAL%" shell wm dismiss-keyguard >nul 2>&1
"%ZOFFLINE_ADB%" -s "%ZOFFLINE_SERIAL%" shell am start -W -n com.zoffline.solo/.MainActivity
if errorlevel 1 (
    echo [错误] 无法打开 zoffline，请在模拟手机内手动打开。
    goto failed
)
echo [完成] 模拟手机已就绪，zoffline 已打开。
echo 后续安装或更新 APK，可直接将文件拖入模拟器窗口。
exit /b 0

:failed
echo.
pause
exit /b 1
