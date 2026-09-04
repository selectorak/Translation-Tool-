#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全局热键模块（Windows）
========================
基于 Win32 API RegisterHotKey 实现，无需第三方依赖：
  - RegisterHotKey(NULL, id, mods, vk)：热键消息投递到注册线程自己的消息队列
  - 后台 daemon 线程运行 GetMessage 循环，收到 WM_HOTKEY 后调用回调
  - 线程退出时注销热键（UnregisterHotKey）

注意（系统级限制）：
  - 全局热键在 UAC 提权窗口聚焦时不触发（除非本程序也以管理员运行）
  - 热键会被本程序"独占"，运行期间其他软件收不到该组合
  - 注册失败通常意味着组合已被占用（如另一个程序实例）

快捷键字符串格式（不区分大小写）：
  "Pause"、"Ctrl+Alt+T"、"Ctrl+Shift+F9" …
  修饰键支持 Ctrl/Control、Alt、Shift、Win，最后一个部分为主键；
  空字符串表示禁用（由调用方处理，parse_combo 返回 None）。
"""

import ctypes
from ctypes import wintypes
import threading
import logging

logger = logging.getLogger("hotkey")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
HOTKEY_ID = 1  # 线程内唯一即可

_MOD_MAP = {
    "CTRL": MOD_CONTROL, "CONTROL": MOD_CONTROL,
    "ALT": MOD_ALT,
    "SHIFT": MOD_SHIFT,
    "WIN": MOD_WIN,
}

_VK_MAP = {
    "PAUSE": 0x13, "BREAK": 0x13,
    "SCROLL": 0x91, "SCROLLLOCK": 0x91,
    "INSERT": 0x2D, "DELETE": 0x2E, "DEL": 0x2E,
    "HOME": 0x24, "END": 0x23,
    "PGUP": 0x21, "PAGEUP": 0x21, "PGDN": 0x22, "PAGEDOWN": 0x22,
    "SPACE": 0x20, "TAB": 0x09,
    "ESC": 0x1B, "ESCAPE": 0x1B,
    "BACKQUOTE": 0xC0, "`": 0xC0,
    "MINUS": 0xBD, "-": 0xBD,
    "EQUALS": 0xBB, "=": 0xBB,
}
for _i in range(1, 25):
    _VK_MAP[f"F{_i}"] = 0x6F + _i  # F1 = 0x70


def parse_combo(combo: str):
    """解析快捷键字符串 → (修饰键, 虚拟键码)；无效返回 None"""
    parts = [p.strip() for p in (combo or "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for part in parts[:-1]:
        mod = _MOD_MAP.get(part.upper())
        if mod is None:
            return None
        mods |= mod

    key = parts[-1].upper()
    vk = _VK_MAP.get(key)
    if vk is None:
        # 字母 / 数字直接用 ASCII 码作为虚拟键码
        if len(key) == 1 and ("A" <= key <= "Z" or "0" <= key <= "9"):
            vk = ord(key)
        else:
            return None
    return mods, vk


class GlobalHotkeyManager:
    """全局热键管理器（单热键）：注册 / 注销 / 重绑

    回调在热键线程中执行，调用方需自行投递回主线程
    （本项目通过 task_queue / root.after 转发）。
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._callback = None
        self._reg_ok = False
        self._reg_err = ""
        self._reg_event = threading.Event()

    def start(self, combo: str, callback) -> tuple:
        """注册热键并启动监听线程，返回 (是否成功, 失败原因)"""
        self.stop()
        parsed = parse_combo(combo)
        if parsed is None:
            return False, f"无效的快捷键「{combo}」"

        self._callback = callback
        self._reg_event.clear()
        self._thread = threading.Thread(
            target=self._loop, args=parsed,
            name="global-hotkey", daemon=True,
        )
        self._thread.start()
        # 等待线程完成注册，把结果带回调用方
        self._reg_event.wait(timeout=2.0)
        return self._reg_ok, self._reg_err

    def rebind(self, combo: str, callback) -> tuple:
        """更换热键（先注销旧的再注册新的）"""
        return self.start(combo, callback)

    def stop(self) -> None:
        """注销热键并停止监听线程"""
        thread, tid = self._thread, self._thread_id
        self._thread = None
        self._callback = None
        if thread and thread.is_alive() and tid:
            user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            thread.join(timeout=2.0)

    # ---------- 内部 ----------

    def _loop(self, mods: int, vk: int):
        """监听线程：注册热键 + 消息循环（注册与消息循环必须同线程）"""
        self._thread_id = kernel32.GetCurrentThreadId()
        self._reg_ok = bool(user32.RegisterHotKey(None, HOTKEY_ID, mods, vk))
        if self._reg_ok:
            self._reg_err = ""
        else:
            self._reg_err = (f"注册失败（可能已被其他程序占用，"
                             f"错误码 {ctypes.get_last_error()}）")
        self._reg_event.set()
        if not self._reg_ok:
            return

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message != WM_HOTKEY:
                    continue
                callback = self._callback
                if callback is None:
                    continue
                try:
                    callback()
                except Exception as e:
                    logger.error(f"热键回调异常: {e}")
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self._thread_id = 0
