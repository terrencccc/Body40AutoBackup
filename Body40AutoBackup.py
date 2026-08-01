# -*- coding: utf-8 -*-
"""
Body40AutoBackup.py

功能：
1. 程式放在備份硬碟內，雙擊後等待 BODY40 密錄器。
2. 自動偵測根目錄含 DPB40 的密錄器。
3. 只備份 DPB40\VIDEO 裡的影片，不備份照片及其他資料。
4. 備份到：
   <程式所在硬碟>\◆◆◆密錄器備份◆◆\YYYY-MM-DD\
5. 日期資料夾點開後直接就是影片，不再多包一層 VIDEO。
6. 顯示檔案數量、目前檔名及實際進度條。
7. 不刪除密錄器原始檔。
"""

from __future__ import annotations

import ctypes
import datetime as dt
import os
import shutil
import string
import sys
import threading
import time
from pathlib import Path
from tkinter import Tk, Label, Button, StringVar, messagebox
from tkinter.ttk import Progressbar

APP_NAME = "Body40 自動備份"
POLL_SECONDS = 2
SOURCE_ROOT_FOLDER = "DPB40"
VIDEO_FOLDER_NAME = "VIDEO"

DESTINATION_FOLDER_CANDIDATES = (
    "◆◆◆密錄器備份◆◆",
    "♦♦♦密錄器備份♦♦",
    "密錄器備份",
)

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v",
    ".wmv", ".mts", ".m2ts", ".ts", ".3gp",
    ".mpg", ".mpeg", ".vob"
}

COPY_BUFFER_SIZE = 4 * 1024 * 1024


def get_app_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = get_app_directory()


def list_windows_drives() -> list[Path]:
    drives: list[Path] = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()

    for index, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << index):
            root = Path(f"{letter}:\\")
            try:
                if root.exists():
                    drives.append(root)
            except OSError:
                pass

    return drives


def drive_type(root: Path) -> int:
    return ctypes.windll.kernel32.GetDriveTypeW(str(root))


def find_video_folder(root: Path) -> Path | None:
    """
    優先尋找 DPB40\VIDEO。
    若大小寫不同，則用不分大小寫方式尋找。
    """
    dpb40 = root / SOURCE_ROOT_FOLDER
    if not dpb40.is_dir():
        return None

    direct = dpb40 / VIDEO_FOLDER_NAME
    if direct.is_dir():
        return direct

    try:
        for child in dpb40.iterdir():
            if child.is_dir() and child.name.casefold() == VIDEO_FOLDER_NAME.casefold():
                return child
    except (OSError, PermissionError):
        return None

    return None


def find_source_drive() -> tuple[Path, Path] | None:
    """
    找出含 DPB40\VIDEO 的密錄器。
    排除程式所在磁碟，避免把備份硬碟誤認為來源。
    """
    app_drive = APP_DIR.drive.upper()
    candidates: list[tuple[Path, Path]] = []

    for root in list_windows_drives():
        try:
            if root.drive.upper() == app_drive:
                continue

            video_folder = find_video_folder(root)
            if video_folder is not None:
                candidates.append((root, video_folder))
        except (OSError, PermissionError):
            continue

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            0 if drive_type(item[0]) == 2 else 1,
            str(item[0])
        )
    )
    return candidates[0]


def find_destination_folder() -> Path | None:
    """
    使用 EXE 所在磁碟作為備份硬碟。
    """
    root = Path(APP_DIR.drive + "\\")
    if not root.exists():
        return None

    for folder_name in DESTINATION_FOLDER_CANDIDATES:
        candidate = root / folder_name
        if candidate.is_dir():
            return candidate

    standard = root / DESTINATION_FOLDER_CANDIDATES[0]
    try:
        standard.mkdir(parents=True, exist_ok=True)
        return standard
    except (OSError, PermissionError):
        return None


def collect_video_files(video_folder: Path) -> list[Path]:
    files: list[Path] = []

    try:
        for item in video_folder.rglob("*"):
            if item.is_file() and item.suffix.casefold() in VIDEO_EXTENSIONS:
                files.append(item)
    except (OSError, PermissionError):
        pass

    files.sort(key=lambda p: (p.stat().st_mtime if p.exists() else 0, p.name.casefold()))
    return files


def get_unique_destination(target_folder: Path, source_file: Path) -> Path:
    """
    日期資料夾內直接放影片。
    遇到同名檔案時自動加上 _2、_3，避免覆蓋。
    """
    candidate = target_folder / source_file.name

    if not candidate.exists():
        return candidate

    try:
        if (
            candidate.stat().st_size == source_file.stat().st_size
            and int(candidate.stat().st_mtime) == int(source_file.stat().st_mtime)
        ):
            return candidate
    except OSError:
        pass

    stem = source_file.stem
    suffix = source_file.suffix
    index = 2

    while True:
        candidate = target_folder / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def copy_file_with_progress(
    source: Path,
    destination: Path,
    on_chunk
) -> bool:
    """
    回傳 True 表示有實際複製，False 表示檔案已存在且相同。
    """
    if destination.exists():
        try:
            if (
                destination.stat().st_size == source.stat().st_size
                and int(destination.stat().st_mtime) == int(source.stat().st_mtime)
            ):
                on_chunk(source.stat().st_size)
                return False
        except OSError:
            pass

    temp_destination = destination.with_suffix(destination.suffix + ".part")

    try:
        if temp_destination.exists():
            temp_destination.unlink()
    except OSError:
        pass

    try:
        with source.open("rb") as src, temp_destination.open("wb") as dst:
            while True:
                chunk = src.read(COPY_BUFFER_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
                on_chunk(len(chunk))

        shutil.copystat(source, temp_destination)
        temp_destination.replace(destination)
        return True

    except Exception:
        try:
            if temp_destination.exists():
                temp_destination.unlink()
        except OSError:
            pass
        raise


class BackupApp:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title(APP_NAME)
        self.root.geometry("540x300")
        self.root.resizable(False, False)

        self.status_text = StringVar(value="正在啟動…")
        self.detail_text = StringVar(value="")
        self.file_text = StringVar(value="")
        self.progress_text = StringVar(value="0%")

        Label(
            self.root,
            text=APP_NAME,
            font=("Microsoft JhengHei UI", 20, "bold")
        ).pack(pady=(22, 10))

        Label(
            self.root,
            textvariable=self.status_text,
            font=("Microsoft JhengHei UI", 13)
        ).pack(pady=4)

        Label(
            self.root,
            textvariable=self.detail_text,
            font=("Microsoft JhengHei UI", 10),
            wraplength=500,
            justify="center"
        ).pack(pady=4)

        Label(
            self.root,
            textvariable=self.file_text,
            font=("Microsoft JhengHei UI", 10),
            wraplength=500,
            justify="center"
        ).pack(pady=(8, 4))

        self.progress = Progressbar(
            self.root,
            orient="horizontal",
            mode="determinate",
            length=440,
            maximum=100
        )
        self.progress.pack(pady=(8, 3))

        Label(
            self.root,
            textvariable=self.progress_text,
            font=("Microsoft JhengHei UI", 10)
        ).pack()

        Button(
            self.root,
            text="結束",
            width=12,
            command=self.close,
            font=("Microsoft JhengHei UI", 10)
        ).pack(pady=14)

        self.running = True
        self.backing_up = False
        self.current_source_drive: str | None = None

        self.worker = threading.Thread(target=self.monitor_loop, daemon=True)
        self.worker.start()

        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def ui(self, callback) -> None:
        self.root.after(0, callback)

    def set_status(self, status: str, detail: str = "", filename: str = "") -> None:
        self.ui(lambda: self.status_text.set(status))
        self.ui(lambda: self.detail_text.set(detail))
        self.ui(lambda: self.file_text.set(filename))

    def set_progress(self, percent: float) -> None:
        value = max(0.0, min(100.0, percent))
        self.ui(lambda: self.progress.configure(value=value))
        self.ui(lambda: self.progress_text.set(f"{value:.1f}%"))

    def notify(self, title: str, text: str, error: bool = False) -> None:
        def show() -> None:
            if error:
                messagebox.showerror(title, text)
            else:
                messagebox.showinfo(title, text)

        self.ui(show)

    def backup_videos(self, video_folder: Path, destination_root: Path) -> None:
        today = dt.datetime.now().strftime("%Y-%m-%d")
        target_folder = destination_root / today
        target_folder.mkdir(parents=True, exist_ok=True)

        video_files = collect_video_files(video_folder)

        if not video_files:
            self.set_status(
                "找不到影片",
                f"已找到密錄器，但 {video_folder} 內沒有可備份的影片。"
            )
            self.set_progress(0)
            self.notify(APP_NAME, "VIDEO 資料夾內沒有找到影片。", error=True)
            return

        total_bytes = sum(file.stat().st_size for file in video_files)
        copied_bytes = 0
        copied_count = 0
        skipped_count = 0

        self.set_progress(0)

        def on_chunk(size: int) -> None:
            nonlocal copied_bytes
            copied_bytes += size
            percent = (copied_bytes / total_bytes * 100) if total_bytes else 100
            self.set_progress(percent)

        for index, source_file in enumerate(video_files, start=1):
            if not self.running:
                return

            self.set_status(
                "正在備份影片",
                f"第 {index}／{len(video_files)} 個",
                source_file.name
            )

            destination_file = get_unique_destination(target_folder, source_file)
            copied = copy_file_with_progress(
                source_file,
                destination_file,
                on_chunk
            )

            if copied:
                copied_count += 1
            else:
                skipped_count += 1

        self.set_progress(100)
        self.set_status(
            "備份完成",
            f"已複製 {copied_count} 個影片，略過 {skipped_count} 個相同檔案。",
            f"位置：{target_folder}"
        )

        self.notify(
            APP_NAME,
            f"備份完成。\n\n"
            f"影片：{copied_count} 個\n"
            f"略過：{skipped_count} 個\n\n"
            f"位置：\n{target_folder}"
        )

        try:
            os.startfile(target_folder)
        except OSError:
            pass

    def monitor_loop(self) -> None:
        destination_root = find_destination_folder()

        if destination_root is None:
            self.set_status(
                "找不到備份硬碟",
                "請把 Body40AutoBackup.exe 放在「明葦 硬碟」內，再重新啟動。"
            )
            return

        self.set_status(
            "等待 BODY40 密錄器",
            f"只會備份 DPB40\\VIDEO 內的影片。\n備份位置：{destination_root}"
        )
        self.set_progress(0)

        while self.running:
            source = find_source_drive()

            if source is None:
                self.current_source_drive = None
                if not self.backing_up:
                    self.set_status(
                        "等待 BODY40 密錄器",
                        f"只會備份 DPB40\\VIDEO 內的影片。\n備份位置：{destination_root}"
                    )
                    self.set_progress(0)
                time.sleep(POLL_SECONDS)
                continue

            source_drive, video_folder = source
            source_key = str(source_drive)

            if self.backing_up or self.current_source_drive == source_key:
                time.sleep(POLL_SECONDS)
                continue

            self.current_source_drive = source_key
            self.backing_up = True

            try:
                self.backup_videos(video_folder, destination_root)
            except Exception as exc:
                self.set_status(
                    "備份失敗",
                    "請確認硬碟空間、裝置連線及檔案權限。",
                    str(exc)
                )
                self.notify(APP_NAME, f"備份失敗：\n{exc}", error=True)
            finally:
                self.backing_up = False

            self.set_status(
                "請拔除密錄器",
                "拔除後可再次插入並進行下一次備份。"
            )

            while self.running and find_source_drive() is not None:
                time.sleep(POLL_SECONDS)

            self.current_source_drive = None
            self.set_status(
                "等待 BODY40 密錄器",
                f"只會備份 DPB40\\VIDEO 內的影片。\n備份位置：{destination_root}"
            )
            self.set_progress(0)

    def close(self) -> None:
        self.running = False
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if os.name != "nt":
        print("本程式僅支援 Windows。")
        return

    BackupApp().run()


if __name__ == "__main__":
    main()
