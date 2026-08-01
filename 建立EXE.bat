@echo off
chcp 65001 >nul
title 建立 Body40AutoBackup.exe

echo ==========================================
echo   Body40 自動備份 EXE 建立工具
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo 找不到 Python。
    echo 請在可安裝軟體的私人 Windows 電腦執行此檔。
    pause
    exit /b 1
)

python -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo PyInstaller 安裝失敗。
    pause
    exit /b 1
)

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name Body40AutoBackup ^
  Body40AutoBackup.py

if errorlevel 1 (
    echo 建立失敗。
    pause
    exit /b 1
)

echo.
echo 建立完成：
echo %cd%\dist\Body40AutoBackup.exe
echo.
pause
