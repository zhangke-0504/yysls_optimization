## Why

现有目录包含 Windows 样本和反编译伪代码，可用于核对内存监视与清理行为。项目需要一份能够独立构建、运行和验收的实现，并解决同步清理卡住界面和内存差值误报的问题。

## What Changes

- 新建 C++17、原生 Win32 和 CMake 项目，构建 x64 Windows 桌面应用。
- 提供中文主窗口、托盘与可拖动悬浮窗，显示物理内存、提交量、系统缓存和最近一次清理结果。布局和图标采用独立设计。
- 支持手动清理、物理内存阈值触发、定时清理和阈值冷却。手动与自动请求共用同一个后台执行入口。
- 使用 Windows 公开工作集 API，按结果报告成功、跳过和失败数量。以带符号的全系统可用物理内存净变化作为辅助读数。
- 提供构建说明、策略和边界测试，以及普通用户运行和受控进程清理的验收步骤。

## Capabilities

### New Capabilities

- `win32-desktop`: 中文主窗口、内存读数与状态、托盘、悬浮窗和应用生命周期。
- `cleanup-scheduling`: 参数校验、手动和自动触发、并发控制、冷却与停止规则。
- `memory-monitoring-and-trimming`: 系统内存采样、进程工作集裁剪、权限处理和结果统计。

### Modified Capabilities

## Impact

- 新增应用源码、Windows 资源、CMake 构建配置、测试和项目使用说明。`decompiled/memory_optimizer.core.c` 提供行为参考。
- 构建使用 MSVC 和 Windows SDK。运行依赖 Windows 自带的 Win32 库，以当前用户权限启动。
- 清理操作会裁剪当前权限下可访问的进程工作集。清理结果同时展示 API 执行结果和系统内存采样值。
