# -*- coding: utf-8 -*-
"""
BODY40 自動備份 v2.1.0

規則：
1. 來源磁碟標籤必須是「明葦 密錄器」。
2. 目的磁碟標籤必須是「明葦 硬碟」。
3. 目的硬碟根目錄必須已存在名稱含「密錄器備份」的資料夾。
4. 程式只使用既有資料夾，絕不自行新增第二個「密錄器備份」。
5. 只備份 DPB40\VIDEO 內的影片。
6. 依影片檔名日期分類：
   2024_1022... -> 1131022
   2026_0615... -> 1150615
7. 日期資料夾內直接放影片，不建立 VIDEO 子資料夾。
8. 顯示真實百分比、速度、剩餘時間、目前檔名。
"""

from __future__ import annotations

import ctypes
import datetime as dt
import os
import re
import shutil
import string
import sys
import threading
import time
from pathlib import Path
from tkinter import Tk, Label, Button, StringVar, messagebox
from tkinter.ttk import Progressbar

APP_NAME = "BODY40 自動備份"
VERSION = "v4.0.0"
BUILD_DATE = "2026-08-02 FINAL"

SOURCE_VOLUME_LABEL = "明葦 密錄器"
DESTINATION_VOLUME_LABEL = "明葦 硬碟"
SOURCE_ROOT_FOLDER = "DPB40"
VIDEO_FOLDER_NAME = "VIDEO"

POLL_SECONDS = 2
COPY_BUFFER_SIZE = 16 * 1024 * 1024
UI_UPDATE_INTERVAL = 0.2

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v",
    ".wmv", ".mts", ".m2ts", ".ts", ".3gp",
    ".mpg", ".mpeg", ".vob"
}


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


def volume_label(root: Path) -> str:
    volume_name = ctypes.create_unicode_buffer(261)
    file_system_name = ctypes.create_unicode_buffer(261)
    serial_number = ctypes.c_ulong()
    max_component_length = ctypes.c_ulong()
    file_system_flags = ctypes.c_ulong()

    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(str(root)),
        volume_name,
        len(volume_name),
        ctypes.byref(serial_number),
        ctypes.byref(max_component_length),
        ctypes.byref(file_system_flags),
        file_system_name,
        len(file_system_name),
    )

    return volume_name.value.strip() if ok else ""


def normalize_folder_name(name: str) -> str:
    """
    忽略空白與裝飾符號後判斷是否為密錄器備份資料夾。
    例如：
    ◆◆◆密錄器備份◆◆
    ♦♦♦密錄器備份♦♦♦
    密錄器備份
    """
    return re.sub(r"[\s◆◇♦◈★☆●○■□▲△▼▽]+", "", name)


def find_existing_backup_folder(destination_root: Path) -> Path | None:
    """
    只尋找既有的密錄器備份資料夾，絕不建立新的。
    若有多個，優先：
    1. EXE 本身就在其中
    2. 名稱正規化後完全等於「密錄器備份」
    3. 修改時間較舊者，通常是原本既有資料夾
    """
    if normalize_folder_name(APP_DIR.name) == "密錄器備份":
        return APP_DIR

    candidates: list[Path] = []

    try:
        for child in destination_root.iterdir():
            if not child.is_dir():
                continue

            normalized = normalize_folder_name(child.name)
            if normalized == "密錄器備份":
                candidates.append(child)
    except (OSError, PermissionError):
        return None

    if not candidates:
        return None

    candidates.sort(
        key=lambda p: (
            0 if p.resolve() == APP_DIR.resolve() else 1,
            p.stat().st_mtime if p.exists() else float("inf"),
            p.name
        )
    )
    return candidates[0]


def find_destination() -> tuple[Path, Path] | None:
    for root in list_windows_drives():
        try:
            if volume_label(root) != DESTINATION_VOLUME_LABEL:
                continue

            backup_folder = find_existing_backup_folder(root)
            if backup_folder is not None:
                return root, backup_folder
        except (OSError, PermissionError):
            continue

    return None


def find_video_folder(root: Path) -> Path | None:
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


def find_source() -> tuple[Path, Path] | None:
    for root in list_windows_drives():
        try:
            if volume_label(root) != SOURCE_VOLUME_LABEL:
                continue

            video_folder = find_video_folder(root)
            if video_folder is not None:
                return root, video_folder
        except (OSError, PermissionError):
            continue

    return None


def collect_video_files(video_folder: Path) -> list[Path]:
    files: list[Path] = []

    try:
        for item in video_folder.rglob("*"):
            if item.is_file() and item.suffix.casefold() in VIDEO_EXTENSIONS:
                files.append(item)
    except (OSError, PermissionError):
        pass

    files.sort(key=lambda p: p.name.casefold())
    return files


def parse_video_date(source_file: Path) -> dt.date | None:
    """
    FINAL v4.0：
    日期只取檔名，不讀任何檔案時間。
    """
    name = source_file.name

    m = re.match(r'^(\d{4})_(\d{2})(\d{2})_', name)
    if not m:
        m = re.match(r'^(\d{4})(\d{2})(\d{2})_', name)
    if not m:
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})_', name)

    if not m:
        return None

    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None

def to_roc_folder_name(date_value: dt.date) -> str:
    roc_year = date_value.year - 1911
    return f"{roc_year:03d}{date_value.month:02d}{date_value.day:02d}"


def human_size(byte_count: float) -> str:
    value = float(byte_count)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024

    return f"{value:.1f} TB"


def format_eta(seconds: float) -> str:
    if seconds <= 0 or seconds == float("inf"):
        return "計算中"

    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours} 小時 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def files_are_same(source: Path, destination: Path) -> bool:
    try:
        return destination.exists() and destination.stat().st_size == source.stat().st_size
    except OSError:
        return False


def get_destination_path(target_folder: Path, source_file: Path) -> Path:
    candidate = target_folder / source_file.name

    if not candidate.exists() or files_are_same(source_file, candidate):
        return candidate

    stem = source_file.stem
    suffix = source_file.suffix
    index = 2

    while True:
        candidate = target_folder / f"{stem}_{index}{suffix}"
        if not candidate.exists() or files_are_same(source_file, candidate):
            return candidate
        index += 1


class BackupApp:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title(f"{APP_NAME} {VERSION}")
        self.root.geometry("640x420")
        self.root.resizable(False, False)

        self.status_text = StringVar(value="正在啟動…")
        self.route_text = StringVar(value="")
        self.file_text = StringVar(value="")
        self.count_text = StringVar(value="")
        self.progress_text = StringVar(value="0.0%")
        self.speed_text = StringVar(value="速度：0 MB/s")
        self.eta_text = StringVar(value="剩餘時間：計算中")
        self.capacity_text = StringVar(value="已處理：0 B / 0 B")

        Label(
            self.root,
            text=f"{APP_NAME} {VERSION}",
            font=("Microsoft JhengHei UI", 20, "bold")
        ).pack(pady=(18, 4))

        Label(
            self.root,
            text=f"Build {BUILD_DATE}",
            font=("Microsoft JhengHei UI", 9)
        ).pack(pady=(0, 8))

        Label(
            self.root,
            textvariable=self.status_text,
            font=("Microsoft JhengHei UI", 13)
        ).pack(pady=3)

        Label(
            self.root,
            textvariable=self.route_text,
            font=("Microsoft JhengHei UI", 10),
            wraplength=600,
            justify="center"
        ).pack(pady=3)

        Label(
            self.root,
            textvariable=self.file_text,
            font=("Microsoft JhengHei UI", 10),
            wraplength=600,
            justify="center"
        ).pack(pady=(10, 3))

        Label(
            self.root,
            textvariable=self.count_text,
            font=("Microsoft JhengHei UI", 10)
        ).pack(pady=2)

        self.progress = Progressbar(
            self.root,
            orient="horizontal",
            mode="determinate",
            length=520,
            maximum=100
        )
        self.progress.pack(pady=(10, 3))

        Label(
            self.root,
            textvariable=self.progress_text,
            font=("Microsoft JhengHei UI", 10)
        ).pack()

        Label(
            self.root,
            textvariable=self.speed_text,
            font=("Microsoft JhengHei UI", 10)
        ).pack(pady=(8, 2))

        Label(
            self.root,
            textvariable=self.eta_text,
            font=("Microsoft JhengHei UI", 10)
        ).pack(pady=2)

        Label(
            self.root,
            textvariable=self.capacity_text,
            font=("Microsoft JhengHei UI", 10)
        ).pack(pady=2)

        Button(
            self.root,
            text="結束",
            width=12,
            command=self.close,
            font=("Microsoft JhengHei UI", 10)
        ).pack(pady=14)

        self.running = True
        self.backing_up = False
        self.current_source: str | None = None

        self.worker = threading.Thread(target=self.monitor_loop, daemon=True)
        self.worker.start()

        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def ui(self, callback) -> None:
        self.root.after(0, callback)

    def set_progress_ui(
        self,
        percent: float,
        speed_bps: float,
        eta_seconds: float,
        processed_bytes: int,
        total_bytes: int
    ) -> None:
        value = max(0.0, min(100.0, percent))

        self.ui(lambda: self.progress.configure(value=value))
        self.ui(lambda: self.progress_text.set(f"{value:.1f}%"))
        self.ui(lambda: self.speed_text.set(f"速度：{human_size(speed_bps)}/s"))
        self.ui(lambda: self.eta_text.set(f"剩餘時間：約 {format_eta(eta_seconds)}"))
        self.ui(
            lambda: self.capacity_text.set(
                f"已處理：{human_size(processed_bytes)} / {human_size(total_bytes)}"
            )
        )

    def notify(self, text: str, error: bool = False) -> None:
        def show() -> None:
            if error:
                messagebox.showerror(f"{APP_NAME} {VERSION}", text)
            else:
                messagebox.showinfo(f"{APP_NAME} {VERSION}", text)

        self.ui(show)

    def backup_videos(
        self,
        source_root: Path,
        video_folder: Path,
        backup_folder: Path
    ) -> None:
        all_files = collect_video_files(video_folder)

        dated_files: list[tuple[Path, dt.date]] = []
        invalid_files: list[Path] = []

        for file in all_files:
            parsed_date = parse_video_date(file)
            if parsed_date is None:
                invalid_files.append(file)
            else:
                dated_files.append((file, parsed_date))

        if not dated_files:
            self.ui(lambda: self.status_text.set("找不到可辨識日期的影片"))
            self.notify(
                "影片檔名內找不到可辨識日期。\n"
                "程式沒有使用今天日期，也沒有建立任何錯誤資料夾。",
                error=True
            )
            return

        total_bytes = sum(file.stat().st_size for file, _ in dated_files)
        processed_bytes = 0
        copied_count = 0
        skipped_count = 0

        start_time = time.monotonic()
        last_ui_update = 0.0

        self.ui(lambda: self.status_text.set("正在備份影片"))
        self.ui(
            lambda: self.route_text.set(
                f"來源：{SOURCE_VOLUME_LABEL} {source_root}  →  "
                f"目的：{DESTINATION_VOLUME_LABEL} {backup_folder}"
            )
        )

        for index, (source_file, video_date) in enumerate(dated_files, start=1):
            if not self.running:
                return

            folder_name = to_roc_folder_name(video_date)
            target_folder = backup_folder / folder_name
            target_folder.mkdir(parents=True, exist_ok=True)

            destination_file = get_destination_path(target_folder, source_file)
            file_size = source_file.stat().st_size

            self.ui(lambda i=index: self.count_text.set(f"第 {i} / {len(dated_files)} 個"))
            self.ui(
                lambda f=source_file.name, d=folder_name:
                self.file_text.set(f"{f}\n分類：{d}")
            )

            if files_are_same(source_file, destination_file):
                processed_bytes += file_size
                skipped_count += 1
            else:
                temp_file = destination_file.with_suffix(destination_file.suffix + ".part")

                try:
                    if temp_file.exists():
                        temp_file.unlink()
                except OSError:
                    pass

                try:
                    with source_file.open("rb") as src, temp_file.open("wb") as dst:
                        while self.running:
                            chunk = src.read(COPY_BUFFER_SIZE)
                            if not chunk:
                                break

                            dst.write(chunk)
                            processed_bytes += len(chunk)

                            now = time.monotonic()
                            if now - last_ui_update >= UI_UPDATE_INTERVAL:
                                elapsed = max(now - start_time, 0.001)
                                speed = processed_bytes / elapsed
                                remaining = max(total_bytes - processed_bytes, 0)
                                eta = remaining / speed if speed > 0 else float("inf")
                                percent = processed_bytes / total_bytes * 100

                                self.set_progress_ui(
                                    percent,
                                    speed,
                                    eta,
                                    processed_bytes,
                                    total_bytes
                                )
                                last_ui_update = now

                    shutil.copystat(source_file, temp_file)
                    temp_file.replace(destination_file)
                    copied_count += 1

                except Exception:
                    try:
                        if temp_file.exists():
                            temp_file.unlink()
                    except OSError:
                        pass
                    raise

            elapsed = max(time.monotonic() - start_time, 0.001)
            speed = processed_bytes / elapsed
            remaining = max(total_bytes - processed_bytes, 0)
            eta = remaining / speed if speed > 0 else float("inf")
            percent = processed_bytes / total_bytes * 100

            self.set_progress_ui(
                percent,
                speed,
                eta,
                processed_bytes,
                total_bytes
            )

        elapsed = max(time.monotonic() - start_time, 0.001)
        average_speed = processed_bytes / elapsed
        self.set_progress_ui(100, average_speed, 0, total_bytes, total_bytes)

        self.ui(lambda: self.status_text.set("備份完成"))
        self.ui(
            lambda: self.file_text.set(
                f"複製 {copied_count} 個，略過 {skipped_count} 個。"
            )
        )
        self.ui(
            lambda: self.count_text.set(
                f"無法辨識日期而略過：{len(invalid_files)} 個"
            )
        )

        self.notify(
            f"備份完成。\n\n"
            f"版本：{VERSION}\n"
            f"複製：{copied_count} 個\n"
            f"略過既有：{skipped_count} 個\n"
            f"無法辨識日期：{len(invalid_files)} 個\n"
            f"平均速度：{human_size(average_speed)}/s\n\n"
            f"位置：\n{backup_folder}"
        )

        try:
            os.startfile(backup_folder)
        except OSError:
            pass

    def monitor_loop(self) -> None:
        destination = find_destination()

        if destination is None:
            self.ui(lambda: self.status_text.set("找不到既有備份資料夾"))
            self.ui(
                lambda: self.route_text.set(
                    f"請確認「{DESTINATION_VOLUME_LABEL}」根目錄內，"
                    f"已存在名稱含「密錄器備份」的資料夾。"
                )
            )
            self.ui(
                lambda: self.file_text.set(
                    "為避免產生第二個資料夾，程式已停止，沒有建立任何新資料夾。"
                )
            )
            self.notify(
                f"找不到「{DESTINATION_VOLUME_LABEL}」內既有的密錄器備份資料夾。\n\n"
                f"程式不會自行新增第二個資料夾。",
                error=True
            )
            return

        _, backup_folder = destination

        self.ui(lambda: self.status_text.set("等待明葦 密錄器"))
        self.ui(lambda: self.route_text.set(f"目的資料夾：{backup_folder}"))
        self.ui(
            lambda: self.file_text.set(
                "只備份 DPB40\\VIDEO，依影片檔名建立民國日期資料夾。"
            )
        )

        while self.running:
            source = find_source()

            if source is None:
                self.current_source = None
                time.sleep(POLL_SECONDS)
                continue

            source_root, video_folder = source
            source_key = str(source_root)

            if self.backing_up or self.current_source == source_key:
                time.sleep(POLL_SECONDS)
                continue

            self.current_source = source_key
            self.backing_up = True

            try:
                self.backup_videos(source_root, video_folder, backup_folder)
            except Exception as exc:
                self.ui(lambda: self.status_text.set("備份失敗"))
                self.ui(lambda: self.file_text.set(str(exc)))
                self.notify(
                    f"備份失敗：\n{exc}\n\n"
                    f"請確認裝置連線、硬碟空間與檔案權限。",
                    error=True
                )
            finally:
                self.backing_up = False

            self.ui(lambda: self.status_text.set("請拔除明葦 密錄器"))
            self.ui(lambda: self.route_text.set("拔除後可再次插入進行下一次備份。"))

            while self.running and find_source() is not None:
                time.sleep(POLL_SECONDS)

            self.current_source = None
            self.ui(lambda: self.status_text.set("等待明葦 密錄器"))
            self.ui(lambda: self.route_text.set(f"目的資料夾：{backup_folder}"))
            self.ui(
                lambda: self.file_text.set(
                    "只備份 DPB40\\VIDEO，依影片檔名建立民國日期資料夾。"
                )
            )
            self.set_progress_ui(0, 0, 0, 0, 0)

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
