# StarSky Python 签到

签到引擎位于 `core.py`，使用 Python 标准库 `ctypes` 调用 Win32 API，使用 Pillow 截图并识别窗口右下方的蓝色签到按钮和灰色已签到按钮。无需 PowerShell、Node.js 或额外的自动化运行时。

在项目根目录运行：

```powershell
python modules/starsky/scripts/checkin.py
python modules/starsky/scripts/checkin.py --schedule 09:30
python modules/starsky/scripts/checkin.py --remove-schedule
```

双击 `scripts/一键签到.bat` 或 `scripts/设置每日自动签到.bat` 也会启动同一个 Python 入口。

配置继续读取 `data/starsky/设置.ini`，日志写入 `data/starsky/日志/签到日志.txt`。`STARSKY_HOME` 环境变量或 `--home` 可以指定其他数据目录。已有软件路径、自动启动、等待超时、签到后关闭软件设置均继续有效。

StarSky 需要已登录，并在可交互的 Windows 桌面运行。管理员权限通过 Windows 原生 UAC 申请。每日计划任务需要用户保持登录，锁屏时无法读取窗口并点击。点击前会确认目标坐标属于 StarSky，窗口被遮挡时中止。只有检测到已签到状态才报告成功，无法确认时返回退出码 6 并保存 `签到后界面.png`。

原来指向 `.ps1` 的每日计划任务需要重新保存签到时间；页面会识别并提示旧任务，手动签到会直接使用新的 Python 引擎。
