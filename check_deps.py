#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
依赖检查工具
运行此脚本可诊断所有依赖是否就绪：
  python check_deps.py
"""

import os
import shutil
import sys

print("=" * 55)
print("  翻译软件 - 依赖检查")
print("=" * 55)
print(f"  Python: {sys.executable}")
print(f"  CWD:    {os.getcwd()}")
print("-" * 55)

all_ok = True

# 1. 基础依赖检查（剪贴板翻译 + 在线翻译引擎必需）
base_packages = [
    ("requests", "requests"),
    ("pyperclip", "pyperclip"),
]
# 屏幕翻译依赖（可选）
ocr_packages = [
    ("pytesseract", "pytesseract"),
    ("cv2", "opencv-python"),
    ("PIL", "Pillow"),
    ("mss", "mss"),
    ("numpy", "numpy"),
]

print("  基础依赖:")
for mod_name, pkg_name in base_packages:
    try:
        mod = __import__(mod_name)
        ver = getattr(mod, "__version__", "?")
        print(f"  [OK] {pkg_name:20s} -> {ver}")
    except ImportError:
        print(f"  [!!] {pkg_name:20s} -> NOT INSTALLED  (pip install {pkg_name})")
        all_ok = False

print("-" * 55)
print("  屏幕翻译依赖:")
for mod_name, pkg_name in ocr_packages:
    try:
        mod = __import__(mod_name)
        ver = getattr(mod, "__version__", "?")
        print(f"  [OK] {pkg_name:20s} -> {ver}")
    except ImportError:
        print(f"  [!!] {pkg_name:20s} -> NOT INSTALLED  (pip install -r requirements-ocr.txt)")
        all_ok = False

# 2. Tesseract 系统程序检查（优先 PATH 与注册表，常见安装路径兜底）
print("-" * 55)
print("  查找 Tesseract-OCR 系统程序...")

tesseract_path = shutil.which("tesseract")

if not tesseract_path:
    # 注册表查询（UB-Mannheim 安装器写入的位置）
    try:
        import winreg
        for root, key, value_name in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tesseract-OCR", None),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Tesseract-OCR", None),
        ]:
            try:
                with winreg.OpenKey(root, key) as k:
                    tesseract_path = os.path.join(winreg.QueryValueEx(k, "Path")[0], "tesseract.exe")
                    if os.path.exists(tesseract_path):
                        break
            except OSError:
                continue
    except ImportError:
        pass

if not tesseract_path:
    search_paths = [
        os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expanduser(r"~\AppData\Local\Tesseract-OCR\tesseract.exe"),
    ]
    for p in search_paths:
        if os.path.exists(p):
            tesseract_path = p
            break

if tesseract_path:
    print(f"  [OK] Tesseract -> {tesseract_path}")
else:
    print("  [!!] Tesseract-OCR -> NOT FOUND")
    print("        Download: https://github.com/UB-Mannheim/tesseract/wiki")
    all_ok = False

# 3. Tesseract 语言包检查
if tesseract_path:
    tessdata_dir = os.path.join(os.path.dirname(tesseract_path), "tessdata")
    print("-" * 55)
    print(f"  语言包检查 ({tessdata_dir})")
    for lang, name in [("chi_sim", "Chinese Simplified"), ("eng", "English")]:
        trained = os.path.join(tessdata_dir, f"{lang}.traineddata")
        if os.path.exists(trained):
            size_kb = os.path.getsize(trained) // 1024
            print(f"  [OK] {name:20s} -> {size_kb} KB")
        else:
            print(f"  [!!] {name:20s} -> MISSING")
            all_ok = False

# 4. 功能测试
if all_ok:
    print("-" * 55)
    print("  功能测试...")
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
        os.environ["TESSDATA_PREFIX"] = tessdata_dir

        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (300, 60), "white")
        d = ImageDraw.Draw(img)
        d.text((10, 15), "Test Hello 123", fill="black")
        text = pytesseract.image_to_string(img, lang="eng", config="--psm 6")
        print(f"  [OK] OCR test -> {text.strip()!r}")

        try:
            import cv2, numpy as np
            arr = np.array(img)
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
            print(f"  [OK] OpenCV test -> image shape {arr.shape}")
        except Exception as e:
            print(f"  [WARN] OpenCV test -> {e}")

    except Exception as e:
        print(f"  [!!] Function test failed: {e}")
        all_ok = False

print("=" * 55)
if all_ok:
    print("  ALL CHECKS PASSED - Screen translation ready!")
else:
    print("  Some checks FAILED - see above for details.")
print("=" * 55)

if not all_ok:
    input("\nPress Enter to exit...")
    sys.exit(1)
