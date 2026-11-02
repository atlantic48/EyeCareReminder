@echo off
REM 构建护眼提醒 exe（单文件、无控制台窗口）
REM 依赖：pip install numpy mediapipe pyinstaller
REM face_landmarker.task 需与本脚本同目录（已随项目提供）

pyinstaller --noconfirm --clean --windowed --onefile ^
  --name EyeCareReminder ^
  --add-data "face_landmarker.task;." ^
  --collect-all mediapipe ^
  --hidden-import=mediapipe.tasks.python.vision ^
  --hidden-import=mediapipe.tasks.python.core.base_options ^
  eye_care_app.py

echo.
echo 构建完成，exe 位于 dist\EyeCareReminder.exe
pause
