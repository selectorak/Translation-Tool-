#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成测试音频，用于验证音频翻译功能"""
import pyttsx3
import os
import time

# 测试短语（英文 + 中文，Whisper 可以识别）
TEST_PHRASES = [
    ("Hello world, this is a test of the audio translation system.", "en"),
    ("The weather is beautiful today and I feel very happy.", "en"),
    ("Artificial intelligence is changing the world rapidly.", "en"),
    ("你好世界，这是音频翻译系统的测试。", "zh"),
    ("今天天气真好，阳光明媚。", "zh"),
]

OUTPUT_PATH = r"D:\人工智能\翻译软件\test_audio.wav"

def generate_test_audio():
    print("正在生成测试音频...")
    engine = pyttsx3.init()
    
    # 获取可用语音
    voices = engine.getProperty('voices')
    print(f"可用语音: {len(voices)} 个")
    for v in voices:
        print(f"  - {v.name} ({v.languages})")
    
    # 保存到 WAV
    engine.save_to_file(" ".join(p for p, _ in TEST_PHRASES), OUTPUT_PATH)
    engine.runAndWait()
    
    size_kb = os.path.getsize(OUTPUT_PATH) / 1024
    print(f"\n[OK] Test audio: {OUTPUT_PATH} ({size_kb:.0f} KB)")
    print(f"\n测试步骤:")
    print(f"  1. 用任意播放器打开播放: {OUTPUT_PATH}")
    print(f"  2. 打开翻译助手，点击「🎙 音频翻译」")
    print(f"  3. 播放测试音频，观察控制台和翻译窗口")
    print(f"\n预期识别内容:")
    for phrase, lang in TEST_PHRASES:
        print(f"  [{lang}] {phrase}")
    
    return OUTPUT_PATH

if __name__ == "__main__":
    path = generate_test_audio()
    
    # 尝试自动播放
    try:
        import subprocess
        print("\n正在自动播放...")
        subprocess.Popen(['start', path], shell=True)
    except Exception:
        pass
    
    input("\n按回车键退出...")
