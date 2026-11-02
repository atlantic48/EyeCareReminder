# -*- coding: utf-8 -*-
"""
护眼提醒 (EyeCare Reminder)
轻量级 Windows 桌面应用：调用摄像头实时检测眨眼，统计每分钟眨眼次数；
评估间隔与眨眼阈值均可由用户在界面配置：每 N 分钟计算一次平均每分钟眨眼次数，
若低于阈值（次/分）则弹出提醒弹窗。持续循环，直到用户关闭。

技术说明：
- GUI 使用 ctypes 直接调用 Win32 API（零额外 GUI 依赖，资源占用低）。
- 摄像头采集使用 OpenCV (VideoCapture)。
- 眨眼检测使用 MediaPipe FaceMesh 计算眼睛纵横比 (EAR)，精度高、CPU 占用低。
- 打包为单文件 exe（PyInstaller --windowed）后双击即可运行。
"""

import os
import sys
import time
import math
import threading
import logging
import tempfile
import ctypes
from ctypes import wintypes

# OpenCV 在模块级一次性导入（供检测线程与预览绘制共用，避免重复 import 造成卡顿）
import cv2  # noqa: E402
import numpy as np  # noqa: E402


def resource_path(rel):
    """获取资源文件的真实路径（开发态为脚本目录，打包态为 _MEIPASS）。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

# ---------- 日志（便于在用户机器上排查问题） ----------
_LOG_PATH = os.path.join(tempfile.gettempdir(), "eye_care_reminder.log")
try:
    logging.basicConfig(
        filename=_LOG_PATH, level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
except Exception:
    logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eye_care")


# ---------- 配置参数 ----------
EAR_CLOSE = 0.21          # 眼睛闭合阈值（EAR 低于此值视为闭眼）
EAR_OPEN = 0.26           # 睁眼恢复阈值（迟滞，避免重复计数）
MIN_BLINK_PER_MIN = 11    # 平均每分钟眨眼次数告警阈值（默认）
EVAL_WINDOW_MIN = 5       # 每 5 分钟评估一次（默认）
PREVIEW_W, PREVIEW_H = 320, 240   # 预览尺寸

# 用户可配置项（运行时由界面编辑框修改，见 create_controls 中的 EDIT 控件）
config = {
    "eval_window_min": EVAL_WINDOW_MIN,     # 评估/提醒间隔（分钟），范围 1–60
    "min_blink_per_min": MIN_BLINK_PER_MIN, # 眨眼告警阈值（次/分），范围 1–120
}
BLINK_THROTTLE = 0.03     # 检测循环节流（约 30fps 上限）

# MediaPipe 眼睛关键点索引（左眼 / 右眼，各 6 点用于 EAR）
LEFT_EYE = [362, 385, 387, 263, 373, 380]
RIGHT_EYE = [33, 160, 158, 133, 153, 144]


# ---------- 共享状态（检测线程写，UI 线程读） ----------
state_lock = threading.Lock()
state = {
    "running": False,
    "face": False,
    "minute_blinks": 0,
    "last_avg": None,
    "cam_status": "unknown",   # unknown | ok | error
    "frame": None,             # 最新预览帧（小尺寸 BGR numpy 数组）
    "ear": None,
}


def set_running(v):
    with state_lock:
        state["running"] = v


def get_state():
    with state_lock:
        return dict(state)


# ---------- ctypes / Win32 常量 ----------
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_CREATE = 0x0001
WM_APP = 0x8000
WM_REMINDER = WM_APP + 1
WM_CAM_ERROR = WM_APP + 2

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_BORDER = 0x00800000
SS_BITMAP = 0x0000000E
SS_LEFT = 0x00000000
BS_PUSHBUTTON = 0x00000000
IMAGE_BITMAP = 0
STM_SETIMAGE = 0x0172
MB_ICONINFORMATION = 0x00000040
MB_TOPMOST = 0x00040000
IDC_ARROW = 32512
COLOR_BTNFACE = 15

ID_START = 1001
ID_STOP = 1002
ID_HELP = 1003
ID_EXIT = 1004
ID_INTERVAL = 1005
ID_THRESHOLD = 1006
ES_NUMBER = 0x2000

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

# 设置常用 API 的参数/返回类型，避免 64 位下的类型错误
user32.SetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p]
user32.SetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.MessageBoxW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
user32.MessageBoxW.restype = ctypes.c_int
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
user32.SendMessageW.restype = ctypes.c_void_p
user32.CreateWindowExW.argtypes = [
    ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.EnableWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.EnableWindow.restype = ctypes.c_int
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HANDLE
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HANDLE]
user32.ReleaseDC.restype = ctypes.c_int
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.SetProcessDPIAware.argtypes = []
user32.SetProcessDPIAware.restype = ctypes.c_int

kernel32.GetModuleHandleW.argtypes = [ctypes.c_void_p]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

# 其它消息循环相关 API 的类型声明（确保 64 位下句柄/指针不被截断）
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_long
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = ctypes.c_int
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_long
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = ctypes.c_long
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = ctypes.c_int
user32.KillTimer.argtypes = [wintypes.HWND, wintypes.UINT]
user32.KillTimer.restype = ctypes.c_int
user32.SetTimer.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_uint

gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteObject.restype = ctypes.c_int


# ---------- GDI 位图结构 ----------
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint),
        ("biWidth", ctypes.c_int),
        ("biHeight", ctypes.c_int),
        ("biPlanes", ctypes.c_short),
        ("biBitCount", ctypes.c_short),
        ("biCompression", ctypes.c_uint),
        ("biSizeImage", ctypes.c_uint),
        ("biXPelsPerMeter", ctypes.c_int),
        ("biYPelsPerMeter", ctypes.c_int),
        ("biClrUsed", ctypes.c_uint),
        ("biClrImportant", ctypes.c_uint),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


def cv2_to_hbitmap(frame):
    """将 OpenCV BGR 帧转为 32 位 DIB 位图句柄（供 STATIC 控件显示）。"""
    h, w = frame.shape[:2]
    bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
    bgra = np.ascontiguousarray(bgra)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # 负值 = 自上而下的 DIB
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    bmi.bmiHeader.biSizeImage = w * h * 4
    pv = ctypes.c_void_p()
    hdc = user32.GetDC(0)
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(pv), None, 0)
    user32.ReleaseDC(0, hdc)
    if not hbm or not pv:
        return 0
    ctypes.memmove(pv, bgra.ctypes.data, bgra.nbytes)
    return hbm


# ---------- EAR 眨眼检测 ----------
def eye_aspect_ratio(landmarks, idx):
    """根据 6 个眼睛关键点计算眼睛纵横比 (EAR)。landmarks 为归一化坐标。"""
    pts = [landmarks[i] for i in idx]
    # 欧氏距离
    def dist(a, b):
        return math.hypot(a.x - b.x, a.y - b.y)
    # EAR = (|p2-p6| + |p3-p5|) / (2 * |p1-p4|)
    return (dist(pts[1], pts[5]) + dist(pts[2], pts[4])) / (2.0 * dist(pts[0], pts[3]))


# ---------- 检测线程 ----------
_detect_thread = None


def detect_loop(ui_hwnd):
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core import base_options as _base_options
    except Exception as e:
        log.exception("导入 cv2 / mediapipe 失败: %s", e)
        with state_lock:
            state["cam_status"] = "error"
        user32.PostMessageW(ui_hwnd, WM_CAM_ERROR, 1, 0)
        set_running(False)
        return

    # 加载人脸关键点模型（用于 EAR 眨眼检测）
    # 注意：MediaPipe 的 C++ 层无法打开含非 ASCII（如中文）字符的绝对路径，
    # 而用户目录/临时目录（_MEIPASS）常含中文，因此改用内存字节方式加载。
    try:
        model_path = resource_path("face_landmarker.task")
        if not os.path.exists(model_path):
            raise FileNotFoundError(model_path)
        with open(model_path, "rb") as _mf:
            model_bytes = _mf.read()
        if not model_bytes:
            raise ValueError("模型文件为空")
        _base = _base_options.BaseOptions(model_asset_buffer=model_bytes)
        _opts = vision.FaceLandmarkerOptions(
            base_options=_base,
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
        )
        face_landmarker = vision.FaceLandmarker.create_from_options(_opts)
    except Exception as e:
        log.exception("加载人脸模型失败: %s", e)
        with state_lock:
            state["cam_status"] = "error"
        user32.PostMessageW(ui_hwnd, WM_CAM_ERROR, 3, 0)
        set_running(False)
        return

    # 1) 打开摄像头（尝试多个索引；CAP_DSHOW 在 Windows 上更稳定）
    cap = None
    for idx in range(0, 3):
        try:
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        except Exception:
            cap = None
        if cap is not None and cap.isOpened():
            # 验证能读到帧（某些情况下 isOpened 为真但无信号/无权限）
            ok = False
            for _ in range(15):
                ret, _ = cap.read()
                if ret:
                    ok = True
                    break
                time.sleep(0.05)
            if ok:
                break
            cap.release()
            cap = None

    if cap is None or not cap.isOpened():
        log.error("无法打开摄像头")
        with state_lock:
            state["cam_status"] = "error"
            state["frame"] = None
        user32.PostMessageW(ui_hwnd, WM_CAM_ERROR, 0, 0)
        set_running(False)
        return

    with state_lock:
        state["cam_status"] = "ok"

    blink_closed = False
    minute_start = time.time()
    per_minute = []
    ts_base = time.time()
    last_ts = -1

    try:
        while get_state()["running"]:
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.1)
                continue

            frame = cv2.flip(frame, 1)  # 镜像，符合自拍直觉
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(mp.ImageFormat.SRGB, rgb)
            ts = int((time.time() - ts_base) * 1000)
            if ts <= last_ts:
                ts = last_ts + 1
            last_ts = ts
            try:
                results = face_landmarker.detect_for_video(mp_image, ts)
            except Exception:
                results = None

            ear = None
            face_detected = False
            if results is not None and results.face_landmarks:
                face_detected = True
                lm = results.face_landmarks[0]
                try:
                    ear_l = eye_aspect_ratio(lm, LEFT_EYE)
                    ear_r = eye_aspect_ratio(lm, RIGHT_EYE)
                    ear = (ear_l + ear_r) / 2.0
                    # 眨眼判定（迟滞）
                    if ear < EAR_CLOSE:
                        blink_closed = True
                    elif ear > EAR_OPEN and blink_closed:
                        with state_lock:
                            state["minute_blinks"] += 1
                        blink_closed = False
                except Exception:
                    ear = None

            # 构造预览帧 + 叠加信息
            small = cv2.resize(frame, (PREVIEW_W, PREVIEW_H))
            with state_lock:
                mb = state["minute_blinks"]
            cv2.putText(small, f"本分钟眨眼: {mb}", (8, 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 220, 120), 2)
            dot_color = (0, 200, 0) if face_detected else (0, 0, 220)
            cv2.circle(small, (PREVIEW_W - 18, 18), 8, dot_color, -1)
            if ear is not None:
                cv2.putText(small, f"EAR:{ear:.2f}", (8, PREVIEW_H - 12),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 0), 1)

            with state_lock:
                state["face"] = face_detected
                state["ear"] = ear
                state["frame"] = small

            # 每分钟记账
            now = time.time()
            if now - minute_start >= 60.0:
                with state_lock:
                    per_minute.append(state["minute_blinks"])
                    state["minute_blinks"] = 0
                minute_start = now
                if len(per_minute) >= config["eval_window_min"]:
                    avg = sum(per_minute) / len(per_minute)
                    with state_lock:
                        state["last_avg"] = avg
                    if avg < config["min_blink_per_min"]:
                        user32.PostMessageW(ui_hwnd, WM_REMINDER, 0, 0)
                    per_minute.clear()

            time.sleep(BLINK_THROTTLE)
    except Exception as e:
        log.exception("检测循环异常: %s", e)
        with state_lock:
            state["cam_status"] = "error"
        user32.PostMessageW(ui_hwnd, WM_CAM_ERROR, 2, 0)
    finally:
        try:
            cap.release()
        except Exception:
            pass
        try:
            face_landmarker.close()
        except Exception:
            pass
        with state_lock:
            state["cam_status"] = "closed"
            state["frame"] = None
        set_running(False)
        log.info("检测线程结束")


# ---------- UI ----------
CTRL = {}


def create_controls(hwnd):
    style_static = WS_CHILD | WS_VISIBLE | SS_LEFT
    style_preview = WS_CHILD | WS_VISIBLE | SS_BITMAP | WS_BORDER
    style_btn = WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON
    style_edit = WS_CHILD | WS_VISIBLE | WS_BORDER | ES_NUMBER

    # 设置区：提醒间隔 / 眨眼阈值（点击“开始监测”后生效）
    CTRL["lbl_interval"] = user32.CreateWindowExW(
        0, "STATIC", "提醒间隔（分钟）：", style_static,
        20, 20, 150, 22, hwnd, 0, kernel32.GetModuleHandleW(0), 0)
    CTRL["interval"] = user32.CreateWindowExW(
        0, "EDIT", str(config["eval_window_min"]), style_edit,
        175, 17, 55, 24, hwnd, ID_INTERVAL, kernel32.GetModuleHandleW(0), 0)
    CTRL["lbl_threshold"] = user32.CreateWindowExW(
        0, "STATIC", "眨眼阈值（次/分）：", style_static,
        20, 52, 150, 22, hwnd, 0, kernel32.GetModuleHandleW(0), 0)
    CTRL["threshold"] = user32.CreateWindowExW(
        0, "EDIT", str(config["min_blink_per_min"]), style_edit,
        175, 49, 55, 24, hwnd, ID_THRESHOLD, kernel32.GetModuleHandleW(0), 0)
    CTRL["hint"] = user32.CreateWindowExW(
        0, "STATIC", "（开始监测后生效）", style_static,
        240, 52, 160, 22, hwnd, 0, kernel32.GetModuleHandleW(0), 0)

    CTRL["preview"] = user32.CreateWindowExW(
        0, "STATIC", None, style_preview,
        40, 90, PREVIEW_W, PREVIEW_H, hwnd, 0, kernel32.GetModuleHandleW(0), 0)

    CTRL["status"] = user32.CreateWindowExW(
        0, "STATIC", "状态：未开始", style_static,
        40, 345, 360, 24, hwnd, 0, kernel32.GetModuleHandleW(0), 0)
    CTRL["minute"] = user32.CreateWindowExW(
        0, "STATIC", "本分钟眨眼：0", style_static,
        40, 375, 360, 24, hwnd, 0, kernel32.GetModuleHandleW(0), 0)
    CTRL["avg"] = user32.CreateWindowExW(
        0, "STATIC", "近 5 分钟均值：—", style_static,
        40, 405, 360, 24, hwnd, 0, kernel32.GetModuleHandleW(0), 0)
    CTRL["cam"] = user32.CreateWindowExW(
        0, "STATIC", "摄像头：—", style_static,
        40, 435, 360, 24, hwnd, 0, kernel32.GetModuleHandleW(0), 0)

    CTRL["start"] = user32.CreateWindowExW(
        0, "BUTTON", "开始监测", style_btn,
        40, 500, 80, 38, hwnd, ID_START, kernel32.GetModuleHandleW(0), 0)
    CTRL["stop"] = user32.CreateWindowExW(
        0, "BUTTON", "停止", style_btn,
        132, 500, 80, 38, hwnd, ID_STOP, kernel32.GetModuleHandleW(0), 0)
    CTRL["help"] = user32.CreateWindowExW(
        0, "BUTTON", "摄像头帮助", style_btn,
        224, 500, 80, 38, hwnd, ID_HELP, kernel32.GetModuleHandleW(0), 0)
    CTRL["exit"] = user32.CreateWindowExW(
        0, "BUTTON", "退出", style_btn,
        316, 500, 80, 38, hwnd, ID_EXIT, kernel32.GetModuleHandleW(0), 0)

    # 初始按钮状态
    user32.EnableWindow(CTRL["stop"], 0)


def read_config_from_ui(hwnd):
    """从编辑框读取并校验配置，写入全局 config。返回 (ok, err_msg)。"""
    buf = ctypes.create_unicode_buffer(32)
    # 提醒间隔
    user32.GetWindowTextW(CTRL["interval"], buf, 32)
    try:
        v = int(buf.value.strip())
    except Exception:
        return False, "提醒间隔请输入整数（分钟）。"
    if not (1 <= v <= 60):
        return False, "提醒间隔需在 1–60 分钟之间。"
    # 眨眼阈值
    user32.GetWindowTextW(CTRL["threshold"], buf, 32)
    try:
        t = int(buf.value.strip())
    except Exception:
        return False, "眨眼阈值请输入整数（次/分）。"
    if not (1 <= t <= 120):
        return False, "眨眼阈值需在 1–120 次/分之间。"
    config["eval_window_min"] = v
    config["min_blink_per_min"] = t
    return True, None


def on_start(hwnd):
    global _detect_thread
    st = get_state()
    if st["running"]:
        return
    ok, err = read_config_from_ui(hwnd)
    if not ok:
        user32.MessageBoxW(hwnd, err, "设置无效", MB_ICONINFORMATION | MB_TOPMOST)
        return
    if st["cam_status"] == "error":
        # 允许在修复后重新尝试
        with state_lock:
            state["cam_status"] = "unknown"
    set_running(True)
    _detect_thread = threading.Thread(target=detect_loop, args=(hwnd,), daemon=True)
    _detect_thread.start()
    user32.SetWindowTextW(CTRL["status"], "状态：监测中…")
    user32.EnableWindow(CTRL["start"], 0)
    user32.EnableWindow(CTRL["stop"], 1)


def on_stop(hwnd):
    set_running(False)
    global _detect_thread
    if _detect_thread is not None:
        _detect_thread.join(timeout=2.0)
        _detect_thread = None
    user32.SetWindowTextW(CTRL["status"], "状态：已停止")
    user32.EnableWindow(CTRL["start"], 1)
    user32.EnableWindow(CTRL["stop"], 0)


def show_reminder():
    win = config["eval_window_min"]
    thr = config["min_blink_per_min"]
    text = (
        f"您已连续 {win} 分钟平均每分钟眨眼少于 {thr} 次。\n\n"
        "长时间用眼容易疲劳，请注意休息：\n"
        "· 远眺 20 秒，放松睫状肌\n"
        "· 闭眼或眨眼数次，滋润眼球\n"
        "· 起身活动，调整坐姿与屏幕距离\n\n"
        "保护视力，从每一次眨眼开始～"
    )
    user32.MessageBoxW(0, text, "护眼提醒", MB_ICONINFORMATION | MB_TOPMOST)


def show_camera_error(code):
    text = (
        "未能使用摄像头，可能原因：\n\n"
        "1. 本机没有摄像头，或摄像头被其他程序占用；\n"
        "2. Windows 未授予本应用摄像头权限。\n\n"
        "请按以下步骤检查：\n"
        "· 打开「设置 → 隐私和安全性 → 相机」，\n"
        "  开启“允许桌面应用访问你的相机”；\n"
        "· 关闭可能占用摄像头的其他软件（如会议、相机 App）；\n"
        "· 确认摄像头已正确连接后，点击“开始监测”重试。\n\n"
        "（错误代码：%d）"
    ) % code
    user32.MessageBoxW(0, text, "摄像头不可用", MB_ICONINFORMATION | MB_TOPMOST)


def open_camera_help():
    try:
        os.startfile("ms-settings:privacy-webcam")
    except Exception:
        pass
    show_camera_error(-1)


def update_ui():
    st = get_state()
    user32.SetWindowTextW(CTRL["minute"], f"本分钟眨眼：{st['minute_blinks']}")
    if st["last_avg"] is None:
        user32.SetWindowTextW(CTRL["avg"], f"近 {config['eval_window_min']} 分钟均值：—（需累计 {config['eval_window_min']} 分钟）")
    else:
        user32.SetWindowTextW(CTRL["avg"], f"近 {config['eval_window_min']} 分钟均值：{st['last_avg']:.1f} 次/分")

    cam = st["cam_status"]
    if cam == "ok":
        face_txt = "（已检测到人脸）" if st["face"] else "（未检测到人脸）"
        user32.SetWindowTextW(CTRL["cam"], "摄像头：正常 " + face_txt)
    elif cam == "error":
        user32.SetWindowTextW(CTRL["cam"], "摄像头：不可用")
    elif cam == "closed":
        user32.SetWindowTextW(CTRL["cam"], "摄像头：已关闭")
    else:
        user32.SetWindowTextW(CTRL["cam"], "摄像头：—")

    # 预览
    if st["frame"] is not None:
        hbm = cv2_to_hbitmap(st["frame"])
        if hbm:
            old = user32.SendMessageW(CTRL["preview"], STM_SETIMAGE, IMAGE_BITMAP, hbm)
            if old:
                gdi32.DeleteObject(old)


def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_CREATE:
        create_controls(hwnd)
        user32.SetTimer(hwnd, 1, 200, 0)
        return 0
    elif msg == WM_COMMAND:
        cid = wparam & 0xFFFF
        if cid == ID_START:
            on_start(hwnd)
        elif cid == ID_STOP:
            on_stop(hwnd)
        elif cid == ID_HELP:
            open_camera_help()
        elif cid == ID_EXIT:
            user32.DestroyWindow(hwnd)
        return 0
    elif msg == WM_TIMER:
        update_ui()
        return 0
    elif msg == WM_REMINDER:
        show_reminder()
        return 0
    elif msg == WM_CAM_ERROR:
        show_camera_error(int(wparam))
        # 同步按钮/状态
        with state_lock:
            state["running"] = False
        user32.SetWindowTextW(CTRL["status"], "状态：摄像头不可用")
        user32.EnableWindow(CTRL["start"], 1)
        user32.EnableWindow(CTRL["stop"], 0)
        return 0
    elif msg == WM_CLOSE:
        set_running(False)
        user32.DestroyWindow(hwnd)
        return 0
    elif msg == WM_DESTROY:
        set_running(False)
        user32.KillTimer(hwnd, 1)
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def run_selftest():
    """无界面自检：验证冻结环境下模型加载与检测循环（普通用户无需使用）。"""
    import threading
    log.info("[selftest] 启动检测循环（无界面）...")
    set_running(True)
    captured = []

    real_post = user32.PostMessageW

    def fake_post(hwnd, msg, w, l):
        if msg == WM_CAM_ERROR:
            captured.append(("cam_error", int(w)))
        elif msg == WM_REMINDER:
            captured.append(("reminder", 0))
        return 0

    user32.PostMessageW = fake_post
    try:
        t = threading.Thread(target=detect_loop, args=(0,), daemon=True)
        t.start()
        time.sleep(8)
        s = get_state()
        log.info("[selftest] cam_status = %s", s["cam_status"])
        log.info("[selftest] face = %s ear = %s", s["face"], s["ear"])
        log.info("[selftest] frame_ready = %s", s["frame"] is not None)
        log.info("[selftest] messages = %s", captured)
    finally:
        user32.PostMessageW = real_post
        set_running(False)
        time.sleep(1)
    log.info("[selftest] 完成")
    return 0


def main():
    user32.SetProcessDPIAware()
    hinst = kernel32.GetModuleHandleW(0)

    wndproc_ref = WNDPROC(wndproc)  # 必须保持引用，避免被 GC

    class WNDCLASSEX(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("style", ctypes.c_uint),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", ctypes.c_wchar_p),
            ("lpszClassName", ctypes.c_wchar_p),
            ("hIconSm", wintypes.HANDLE),
        ]

    hcursor = user32.LoadCursorW(0, ctypes.c_void_p(IDC_ARROW))
    wc = WNDCLASSEX()
    wc.cbSize = ctypes.sizeof(WNDCLASSEX)
    wc.style = 0
    wc.lpfnWndProc = wndproc_ref
    wc.hInstance = hinst
    wc.hCursor = hcursor
    wc.hbrBackground = ctypes.c_void_p(COLOR_BTNFACE + 1)
    wc.lpszClassName = "EyeCareWndClass"

    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if not atom:
        log.error("注册窗口类失败")
        return

    hwnd = user32.CreateWindowExW(
        0, "EyeCareWndClass", "护眼提醒 · EyeCare",
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        -2147483648, -2147483648, 440, 600,
        0, 0, hinst, 0,
    )
    if not hwnd:
        log.error("创建窗口失败")
        return

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    try:
        if "--selftest" in sys.argv:
            run_selftest()
        else:
            main()
    except Exception as e:
        log.exception("主程序异常: %s", e)
