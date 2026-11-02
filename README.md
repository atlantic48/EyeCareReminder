# 护眼提醒 · EyeCare Reminder

> 轻量级 Windows 桌面应用：调用笔记本/台式机摄像头实时检测眨眼，按可配置的统计周期评估「平均每分钟眨眼次数」，低于阈值时弹出护眼提醒。

## 一、项目简介

长时间盯屏会导致眨眼次数显著下降，使眼球表面泪液蒸发加快，引发眼干、视疲劳甚至视力下降。**护眼提醒**通过摄像头 + 机器学习人脸关键点模型，实时计算眼睛纵横比（EAR, Eye Aspect Ratio）来检测每一次眨眼，并统计每分钟眨眼次数；当「平均每分钟眨眼次数」连续低于用户设定阈值时，主动弹出休息提醒，帮助用户养成健康用眼习惯。

本程序特点：

- **高精度检测**：基于 MediaPipe FaceLandmarker（468/478 点人脸网格）的 EAR 算法，比传统 Haar 级联更稳、更准。
- **完全本地运行**：模型已内嵌，无需联网、无需上传任何画面，隐私友好。
- **零额外 GUI 依赖**：界面用 `ctypes` 直接调用 Win32 API 自绘，运行时内存/CPU 占用低。
- **配置灵活**：提醒间隔（分钟）与眨眼阈值（次/分）均可在界面中自行设定。
- **单文件 exe**：双击即运行，无需安装 Python 环境（详见下文「从可执行文件运行」）。

## 二、功能说明

| 功能 | 说明 |
|------|------|
| 实时眨眼检测 | 调用摄像头，逐帧计算双眼 EAR，带迟滞判定，避免一次闭合重复计数。 |
| 每分钟统计 | 实时累计「本分钟眨眼次数」，并在界面展示。 |
| 周期评估 + 提醒 | 每满 N 分钟（可配置）计算一次平均每分钟眨眼次数；若 **低于阈值** 则弹出一次护眼提醒弹窗，否则不提醒。评估后清空窗口重新累计，循环运行直到手动关闭。 |
| 实时预览 | 主界面显示镜像后的摄像头画面，叠加「本分钟眨眼数」「是否检测到人脸」「当前 EAR」等信息。 |
| 摄像头权限/异常提示 | 无摄像头、被占用或权限不足时，弹出友好排查指引，并可一键打开系统相机隐私设置。 |
| 可配置项 | 提醒间隔（分钟）、眨眼告警阈值（次/分）均可通过界面编辑框设置（开始监测后生效）。 |

## 三、安装与使用

### 方式 A：从可执行文件运行（推荐，最简单）

1. 下载 `EyeCareReminder.exe`（见 GitHub Releases）。
2. 双击运行。无需安装 Python，无需联网。
3. 首次运行会在系统临时目录解包约 300 MB（正常现象，需保证临时盘有空间）。

> 若杀毒软件拦截，属 PyInstaller 单文件打包的常见误报，将本程序加入白名单即可。

### 方式 B：从源代码运行（适合开发者）

**环境要求**

- Windows 10 / 11（64 位）
- Python 3.10–3.13（建议使用 3.11+）
- 一个可用的摄像头，并已授予桌面应用摄像头权限

**步骤**

```bash
# 1. 克隆仓库
git clone https://github.com/<你的用户名>/eye-care-reminder.git
cd eye-care-reminder

# 2. 创建并激活虚拟环境（可选但推荐）
python -m venv .venv
.venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 运行
python eye_care_app.py
```

### 使用步骤

1. 启动程序，界面顶部两个输入框分别设置「提醒间隔（分钟）」与「眨眼阈值（次/分）」。
2. 点击 **开始监测**，程序打开摄像头并开始检测（界面实时显示预览与人脸状态）。
3. 当平均每分钟眨眼次数连续低于设定阈值时，自动弹出护眼提醒。
4. 点击 **停止** 可暂停检测；点击 **摄像头帮助** 可打开系统相机隐私设置；点击 **退出** 关闭程序。

## 四、配置项说明

程序提供两项用户可配置参数，位于主界面顶部编辑框，**修改后点击「开始监测」生效**（运行中修改将在下一轮评估/下次开始时应用）。

| 配置项 | 对应界面控件 | 默认值 | 取值范围 | 含义 |
|--------|--------------|--------|----------|------|
| 评估/提醒间隔 | 「提醒间隔（分钟）」 | 5 | 1–60 分钟 | 每累计满该分钟数，计算一次平均每分钟眨眼次数并判断是否提醒。 |
| 眨眼告警阈值 | 「眨眼阈值（次/分）」 | 11 | 1–120 次/分 | 平均每分钟眨眼次数低于该值即触发提醒；高于或等于则不提醒。 |

> 提示：一般成年人安静注视屏幕时每分钟眨眼约 15–20 次；当低于约 10–12 次时眼表干燥风险上升。默认阈值 11 次/分、间隔 5 分钟是较保守的提醒策略，你可按自身感受调整。

除界面配置外，源码顶部常量还暴露了底层检测参数（开发者可调，普通用户无需改动）：

- `EAR_CLOSE = 0.21`：EAR 低于此值判定为「闭眼」（用于检测一次眨眼）。
- `EAR_OPEN = 0.26`：EAR 高于此值且此前处于闭眼，判定为「完成一次眨眼」（迟滞，避免重复计数）。

## 五、文件结构概览

```
eye-care-reminder/
├── eye_care_app.py        # 主程序源码（ctypes Win32 GUI + OpenCV/MediaPipe 检测 + 提醒逻辑）
├── face_landmarker.task   # MediaPipe 人脸关键点模型文件（已内嵌，源码运行必需）
├── build.bat              # 一键打包脚本（PyInstaller 单文件 exe）
├── EyeCareReminder.spec   # PyInstaller 规格文件（由 build.bat 生成，用于可复现构建）
├── requirements.txt       # Python 运行/构建依赖清单
├── .gitignore             # 忽略构建产物（build/、dist/、__pycache__ 等）
├── README.md              # 本文档
├── LICENSE                # MIT 许可证
└── dist/
    └── EyeCareReminder.exe  # 打包产物（建议作为 GitHub Release 发布，不提交进仓库）
```

## 六、打包为 exe（开发者）

```bash
pip install -r requirements.txt
build.bat        # 等价于下方 PyInstaller 命令
```

或手动执行：

```bash
python -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name EyeCareReminder ^
  --add-data "face_landmarker.task;." ^
  --collect-all mediapipe ^
  --hidden-import=mediapipe.tasks.python.vision ^
  --hidden-import=mediapipe.tasks.python.core.base_options ^
  eye_care_app.py
```

> 注：模型以「内存字节」方式加载（`model_asset_buffer`），已规避 MediaPipe 在含中文路径（如中文用户名）下无法打开模型文件的问题。

## 七、最低运行环境 / 依赖

- **操作系统**：Windows 10 / 11（64 位）。不支持 macOS / Linux；不支持 32 位 Windows。
- **硬件**：x86-64 CPU；集成或 USB 摄像头（已安装驱动并授予权限）；建议 ≥ 4 GB 内存（运行时约占用 300–500 MB）；磁盘需约 150 MB（exe 本体）+ 约 300 MB 临时空间（运行时解包）。
- **其他**：无需安装 Python、无需联网；极干净的系统中若提示缺少 `vcruntime140.dll` 等，安装「Microsoft Visual C++ 2015–2022 可再发行组件包」即可。普通用户权限即可运行，无需管理员。

## 八、许可证

本项目以 [MIT 许可证](LICENSE) 开源。模型文件 `face_landmarker.task` 来自 Google MediaPipe 官方模型库，遵循其对应许可条款。
