# StarSky Python 签到

签到引擎位于 `src/modules/starsky/core.py`，默认使用本地账户后台签到（`api`）：在隐藏的 32 位 Windows PowerShell 进程中加载已安装 StarSky 的 .NET 程序集，读取本机保存的三份登录态副本，并复用客户端原生加密逻辑发送签到请求。整个过程不打开 StarSky 主界面、不移动鼠标，手动签到无需 UAC 管理员授权。

首次使用需安装 StarSky 并在客户端登录一次；之后可以关闭客户端。登录态只在本机读取，不输出到日志。服务端返回 `code=109`（今天已经领取过流量）也视为正常完成。登录失效时，需要打开 StarSky 重新登录。

在项目根目录运行：

```powershell
python src/modules/starsky/scripts/checkin.py
python src/modules/starsky/scripts/checkin.py --schedule 09:30
python src/modules/starsky/scripts/checkin.py --remove-schedule
```

双击 `src/modules/starsky/scripts/一键签到.bat` 或 `src/modules/starsky/scripts/设置每日自动签到.bat` 也会启动同一个 Python 入口。

配置继续读取 `data/starsky/设置.ini`，日志写入 `data/starsky/日志/签到日志.txt`。`STARSKY_HOME` 环境变量或 `--home` 可以指定其他数据目录。旧配置没有 `签到方式` 时自动使用 `api`，无需手动修改原有 INI 文件；软件路径仍用于定位已安装的 StarSky。

如需兼容原来的操作方式，可在页面设置中选择「窗口点击签到」（`desktop`）。此模式使用 Win32 API 和 Pillow 截图识别按钮，需要 StarSky 已登录、桌面解锁、窗口未被遮挡，执行时会移动鼠标并可能请求 UAC 管理员授权。自动启动、等待超时、签到后关闭软件等设置继续适用于窗口模式。点击前会确认目标坐标属于 StarSky；只有检测到已签到状态才报告成功，无法确认时返回退出码 6 并保存 `签到后界面.png`。

每日计划任务仍通过 Windows 任务计划程序运行 Python 入口，创建或删除计划时会请求 UAC 管理员授权。执行时需保持同一个 Windows 账户已登录，以便读取该账户的本地登录态。后台模式可以锁屏运行；电脑休眠或关机期间不能执行。

原来指向 `.ps1` 的每日计划任务需要重新保存签到时间；页面会识别并提示旧任务，手动签到会直接使用新的 Python 引擎。
