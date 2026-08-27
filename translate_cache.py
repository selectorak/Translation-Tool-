#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译结果缓存模块
==================
借鉴 TranslationPlugin 的双层缓存设计：
  1. 内存 LRU（OrderedDict，进程内快速命中）
  2. 磁盘缓存（存储翻译结果 JSON，跨进程/重启复用）

缓存键 = md5(文本 + 源语言 + 目标语言 + 引擎 id + 配置令牌)：
  - 引擎 id：不同引擎的结果互不污染（回退时各自有缓存）
  - 配置令牌：API 密钥等配置变更后旧缓存自动失效
"""

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict

MEMORY_CACHE_SIZE = 1024        # 内存缓存条数上限
DISK_CACHE_MAX_FILES = 512      # 磁盘缓存文件数上限
DISK_CACHE_MAX_AGE_DAYS = 5     # 磁盘缓存最长保留天数（裁剪时参考）


class TranslationCache:
    """双层翻译结果缓存（线程安全）"""

    def __init__(self, cache_dir: str):
        self._dir = cache_dir
        self._mem: OrderedDict = OrderedDict()
        self._lock = threading.RLock()
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            self._dir = None  # 磁盘缓存不可用，退化为仅内存

    @staticmethod
    def make_key(text: str, from_lang: str, to_lang: str,
                 engine_id: str, token: str) -> str:
        """生成缓存键（文本 + 语言 + 引擎 + 配置令牌）"""
        raw = f"{text}|{from_lang}|{to_lang}|{engine_id}|{token}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def get(self, key: str):
        """查缓存（内存 → 磁盘），命中返回结果字符串，未命中返回 None"""
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]

        if self._dir:
            path = os.path.join(self._dir, key + ".json")
            try:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    result = data.get("result", "")
                    with self._lock:
                        self._mem[key] = result
                        self._mem.move_to_end(key)
                        self._trim_memory()
                    return result
            except (OSError, ValueError):
                # 磁盘缓存损坏：删除该文件
                try:
                    os.remove(path)
                except OSError:
                    pass
        return None

    def put(self, key: str, result: str) -> None:
        """写入缓存（内存 + 磁盘）"""
        if result is None:
            return
        with self._lock:
            self._mem[key] = result
            self._mem.move_to_end(key)
            self._trim_memory()

        if self._dir:
            path = os.path.join(self._dir, key + ".json")
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"result": result, "time": time.time()},
                              f, ensure_ascii=False)
                os.replace(tmp, path)
                self._trim_disk()
            except OSError:
                pass

    def clear(self) -> None:
        """清空全部缓存"""
        with self._lock:
            self._mem.clear()
        if self._dir:
            try:
                for f in os.listdir(self._dir):
                    if f.endswith(".json"):
                        os.remove(os.path.join(self._dir, f))
            except OSError:
                pass

    # ---------- 内部 ----------

    def _trim_memory(self):
        while len(self._mem) > MEMORY_CACHE_SIZE:
            self._mem.popitem(last=False)

    def _trim_disk(self):
        """磁盘文件数超限时按修改时间删除最旧（LRU 裁剪）"""
        if self._dir is None:
            return
        try:
            files = [f for f in os.listdir(self._dir) if f.endswith(".json")]
            if len(files) <= DISK_CACHE_MAX_FILES:
                return
            paths = [
                (os.path.join(self._dir, f),
                 os.path.getmtime(os.path.join(self._dir, f)))
                for f in files
            ]
            paths.sort(key=lambda x: x[1])
            for p, _ in paths[:len(files) - DISK_CACHE_MAX_FILES]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except OSError:
            pass
