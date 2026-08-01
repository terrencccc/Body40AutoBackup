# -*- coding: utf-8 -*-
"""
Body40AutoBackup.py
用途：
1. 將本程式放在「明葦 硬碟」內。
2. 雙擊啟動後，程式持續等待 BODY40 密錄器。
3. 偵測到根目錄含 DPB40 的裝置後，自動備份到：
   <備份硬碟>\◆◆◆密錄器備份◆◆\YYYY-MM-DD\
4. 備份完成後顯示訊息，並繼續等待下一次插入。

注意：
- 不刪除密錄器原始檔。
- 不依賴固定磁碟代號。
- 僅使用 Windows 內建功能與 Python 標準函式庫。
- 打包成 EXE 後可免安裝 Python 使用。
"""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import shutil
import string
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import Tk, Label, Button, StringVar, messagebox

APP_NAME = "Body40 自動備份"
POLL_SECONDS = 2
SOURCE_FOLDER_NAME = "DPB40"
DESTINATION_FOLDER_CANDIDATES = (
    "◆◆◆密錄器備份◆◆",
    "♦♦♦密錄器備份♦♦",
    "密錄器備份",
)
LOG_FILE_NAME = "Body40Backup.log"
STATE_FILE_NAME = "Body40Backup_state.json"


def get_app_directory() -> Path:
    """取得程式所在目錄；打包成 EXE 後也會指向 EXE 所在位置。"""
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
    # 2=Removable, 3=Fixed, 4=Network, 5=CDROM, 6=RAMDisk
    return ctypes.windll.kernel32.GetDriveTypeW(str(root))


def volume_label(root: Path) -> str:
    volume_name_buffer = ctypes.create_unicode_buffer(261)
    fs_name_buffer = ctypes.create_unicode_buffer(261)
    serial_number = ctypes.c_ulong()
    max_component_length = ctypes.c_ulong()
    file_system_flags = ctypes.c_ulong()

    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(str(root)),
        volume_name_buffer,
        ctypes.sizeof(volume_name_buffer),
        ctypes.byref(serial_number),
        ctypes.byref(max_component_length),
        ctypes.byref(file_system_flags),
        fs_name_buffer,
        ctypes.sizeof(fs_name_buffer),
    )
    return volume_name_buffer.value if ok else ""


def find_source_drive() -> Path | None:
    """來源判斷：磁碟根目錄存在 DPB40，且不是程式所在磁碟。"""
    app_drive = APP_DIR.drive.upper()
    candidates: list[Path] = []

    for root in list_windows_drives():
        try:
            if root.drive.upper() == app_drive:
                continue
            source_dir = root / SOURCE_FOLDER_NAME
            if source_dir.is_dir():
                candidates.append(root)
        except (OSError, PermissionError):
            continue

    if not candidates:
        return None

    # 優先可移除式磁碟，再依磁碟代號排序。
    candidates.sort(key=lambda p: (0 if drive_type(p) == 2 else 1, str(p)))
    return candidates[0]


def find_destination_root() -> tuple[Path, Path] | None:
    """
    優先使用程式所在磁碟作為備份硬碟。
    找出既有的「密錄器備份」資料夾；若不存在則建立標準名稱。
    回傳：(備份硬碟根目錄, 備份主資料夾)
    """
    root = Path(APP_DIR.drive + "\\")
    if not root.exists():
        return None

    for folder_name in DESTINATION_FOLDER_CANDIDATES:
        candidate = root / folder_name
        if candidate.is_dir():
            return root, candidate

    standard = root / DESTINATION_FOLDER_CANDIDATES[0]
    try:
        standard.mkdir(parents=True, exist_ok=True)
        return root, standard
    except (OSError, PermissionError):
        return None


def safe_log(message: str) -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}\n"
    try:
        with (APP_DIR / LOG_FILE_NAME).open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def load_state() -> dict:
    path = APP_DIR / STATE_FILE_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    path = APP_DIR / STATE_FILE_NAME
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def source_signature(source_root: Path) -> str:
    """
    使用磁碟代號、卷標及 DPB40 最後修改時間建立簡易識別。
    可避免同一次插入被重複啟動備份。
    """
    try:
        modified = int((source_root / SOURCE_FOLDER_NAME).stat().st_mtime)
    except OSError:
        modified = 0
    return f"{source_root.drive}|{volume_label(source_root)}|{modified}"


def unique_target_folder(base_folder: Path) -> Path:
    today = dt.datetime.now().strftime("%Y-%m-%d")
    target = base_folder / today
    target.mkdir(parents=True, exist_ok=True)
    return target


def robocopy_backup(source: Path, target: Path) -> tuple[bool, str]:
    """
    使用 Windows 內建 robocopy。
    回傳碼 0~7 均代表成功或有可接受差異；8 以上才視為失敗。
    """
    cmd = [
        "robocopy",
        str(source),
        str(target),
        "/E",          # 包含子資料夾
        "/COPY:DAT",   # 資料、屬性、時間
        "/DCOPY:DAT",
        "/R:2",        # 失敗重試 2 次
        "/W:2",        # 每次等 2 秒
        "/XJ",         # 排除 junction
        "/FFT",        # 相容 FAT/exFAT 時間精度
        "/NP",
        "/NFL",
        "/NDL",
    ]

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=creationflags,
    )
    success = result.returncode <= 7
    detail = (result.stdout or result.stderr or "").strip()
    return success, detail


def shutil_backup(source: Path, target: Path) -> tuple[bool, str]:
    """robocopy 無法使用時的備援方案。"""
    copied = 0
    skipped = 0
    try:
        for item in source.rglob("*"):
            relative = item.relative_to(source)
            destination = target / relative

            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                try:
                    if (
                        destination.stat().st_size == item.stat().st_size
                        and int(destination.stat().st_mtime) >= int(item.stat().st_mtime)
                    ):
                        skipped += 1
                        continue
                except OSError:
                    pass

            shutil.copy2(item, destination)
            copied += 1

        return True, f"複製 {copied} 個檔案，略過 {skipped} 個既有檔案。"
    except Exception as exc:
        return False, str(exc)


class BackupApp:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title(APP_NAME)
        self.root.geometry("470x210")
        self.root.resizable(False, False)

        self.status = StringVar(value="正在啟動…")
        self.detail = StringVar(value="請先插入備份硬碟，再啟動本程式。")

        Label(self.root, text=APP_NAME, font=("Microsoft JhengHei UI", 18, "bold")).pack(pady=(20, 8))
        Label(self.root, textvariable=self.status, font=("Microsoft JhengHei UI", 12)).pack(pady=6)
        Label(
            self.root,
            textvariable=self.detail,
            font=("Microsoft JhengHei UI", 10),
            wraplength=430,
            justify="center",
        ).pack(pady=6)

        Button(
            self.root,
            text="結束",
            width=12,
            command=self.close,
            font=("Microsoft JhengHei UI", 10),
        ).pack(pady=10)

        self.running = True
        self.backing_up = False
        self.last_seen_signature: str | None = None
        self.worker = threading.Thread(target=self.monitor_loop, daemon=True)
        self.worker.start()

        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def set_status(self, status: str, detail: str = "") -> None:
        self.root.after(0, lambda: self.status.set(status))
        self.root.after(0, lambda: self.detail.set(detail))

    def notify(self, title: str, text: str, error: bool = False) -> None:
        def _show() -> None:
            if error:
                messagebox.showerror(title, text)
            else:
                messagebox.showinfo(title, text)
        self.root.after(0, _show)

    def monitor_loop(self) -> None:
        safe_log("程式啟動。")
        destination = find_destination_root()

        if destination is None:
            self.set_status(
                "找不到備份硬碟",
                "請將本程式放在「明葦 硬碟」內，再重新啟動。",
            )
            safe_log("無法取得備份硬碟。")
            return

        destination_drive, destination_folder = destination
        self.set_status(
            "等待 BODY40 密錄器",
            f"備份位置：{destination_folder}",
        )

        while self.running:
            source_drive = find_source_drive()

            if source_drive is None:
                self.last_seen_signature = None
                if not self.backing_up:
                    self.set_status(
                        "等待 BODY40 密錄器",
                        f"備份位置：{destination_folder}",
                    )
                time.sleep(POLL_SECONDS)
                continue

            signature = source_signature(source_drive)
            if signature == self.last_seen_signature or self.backing_up:
                time.sleep(POLL_SECONDS)
                continue

            self.last_seen_signature = signature
            self.backing_up = True

            source_folder = source_drive / SOURCE_FOLDER_NAME
            target_folder = unique_target_folder(destination_folder)

            self.set_status(
                "偵測到密錄器，開始備份",
                f"{source_folder}  →  {target_folder}",
            )
            safe_log(f"開始備份：{source_folder} -> {target_folder}")

            try:
                success, detail = robocopy_backup(source_folder, target_folder)
            except FileNotFoundError:
                success, detail = shutil_backup(source_folder, target_folder)
            except Exception as exc:
                success, detail = False, str(exc)

            if success:
                safe_log(f"備份完成。{detail}")
                self.set_status(
                    "備份完成",
                    f"已備份到：{target_folder}\n可拔除密錄器，程式會繼續等待下一次。",
                )
                self.notify(
                    APP_NAME,
                    f"備份完成。\n\n位置：\n{target_folder}",
                )
                try:
                    os.startfile(target_folder)
                except OSError:
                    pass
            else:
                safe_log(f"備份失敗：{detail}")
                self.set_status(
                    "備份失敗",
                    "請確認硬碟空間、裝置連線及檔案權限。",
                )
                self.notify(
                    APP_NAME,
                    f"備份失敗：\n{detail}",
                    error=True,
                )

            self.backing_up = False

            # 等待密錄器拔除後才允許下一次備份，
            # 避免裝置仍插著時重複執行。
            while self.running and find_source_drive() is not None:
                time.sleep(POLL_SECONDS)

            self.last_seen_signature = None

    def close(self) -> None:
        self.running = False
        safe_log("程式結束。")
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if os.name != "nt":
        print("本程式僅支援 Windows。")
        return

    app = BackupApp()
    app.run()


if __name__ == "__main__":
    main()
