# 内存优化器

一个使用 C++17 和原生 Win32 的 Windows x64 应用。主窗口显示物理内存、系统提交量和系统缓存，支持手动裁剪工作集、定时清理和物理内存阈值触发。应用以当前用户权限运行。

## 构建和启动

需要 Visual Studio 2022 或 VS Build Tools 2022 的 C++ 桌面开发工作负载（含 x64 MSVC 和 Windows SDK），以及 CMake 3.21 或更高版本。在项目根目录的 PowerShell 中运行：

```powershell
cmake -S . -B build/x64-vs2022 -G 'Visual Studio 17 2022' -A x64
cmake --build build/x64-vs2022 --config Release
.\build\x64-vs2022\Release\MemoryOptimizer.exe
```

若 PowerShell 找不到 `cmake`，可以安装独立版 CMake 并加入 `PATH`，或将 VS 自带的 CMake 目录临时加入当前会话。例如，Build Tools 装在 `D:\program_files\VSBuildTools2022` 时：

```powershell
$env:Path = 'D:\program_files\VSBuildTools2022\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;' + $env:Path
```

只构建应用时，可在第一条配置命令中追加 `-DBUILD_TESTING=OFF`。

## 发布与仓库内容

- `build/` 和 `out/` 是本地构建目录，不提交到 Git。发布时在 GitHub Releases 中创建对应版本的 Release，将 `build/x64-vs2022/Release/MemoryOptimizer.exe` 作为附件上传即可，不用上传整个构建目录。
- `observations/` 下只跟踪 `test_scripts/`。运行日志、采集数据、分析报告及下载的工具都保留在本地，请将采集输出放在 `test_scripts/` 之外。运行 `watch_relaunch.py` 时，可以用 `--presentmon` 指定自行下载的 PresentMon 路径。

## 使用

- 默认阈值为 `80%`，定时间隔为 `60` 分钟，阈值冷却为 `30` 分钟。点击「启动优化」后，超过阈值且冷却结束即可触发。定时清理独立于冷却。点击「停止优化」会停止安排新的自动清理。
- 「手动清理」在空闲时可用。最近完成的结果显示成功、跳过和失败的进程数，以及两次全系统采样之间可用物理内存的净变化。应用会跳过当前用户无法打开的进程。
- 「显示悬浮窗」打开置顶内存读数。悬浮窗可拖动，右键可隐藏。主窗口上的两个角落按钮可调整停靠位置。
- 托盘图标可用时，最小化会将主窗口隐藏到托盘。点击托盘图标恢复窗口，右键菜单可恢复或退出。

## 验证

```powershell
ctest --test-dir build/x64-vs2022 -C Release --output-on-failure
```

`memory-core` 使用模拟时间检查触发边界，并覆盖输入、结果计数和内存净变化。`memory-working-set` 启动一个分配并触碰 64 MiB 内存的受控子进程，检查工作集裁剪、存活状态及句柄计数。

在有桌面和 Explorer 托盘的普通用户会话中可运行窗口消息测试。它会启动测试实例并通过 Release 程序的手动清理入口执行一次实际清理。另一个使用可控清理结果的测试实例用于验证忙碌、退出和托盘失败路径。

```powershell
python tests/desktop_smoke.py build/x64-vs2022/Release/MemoryOptimizer.exe build/x64-vs2022/Release/memory_gui_fixture.exe
```

手工验收时，关闭已有内存优化器实例，然后启动 Release 程序。检查三项中文读数约每秒刷新，启动和停止能锁定及解锁设置，悬浮窗能打开、拖动、停靠和隐藏。最小化后从托盘恢复，再点击「手动清理」查看计数和净变化。分别通过主窗口关闭和托盘退出，检查退出后进程和托盘图标消失。

### 本机验收记录

- Windows 10 x64，普通用户会话。全新构建通过，`dumpbin` 确认为 x64 Windows GUI。
- CTest `2/2` 通过。受控子进程的工作集从 `80,601,088` 字节降至 `0` 字节。子进程保持运行并验证原内存内容，测试进程句柄数保持一致。
- Release 窗口消息测试通过，覆盖中文读数、每秒刷新、错误提示、启动和停止、托盘恢复与退出命令、悬浮窗显示、拖动、停靠和隐藏。记录的一次手动操作裁剪成功 `307` 个进程，跳过 `137` 个，失败 `0` 个，采样净变化 `+287.9 MiB`。
- 可控测试实例通过了清理期间的窗口响应、重复请求合并、停止后在途任务正常完成、负净变化显示、有序退出及托盘注册失败回退。主窗口中文文本另通过 Windows UI Automation 读取。窗口截图捕获超时，交互验收使用窗口消息测试。
