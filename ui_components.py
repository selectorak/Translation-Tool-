"""
UI 组件模块
==============
包含浮动翻译弹窗、设置对话框等可复用的 UI 组件。
"""

import tkinter as tk
from tkinter import messagebox

from theme import Theme
from config import FLOAT_POPUP_WIDTH, ENGINE_INFO
from translate_engines import load_config, save_config, reload_config


# =========================== 浮动翻译弹窗 ===========================
class FloatPopup(tk.Toplevel):
    """跨软件剪贴板翻译的浮动弹窗（可拖拽调整大小，不自动关闭）"""

    def __init__(self, master, on_close_callback=None):
        super().__init__(master)
        self.on_close_callback = on_close_callback
        self._setup_window()
        self._build_ui()
        self.withdraw()  # 初始隐藏

    def _setup_window(self):
        self.title("翻译结果")
        self.resizable(True, True)
        self.minsize(280, 180)
        self.attributes("-topmost", True)
        self.configure(bg=Theme.CARD_BG)
        self.protocol("WM_DELETE_WINDOW", self.hide_popup)
        self.bind("<Escape>", lambda e: self.hide_popup())

    def _build_ui(self):
        container = tk.Frame(self, bg=Theme.CARD_BG, bd=0)
        container.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # 语言提示
        top_bar = tk.Frame(container, bg=Theme.CARD_BG)
        top_bar.pack(fill=tk.X, pady=(0, 6))
        self.lang_hint = tk.Label(top_bar, text="", font=("Microsoft YaHei", 9, "bold"),
                                  bg=Theme.CARD_BG, fg=Theme.PRIMARY)
        self.lang_hint.pack(side=tk.LEFT)

        # 源文本区域
        src_frame = tk.Frame(container, bg=Theme.BG, bd=0,
                             highlightbackground=Theme.BORDER, highlightthickness=1)
        src_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 4))
        src_label = tk.Label(src_frame, text="原文", font=("Microsoft YaHei", 8),
                             bg=Theme.BG, fg=Theme.TEXT_HINT)
        src_label.pack(anchor=tk.W, padx=8, pady=(4, 0))
        self.source_text = tk.Text(src_frame, height=3, font=("Microsoft YaHei", 11),
                                   bg=Theme.BG, fg=Theme.TEXT_SEC, bd=0,
                                   wrap=tk.WORD, state=tk.DISABLED,
                                   relief=tk.FLAT, padx=8, pady=4)
        self.source_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        # 翻译结果区域
        result_frame = tk.Frame(container, bg=Theme.CARD_BG)
        result_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        result_label = tk.Label(result_frame, text="▼ 译文",
                                font=("Microsoft YaHei", 8),
                                bg=Theme.CARD_BG, fg=Theme.TEXT_HINT)
        result_label.pack(anchor=tk.W)
        result_inner = tk.Frame(result_frame, bg=Theme.CARD_BG)
        result_inner.pack(fill=tk.BOTH, expand=True)
        self.result_text = tk.Text(result_inner, height=5, font=("Microsoft YaHei", 13),
                                   bg=Theme.CARD_BG, fg=Theme.TEXT, bd=0,
                                   wrap=tk.WORD, state=tk.DISABLED,
                                   relief=tk.FLAT, padx=4, pady=4)
        self.result_scroll = tk.Scrollbar(result_inner, command=self.result_text.yview,
                                          width=6, bg=Theme.CARD_BG, troughcolor=Theme.CARD_BG)
        self.result_text.configure(yscrollcommand=self.result_scroll.set)
        self.result_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.result_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 底部操作栏
        action_bar = tk.Frame(container, bg=Theme.CARD_BG, height=28)
        action_bar.pack(fill=tk.X, pady=(6, 0))
        action_bar.pack_propagate(False)
        copy_btn = tk.Label(action_bar, text="📋 复制译文", font=("Microsoft YaHei", 9),
                            bg=Theme.PRIMARY_BG, fg=Theme.PRIMARY,
                            cursor="hand2", padx=10, pady=2)
        copy_btn.pack(side=tk.LEFT)
        copy_btn.bind("<Button-1>", lambda e: self._copy_result())
        copy_btn.bind("<Enter>", lambda e: copy_btn.configure(bg=Theme.PRIMARY, fg="#fff"))
        copy_btn.bind("<Leave>", lambda e: copy_btn.configure(bg=Theme.PRIMARY_BG, fg=Theme.PRIMARY))

    def show_translation(self, source: str, result: str, from_lang: str, to_lang: str) -> None:
        """显示翻译结果"""
        self.source_text.configure(state=tk.NORMAL)
        self.source_text.delete("1.0", tk.END)
        self.source_text.insert("1.0", source)
        self.source_text.configure(state=tk.DISABLED)

        lang_map = {"zh": "中文", "en": "英文", "auto": "自动检测"}
        self.lang_hint.configure(
            text=f"{lang_map.get(from_lang, from_lang)} → {lang_map.get(to_lang, to_lang)}"
        )

        self.result_text.configure(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert("1.0", result)
        self.result_text.configure(state=tk.DISABLED)

        self._position_bottom_right()
        self.deiconify()
        self.lift()

    def show_loading(self, source: str) -> None:
        """显示加载状态"""
        self.source_text.configure(state=tk.NORMAL)
        self.source_text.delete("1.0", tk.END)
        self.source_text.insert("1.0", source)
        self.source_text.configure(state=tk.DISABLED)
        self.lang_hint.configure(text="翻译中...")
        self.result_text.configure(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert("1.0", "⏳ 正在翻译，请稍候...")
        self.result_text.configure(state=tk.DISABLED)
        self._position_bottom_right()
        self.deiconify()
        self.lift()

    def show_error(self, source: str, error_msg: str) -> None:
        """显示错误"""
        self.source_text.configure(state=tk.NORMAL)
        self.source_text.delete("1.0", tk.END)
        self.source_text.insert("1.0", source)
        self.source_text.configure(state=tk.DISABLED)
        self.lang_hint.configure(text="翻译失败")
        self.result_text.configure(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert("1.0", f"❌ {error_msg}")
        self.result_text.configure(state=tk.DISABLED)
        self._position_bottom_right()
        self.deiconify()
        self.lift()

    def _position_bottom_right(self) -> None:
        """首次显示时定位到屏幕右下角"""
        self.update_idletasks()
        if not hasattr(self, '_user_positioned'):
            screen_w = self.winfo_screenwidth()
            screen_h = self.winfo_screenheight()
            w = FLOAT_POPUP_WIDTH
            h = 300
            x = screen_w - w - 30
            y = screen_h - h - 60
            self.geometry(f"{w}x{h}+{x}+{y}")

    def hide_popup(self) -> None:
        """隐藏弹窗（不销毁，下次翻译时复用）"""
        self.withdraw()
        if self.on_close_callback:
            self.on_close_callback()

    def _copy_result(self) -> None:
        """复制翻译结果到剪贴板"""
        text = self.result_text.get("1.0", tk.END).strip()
        if text and not text.startswith("\u23f3") and not text.startswith("\u274c"):
            self.clipboard_clear()
            self.clipboard_append(text)


# =========================== API密钥设置对话框 ===========================
class SettingsDialog(tk.Toplevel):
    """翻译引擎API密钥配置对话框"""

    ENGINE_INFO = ENGINE_INFO  # 引擎元数据集中定义在 config.py，UI 只负责渲染

    def __init__(self, master):
        super().__init__(master)
        self.title("⚙ 翻译引擎设置")
        self.resizable(True, True)
        self.minsize(520, 420)
        self.configure(bg=Theme.CARD_BG)
        self.transient(master)
        self.grab_set()

        self.config = load_config()
        self.api_keys = self.config.get("api_keys", {})
        self.entries: dict = {}

        self._build_ui()
        self._load_values()
        self._center_on_parent(master)

    def _center_on_parent(self, parent):
        self.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        w = 560
        h = 520
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        outer = tk.Frame(self, bg=Theme.CARD_BG)
        outer.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        canvas = tk.Canvas(outer, bg=Theme.CARD_BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        self.scroll_frame = tk.Frame(canvas, bg=Theme.CARD_BG)

        self.scroll_frame.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 标题
        title_frame = tk.Frame(self.scroll_frame, bg=Theme.CARD_BG)
        title_frame.pack(fill=tk.X, padx=12, pady=(10, 4))
        tk.Label(title_frame, text="⚙ API 密钥配置",
                 font=("Microsoft YaHei", 14, "bold"),
                 bg=Theme.CARD_BG, fg=Theme.TEXT).pack(anchor=tk.W)
        tk.Label(title_frame,
                 text="配置后可选择对应引擎翻译。免费引擎(MyMemory/Google)无需配置。",
                 font=("Microsoft YaHei", 9),
                 bg=Theme.CARD_BG, fg=Theme.TEXT_HINT).pack(anchor=tk.W, pady=(2, 0))

        for engine_key, info in self.ENGINE_INFO.items():
            section = self._build_engine_section(engine_key, info)
            section.pack(fill=tk.X, padx=12, pady=(8, 0))

        # 高级设置
        adv_frame = tk.Frame(self.scroll_frame, bg=Theme.CARD_BG,
                             highlightbackground=Theme.BORDER, highlightthickness=1)
        adv_frame.pack(fill=tk.X, padx=12, pady=(10, 0))
        tk.Label(adv_frame, text="⚡ 高级设置",
                 font=("Microsoft YaHei", 11, "bold"),
                 bg=Theme.CARD_BG, fg=Theme.TEXT, anchor=tk.W).pack(fill=tk.X, padx=12, pady=(8, 4))

        fallback_row = tk.Frame(adv_frame, bg=Theme.CARD_BG)
        fallback_row.pack(fill=tk.X, padx=12, pady=4)
        tk.Label(fallback_row, text="主引擎失败时自动回退:",
                 font=("Microsoft YaHei", 10),
                 bg=Theme.CARD_BG, fg=Theme.TEXT_SEC).pack(side=tk.LEFT)
        self.fallback_var = tk.BooleanVar(value=self.config.get("fallback_enabled", True))
        tk.Checkbutton(fallback_row, variable=self.fallback_var,
                       bg=Theme.CARD_BG, activebackground=Theme.CARD_BG).pack(side=tk.RIGHT, padx=(0, 20))

        chunk_row = tk.Frame(adv_frame, bg=Theme.CARD_BG)
        chunk_row.pack(fill=tk.X, padx=12, pady=(4, 8))
        tk.Label(chunk_row, text="单次最大字符数(自动分块):",
                 font=("Microsoft YaHei", 10),
                 bg=Theme.CARD_BG, fg=Theme.TEXT_SEC).pack(side=tk.LEFT)
        self.chunk_var = tk.StringVar(value=str(self.config.get("max_chunk_size", 5000)))
        tk.Entry(chunk_row, textvariable=self.chunk_var, width=8,
                 font=("Microsoft YaHei", 10), justify=tk.CENTER).pack(side=tk.RIGHT, padx=(0, 20))

        # 底部按钮
        btn_frame = tk.Frame(self.scroll_frame, bg=Theme.CARD_BG)
        btn_frame.pack(fill=tk.X, padx=12, pady=(12, 16))
        save_btn = tk.Label(btn_frame, text="  💾 保存配置  ",
                            font=("Microsoft YaHei", 11, "bold"),
                            bg=Theme.PRIMARY, fg="#ffffff",
                            cursor="hand2", padx=20, pady=6)
        save_btn.pack(side=tk.RIGHT, padx=(8, 0))
        save_btn.bind("<Button-1>", lambda e: self._save())
        save_btn.bind("<Enter>", lambda e: save_btn.configure(bg="#1557b0"))
        save_btn.bind("<Leave>", lambda e: save_btn.configure(bg=Theme.PRIMARY))
        cancel_btn = tk.Label(btn_frame, text="  取消  ",
                              font=("Microsoft YaHei", 11),
                              bg=Theme.BG, fg=Theme.TEXT_SEC,
                              cursor="hand2", padx=16, pady=6)
        cancel_btn.pack(side=tk.RIGHT)
        cancel_btn.bind("<Button-1>", lambda e: self.destroy())
        cancel_btn.bind("<Enter>", lambda e: cancel_btn.configure(bg=Theme.BORDER))
        cancel_btn.bind("<Leave>", lambda e: cancel_btn.configure(bg=Theme.BG))

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        # 只绑定在 canvas 自身（配合 Enter 时获取焦点），
        # 不用 bind_all/unbind_all，避免破坏其他窗口的全局滚轮绑定
        canvas.bind("<MouseWheel>", _on_mousewheel)
        canvas.bind("<Enter>", lambda e: canvas.focus_set())

    def _build_engine_section(self, engine_key: str, info: dict) -> tk.Frame:
        """构建单个引擎的配置区块"""
        import webbrowser

        frame = tk.Frame(self.scroll_frame, bg=Theme.CARD_BG,
                         highlightbackground=Theme.BORDER, highlightthickness=1)
        header = tk.Frame(frame, bg=Theme.PRIMARY_BG, height=32)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text=f"  {info['name']}",
                 font=("Microsoft YaHei", 10, "bold"),
                 bg=Theme.PRIMARY_BG, fg=Theme.PRIMARY).pack(side=tk.LEFT, pady=4)
        link_lbl = tk.Label(header, text="🔗 注册获取密钥",
                            font=("Microsoft YaHei", 8),
                            bg=Theme.PRIMARY_BG, fg=Theme.PRIMARY, cursor="hand2")
        link_lbl.pack(side=tk.RIGHT, padx=8, pady=4)
        link_lbl.bind("<Button-1>", lambda e, u=info['url']: webbrowser.open(u))

        fields_frame = tk.Frame(frame, bg=Theme.CARD_BG)
        fields_frame.pack(fill=tk.X, padx=12, pady=(6, 2))
        for field_key, field_label, field_width in info["fields"]:
            row = tk.Frame(fields_frame, bg=Theme.CARD_BG)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=f"{field_label}:",
                     font=("Microsoft YaHei", 9),
                     bg=Theme.CARD_BG, fg=Theme.TEXT_SEC,
                     width=20, anchor=tk.W).pack(side=tk.LEFT)
            entry = tk.Entry(row, font=("Consolas", 10), width=field_width,
                             show="•", bg="#fafbfc")
            entry.pack(side=tk.LEFT, padx=(4, 0))
            self.entries.setdefault(engine_key, {})[field_key] = entry

        tk.Label(frame, text=f"  💡 {info['note']}",
                 font=("Microsoft YaHei", 8),
                 bg=Theme.CARD_BG, fg=Theme.TEXT_HINT).pack(anchor=tk.W, padx=12, pady=(0, 6))
        return frame

    def _load_values(self) -> None:
        """从配置加载值到输入框"""
        for engine_key, fields in self.api_keys.items():
            if engine_key in self.entries:
                for field_key, entry in self.entries[engine_key].items():
                    value = fields.get(field_key, "")
                    entry.delete(0, tk.END)
                    entry.insert(0, value)

    def _save(self) -> None:
        """保存配置"""
        for engine_key in self.entries:
            if engine_key not in self.api_keys:
                self.api_keys[engine_key] = {}
            for field_key, entry in self.entries[engine_key].items():
                self.api_keys[engine_key][field_key] = entry.get().strip()

        self.config["api_keys"] = self.api_keys
        self.config["fallback_enabled"] = self.fallback_var.get()
        try:
            self.config["max_chunk_size"] = int(self.chunk_var.get())
        except ValueError:
            self.config["max_chunk_size"] = 5000

        if save_config(self.config):
            reload_config()
            messagebox.showinfo("配置已保存", "API密钥配置已保存。\n\n请重新选择翻译引擎以使用新的配置。", parent=self)
            self.destroy()
        else:
            messagebox.showerror("保存失败", "无法写入配置文件，请检查文件权限。", parent=self)
