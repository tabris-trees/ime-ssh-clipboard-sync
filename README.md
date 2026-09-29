# Windows IME 与双向剪贴板 SSH 同步

通过 SSH 在 Windows 与 Linux X11 之间提供两项互相独立的功能：

- Windows 全局快捷键 `Ctrl+Shift+S` 直接交替切换远端 IBus 中文/英文状态。
- Windows ↔ Linux X11 双向同步文本、PNG 图片与普通文件剪贴板。

推荐安装位置是 Windows 公共文档目录下的 `ImeSshSync` 文件夹。程序附带的公共目录版 Quicker 动作通过 `Environment.SpecialFolder.CommonDocuments` 动态定位，因此不依赖用户名或系统盘符。

本程序在当前 Windows 用户会话中运行两个独立后台进程：

- `ime-ssh-sync.ps1`：注册全局快捷键 `Ctrl+Shift+S`，完全独立于 Windows 本地输入法。第一次按下直接调用远端 `ibus-x11-cn`，之后每按一次在远端中文和英文 API 之间交替。
- `clipboard-ssh-sync.ps1`：同步 Windows ↔ Linux X11 的文本、PNG 图片和普通文件。

不会模拟远端按键，不会直接操作远端 IBus/D-Bus。文件夹递归暂不支持。

## 环境要求

- PowerShell 7 (`pwsh.exe`)，以 STA 模式运行剪贴板进程。
- Windows OpenSSH 的 `ssh.exe` 与 `scp.exe`。
- SSH 密钥登录：目标地址和端口由未提交的 `config.psd1` 配置。
- 远端 X11 会话和约定的稳定 API 已可用。
- Windows 端需要有可从远端访问的 X Server（默认显示号 `:0.0`）。脚本会在远端通过 `SSH_CONNECTION` 动态取得当前 Windows 客户端 IP，因此不依赖固定 IP，也不依赖 Quicker 继承 `DISPLAY`。

## 一键启动

首次使用时复制示例配置并填写所有 `XXX`：

```powershell
Copy-Item .\config.example.psd1 .\config.psd1
notepad .\config.psd1
```

双击 `start-hidden.vbs`。它会隐藏启动 IME 与剪贴板两个进程。两个进程都有用户级互斥锁，重复双击不会产生重复实例。

启动后，按一次 `Ctrl+Shift+S` 切换一次远端输入法。按住快捷键不会连续触发；本地微软拼音状态不会被读取，也不会触发远端切换。进程每次启动后的第一次按键固定切到远端中文，后续按键在中文/英文之间交替。该组合键是 Windows 全局快捷键，会覆盖前台应用原本的 `Ctrl+Shift+S` 功能。

Quicker 用户请导入 `Quicker_切换IME与剪贴板同步_公共目录版.json`。

## 停止与检查

停止：

```powershell
pwsh.exe -NoProfile -File .\stop.ps1
```

查看本地进程、远端剪贴板状态和近期日志：

```powershell
pwsh.exe -NoProfile -File .\status.ps1
```

## 登录时自动启动（可选）

安装当前用户的登录启动快捷方式：

```powershell
pwsh.exe -NoProfile -File .\install-startup.ps1
```

移除登录启动快捷方式：

```powershell
pwsh.exe -NoProfile -File .\uninstall-startup.ps1
```

快捷方式指向本目录；因此安装后不要移动或删除本目录。如需移动，先卸载，再在新位置重新安装。

## 远端如何启动

无需手工启动 systemd 服务：

- IME：按下 Windows 全局快捷键时直接调用配置目录中的 `ibus-x11-cn` 或 `ibus-x11-en`。
- Windows → Linux 剪贴板：写入 API 自动启动/替换远端 X11 clipboard owner。
- Linux → Windows 剪贴板：Windows 剪贴板进程自动运行持久命令 `ssh -T ... clipboard-x11-watch`，断线后指数退避重连。

只需保证远端 X11 会话存在、SSH 可连接、远端脚本保持可执行。

## SSH 密钥配置

在新的 Windows 电脑打开 PowerShell：

```powershell
ssh-keygen -t ed25519
Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub"
```

把输出的整行公钥追加到远端 `~/.ssh/authorized_keys`，然后验证：

```powershell
ssh -p XXX XXX@XXX true
```

首次连接时核对并接受服务器指纹。测试命令不再要求密码后，再启动本程序。私钥始终保留在本机，不要提交到仓库。

## 仓库结构

- 根目录：Windows PowerShell、VBS 启动器、Quicker 动作和管理脚本。
- `server/clipboard-x11.py`：Linux X11 剪贴板实现。
- `server/ibus-x11`：Linux 私有 IBus 会话实现。
- `server/bin/`：当前部署的稳定 API 包装脚本。

`server/` 保存服务端实现，用户目录通过 `$HOME` 或 `Path.home()` 动态定位。部署前请确认 Python 依赖 Pillow、python-xlib，以及 `xclip`、IBus、D-Bus 和 X11 环境可用。

## 日志与缓存

- IME 日志：`%LOCALAPPDATA%\ImeSshSync\ime-ssh-sync.log`
- 剪贴板日志：`%LOCALAPPDATA%\ImeSshSync\clipboard-ssh-sync.log`
- Linux → Windows 文件缓存：`%LOCALAPPDATA%\ImeSshSync\ClipboardInbox\<generation>\`

默认限制：文本 4 MiB、PNG 32 MiB、文件最多 100 个且总计 256 MiB。日志不记录剪贴板正文。
