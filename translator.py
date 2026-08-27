#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中英翻译助手 - 桌面版
========================
功能：
  1. 界面整体翻译  — 在输入框中输入文字，点击按钮翻译全部
  2. 选择区域翻译  — 在输入框中鼠标选中文字，自动翻译选中部分
  3. 跨软件划词翻译 — 在其他软件(Ctrl+C)复制文字，自动检测并弹出翻译
  4. 翻译显示区    — 主窗口显示翻译结果
  5. 屏幕区域翻译  — 拖拽选择屏幕区域，OCR识别+语义判断后翻译

使用方式：
  - 直接运行: python translator.py
  - 双击 run.bat 启动
  - 在其他软件中 Ctrl+C 复制文字 → 自动弹出翻译浮窗
  - Ctrl+Shift+S → 屏幕区域翻译
"""

import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK","TRUE")
os.environ.setdefault("HF_ENDPOINT","https://hf-mirror.com")

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import time
import re
import sys
import logging

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
logger = logging.getLogger("translator")

from theme import Theme
from config import (
    APP_TITLE, APP_VERSION,
    CHECK_CLIPBOARD_INTERVAL,
    CLIPBOARD_PREVIEW_CHARS, SELECTION_CHECK_DELAY_MS, TASK_QUEUE_POLL_MS,
    FLOAT_POPUP_AUTO_HIDE, FLOAT_POPUP_WIDTH, FLOAT_POPUP_HEIGHT,
    MAIN_WIN_WIDTH, MAIN_WIN_HEIGHT,
)
from ui_components import FloatPopup, SettingsDialog

# =========================== 屏幕翻译模块（懒加载） ===========================
_screen_translator = None
_audio_translator = None

def _get_audio_translator(app):
    """懒加载音频翻译模块"""
    global _audio_translator
    if _audio_translator is None:
        try:
            from audio_translator import AudioTranslator, check_audio_deps
            # 提前检查依赖（但不阻塞，模型在 start 时才加载）
            missing = check_audio_deps()
            if missing:
                messagebox.showwarning(
                    "依赖缺失",
                    "音频翻译需要以下依赖：\n\n" +
                    "\n".join(f"  • {m}" for m in missing)
                )
                return None
            _audio_translator = AudioTranslator(engine=TranslateEngine)
        except ImportError as e:
            messagebox.showwarning("模块缺失", f"音频翻译模块加载失败:\n{e}\n\n请确保 audio_translator.py 在同一目录")
            return None
    return _audio_translator

def _get_screen_translator(app):
    """懒加载屏幕翻译模块"""
    global _screen_translator
    if _screen_translator is None:
        try:
            # 先检查 pytesseract 能否导入（提前给出明确错误）
            import importlib
            for dep, pkg_name in [("pytesseract", "pytesseract"), ("cv2", "opencv-python"),
                                   ("PIL", "Pillow"), ("mss", "mss")]:
                try:
                    importlib.import_module(dep)
                except ImportError:
                    messagebox.showwarning(
                        "依赖缺失",
                        f"屏幕翻译需要安装 {pkg_name}\n\n请运行: pip install {pkg_name}"
                    )
                    return None

            from screen_translator import ScreenTranslator
            _screen_translator = ScreenTranslator(
                translate_engine=TranslateEngine,
                on_result_callback=app._on_screen_translate_result,
                on_cancel_callback=app._restore_after_screen_capture
            )

            # 检查 Tesseract 系统程序
            from screen_translator import check_all_deps
            missing = check_all_deps()
            if missing:
                messagebox.showwarning(
                    "依赖缺失",
                    "缺少以下组件，请安装后重试：\n\n" +
                    "\n".join(f"  • {m}" for m in missing)
                )
                _screen_translator = None
                return None

        except ImportError as e:
            messagebox.showwarning("模块缺失", f"屏幕翻译模块加载失败:\n{e}\n\n请确保 screen_translator.py 在同一目录")
            return None
    return _screen_translator


# =========================== 翻译引擎（导入新模块） ===========================
from translate_engines import (
    TranslateEngineManager, get_engine_manager, translate, detect_lang,
    get_available_engines, reload_config, load_config, save_config
)

# 兼容旧代码的别名（全局引擎管理器实例）
TranslateEngine = get_engine_manager()
# =========================== 主应用窗口 ===========================
class TranslatorApp:
    """翻译助手主应用"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry(f"{MAIN_WIN_WIDTH}x{MAIN_WIN_HEIGHT}")
        self.root.minsize(560, 480)
        self.root.configure(bg=Theme.BG)

        # 设置图标（如果有的话）
        try:
            icon_path = os.path.join(os.path.dirname(__file__), "icon.ico")
            if os.path.exists(icon_path):
                self.root.iconbitmap(icon_path)
        except Exception:
            pass

        # 窗口居中
        self._center_window()

        # 状态变量
        self.last_clipboard_text = ""     # 上次剪贴板内容
        self.current_translation = ""     # 当前翻译结果
        self.monitoring = True            # 剪贴板监控开关
        self.selected_translate_enabled = True  # 划词翻译开关
        self._closing = False             # 窗口关闭防重入标志
        self._monitor_event = threading.Event()  # 监控暂停/恢复（set=监控中）
        self._monitor_event.set()
        self.clipboard_thread = None      # 常驻监控线程
        self._translate_seq = 0           # 翻译任务序号（过期结果丢弃，防乱序覆盖）

        # 线程间通信
        self.task_queue = queue.Queue()

        # 创建浮动弹窗
        self.float_popup = FloatPopup(self.root, on_close_callback=self._on_popup_closed)

        # 构建界面
        self._build_ui()
        self._bind_events()

        # 初始化剪贴板
        try:
            import pyperclip
            self.last_clipboard_text = pyperclip.paste() or ""
        except Exception:
            self.last_clipboard_text = ""

        # 启动剪贴板监控
        self._start_clipboard_monitor()

        # 定期处理任务队列
        self._process_queue()

        # 窗口关闭处理
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 启动时诊断屏幕翻译依赖（后台线程执行，不阻塞窗口显示）
        threading.Thread(target=self._diagnose_screen_translate, daemon=True).start()

    # ==================== UI 构建 ====================

    def _center_window(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = MAIN_WIN_WIDTH
        h = MAIN_WIN_HEIGHT
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        """构建主界面"""
        # ---- 顶部导航栏 ----
        nav = tk.Frame(self.root, bg=Theme.CARD_BG, height=48,
                      highlightbackground=Theme.BORDER, highlightthickness=1)
        nav.pack(fill=tk.X)
        nav.pack_propagate(False)

        title_lbl = tk.Label(nav, text="🌐 中英翻译助手",
                            font=("Microsoft YaHei", 13, "bold"),
                            bg=Theme.CARD_BG, fg=Theme.PRIMARY)
        title_lbl.pack(side=tk.LEFT, padx=16, pady=10)

        # 监控状态指示
        self.monitor_indicator = tk.Label(nav, text="🟢 剪贴板监控中",
                                         font=("Microsoft YaHei", 9),
                                         bg=Theme.CARD_BG, fg=Theme.SUCCESS)
        self.monitor_indicator.pack(side=tk.RIGHT, padx=12, pady=12)

        # 置顶按钮
        self.topmost_btn = tk.Label(nav, text="📌 置顶", font=("Microsoft YaHei", 9),
                                   bg=Theme.CARD_BG, fg=Theme.TEXT_SEC,
                                   cursor="hand2", padx=6, pady=2)
        self.topmost_btn.pack(side=tk.RIGHT, padx=4, pady=10)
        self.topmost_btn.bind("<Button-1>", lambda e: self._toggle_topmost())

        # 主题切换按钮
        self.theme_btn = tk.Label(nav, text="🌙 暗色", font=("Microsoft YaHei", 9),
                                  bg=Theme.CARD_BG, fg=Theme.TEXT_SEC,
                                  cursor="hand2", padx=6, pady=2)
        self.theme_btn.pack(side=tk.RIGHT, padx=4, pady=10)
        self.theme_btn.bind("<Button-1>", lambda e: self._toggle_theme())

        # ---- 语言选择栏 ----
        lang_bar = tk.Frame(self.root, bg=Theme.BG, height=50)
        lang_bar.pack(fill=tk.X, padx=16, pady=(12, 0))
        lang_bar.pack_propagate(False)

        lang_frame = tk.Frame(lang_bar, bg=Theme.CARD_BG,
                             highlightbackground=Theme.BORDER, highlightthickness=1)
        lang_frame.pack(side=tk.LEFT, padx=(0, 10))

        tk.Label(lang_frame, text="源语言:", font=("Microsoft YaHei", 10),
                bg=Theme.CARD_BG, fg=Theme.TEXT_SEC).pack(side=tk.LEFT, padx=(10, 4), pady=8)

        self.src_lang_var = tk.StringVar(value="auto")
        src_lang_cb = ttk.Combobox(lang_frame, textvariable=self.src_lang_var,
                                   values=["auto", "zh", "en"], state="readonly",
                                   width=14, font=("Microsoft YaHei", 10))
        src_lang_cb.pack(side=tk.LEFT, padx=(0, 4), pady=8)

        # 语言名称映射（源/目标各一套：auto 的语义不同）
        self.SRC_LANG_LABELS = {"auto": "🔍 自动检测", "zh": "🇨🇳 中文", "en": "🇺🇸 英文"}
        self.TGT_LANG_LABELS = {"auto": "🔍 自动选择", "zh": "🇨🇳 中文", "en": "🇺🇸 英文"}
        src_lang_cb["values"] = ["🔍 自动检测", "🇨🇳 中文", "🇺🇸 英文"]

        # 交换按钮
        swap_btn = tk.Label(lang_bar, text="⇄", font=("Segoe UI", 16, "bold"),
                           bg=Theme.CARD_BG, fg=Theme.PRIMARY, cursor="hand2",
                           width=2, highlightbackground=Theme.BORDER, highlightthickness=1)
        swap_btn.pack(side=tk.LEFT, padx=2)
        swap_btn.bind("<Button-1>", lambda e: self._swap_languages())
        swap_btn.bind("<Enter>", lambda e: swap_btn.configure(bg=Theme.get("PRIMARY_BG")))
        swap_btn.bind("<Leave>", lambda e: swap_btn.configure(bg=Theme.get("CARD_BG")))

        tgt_frame = tk.Frame(lang_bar, bg=Theme.CARD_BG,
                            highlightbackground=Theme.BORDER, highlightthickness=1)
        tgt_frame.pack(side=tk.LEFT, padx=(10, 0))

        tk.Label(tgt_frame, text="目标语言:", font=("Microsoft YaHei", 10),
                bg=Theme.CARD_BG, fg=Theme.TEXT_SEC).pack(side=tk.LEFT, padx=(10, 4), pady=8)

        self.tgt_lang_var = tk.StringVar(value="auto")
        tgt_lang_cb = ttk.Combobox(tgt_frame, textvariable=self.tgt_lang_var,
                                   values=["🔍 自动选择", "🇨🇳 中文", "🇺🇸 英文"], state="readonly",
                                   width=14, font=("Microsoft YaHei", 10))
        tgt_lang_cb.pack(side=tk.LEFT, padx=(0, 4), pady=8)

        # 翻译引擎选择器
        engine_frame = tk.Frame(lang_bar, bg=Theme.CARD_BG,
                               highlightbackground=Theme.BORDER, highlightthickness=1)
        engine_frame.pack(side=tk.LEFT, padx=(10, 0))

        tk.Label(engine_frame, text="引擎:", font=("Microsoft YaHei", 10),
                bg=Theme.CARD_BG, fg=Theme.TEXT_SEC).pack(side=tk.LEFT, padx=(8, 2), pady=8)

        self.engine_var = tk.StringVar(value="auto")
        self.engine_cb = ttk.Combobox(engine_frame, textvariable=self.engine_var,
                                      values=[], state="readonly",
                                      width=16, font=("Microsoft YaHei", 10))
        self.engine_cb.pack(side=tk.LEFT, padx=(0, 6), pady=8)
        self.engine_cb.bind("<<ComboboxSelected>>", lambda e: self._on_engine_changed())
        self._refresh_engine_list()

        # 设置按钮
        settings_btn = tk.Label(lang_bar, text=" ⚙ ",
                               font=("Microsoft YaHei", 13),
                               bg=Theme.CARD_BG, fg=Theme.TEXT_SEC,
                               cursor="hand2", padx=4, pady=6,
                               highlightbackground=Theme.BORDER, highlightthickness=1)
        settings_btn.pack(side=tk.LEFT, padx=(6, 0))
        settings_btn.bind("<Button-1>", lambda e: self._open_settings())
        settings_btn.bind("<Enter>", lambda e: settings_btn.configure(bg=Theme.get("PRIMARY_BG"), fg=Theme.get("PRIMARY")))
        settings_btn.bind("<Leave>", lambda e: settings_btn.configure(bg=Theme.get("CARD_BG"), fg=Theme.get("TEXT_SEC")))

        # 音频翻译按钮
        self.audio_translate_btn = tk.Label(lang_bar, text="  🎙 音频翻译  ",
                                           font=("Microsoft YaHei", 11, "bold"),
                                           bg="#ea4335", fg="#ffffff",
                                           cursor="hand2", padx=14, pady=8)
        self.audio_translate_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self.audio_translate_btn.bind("<Button-1>", lambda e: self._do_audio_translate())
        self.audio_translate_btn.bind("<Enter>", lambda e: self.audio_translate_btn.configure(bg="#c5221f"))
        self.audio_translate_btn.bind("<Leave>", lambda e: self.audio_translate_btn.configure(bg="#ea4335"))

        # 屏幕翻译按钮
        self.screen_translate_btn = tk.Label(lang_bar, text="  🖥 屏幕翻译  ",
                                            font=("Microsoft YaHei", 11, "bold"),
                                            bg=Theme.SUCCESS, fg="#ffffff",
                                            cursor="hand2", padx=14, pady=8)
        self.screen_translate_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self.screen_translate_btn.bind("<Button-1>", lambda e: self._do_screen_translate())
        self.screen_translate_btn.bind("<Enter>", lambda e: self.screen_translate_btn.configure(bg="#2d9249"))
        self.screen_translate_btn.bind("<Leave>", lambda e: self.screen_translate_btn.configure(bg=Theme.SUCCESS))

        # 整体翻译按钮
        self.translate_btn = tk.Label(lang_bar, text="  📝 整体翻译  ",
                                     font=("Microsoft YaHei", 11, "bold"),
                                     bg=Theme.PRIMARY, fg="#ffffff",
                                     cursor="hand2", padx=18, pady=8)
        self.translate_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self.translate_btn.bind("<Button-1>", lambda e: self._do_full_translate())
        self.translate_btn.bind("<Enter>", lambda e: self.translate_btn.configure(bg="#1557b0"))
        self.translate_btn.bind("<Leave>", lambda e: self.translate_btn.configure(bg=Theme.PRIMARY))

        # ---- 主面板（输入 + 输出，grid 布局强制等分） ----
        main_panel = tk.Frame(self.root, bg=Theme.BG)
        main_panel.pack(fill=tk.BOTH, expand=True, padx=16, pady=10)
        main_panel.grid_rowconfigure(0, weight=1, uniform="panel")
        main_panel.grid_rowconfigure(1, weight=1, uniform="panel")
        main_panel.grid_columnconfigure(0, weight=1)

        # 输入区域
        input_frame = tk.Frame(main_panel, bg=Theme.CARD_BG,
                              highlightbackground=Theme.BORDER, highlightthickness=1)
        input_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 3))

        input_header = tk.Frame(input_frame, bg=Theme.CARD_BG, height=30)
        input_header.pack(fill=tk.X, padx=12, pady=(8, 0))
        input_header.pack_propagate(False)

        tk.Label(input_header, text="📥 原文输入",
                font=("Microsoft YaHei", 10, "bold"),
                bg=Theme.CARD_BG, fg=Theme.TEXT_SEC).pack(side=tk.LEFT)

        self.input_char_count = tk.Label(input_header, text="0 字符",
                                        font=("Microsoft YaHei", 9),
                                        bg=Theme.CARD_BG, fg=Theme.TEXT_HINT)
        self.input_char_count.pack(side=tk.RIGHT)

        # 输入文本框 + 滚动条
        input_text_frame = tk.Frame(input_frame, bg=Theme.CARD_BG)
        input_text_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        self.input_text = tk.Text(input_text_frame, font=("Microsoft YaHei", 12),
                                 bg=Theme.CARD_BG, fg=Theme.TEXT,
                                 bd=0, wrap=tk.WORD, relief=tk.FLAT,
                                 insertbackground=Theme.PRIMARY,
                                 selectbackground=Theme.PRIMARY_BG,
                                 selectforeground=Theme.TEXT,
                                 padx=12, pady=8, undo=True, maxundo=50)
        input_scroll = tk.Scrollbar(input_text_frame, command=self.input_text.yview,
                                    width=8, bg=Theme.BG, troughcolor=Theme.CARD_BG,
                                    activebackground=Theme.TEXT_HINT)
        self.input_text.configure(yscrollcommand=input_scroll.set)
        self.input_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        input_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 输入框占位符
        self._add_placeholder(self.input_text, "在此输入要翻译的文字...\n\n💡 提示：\n  • 鼠标选中文字 → 自动区域翻译\n  • Ctrl+Enter → 整体翻译\n  • Ctrl+Shift+S → 屏幕区域翻译\n  • Ctrl+Shift+A → 音频翻译\n  • 在其他软件 Ctrl+C → 自动弹出翻译")

        # 输入框底部按钮
        input_actions = tk.Frame(input_frame, bg=Theme.CARD_BG, height=34)
        input_actions.pack(fill=tk.X, padx=10, pady=(0, 6))
        input_actions.pack_propagate(False)

        self._make_small_btn(input_actions, "清空", self._clear_input).pack(side=tk.LEFT, padx=(0, 6))
        self._make_small_btn(input_actions, "📋 粘贴", self._paste_to_input).pack(side=tk.LEFT, padx=(0, 6))
        self._make_small_btn(input_actions, "🔊 朗读", self._speak_input).pack(side=tk.LEFT)

        # 输出区域
        output_frame = tk.Frame(main_panel, bg=Theme.CARD_BG,
                               highlightbackground=Theme.BORDER, highlightthickness=1)
        output_frame.grid(row=1, column=0, sticky="nsew", pady=(3, 0))

        output_header = tk.Frame(output_frame, bg=Theme.CARD_BG, height=30)
        output_header.pack(fill=tk.X, padx=12, pady=(8, 0))
        output_header.pack_propagate(False)

        tk.Label(output_header, text="📤 翻译结果",
                font=("Microsoft YaHei", 10, "bold"),
                bg=Theme.CARD_BG, fg=Theme.TEXT_SEC).pack(side=tk.LEFT)

        self.output_char_count = tk.Label(output_header, text="0 字符",
                                         font=("Microsoft YaHei", 9),
                                         bg=Theme.CARD_BG, fg=Theme.TEXT_HINT)
        self.output_char_count.pack(side=tk.RIGHT)

        # 输出文本框 + 滚动条
        output_text_frame = tk.Frame(output_frame, bg=Theme.CARD_BG)
        output_text_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        self.output_text = tk.Text(output_text_frame, font=("Microsoft YaHei", 12),
                                  bg=Theme.CARD_BG, fg=Theme.TEXT,
                                  bd=0, wrap=tk.WORD, relief=tk.FLAT,
                                  state=tk.DISABLED,
                                  selectbackground=Theme.PRIMARY_BG,
                                  selectforeground=Theme.TEXT,
                                  padx=12, pady=8)
        output_scroll = tk.Scrollbar(output_text_frame, command=self.output_text.yview,
                                     width=8, bg=Theme.BG, troughcolor=Theme.CARD_BG,
                                     activebackground=Theme.TEXT_HINT)
        self.output_text.configure(yscrollcommand=output_scroll.set)
        self.output_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        output_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 输出框底部按钮
        output_actions = tk.Frame(output_frame, bg=Theme.CARD_BG, height=34)
        output_actions.pack(fill=tk.X, padx=10, pady=(0, 6))
        output_actions.pack_propagate(False)

        self._make_small_btn(output_actions, "📋 复制", self._copy_output).pack(side=tk.LEFT, padx=(0, 6))
        self._make_small_btn(output_actions, "🔊 朗读", self._speak_output).pack(side=tk.LEFT, padx=(0, 6))
        self._make_small_btn(output_actions, "🔄 反向翻译", self._reverse_translate).pack(side=tk.LEFT)

        # ---- 底部状态栏 ----
        status_bar = tk.Frame(self.root, bg=Theme.BG, height=30)
        status_bar.pack(fill=tk.X, padx=16, pady=(0, 8))
        status_bar.pack_propagate(False)

        self.status_label = tk.Label(status_bar, text="✅ 就绪 | Ctrl+C 跨软件 | Ctrl+Shift+S 屏幕 | Ctrl+Shift+A 音频 | ⚙ 设置",
                                    font=("Microsoft YaHei", 8),
                                    bg=Theme.BG, fg=Theme.TEXT_HINT)
        self.status_label.pack(side=tk.LEFT)

        # 剪贴板监控开关
        self.monitor_toggle_btn = tk.Label(status_bar, text="⏸ 暂停监控",
                                          font=("Microsoft YaHei", 8),
                                          bg=Theme.BG, fg=Theme.PRIMARY,
                                          cursor="hand2")
        self.monitor_toggle_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self.monitor_toggle_btn.bind("<Button-1>", lambda e: self._toggle_monitoring())

        # 划词翻译开关
        self.selected_toggle_btn = tk.Label(status_bar, text="📝 划词翻译: 开",
                                           font=("Microsoft YaHei", 8),
                                           bg=Theme.BG, fg=Theme.PRIMARY,
                                           cursor="hand2")
        self.selected_toggle_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self.selected_toggle_btn.bind("<Button-1>", lambda e: self._toggle_selected_translate())

    def _make_small_btn(self, parent, text, command):
        """创建小型操作按钮"""
        btn = tk.Label(parent, text=text, font=("Microsoft YaHei", 9),
                      bg=Theme.BG, fg=Theme.TEXT_SEC, cursor="hand2",
                      padx=10, pady=3)
        btn.bind("<Button-1>", lambda e: command())
        btn.bind("<Enter>", lambda e: btn.configure(bg=Theme.get("PRIMARY_BG"), fg=Theme.get("PRIMARY")))
        btn.bind("<Leave>", lambda e: btn.configure(bg=Theme.get("BG"), fg=Theme.get("TEXT_SEC")))
        return btn

    def _add_placeholder(self, text_widget, placeholder_text):
        """为输入框添加占位符文字"""
        text_widget.insert("1.0", placeholder_text)
        text_widget.configure(fg=Theme.TEXT_HINT)
        self._placeholder_active = True
        self._placeholder_text = placeholder_text

        def on_focus_in(event):
            if self._placeholder_active:
                text_widget.delete("1.0", tk.END)
                text_widget.configure(fg=Theme.TEXT)
                self._placeholder_active = False

        def on_focus_out(event):
            if not text_widget.get("1.0", tk.END).strip():
                text_widget.insert("1.0", placeholder_text)
                text_widget.configure(fg=Theme.TEXT_HINT)
                self._placeholder_active = True

        text_widget.bind("<FocusIn>", on_focus_in)
        text_widget.bind("<FocusOut>", on_focus_out)

        # 提示文本需要更新以包含屏幕翻译
        self._update_placeholder_hint = lambda: None

    # ==================== 事件绑定 ====================

    def _bind_events(self):
        """绑定事件"""
        # 输入框文字变化
        self.input_text.bind("<<Modified>>", self._on_input_modified)
        # 输入框选中文字（鼠标松开时检测）
        self.input_text.bind("<ButtonRelease-1>", self._on_input_selection)
        # Ctrl+Enter 整体翻译
        self.input_text.bind("<Control-Return>", lambda e: self._do_full_translate())
        self.input_text.bind("<Control-Key-Return>", lambda e: self._do_full_translate())
        # Ctrl+Shift+S 屏幕翻译（全局快捷键）
        self.root.bind("<Control-Shift-S>", lambda e: self._do_screen_translate())
        self.root.bind("<Control-Shift-Key-S>", lambda e: self._do_screen_translate())
        # Ctrl+Shift+A 音频翻译（全局快捷键）
        self.root.bind("<Control-Shift-A>", lambda e: self._do_audio_translate())
        self.root.bind("<Control-Shift-Key-A>", lambda e: self._do_audio_translate())

    # ==================== 剪贴板监控 ====================

    def _start_clipboard_monitor(self):
        """启动剪贴板监控线程（常驻单线程，Event 控制暂停/恢复，避免叠加线程）"""
        if self.clipboard_thread and self.clipboard_thread.is_alive():
            return
        self.clipboard_thread = threading.Thread(target=self._clipboard_loop, daemon=True)
        self.clipboard_thread.start()

    def _clipboard_loop(self):
        """剪贴板监控循环（后台线程）"""
        import pyperclip
        while not self._closing:
            # Event 暂停/恢复：暂停时 wait 超时后继续循环检查
            self._monitor_event.wait(timeout=CHECK_CLIPBOARD_INTERVAL)
            if self._closing:
                break
            if not self._monitor_event.is_set():
                continue
            try:
                current = pyperclip.paste() or ""
                if current and current != self.last_clipboard_text and len(current.strip()) >= 2:
                    self.last_clipboard_text = current
                    # 放入任务队列，由主线程处理
                    self.task_queue.put(("clipboard", current.strip()))
            except Exception as e:
                logger.error(f"剪贴板监控错误: {e}")

    def _safe_after(self, fn):
        """主线程回调投递：窗口关闭后静默丢弃，避免 TclError"""
        if self._closing:
            return
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    def _new_translate_seq(self):
        """分配新的翻译任务序号（使旧任务的结果过期）"""
        self._translate_seq += 1
        return self._translate_seq

    def _is_current_seq(self, seq):
        """判断任务序号是否仍是最新（主线程调用）"""
        return seq == self._translate_seq

    def _process_queue(self):
        """处理任务队列（主线程定时调用）"""
        try:
            while True:
                task_type, data = self.task_queue.get_nowait()
                if task_type == "clipboard":
                    self._handle_clipboard_change(data)
                elif task_type == "selection":
                    self._handle_selection_translate(data)
        except queue.Empty:
            pass
        finally:
            self.root.after(TASK_QUEUE_POLL_MS, self._process_queue)

    def _handle_clipboard_change(self, text):
        """处理剪贴板变化 — 跨软件划词翻译（支持任意长度文本，自动分块翻译）"""
        if not text or len(text) < 2:
            return
        # 不再限制长度，由翻译引擎自动分块处理
        # 显示时截断预览（浮窗空间有限，但完整翻译）
        preview = text[:CLIPBOARD_PREVIEW_CHARS] + ("..." if len(text) > CLIPBOARD_PREVIEW_CHARS else "")

        # 解析语言对（目标 auto 时自动互译）
        from_lang, to_lang = self._resolve_lang_pair(text)

        # 显示加载状态（用预览文本）
        self.float_popup.show_loading(preview)

        # 分配任务序号，旧任务结果将过期丢弃（防乱序覆盖）
        seq = self._new_translate_seq()

        # 在后台线程执行翻译（传完整文本）
        threading.Thread(target=self._do_clipboard_translate,
                        args=(text, from_lang, to_lang, seq), daemon=True).start()

    def _do_clipboard_translate(self, text, from_lang, to_lang, seq):
        """后台执行剪贴板翻译（浮窗 + 主窗口同步显示）"""
        try:
            result = TranslateEngine.translate(text, from_lang, to_lang)
            detected = TranslateEngine.detect_lang(text) if from_lang == "auto" else from_lang
            # 回到主线程：仅当任务仍是最新时才显示
            self._safe_after(lambda: self.float_popup.show_translation(
                text, result, detected, to_lang
            ) if self._is_current_seq(seq) else None)
            self._safe_after(lambda: self._sync_clipboard_to_main(text, result, detected, to_lang)
                            if self._is_current_seq(seq) else None)
        except Exception as e:
            logger.warning(f"剪贴板翻译失败: {e}")
            self._safe_after(lambda: self.float_popup.show_error(text, str(e))
                            if self._is_current_seq(seq) else None)

    def _fill_input_text(self, text):
        """清空占位符并将文本填入输入框（剪贴板/屏幕翻译/反向翻译共用）"""
        if self._placeholder_active:
            self.input_text.configure(fg=Theme.TEXT)
            self._placeholder_active = False
        self.input_text.delete("1.0", tk.END)
        self.input_text.insert("1.0", text)
        self._on_input_modified()

    def _sync_clipboard_to_main(self, source, result, from_lang, to_lang):
        """将剪贴板翻译结果同步到主窗口：输入框填充原文，输出区显示译文"""
        self.current_translation = result

        # ★ 将原文填入输入框
        self._fill_input_text(source)

        # ★ 译文显示在输出区
        self._set_output_text(result)
        self.status_label.configure(text="✅ 剪贴板翻译完成 | 原文已填入输入框，译文已显示")

    # ==================== 输入框选中翻译 ====================

    def _on_input_selection(self, event):
        """输入框中鼠标选中文字后触发"""
        if not self.selected_translate_enabled:
            return
        # 延迟检查（等选择完成）
        self.root.after(SELECTION_CHECK_DELAY_MS, self._check_input_selection)

    def _check_input_selection(self):
        """检查输入框中是否有选中文字（支持任意长度选择）"""

        try:
            if self.input_text.tag_ranges("sel"):
                selected = self.input_text.get("sel.first", "sel.last")
                if selected and len(selected.strip()) >= 2:
                    # 不再限制最大长度，由翻译引擎自动分块处理
                    # 确认不是占位符文字
                    if not self._placeholder_active or selected.strip() != self._placeholder_text.split('\n')[0]:
                        self.task_queue.put(("selection", selected.strip()))
        except tk.TclError:
            pass

    def _handle_selection_translate(self, text):
        """处理输入框选中翻译 — 在主窗口输出区显示"""
        if not text or len(text) < 2:
            return

        # 解析语言对（目标 auto 时自动互译）
        from_lang, to_lang = self._resolve_lang_pair(text)

        # 在输出区显示加载状态
        self._set_output_text("⏳ 正在翻译选中文字...")
        self.status_label.configure(text="🔄 翻译选中文字中...")

        # 分配任务序号（过期结果丢弃）
        seq = self._new_translate_seq()

        threading.Thread(target=self._do_selection_translate,
                        args=(text, from_lang, to_lang, seq), daemon=True).start()

    def _do_selection_translate(self, text, from_lang, to_lang, seq):
        """后台执行选中文字翻译"""
        try:
            result = TranslateEngine.translate(text, from_lang, to_lang)
            display = f"📌 选中文字翻译\n{'─' * 40}\n原文: {text}\n\n译文: {result}"
            self._safe_after(lambda: self._set_output_text(display)
                            if self._is_current_seq(seq) else None)
            self._safe_after(lambda: self.status_label.configure(text="✅ 选中文字翻译完成")
                            if self._is_current_seq(seq) else None)
        except Exception as e:
            logger.warning(f"选中文字翻译失败: {e}")
            self._safe_after(lambda: self._set_output_text(f"❌ 翻译失败: {e}")
                            if self._is_current_seq(seq) else None)
            self._safe_after(lambda: self.status_label.configure(text="❌ 翻译失败")
                            if self._is_current_seq(seq) else None)

    # ==================== 整体翻译 ====================

    def _do_full_translate(self):
        """整体翻译按钮回调"""
        text = self._get_input_text()
        if not text.strip():
            self.status_label.configure(text="⚠️ 请先输入要翻译的文字")
            return

        # 解析语言对（目标 auto 时自动互译）
        from_lang, to_lang = self._resolve_lang_pair(text)

        self._set_output_text("⏳ 正在翻译，请稍候...")
        self.status_label.configure(text="🔄 整体翻译中...")
        self.translate_btn.configure(text="  ⏳ 翻译中...  ", bg=Theme.TEXT_HINT)

        # 分配任务序号（过期结果丢弃）
        seq = self._new_translate_seq()

        threading.Thread(target=self._do_full_translate_thread,
                        args=(text, from_lang, to_lang, seq), daemon=True).start()

    def _do_full_translate_thread(self, text, from_lang, to_lang, seq):
        """后台执行整体翻译"""
        try:
            result = TranslateEngine.translate(text, from_lang, to_lang)
            self._safe_after(lambda: self._display_full_result(text, result, from_lang, to_lang)
                            if self._is_current_seq(seq) else None)
            self._safe_after(lambda: self.status_label.configure(text="✅ 翻译完成")
                            if self._is_current_seq(seq) else None)
        except Exception as e:
            logger.warning(f"整体翻译失败: {e}")
            self._safe_after(lambda: self._set_output_text(f"❌ 翻译失败: {e}")
                            if self._is_current_seq(seq) else None)
            self._safe_after(lambda: self.status_label.configure(text="❌ 翻译失败")
                            if self._is_current_seq(seq) else None)
        finally:
            self._safe_after(lambda: self.translate_btn.configure(
                text="  📝 整体翻译  ", bg=Theme.PRIMARY)
                if self._is_current_seq(seq) else None)

    def _display_full_result(self, source, result, from_lang, to_lang):
        """显示整体翻译结果"""
        display = ""
        # 尝试逐句对照
        src_sentences = self._split_sentences(source)
        tgt_sentences = self._split_sentences(result)

        if len(src_sentences) > 1 and len(src_sentences) == len(tgt_sentences):
            for i, (s, t) in enumerate(zip(src_sentences, tgt_sentences)):
                display += f"【{i+1}】{s}\n     → {t}\n\n"
        else:
            display = result

        self._set_output_text(display.strip())
        self.current_translation = result

    def _split_sentences(self, text):
        """分割句子"""
        parts = re.split(r'(?<=[.!?。！？])\s*', text)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) <= 1:
            parts = [l.strip() for l in text.split('\n') if l.strip()]
        return parts if parts else [text]

    # ==================== 辅助方法 ====================

    def _get_input_text(self):
        """获取输入框实际文本"""
        if self._placeholder_active:
            return ""
        return self.input_text.get("1.0", tk.END).strip()

    def _set_output_text(self, text):
        """设置输出框文本"""
        self.output_text.configure(state=tk.NORMAL)
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", text)
        self.output_text.configure(state=tk.DISABLED)
        # 更新字符数
        self.output_char_count.configure(text=f"{len(text)} 字符")

    def _get_src_lang_code(self):
        """获取源语言代码"""
        val = self.src_lang_var.get()
        mapping = {"🔍 自动检测": "auto", "🇨🇳 中文": "zh", "🇺🇸 英文": "en"}
        return mapping.get(val, "auto")

    def _get_tgt_lang_code(self):
        """获取目标语言代码"""
        val = self.tgt_lang_var.get()
        mapping = {"🔍 自动选择": "auto", "🇨🇳 中文": "zh", "🇺🇸 英文": "en"}
        return mapping.get(val, "auto")

    def _resolve_lang_pair(self, text):
        """解析源/目标语言对。

        目标为 auto 时按源语言自动互译（仅中英：中→英、英/其他→中）；
        源为 auto 且目标为 auto 时先检测源语言再定方向。
        """
        from_lang = self._get_src_lang_code()
        to_lang = self._get_tgt_lang_code()
        if to_lang == "auto":
            if from_lang == "auto":
                from_lang = TranslateEngine.detect_lang(text)
            to_lang = "en" if from_lang == "zh" else "zh"
        return from_lang, to_lang

    def _on_input_modified(self, event=None):
        """输入框内容变化"""
        if self._placeholder_active:
            self.input_char_count.configure(text="0 字符")
        else:
            text = self.input_text.get("1.0", tk.END).strip()
            self.input_char_count.configure(text=f"{len(text)} 字符")
        self.input_text.edit_modified(False)

    def _clear_input(self):
        """清空输入"""
        self.input_text.delete("1.0", tk.END)
        if not self._placeholder_active:
            self.input_text.insert("1.0", self._placeholder_text)
            self.input_text.configure(fg=Theme.TEXT_HINT)
            self._placeholder_active = True
        self.input_char_count.configure(text="0 字符")

    def _paste_to_input(self):
        """粘贴到输入框"""
        try:
            text = self.root.clipboard_get()
            if text:
                if self._placeholder_active:
                    self.input_text.delete("1.0", tk.END)
                    self.input_text.configure(fg=Theme.TEXT)
                    self._placeholder_active = False
                self.input_text.insert(tk.INSERT, text)
                self._on_input_modified()
        except tk.TclError:
            pass

    def _copy_output(self):
        """复制翻译结果"""
        text = self.output_text.get("1.0", tk.END).strip()
        if text and not text.startswith("⏳") and not text.startswith("❌"):
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status_label.configure(text="📋 翻译结果已复制到剪贴板")
            self.root.after(2000, lambda: self.status_label.configure(
                text="✅ 就绪 | Ctrl+C 跨软件 | Ctrl+Shift+S 屏幕 | Ctrl+Shift+A 音频"))

    def _speak_input(self):
        """朗读输入文字"""
        text = self._get_input_text()
        if not text:
            return
        lang = self._get_src_lang_code()
        if lang == "auto":
            lang = TranslateEngine.detect_lang(text)
        self._speak(text, lang)

    def _speak_output(self):
        """朗读翻译结果"""
        text = self.output_text.get("1.0", tk.END).strip()
        if not text or text.startswith("⏳") or text.startswith("❌"):
            return
        tgt = self._get_tgt_lang_code()
        if tgt == "auto":
            # 目标自动：按译文内容检测语言选声音
            tgt = TranslateEngine.detect_lang(text)
        self._speak(text, tgt)

    def _speak(self, text, lang):
        """TTS 语音朗读（后台线程执行，避免 SAPI 阻塞主线程 UI）"""

        def _run():
            try:
                # 使用 Windows 内置的 SAPI
                import win32com.client as win32
                speaker = win32.Dispatch("SAPI.SpVoice")
                voice_lang = "Chinese" if lang == "zh" else "English"
                for voice in speaker.GetVoices():
                    if voice_lang in voice.GetDescription():
                        speaker.Voice = voice
                        break
                speaker.Speak(text)
            except ImportError:
                # 如果没有 win32com，尝试使用 pyttsx3
                try:
                    import pyttsx3
                    engine = pyttsx3.init()
                    engine.say(text)
                    engine.runAndWait()
                except ImportError:
                    self._safe_after(lambda: self.status_label.configure(
                        text="⚠️ 语音朗读需要安装 pywin32 或 pyttsx3"))
            except Exception as e:
                logger.warning(f"TTS 朗读失败: {e}")

        threading.Thread(target=_run, daemon=True).start()

    # ==================== 屏幕翻译 ====================

    def _diagnose_screen_translate(self):
        """启动时诊断屏幕翻译依赖（输出到控制台）"""
        try:
            import importlib, os as _os
            issues = []
            for mod_name, pkg in [("pytesseract", "pytesseract"), ("cv2", "opencv-python"),
                                   ("PIL", "Pillow"), ("mss", "mss")]:
                try:
                    importlib.import_module(mod_name)
                except ImportError:
                    issues.append(f"  [MISS] {pkg} -> pip install {pkg}")

            if not issues:
                # 检查 Tesseract 系统程序
                try:
                    from screen_translator import _find_tesseract
                    tesseract_path = _find_tesseract()
                    if tesseract_path:
                        logger.info(f"屏幕翻译 Tesseract -> {tesseract_path}")
                    else:
                        issues.append("  [MISS] Tesseract-OCR system program")
                        issues.append("         Download: https://github.com/UB-Mannheim/tesseract/wiki")
                except Exception:
                    pass

            if issues:
                logger.warning("屏幕翻译依赖缺失:")
                for i in issues:
                    logger.warning(i)
            else:
                logger.info("屏幕翻译所有依赖就绪")

            # 检查音频翻译依赖
            try:
                from audio_translator import check_audio_deps
                audio_missing = check_audio_deps()
                if audio_missing:
                    logger.warning("音频翻译依赖缺失:")
                    for m in audio_missing:
                        logger.warning(f"  {m}")
                else:
                    logger.info("音频翻译所有依赖就绪")
            except Exception:
                pass

        except Exception:
            pass  # 静默失败，不影响主程序

    def _do_audio_translate(self):
        """触发音频翻译"""
        at = _get_audio_translator(self)
        if at is None:
            return
        self.status_label.configure(text="🎙 启动音频翻译...")
        at.start(self.root)

    def _do_screen_translate(self):
        """触发屏幕区域翻译"""
        st = _get_screen_translator(self)
        if st is None:
            return
        self.status_label.configure(text="🖥 请在屏幕上拖拽选择要翻译的区域...")
        # 隐藏主窗口和控制台，避免挡住屏幕
        self._hide_for_screen_capture()
        # 延迟启动，让窗口先隐藏
        self.root.after(150, lambda: st.start(self.root))

    def _hide_for_screen_capture(self):
        """隐藏主窗口和控制台窗口"""
        self._console_hwnd = None
        # 隐藏控制台窗口（Windows）
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                if hwnd:
                    self._console_hwnd = hwnd
                    ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
            except Exception:
                pass
        # 隐藏主窗口
        self.root.withdraw()

    def _restore_after_screen_capture(self):
        """恢复主窗口和控制台窗口"""
        # 恢复控制台
        if self._console_hwnd and sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(self._console_hwnd, 1)  # SW_SHOW
            except Exception:
                pass
            self._console_hwnd = None
        # 恢复主窗口
        self.root.deiconify()
        self.root.lift()

    def _on_screen_translate_result(self, source, result, from_lang, to_lang):
        """屏幕翻译完成回调 — 同步到主窗口和浮窗

        屏幕翻译是用户主动确认的最新操作：递增任务序号使所有在途的
        剪贴板/选中翻译结果过期，避免它们回写覆盖屏幕翻译结果。
        """
        # 先恢复窗口
        self._restore_after_screen_capture()
        self._translate_seq += 1  # 作废在途的剪贴板/选中翻译任务

        self.current_translation = result

        # 将原文填入输入框
        self._fill_input_text(source)

        # 译文显示在输出区
        self._set_output_text(result)
        self.status_label.configure(text="✅ 屏幕翻译完成")

        # 同步到浮动弹窗
        detected = from_lang if from_lang != "auto" else TranslateEngine.detect_lang(source)
        self.float_popup.show_translation(source, result, detected, to_lang)

    def _reverse_translate(self):
        """反向翻译：交换语言方向并翻译结果"""
        text = self.output_text.get("1.0", tk.END).strip()
        if not text or text.startswith("⏳") or text.startswith("❌"):
            return

        # 交换语言方向
        src_val = self._get_src_lang_code()
        tgt_val = self._get_tgt_lang_code()

        # 设置新的方向
        new_src = tgt_val
        new_tgt = src_val if src_val != "auto" else TranslateEngine.detect_lang(text)

        # 更新UI（源/目标各用各的标签：auto 在目标侧是"自动选择"）
        self.src_lang_var.set(self.SRC_LANG_LABELS.get(new_src, "🔍 自动检测"))
        self.tgt_lang_var.set(self.TGT_LANG_LABELS.get(new_tgt, "🔍 自动选择"))

        # 把翻译结果放入输入框
        self._fill_input_text(text)

        # 执行翻译
        self._do_full_translate()

    def _swap_languages(self):
        """交换源语言和目标语言（auto 原样换边）"""
        src_val = self._get_src_lang_code()
        tgt_val = self._get_tgt_lang_code()

        self.src_lang_var.set(self.SRC_LANG_LABELS.get(tgt_val, "🔍 自动检测"))
        self.tgt_lang_var.set(self.TGT_LANG_LABELS.get(src_val, "🔍 自动选择"))

    def _toggle_monitoring(self):
        """切换剪贴板监控（常驻线程 + Event 暂停/恢复，无线程叠加）"""
        self.monitoring = not self.monitoring
        if self.monitoring:
            self.monitor_indicator.configure(text="🟢 剪贴板监控中", fg=Theme.SUCCESS)
            self.monitor_toggle_btn.configure(text="⏸ 暂停监控")
            self._monitor_event.set()
            self.status_label.configure(text="🟢 剪贴板监控已开启")
        else:
            self.monitor_indicator.configure(text="🔴 监控已暂停", fg=Theme.ACCENT)
            self.monitor_toggle_btn.configure(text="▶ 开启监控")
            self._monitor_event.clear()
            self.status_label.configure(text="🔴 剪贴板监控已暂停")

    def _toggle_selected_translate(self):
        """切换划词翻译"""
        self.selected_translate_enabled = not self.selected_translate_enabled
        if self.selected_translate_enabled:
            self.selected_toggle_btn.configure(text="📝 划词翻译: 开", fg=Theme.PRIMARY)
        else:
            self.selected_toggle_btn.configure(text="📝 划词翻译: 关", fg=Theme.TEXT_HINT)

    def _toggle_topmost(self):
        """切换窗口置顶"""
        current = self.root.attributes("-topmost")
        self.root.attributes("-topmost", not current)
        if not current:
            self.topmost_btn.configure(text="📌 已置顶", fg=Theme.PRIMARY)
        else:
            self.topmost_btn.configure(text="📌 置顶", fg=Theme.TEXT_SEC)

    def _toggle_theme(self):
        """切换亮色/暗色主题"""
        is_dark = Theme.toggle()

        # 更新主题按钮文字
        self.theme_btn.configure(text="☀️ 亮色" if is_dark else "🌙 暗色")

        # 递归更新所有子控件的颜色
        self._apply_theme_to_widget(self.root)

        # 如果浮动弹窗已创建，也更新其主题
        if hasattr(self, 'float_popup') and self.float_popup and self.float_popup.winfo_exists():
            self._apply_theme_to_widget(self.float_popup)

        self.status_label.configure(text=f"{'🌙 暗色' if is_dark else '☀️ 亮色'}主题已切换")

    def _apply_theme_to_widget(self, widget, depth=0):
        """递归遍历 widget 树，根据当前 Theme 更新颜色"""
        if depth > 20:
            return  # 防止无限递归

        widget_class = widget.winfo_class()
        try:
            if widget_class in ("Frame", "Labelframe", "Tk", "Toplevel"):
                bg = widget.cget("bg")
                if bg in ("#f5f6f8", "#ffffff", "#e0e3e8",
                          "#1e1e2a", "#282836", "#3d3d50"):
                    widget.configure(bg=Theme.get("BG" if bg in ("#f5f6f8", "#1e1e2a") else
                                                  "CARD_BG" if bg in ("#ffffff", "#282836") else
                                                  "BORDER"))
                # 更新 highlightbackground
                try:
                    hb = widget.cget("highlightbackground")
                    if hb in ("#e0e3e8", "#3d3d50"):
                        widget.configure(highlightbackground=Theme.get("BORDER"))
                except Exception:
                    pass

            elif widget_class == "Label":
                fg = widget.cget("fg")
                bg = widget.cget("bg")
                # 更新背景
                if bg in ("#f5f6f8", "#ffffff", "#e0e3e8",
                          "#1e1e2a", "#282836", "#3d3d50"):
                    widget.configure(bg=Theme.get("BG" if bg in ("#f5f6f8", "#1e1e2a") else
                                                  "CARD_BG" if bg in ("#ffffff", "#282836") else
                                                  "BORDER"))
                # 更新前景色
                if fg in ("#202124", "#5f6368", "#9aa0a6", "#1a73e8",
                          "#e8e8f0", "#b0b0c0", "#707080", "#8ab4f8"):
                    color_map = {
                        "#202124": "TEXT", "#e8e8f0": "TEXT",
                        "#5f6368": "TEXT_SEC", "#b0b0c0": "TEXT_SEC",
                        "#9aa0a6": "TEXT_HINT", "#707080": "TEXT_HINT",
                        "#1a73e8": "PRIMARY", "#8ab4f8": "PRIMARY",
                    }
                    widget.configure(fg=Theme.get(color_map.get(fg, "TEXT")))

            elif widget_class == "Text":
                bg = widget.cget("bg")
                fg = widget.cget("fg")
                if bg in ("#ffffff", "#282836"):
                    widget.configure(bg=Theme.get("CARD_BG"))
                if fg in ("#202124", "#e8e8f0", "#9aa0a6"):
                    widget.configure(fg=Theme.get("TEXT" if fg in ("#202124", "#e8e8f0") else "TEXT_HINT"))
                widget.configure(selectbackground=Theme.get("PRIMARY_BG"),
                                 insertbackground=Theme.get("PRIMARY"))

            elif widget_class == "Scrollbar":
                bg = widget.cget("bg")
                trough = widget.cget("troughcolor")
                if bg in ("#f5f6f8", "#1e1e2a"):
                    widget.configure(bg=Theme.get("BG"))
                if trough in ("#ffffff", "#282836"):
                    widget.configure(troughcolor=Theme.get("CARD_BG"))

        except Exception:
            pass  # 某些 widget 不支持所有选项

        # 递归处理子控件
        try:
            for child in widget.winfo_children():
                self._apply_theme_to_widget(child, depth + 1)
        except Exception:
            pass

    # ==================== 引擎选择与设置 ====================

    def _refresh_engine_list(self):
        """刷新引擎下拉列表"""
        engines = get_available_engines()
        display_list = []
        for eng in engines:
            name = eng["display_name"]
            if eng["requires_key"]:
                name += " ✓" if eng["available"] else " (需密钥)"
            display_list.append(name)
        # 自动模式放第一个
        display_list.insert(0, "🔀 自动选择")
        self.engine_cb["values"] = display_list
        # 恢复当前选择
        config = load_config()
        selected = config.get("selected_engine", "auto")
        self._sync_engine_display(selected)
        
        # 更新输入框提示
        current_engine = config.get("selected_engine", "auto")
        if current_engine == "auto":
            hint_suffix = " | 引擎: 自动"
        else:
            eng_info = next((e for e in engines if e["name"] == current_engine), None)
            hint_suffix = f" | 引擎: {eng_info['display_name'] if eng_info else current_engine}"
        self._update_status_hint(hint_suffix)

    def _sync_engine_display(self, engine_name):
        """同步引擎下拉框显示"""
        if engine_name == "auto":
            self.engine_var.set("🔀 自动选择")
        else:
            engines = get_available_engines()
            for eng in engines:
                if eng["name"] == engine_name:
                    display = eng["display_name"]
                    if eng["requires_key"]:
                        display += " ✓" if eng["available"] else " (需密钥)"
                    self.engine_var.set(display)
                    return
            self.engine_var.set(f"{engine_name} (?)")

    def _on_engine_changed(self):
        """引擎选择变更"""
        selected_display = self.engine_var.get()
        if selected_display == "🔀 自动选择":
            engine_name = "auto"
        else:
            # 从显示名反查引擎名
            engines = get_available_engines()
            found = None
            for eng in engines:
                disp = eng["display_name"]
                if eng["requires_key"]:
                    disp += " ✓" if eng["available"] else " (需密钥)"
                if disp == selected_display:
                    found = eng["name"]
                    break
            engine_name = found or "auto"

        # 保存选择
        config = load_config()
        config["selected_engine"] = engine_name
        save_config(config)
        reload_config()

        # 检查所选引擎是否可用（警告与成功提示合并，避免覆盖）
        if engine_name != "auto":
            eng_info = next((e for e in get_available_engines() if e["name"] == engine_name), None)
            if eng_info and not eng_info["available"]:
                self.status_label.configure(text=f"⚠️ 已切换 | {eng_info['display_name']} 未配置API密钥，请点击 ⚙ 设置")
                return

        self.status_label.configure(text=f"✅ 已切换翻译引擎 | 当前: {self.engine_var.get()}")

    def _update_status_hint(self, suffix):
        """更新状态栏提示"""
        pass  # 状态栏在初始化时已设置，此处预留

    def _open_settings(self):
        """打开API密钥设置对话框"""
        SettingsDialog(self.root)
        self._refresh_engine_list()

    def _on_popup_closed(self):
        """浮动弹窗关闭回调"""
        pass

    def _on_close(self):
        """窗口关闭 — 清理所有资源（非阻塞版本，防重入）"""
        if self._closing:
            return
        self._closing = True
        logger.info("正在关闭翻译助手...")

        # 停止剪贴板监控（常驻线程通过标志退出）
        self.monitoring = False
        self._monitor_event.set()

        # 停止音频翻译（stop() 已改为非阻塞，直接调用即可）
        global _audio_translator
        if _audio_translator is not None:
            try:
                _audio_translator.stop()
            except Exception:
                pass

        # 销毁浮动弹窗
        if self.float_popup and self.float_popup.winfo_exists():
            try:
                self.float_popup.destroy()
            except Exception:
                pass

        # 销毁主窗口：mainloop 退出后 run() 自然返回，daemon 线程随进程结束
        self.root.destroy()

    # ==================== 启动 ====================

    def run(self):
        """启动应用"""
        # 注册 Ctrl+C 信号处理，确保退出时清理资源
        import signal
        def _handle_sigint(signum, frame):
            logger.info("正在关闭...")
            self._on_close()
        try:
            signal.signal(signal.SIGINT, _handle_sigint)
        except (ValueError, OSError):
            pass  # 非主线程中无法设置信号处理器

        self.root.mainloop()


# =========================== 入口 ===========================
if __name__ == "__main__":
    # 检查依赖
    missing = []
    try:
        import pyperclip
    except ImportError:
        missing.append("pyperclip")

    if missing:
        logger.error("缺少依赖库，请运行以下命令安装：")
        for m in missing:
            logger.error(f"  pip install {m}")
        print("\n按任意键退出...")
        input()
        sys.exit(1)

    app = TranslatorApp()
    app.run()
