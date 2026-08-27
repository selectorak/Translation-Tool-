#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
屏幕区域翻译模块
==================
功能：
  1. 屏幕区域选择 — 全屏遮罩 + 鼠标拖拽选定区域
  2. 图像预处理   — OpenCV 灰度化、自适应二值化、去噪、对比度增强
  3. OCR文字识别  — Tesseract 中英文双语识别
  4. 语义识别     — 判断文字是否"有意义"（有效字符比例、语言检测、结构分析）
  5. 用户确认     — 弹窗展示识别原文，用户决定是否翻译
  6. 翻译         — 对接现有 TranslateEngine

依赖安装（Windows）：
  pip install opencv-python pytesseract mss Pillow numpy

  ★ 还需要安装 Tesseract-OCR 程序：
    https://github.com/UB-Mannheim/tesseract/wiki
    下载 tesseract-ocr-w64-setup-5.x.x.exe 安装
    安装时勾选中文语言包 (Chinese Simplified)
    默认路径: C:/Program Files/Tesseract-OCR/tesseract.exe
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import re
import os
import sys

# =========================== 懒加载依赖（启动时不阻塞） ===========================
_deps = {}
_deps_check_cache = None  # check_all_deps() 结果缓存（首次扫描后复用）


def _try_import(name):
    """尝试导入模块并缓存到 _deps"""
    try:
        mod = __import__(name)
        _deps[name] = mod
        return mod
    except ImportError:
        return None


def check_all_deps(force=False):
    """检查所有屏幕翻译所需依赖（使用 importlib.util.find_spec 更可靠）

    Args:
        force: True 时强制重新扫描（默认使用缓存结果）
    """
    global _deps_check_cache
    if _deps_check_cache is not None and not force:
        return list(_deps_check_cache)

    import importlib.util as _util
    missing = []

    deps_to_check = [
        ("cv2", "opencv-python"),
        ("numpy", "numpy"),
        ("PIL", "Pillow"),
        ("mss", "mss"),
        ("pytesseract", "pytesseract"),
    ]

    for mod_name, pkg_name in deps_to_check:
        spec = _util.find_spec(mod_name)
        if spec is None:
            missing.append(f"pip install {pkg_name}")
            print(f"[screen_translator] MISSING: {mod_name} ({pkg_name})")
        else:
            # 确保导入并填充 _deps 缓存（供其他组件使用）
            try:
                mod = __import__(mod_name)
                _deps[mod_name] = mod
            except ImportError as e:
                missing.append(f"pip install {pkg_name}")
                print(f"[screen_translator] IMPORT FAILED: {mod_name} - {e}")

    # 检查 Tesseract 可执行文件（仅当 pytesseract Python 包可用时）
    try:
        import pytesseract
        tesseract_exe = _find_tesseract()
        if not tesseract_exe:
            missing.append("Tesseract-OCR 程序 (下载: https://github.com/UB-Mannheim/tesseract/wiki)")
        else:
            pytesseract.pytesseract.tesseract_cmd = tesseract_exe
            # 自动设置 tessdata 路径
            tessdata_dir = os.path.join(os.path.dirname(tesseract_exe), "tessdata")
            if os.path.isdir(tessdata_dir):
                os.environ["TESSDATA_PREFIX"] = tessdata_dir
    except ImportError:
        pass  # pytesseract 未安装的情况已在上面的循环中处理

    _deps_check_cache = list(missing)
    return list(missing)

def _find_tesseract():
    """在常见路径中查找 Tesseract 可执行文件"""
    common_paths = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"C:\Users\Administrator\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
        os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    ]
    for p in common_paths:
        if os.path.exists(p):
            return p
    # 用户自定义路径
    custom_paths = [
        r"D:\tesseract\tesseract.exe",
        r"D:\Tesseract-OCR\tesseract.exe",
        os.path.expanduser(r"~\tesseract\tesseract.exe"),
    ]
    for p in custom_paths:
        if os.path.exists(p):
            return p
    # 尝试从 PATH 中找
    import shutil
    found = shutil.which("tesseract")
    return found


# =========================== 图像预处理器 (OpenCV) ===========================
class ImagePreprocessor:
    """
    使用 OpenCV 对截取图像进行预处理，提升 OCR 准确率。
    
    处理管线：
      原始图像 → 灰度化 → 降噪 → 对比度增强 → 自适应二值化 → 形态学清理
    """

    @staticmethod
    def preprocess(pil_image, method="auto"):
        """
        预处理 PIL 图像，返回处理后的 PIL 图像
        
        Args:
            pil_image: PIL.Image 对象
            method: "auto" | "light" | "heavy" | "none"
        Returns:
            PIL.Image 对象，以及方法名称
        """
        cv2 = _deps.get("cv2") or _try_import("cv2")
        np = _deps.get("numpy") or _try_import("numpy")
        if cv2 is None or np is None:
            return pil_image, "none"

        # PIL → numpy array (RGB → BGR)
        img = np.array(pil_image)
        if len(img.shape) == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        else:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

        # 灰度化
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 分析图像特征
        mean_val = np.mean(gray)
        std_val = np.std(gray)

        # 根据图像亮度选择处理策略
        if method == "auto":
            if std_val < 30:
                method = "heavy"   # 低对比度 → 强处理
            else:
                method = "light"   # 中等/高对比度 → 轻处理（灰度 + Otsu）

        if method == "none":
            return pil_image, "none"

        processed = gray.copy()

        # ---- 1. 降噪 ----
        if method == "heavy":
            # 大图保护：fastNlMeansDenoising 在超大面积上极慢（数秒到数十秒），
            # 最长边超过 2000px 时降级为轻量滤波，避免"假死"
            h, w = processed.shape[:2]
            if max(h, w) > 2000:
                print(f"[预处理] 图像 {w}x{h} 过大，heavy 降级为 light 处理")
                processed = cv2.GaussianBlur(processed, (3, 3), 0)
                method = "light"
            else:
                # 非局部均值降噪（保留文字边缘）
                processed = cv2.fastNlMeansDenoising(processed, None, 10, 7, 21)
        else:
            # 高斯模糊去小噪点
            processed = cv2.GaussianBlur(processed, (3, 3), 0)

        # ---- 2. 对比度增强 (CLAHE) ----
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        processed = clahe.apply(processed)

        # ---- 3. 自适应二值化 ----
        if method == "heavy":
            # 对低质量图像使用更激进的二值化
            processed = cv2.adaptiveThreshold(
                processed, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 15, 8
            )
        else:
            # Otsu 自动阈值
            _, processed = cv2.threshold(processed, 0, 255,
                                         cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # ---- 4. 形态学清理 ----
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        # 闭运算：填补文字内部小空洞
        processed = cv2.morphologyEx(processed, cv2.MORPH_CLOSE, kernel)
        # 去除孤立噪点
        processed = cv2.medianBlur(processed, 1)

        # 转回 PIL
        result_pil = ImagePreprocessor._array_to_pil(processed)
        return result_pil, method

    @staticmethod
    def _array_to_pil(arr):
        """numpy array → PIL Image"""
        from PIL import Image as PILImage
        return PILImage.fromarray(arr)


# =========================== OCR 文字识别 ===========================
class OCRProcessor:
    """
    OCR 文字识别器 — 使用 Tesseract
    
    支持中英文混合识别，返回带置信度的结构化结果。
    """

    def __init__(self):
        self.pytesseract = _deps.get("pytesseract") or _try_import("pytesseract")

    def is_available(self):
        return self.pytesseract is not None

    def recognize(self, pil_image, preprocess_method="auto"):
        """
        对图像执行 OCR 识别
        
        Args:
            pil_image: PIL.Image
            preprocess_method: 预处理方法
            
        Returns:
            dict {
                "text": str,           # 原始识别文本
                "confidence": float,   # 平均置信度 (0-100)
                "lines": [str],        # 逐行文本
                "details": [...],      # Tesseract 详细结果
                "preprocess": str,     # 使用的预处理方法
            }
        """
        if not self.is_available():
            return {"text": "", "confidence": 0, "lines": [], "details": [], "preprocess": "none", "error": "pytesseract 未安装"}

        # 预处理
        processed_img, used_method = ImagePreprocessor.preprocess(pil_image, preprocess_method)

        try:
            import pytesseract
            # 获取详细识别结果
            details = pytesseract.image_to_data(
                processed_img,
                lang="chi_sim+eng",      # 中文简体 + 英文
                output_type=pytesseract.Output.DICT,
                config="--psm 6"          # 假设为统一的文本块
            )

            # 提取有效文本行
            lines = []
            current_line = []
            current_block = -1
            current_par = -1

            for i, text in enumerate(details["text"]):
                conf = int(details["conf"][i]) if details["conf"][i] != "-1" else -1
                block = details["block_num"][i]
                par = details["par_num"][i]
                line = details["line_num"][i]

                if text.strip():
                    # 根据 block/par/line 分组
                    if block != current_block or par != current_par or line != (current_line[-1]["line"] if current_line else -1):
                        if current_line:
                            line_text = " ".join([c["text"] for c in current_line])
                            lines.append(line_text)
                        current_line = []
                        current_block = block
                        current_par = par
                    current_line.append({"text": text, "conf": conf, "line": line})

            # 最后一行
            if current_line:
                line_text = " ".join([c["text"] for c in current_line])
                lines.append(line_text)

            # 计算平均置信度
            confidences = [int(c) for c in details["conf"] if c != "-1"]
            avg_conf = sum(confidences) / len(confidences) if confidences else 0

            full_text = "\n".join(lines)

            return {
                "text": full_text,
                "confidence": round(avg_conf, 1),
                "lines": lines,
                "details": details,
                "preprocess": used_method,
            }

        except Exception as e:
            return {
                "text": "",
                "confidence": 0,
                "lines": [],
                "details": [],
                "preprocess": used_method,
                "error": str(e),
            }


# =========================== 语义分析器 ===========================
class SemanticAnalyzer:
    """
    语义分析器 — 判断 OCR 提取的文字是否"有意义"

    判断维度：
      1. 有效字符比例 — 中文/英文/数字/标点占比
      2. 熵值/结构 — 是否为随机乱码
      3. 最小长度 — 至少包含有意义的内容
      4. 语言判定 — 中/英/混合/乱码
    """

    # 中文字符范围
    CJK_RANGE = r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]'
    # 英文字母
    EN_RANGE = r'[a-zA-Z]'
    # 数字
    DIGIT_RANGE = r'[0-9]'
    # 常见标点（\' 与 " 为转义后的 ASCII 引号，避免截断字符串字面量）
    PUNCT_RANGE = r'[，。！？、；：\'"（）《》【】.,!?;:()\[\]{}…—\-\s\n\r\t]'

    @classmethod
    def analyze(cls, text):
        """
        分析文本，返回语义评分和建议

        Returns:
            dict {
                "text": str,              # 原始文本
                "meaningful": bool,       # 是否建议翻译
                "score": float,           # 语义评分 (0-100)
                "confidence": str,        # "high" | "medium" | "low" | "none"
                "lang": str,              # "zh" | "en" | "mixed" | "unknown"
                "reason": str,            # 判断理由
                "valid_ratio": float,     # 有效字符比例
                "stats": dict,            # 字符统计
            }
        """
        if not text or not text.strip():
            return cls._result(text, False, 0, "none", "unknown", "空文本", 0, {})

        stripped = text.strip()

        # 统计各类字符
        stats = {
            "total": len(stripped),
            "chinese": len(re.findall(cls.CJK_RANGE, stripped)),
            "english": len(re.findall(cls.EN_RANGE, stripped)),
            "digit": len(re.findall(cls.DIGIT_RANGE, stripped)),
            "punct": len(re.findall(cls.PUNCT_RANGE, stripped)),
        }
        stats["other"] = stats["total"] - sum([
            stats["chinese"], stats["english"], stats["digit"], stats["punct"]
        ])
        stats["valid"] = stats["chinese"] + stats["english"] + stats["digit"] + stats["punct"]

        # 有效字符比例
        valid_ratio = stats["valid"] / stats["total"] if stats["total"] > 0 else 0

        # ---- 判定语言 ----
        total_alpha = stats["chinese"] + stats["english"]
        if stats["chinese"] > 0 and stats["english"] > 0:
            if stats["chinese"] / total_alpha > 0.3 and stats["english"] / total_alpha > 0.3:
                lang = "mixed"
            elif stats["chinese"] > stats["english"]:
                lang = "zh"
            else:
                lang = "en"
        elif stats["chinese"] > 0:
            lang = "zh"
        elif stats["english"] > 0:
            lang = "en"
        else:
            lang = "unknown"

        # ---- 计算评分 ----
        score = 0
        reasons = []

        # 1. 有效字符比例 (权重 40)
        if valid_ratio >= 0.85:
            score += 40
        elif valid_ratio >= 0.6:
            score += 25
            reasons.append("部分无效字符")
        elif valid_ratio >= 0.4:
            score += 10
            reasons.append("较多无效字符")
        else:
            reasons.append("大量无效字符")

        # 2. 单词/词语数量 (权重 25)
        # 中文按单字拆分，英文按空格分词
        zh_words = re.findall(cls.CJK_RANGE, stripped)
        en_words = re.findall(r'[a-zA-Z]{2,}', stripped)
        word_count = len(zh_words) + len(en_words)

        if word_count >= 8:
            score += 25
        elif word_count >= 4:
            score += 18
        elif word_count >= 2:
            score += 10
        else:
            reasons.append("文字量太少")

        # 3. 是否有完整句子结构 (权重 20)
        has_sentence = bool(re.search(r'[。！？\.!\?]', stripped))
        has_newline_struct = '\n' in stripped and len(stripped.split('\n')) >= 2
        if has_sentence or has_newline_struct:
            score += 20
        elif word_count >= 6:
            score += 10

        # 4. 乱码检测 (扣分)
        # 检测连续无意义字符
        gibberish_pattern = re.findall(r'[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9\s\.,!?;:\'"…—\-，。！？、；：（）《》【】\[\]{}]{3,}', stripped)
        if gibberish_pattern:
            score -= min(20, len(gibberish_pattern) * 5)
            reasons.append("检测到疑似乱码")

        # 5. 常见UI文字过滤
        ui_patterns = [
            r'^[0-9]{1,2}:[0-9]{2}$',           # 时间格式
            r'^[0-9]+%$',                         # 百分比
            r'^[0-9]+\s*(px|em|rem|pt)$',         # CSS单位
            r'^(确定|取消|关闭|保存|提交|删除|编辑|查看|搜索|登录|注册)$',  # 单个按钮文字
        ]
        for pat in ui_patterns:
            if re.match(pat, stripped):
                score = max(0, score - 15)
                reasons.append("疑似UI控件文字")
                break

        # 限制分数范围
        score = max(0, min(100, score))

        # ---- 判定结果 ----
        if score >= 60:
            meaningful = True
            confidence = "high"
        elif score >= 35:
            meaningful = True
            confidence = "medium"
        elif score >= 15:
            meaningful = False
            confidence = "low"
        else:
            meaningful = False
            confidence = "none"

        if not meaningful and not reasons:
            reasons.append("语义评分过低")

        return cls._result(
            text=stripped,
            meaningful=meaningful,
            score=score,
            confidence=confidence,
            lang=lang,
            reason="; ".join(reasons) if reasons else "文本质量良好",
            valid_ratio=round(valid_ratio, 2),
            stats=stats,
        )

    @staticmethod
    def _result(text, meaningful, score, confidence, lang, reason, valid_ratio, stats):
        return {
            "text": text,
            "meaningful": meaningful,
            "score": score,
            "confidence": confidence,
            "lang": lang,
            "reason": reason,
            "valid_ratio": valid_ratio,
            "stats": stats,
        }


# =========================== 区域选择器 ===========================
class RegionSelector:
    """
    全屏半透明遮罩 + 鼠标拖拽选区域

    用法:
        selector = RegionSelector()
        region = selector.select()  # 阻塞直到用户选择或取消
        # region = (x, y, w, h) 或 None
    """

    def __init__(self):
        self.win = None
        self.canvas = None
        self.screenshot = None  # PIL Image of full screen
        self.tk_image = None
        self.region = None      # (x, y, w, h)

        # 拖拽状态
        self.start_x = None
        self.start_y = None
        self.current_rect = None

        # 显示大小（屏幕的缩放版本，用于超高分辨率屏幕）
        self.scale = 1.0
        self.offset_x = 0
        self.offset_y = 0

    def select(self):
        """
        启动区域选择。阻塞直到用户完成选择。
        返回 (x, y, w, h) 元组，取消返回 None。
        """
        # 截取全屏
        self._capture_fullscreen()

        # 创建全屏窗口
        self.win = tk.Toplevel()
        self.win.attributes("-fullscreen", True)
        self.win.attributes("-topmost", True)
        self.win.configure(cursor="crosshair")
        self.win.config(cursor="crosshair")

        # 取消窗口装饰
        self.win.overrideredirect(True)

        # 获取实际屏幕尺寸
        self.win.update_idletasks()
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()

        # 构建 Canvas
        self.canvas = tk.Canvas(
            self.win, width=screen_w, height=screen_h,
            bg="black", highlightthickness=0, bd=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # 显示截图作为背景（半透明效果通过覆盖实现）
        if self.screenshot:
            # 缩放截图以适应屏幕
            img_w, img_h = self.screenshot.size
            self.scale = min(screen_w / img_w, screen_h / img_h, 1.0)
            new_w = int(img_w * self.scale)
            new_h = int(img_h * self.scale)
            self.offset_x = (screen_w - new_w) // 2
            self.offset_y = (screen_h - new_h) // 2

            # 缩放图像
            resized = self.screenshot.resize((new_w, new_h))

            # PIL → Tk PhotoImage
            from PIL import ImageTk
            self.tk_image = ImageTk.PhotoImage(resized)
            self.canvas.create_image(
                self.offset_x + new_w // 2, self.offset_y + new_h // 2,
                image=self.tk_image, anchor=tk.CENTER
            )

        # 绘制半透明遮罩层
        self._overlay = self.canvas.create_rectangle(
            0, 0, screen_w, screen_h,
            fill="black", stipple="gray50", outline=""
        )

        # 提示文字
        font_size = max(12, min(24, screen_w // 60))
        self._hint_text = self.canvas.create_text(
            screen_w // 2, 40,
            text="🖱 拖拽鼠标选择要翻译的屏幕区域 | 按 ESC 取消",
            fill="#ffffff", font=("Microsoft YaHei", font_size, "bold"),
            anchor=tk.CENTER
        )

        # 绑定鼠标事件
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Escape>", self._on_cancel)

        # 设置窗口焦点
        self.win.focus_force()
        self.win.grab_set()

        # 阻塞等待
        self.win.wait_window()

        # 清理（screenshot 保留供后续 crop 复用，由调用方释放）
        if self.tk_image:
            self.tk_image = None

        return self.region

    def _capture_fullscreen(self):
        """截取全屏"""
        try:
            # 优先使用 mss（快）
            mss = _deps.get("mss") or _try_import("mss")
            if mss:
                with mss.mss() as sct:
                    monitor = sct.monitors[1]  # 主显示器
                    screenshot = sct.grab(monitor)
                    from PIL import Image as PILImage
                    self.screenshot = PILImage.frombytes(
                        "RGB", screenshot.size, screenshot.bgra, "raw", "BGRX"
                    )
                    return
        except Exception:
            pass

        # 备用：PIL.ImageGrab
        try:
            from PIL import ImageGrab
            self.screenshot = ImageGrab.grab()
        except Exception:
            self.screenshot = None

    def _on_press(self, event):
        """鼠标按下"""
        self.start_x = event.x
        self.start_y = event.y
        if self.current_rect:
            self.canvas.delete(self.current_rect)
            self.current_rect = None

    def _on_drag(self, event):
        """鼠标拖拽"""
        if self.current_rect:
            self.canvas.delete(self.current_rect)

        # 绘制选择矩形
        self.current_rect = self.canvas.create_rectangle(
            self.start_x, self.start_y, event.x, event.y,
            outline="#00ff00", width=2, dash=(8, 4),
        )

        # 更新提示
        w = abs(event.x - self.start_x)
        h = abs(event.y - self.start_y)
        self.canvas.itemconfigure(
            self._hint_text,
            text=f"📐 选中区域: {w}×{h} px | 松开鼠标确认 | ESC 取消"
        )

    def _on_release(self, event):
        """鼠标释放 — 确认选择"""
        if self.start_x is None or self.start_y is None:
            return

        x1, y1 = self.start_x, self.start_y
        x2, y2 = event.x, event.y

        # 确保 x1<x2, y1<y2
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)

        w = x2 - x1
        h = y2 - y1

        # 最小选择区域（20×20 像素）
        if w < 20 or h < 20:
            # 选择太小，忽略
            if self.current_rect:
                self.canvas.delete(self.current_rect)
                self.current_rect = None
            self.canvas.itemconfigure(
                self._hint_text,
                text="⚠️ 选择区域太小（至少 20×20 像素）| 请重新选择 | ESC 取消",
                fill="#ff6666"
            )
            self.start_x = None
            self.start_y = None
            return

        # 转换回原始截图坐标
        orig_x1 = int((x1 - self.offset_x) / self.scale) if self.scale else x1
        orig_y1 = int((y1 - self.offset_y) / self.scale) if self.scale else y1
        orig_w = int(w / self.scale) if self.scale else w
        orig_h = int(h / self.scale) if self.scale else h

        self.region = (max(0, orig_x1), max(0, orig_y1), orig_w, orig_h)

        self._cleanup()

    def _on_cancel(self, event=None):
        """ESC 取消"""
        self.region = None
        self._cleanup()

    def _cleanup(self):
        """清理窗口"""
        if self.win:
            try:
                self.win.grab_release()
                self.win.destroy()
            except Exception:
                pass
            self.win = None


# =========================== 翻译确认对话框 ===========================
class TranslateConfirmDialog:
    """
    翻译确认对话框 — 展示OCR识别文字，让用户决定是否翻译

    ┌──────────────────────────────────────┐
    │  🖥 屏幕翻译 - 文字识别结果            │
    ├──────────────────────────────────────┤
    │  语义评分: 85/100  [████████░░] 高     │
    │  检测语言: 中文  | 置信度: 92.3%       │
    ├──────────────────────────────────────┤
    │  识别原文:                            │
    │  ┌────────────────────────────────┐  │
    │  │ 这是从屏幕截图中识别出的       │  │
    │  │ 文字内容。您可以编辑后再翻译。 │  │
    │  │                                │  │
    │  └────────────────────────────────┘  │
    ├──────────────────────────────────────┤
    │  [✏ 编辑后翻译] [🌐 直接翻译] [✕ 取消] │
    └──────────────────────────────────────┘
    """

    def __init__(self, parent, ocr_result, semantic_result):
        """
        Args:
            parent: 父窗口
            ocr_result: OCRProcessor.recognize() 的返回值
            semantic_result: SemanticAnalyzer.analyze() 的返回值
        """
        self.parent = parent
        self.ocr_result = ocr_result
        self.semantic_result = semantic_result
        self.result = None  # "translate" | "edit_translate" | "cancel"
        self.edited_text = None

        self._build_dialog()
        self._wait()

    def _build_dialog(self):
        self.dialog = tk.Toplevel(self.parent)
        self.dialog.title("屏幕翻译 — 确认")
        self.dialog.resizable(True, True)
        self.dialog.minsize(480, 380)
        self.dialog.attributes("-topmost", True)

        # 居中
        w, h = 520, 460
        sw = self.dialog.winfo_screenwidth()
        sh = self.dialog.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.dialog.geometry(f"{w}x{h}+{x}+{y}")

        self.dialog.configure(bg="#f5f6f8")

        # 主容器
        main = tk.Frame(self.dialog, bg="#f5f6f8")
        main.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        # 标题
        title = tk.Label(main, text="🖥 屏幕翻译 — 文字识别结果",
                        font=("Microsoft YaHei", 13, "bold"),
                        bg="#f5f6f8", fg="#1a73e8")
        title.pack(anchor=tk.W, pady=(0, 10))

        # ---- 语义分析信息栏 ----
        info_frame = tk.Frame(main, bg="#ffffff",
                             highlightbackground="#e0e3e8", highlightthickness=1)
        info_frame.pack(fill=tk.X, pady=(0, 8))

        # 语义评分行
        score = self.semantic_result["score"]
        conf = self.semantic_result["confidence"]
        conf_labels = {"high": "✅ 高", "medium": "⚠️ 中", "low": "⚠️ 低", "none": "❌ 极低"}
        conf_colors = {"high": "#34a853", "medium": "#f9ab00", "low": "#ea4335", "none": "#ea4335"}

        score_row = tk.Frame(info_frame, bg="#ffffff")
        score_row.pack(fill=tk.X, padx=12, pady=(10, 2))

        tk.Label(score_row, text=f"语义评分: {score}/100",
                font=("Microsoft YaHei", 10, "bold"),
                bg="#ffffff", fg="#202124").pack(side=tk.LEFT)

        # 进度条
        bar_frame = tk.Frame(score_row, bg="#e0e3e8", width=120, height=12)
        bar_frame.pack(side=tk.LEFT, padx=8)
        bar_frame.pack_propagate(False)
        bar_fill = tk.Frame(bar_frame, bg=conf_colors.get(conf, "#ea4335"),
                           width=int(120 * score / 100), height=12)
        bar_fill.place(x=0, y=0)

        tk.Label(score_row, text=conf_labels.get(conf, "未知"),
                font=("Microsoft YaHei", 10),
                bg="#ffffff", fg=conf_colors.get(conf, "#5f6368")).pack(side=tk.LEFT, padx=4)

        # 第二行信息
        info_row2 = tk.Frame(info_frame, bg="#ffffff")
        info_row2.pack(fill=tk.X, padx=12, pady=(2, 10))

        lang_map = {"zh": "🇨🇳 中文", "en": "🇺🇸 英文", "mixed": "🔀 中英混合", "unknown": "❓ 未知"}
        tk.Label(info_row2, text=f"检测语言: {lang_map.get(self.semantic_result['lang'], '未知')}",
                font=("Microsoft YaHei", 9), bg="#ffffff", fg="#5f6368").pack(side=tk.LEFT)

        tk.Label(info_row2, text=f"OCR置信度: {self.ocr_result.get('confidence', 0)}%",
                font=("Microsoft YaHei", 9), bg="#ffffff", fg="#5f6368").pack(side=tk.LEFT, padx=20)

        tk.Label(info_row2, text=f"预处理: {self.ocr_result.get('preprocess', 'none')}",
                font=("Microsoft YaHei", 9), bg="#ffffff", fg="#5f6368").pack(side=tk.LEFT)

        # ---- 识别原文 ----
        text_label = tk.Label(main, text="📝 识别原文（可编辑）：",
                             font=("Microsoft YaHei", 10, "bold"),
                             bg="#f5f6f8", fg="#202124")
        text_label.pack(anchor=tk.W, pady=(4, 4))

        text_frame = tk.Frame(main, bg="#ffffff",
                             highlightbackground="#e0e3e8", highlightthickness=1)
        text_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.text_box = tk.Text(text_frame, font=("Microsoft YaHei", 11),
                               bg="#ffffff", fg="#202124",
                               wrap=tk.WORD, relief=tk.FLAT,
                               padx=10, pady=8, height=8,
                               insertbackground="#1a73e8")
        text_scroll = tk.Scrollbar(text_frame, command=self.text_box.yview, width=6)
        self.text_box.configure(yscrollcommand=text_scroll.set)
        self.text_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 填入 OCR 文本
        ocr_text = self.ocr_result.get("text", "")
        if ocr_text:
            self.text_box.insert("1.0", ocr_text)
        else:
            self.text_box.insert("1.0", "（未识别到文字）")
        self.text_box.focus_set()

        # ---- 判断理由 ----
        if self.semantic_result.get("reason"):
            reason_text = self.semantic_result["reason"]
            reason_label = tk.Label(main,
                                   text=f"💡 分析: {reason_text}",
                                   font=("Microsoft YaHei", 8),
                                   bg="#f5f6f8", fg="#9aa0a6",
                                   wraplength=480)
            reason_label.pack(anchor=tk.W, pady=(0, 8))

        # ---- 底部按钮 ----
        btn_frame = tk.Frame(main, bg="#f5f6f8")
        btn_frame.pack(fill=tk.X)

        # 编辑后翻译
        edit_btn = tk.Label(btn_frame, text="  ✏ 编辑后翻译  ",
                           font=("Microsoft YaHei", 10, "bold"),
                           bg="#e8f0fe", fg="#1a73e8",
                           cursor="hand2", padx=14, pady=6)
        edit_btn.pack(side=tk.LEFT, padx=(0, 8))
        edit_btn.bind("<Button-1>", lambda e: self._on_edit_translate())
        edit_btn.bind("<Enter>", lambda e: edit_btn.configure(bg="#1a73e8", fg="#ffffff"))
        edit_btn.bind("<Leave>", lambda e: edit_btn.configure(bg="#e8f0fe", fg="#1a73e8"))

        # 直接翻译
        translate_btn = tk.Label(btn_frame, text="  🌐 直接翻译  ",
                                font=("Microsoft YaHei", 10, "bold"),
                                bg="#1a73e8", fg="#ffffff",
                                cursor="hand2", padx=14, pady=6)
        translate_btn.pack(side=tk.LEFT, padx=(0, 8))
        translate_btn.bind("<Button-1>", lambda e: self._on_direct_translate())
        translate_btn.bind("<Enter>", lambda e: translate_btn.configure(bg="#1557b0"))
        translate_btn.bind("<Leave>", lambda e: translate_btn.configure(bg="#1a73e8", fg="#ffffff"))

        # 取消
        cancel_btn = tk.Label(btn_frame, text="  ✕ 取消  ",
                             font=("Microsoft YaHei", 10),
                             bg="#ffffff", fg="#5f6368",
                             cursor="hand2", padx=14, pady=6,
                             highlightbackground="#e0e3e8", highlightthickness=1)
        cancel_btn.pack(side=tk.RIGHT, padx=(0, 8))
        cancel_btn.bind("<Button-1>", lambda e: self._on_cancel())
        cancel_btn.bind("<Enter>", lambda e: cancel_btn.configure(bg="#fce8e6", fg="#ea4335"))
        cancel_btn.bind("<Leave>", lambda e: cancel_btn.configure(bg="#ffffff", fg="#5f6368"))

        # ESC 取消
        self.dialog.bind("<Escape>", lambda e: self._on_cancel())
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _wait(self):
        """模态等待"""
        self.dialog.grab_set()
        self.dialog.focus_force()
        self.dialog.wait_window()

    def _on_edit_translate(self):
        """用户编辑后翻译"""
        self.edited_text = self.text_box.get("1.0", tk.END).strip()
        if not self.edited_text:
            self.result = "cancel"
        elif self.edited_text == "（未识别到文字）":
            self.result = "cancel"
        else:
            self.result = "edit_translate"
        self.dialog.destroy()

    def _on_direct_translate(self):
        """直接翻译"""
        self.result = "translate"
        self.edited_text = self.text_box.get("1.0", tk.END).strip()
        self.dialog.destroy()

    def _on_cancel(self):
        """取消"""
        self.result = "cancel"
        self.edited_text = None
        self.dialog.destroy()

    def get_result(self):
        """返回获取结果 (action, text)"""
        return self.result, self.edited_text


# =========================== 屏幕翻译主控 ===========================
class ScreenTranslator:
    """
    屏幕翻译主控类 — 串联整个流程
    
    流程:
      1. 全屏截图
      2. 区域选择 (RegionSelector)
      3. 图像预处理 + OCR (ImagePreprocessor + OCRProcessor)
      4. 语义分析 (SemanticAnalyzer)
      5. 用户确认 (TranslateConfirmDialog)
      6. 翻译 (TranslateEngine)
    """

    _busy = False  # 类级标志：防止嵌套事件循环期间流程重入（热键连按等）

    def __init__(self, translate_engine, on_result_callback=None, on_cancel_callback=None):
        """
        Args:
            translate_engine: 翻译引擎（需有 translate(text, from_lang, to_lang) 方法）
            on_result_callback: 翻译完成回调 callback(source_text, translated_text, from_lang, to_lang)
            on_cancel_callback: 用户取消回调 callback()
        """
        self.translate_engine = translate_engine
        self.on_result_callback = on_result_callback
        self.on_cancel_callback = on_cancel_callback
        self.ocr = OCRProcessor()
        self.parent = None
        self._ui_queue = queue.Queue()  # 后台线程 → 主线程的 UI 任务队列

    def _post_ui(self, fn):
        """把 UI 操作投递到主线程执行（跨线程安全，代替直接调用 parent.after）"""
        if self.parent is not None:
            self._ui_queue.put(fn)

    def _drain_ui_queue(self):
        """主线程周期性消费 UI 任务队列"""
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception as e:
                    print(f"[screen_translator] UI 任务执行失败: {e}")
        except queue.Empty:
            pass
        if self.parent is not None:
            self.parent.after(50, self._drain_ui_queue)

    def start(self, parent_window):
        """
        启动屏幕翻译流程（在后台线程中执行）
        
        Args:
            parent_window: tkinter 父窗口（用于对话框）
        """
        if ScreenTranslator._busy:
            return  # 流程进行中，防止重入
        ScreenTranslator._busy = True
        self.parent = parent_window
        # 启动主线程 UI 任务消费循环
        parent_window.after(50, self._drain_ui_queue)

        # 检查依赖
        missing = check_all_deps()
        if missing:
            msg = "缺少以下依赖，请安装后再使用屏幕翻译功能：\n\n" + "\n".join(f"  • {m}" for m in missing)
            messagebox.showwarning("依赖缺失", msg)
            ScreenTranslator._busy = False
            return

        # 在主线程中先做区域选择
        self._do_region_select(parent_window)

    def _do_region_select(self, parent):
        """第1步：区域选择"""
        selector = RegionSelector()
        region = selector.select()

        if not region:
            # 用户取消
            selector.screenshot = None
            ScreenTranslator._busy = False
            if self.on_cancel_callback:
                parent.after(0, self.on_cancel_callback)
            return

        # 在后台线程执行 OCR 和后续步骤（复用 selector 的全屏截图，避免二次截屏）
        threading.Thread(
            target=self._process_region, args=(parent, region, selector), daemon=True
        ).start()

    def _process_region(self, parent, region, selector):
        """第2-6步：截取 → OCR → 语义 → 确认 → 翻译（后台线程）"""
        need_restore = True  # 默认需要恢复窗口（除非翻译成功回调中自行恢复）

        try:
            # ---- 2. 获取区域图像 ----
            x, y, w, h = region
            # 优先从选择器复用的全屏截图 crop（省一次全屏抓取）
            if selector.screenshot is not None:
                try:
                    screenshot = selector.screenshot.crop((x, y, x + w, y + h))
                except Exception:
                    screenshot = None
            else:
                screenshot = None
            if screenshot is None:
                screenshot = self._capture_region(x, y, w, h)
            # 截图用完即释放大图（4K 全屏约 24MB）
            selector.screenshot = None
            selector.tk_image = None

            if screenshot is None:
                self._show_error("屏幕截取失败")
                return

            # ---- 3. OCR 识别 ----
            ocr_result = self.ocr.recognize(screenshot, preprocess_method="auto")
            if ocr_result.get("error"):
                self._show_error(f"OCR 识别失败: {ocr_result['error']}")
                return

            ocr_text = ocr_result.get("text", "").strip()
            if not ocr_text:
                # 空结果重试：auto/light 失败时用 heavy 预处理再识别一次
                if ocr_result.get("preprocess") != "heavy":
                    print("[OCR] 首次未识别到文字，用 heavy 预处理重试...")
                    ocr_result = self.ocr.recognize(screenshot, preprocess_method="heavy")
                    ocr_text = ocr_result.get("text", "").strip()
            if not ocr_text:
                self._show_error("未在选定区域中识别到文字，请尝试选择更清晰的区域")
                return

            # ---- 4. 语义分析 ----
            semantic_result = SemanticAnalyzer.analyze(ocr_text)

            # ---- 5. 用户确认（主线程构建对话框，Event 等待结果） ----
            result_holder = {"action": None, "text": None}
            confirm_done = threading.Event()

            def show_confirm():
                try:
                    dialog = TranslateConfirmDialog(parent, ocr_result, semantic_result)
                    action, text = dialog.get_result()
                    result_holder["action"] = action
                    result_holder["text"] = text
                except Exception as e:
                    result_holder["action"] = "error"
                    result_holder["error"] = str(e)
                finally:
                    confirm_done.set()

            self._post_ui(show_confirm)
            # Event 阻塞等待（对话框异常也会 set，不会永久挂起）
            confirm_done.wait()

            if result_holder.get("action") == "error":
                self._show_error(f"确认对话框出错: {result_holder.get('error')}")
                return

            action = result_holder["action"]
            text_to_translate = result_holder["text"]

            if action == "cancel" or not text_to_translate:
                return  # 用户取消，finally 中会恢复窗口

            # ---- 6. 翻译 ----
            from_lang = semantic_result["lang"] if semantic_result["lang"] != "unknown" else "auto"
            # 目标语言：中文→英文，其他→中文
            to_lang = "en" if from_lang == "zh" else "zh"

            translated = self.translate_engine.translate(text_to_translate, from_lang, to_lang)

            # 回调（回调中会自行恢复窗口；回调缺失时由本模块恢复）
            if self.on_result_callback:
                need_restore = False
                self._post_ui(lambda: self.on_result_callback(
                    text_to_translate, translated, from_lang, to_lang
                ))

        except Exception as e:
            self._show_error(f"处理异常: {str(e)}")
        finally:
            # 释放选择器截图引用
            if selector.screenshot is not None:
                selector.screenshot = None
            # 除了翻译成功外的所有情况（错误/无文字/取消）都恢复窗口
            if need_restore and self.on_cancel_callback:
                self._post_ui(self.on_cancel_callback)
            ScreenTranslator._busy = False

    def _capture_region(self, x, y, w, h):
        """截取屏幕指定区域，返回 PIL Image

        注意：x/y/w/h 为相对主屏的物理像素坐标；多显示器虚拟桌面下
        需加上主屏在虚拟桌面中的偏移（monitor left/top）才是绝对坐标。
        """
        try:
            mss = _deps.get("mss") or _try_import("mss")
            if mss:
                with mss.mss() as sct:
                    monitor = sct.monitors[1]
                    off_x, off_y = monitor["left"], monitor["top"]
                    mon = {
                        "left": x + off_x, "top": y + off_y,
                        "width": w, "height": h,
                    }
                    screenshot = sct.grab(mon)
                    from PIL import Image as PILImage
                    return PILImage.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        except Exception:
            pass

        try:
            from PIL import ImageGrab
            # ImageGrab 按虚拟桌面坐标裁剪；Windows 高 DPI 下与物理像素存在
            # 缩放差异（已知限制，mss 不可用时才走此兜底）
            return ImageGrab.grab(bbox=(x, y, x + w, y + h))
        except Exception as e:
            print(f"[截图] 失败: {e}")
            return None

    def _show_error(self, msg):
        """在主线程显示错误"""
        def _show():
            messagebox.showerror("屏幕翻译", msg)
        self._post_ui(_show)


# =========================== 独立测试入口 ===========================
if __name__ == "__main__":
    """
    独立测试屏幕翻译功能
    需要先安装依赖：
      pip install opencv-python pytesseract mss Pillow numpy
    并安装 Tesseract-OCR 程序
    """
    # 模拟翻译引擎
    class MockTranslateEngine:
        @staticmethod
        def translate(text, from_lang, to_lang):
            # 简单的模拟翻译
            if "zh" in from_lang:
                return f"[Mock EN Translation] {text}"
            else:
                return f"[Mock ZH Translation] {text}"

    root = tk.Tk()
    root.title("屏幕翻译 - 测试")
    root.geometry("400x300")
    root.configure(bg="#f5f6f8")

    def on_result(source, result, from_lang, to_lang):
        """翻译结果回调"""
        messagebox.showinfo("翻译完成", f"原文:\n{source}\n\n译文:\n{result}")

    translator = ScreenTranslator(MockTranslateEngine(), on_result_callback=on_result)

    # 检查依赖
    missing = check_all_deps()
    dep_status = "✅ 依赖就绪" if not missing else f"⚠️ 缺少 {len(missing)} 个依赖"

    info = tk.Label(root, text=f"屏幕翻译模块测试\n{dep_status}\n\n点击按钮开始选择屏幕区域",
                   font=("Microsoft YaHei", 11), bg="#f5f6f8", fg="#202124",
                   wraplength=350, justify=tk.CENTER)
    info.pack(pady=40)

    btn = tk.Label(root, text="  🖥 选择屏幕区域翻译  ",
                  font=("Microsoft YaHei", 12, "bold"),
                  bg="#1a73e8", fg="#ffffff",
                  cursor="hand2", padx=20, pady=10)
    btn.pack(pady=10)
    btn.bind("<Button-1>", lambda e: translator.start(root))
    btn.bind("<Enter>", lambda e: btn.configure(bg="#1557b0"))
    btn.bind("<Leave>", lambda e: btn.configure(bg="#1a73e8"))

    # 提示
    hint = tk.Label(root, text="提示：也可从 translator.py 中集成使用",
                   font=("Microsoft YaHei", 8), bg="#f5f6f8", fg="#9aa0a6")
    hint.pack(pady=20)

    root.mainloop()
