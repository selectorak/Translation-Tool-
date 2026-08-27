#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音频翻译模块 — WASAPI 音频捕获 + faster-whisper 语音识别 + 实时翻译
特性：
  ★ 流式识别 — 逐句显示原文，不等整段结束
  ★ VAD 过滤 — 跳过静音，只识别有效语音
  ★ 语言锁定 — 首次检测后锁定，避免反复检测
  ★ 上下文提示 — 前文作为 prompt，提高连贯性
  ★ 音频归一化 — RMS 均衡 + 硬限幅
  ★ 模型可选 — tiny/base/small/medium/large-v3，默认 medium（速度与精度平衡）
"""
import tkinter as tk
from tkinter import messagebox
import collections
import threading
import queue
import time
import os
import ctypes
import numpy as np
from ctypes import POINTER, byref, c_uint32, c_ulong
from comtypes import CLSCTX_ALL, GUID, CoInitializeEx

# 兼容性设置
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# 修复 Windows GBK 终端 emoji 输出问题
try:
    import sys as _sys
    _sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# =========================== 配置常量 ===========================
SR = 16000                # Whisper 目标采样率

# ========== 模型选择 ==========
# tiny / base / small / medium / large-v3
#
#   tiny:     最快(GPU实时比≈0.1x), 精度最低, VRAM≈1GB
#   small:    ★推荐(GPU实时比≈0.2x), 较好精度, VRAM≈1GB
#   medium:   平衡(GPU实时比≈0.5x), 精度≈large的95%, VRAM≈3GB
#   large-v3: 最高精度(GPU实时比≈1x), VRAM≈6GB (RTX 4050刚好够)
#
# 启动时自动检测已缓存模型，按优先级回退：large-v3 → medium → small → base → tiny
MODEL = "large-v3"          # 默认 large-v3（最高精度, RTX 4050 6GB 刚好够）

# ========== GPU 加速设置 ==========
FORCE_CPU = False           # False=优先GPU, True=强制CPU（调试用）
GPU_COMPUTE_TYPE = "auto"   # auto=int8_float16(首选) → float16 → int8
# auto 检测流程:
#   1. int8_float16 — 最佳速度/精度平衡（推荐）
#   2. float16       — 最高精度（需要更多VRAM, large-v3可能OOM）
#   3. int8          — 最低VRAM（CPU回退方案）
CPU_THREADS = 4             # CPU模式线程数（<=物理核心数）

# ========== 国内模型下载源 ==========
# 下载优先级: hf-mirror → 官方（modelscope 不支持 faster-whisper）
HF_ENDPOINT = "https://hf-mirror.com"    # HF 国内镜像（推荐）
MODELSCOPE_ENABLED = False               # modelscope 无 faster-whisper 模型

CHUNK_DURATION = 1.5      # 原始音频块时长（秒）
OVERLAP_DURATION = 1.5    # 相邻语音段重叠（秒）— 供 _flush_speech_buffer 使用

# ★ RMS 能量预过滤 — 跳过静音，不给 Whisper 浪费算力
# 关键修复：自适应阈值 + 基于时间而非帧数的静音检测
RMS_SPEECH_THRESHOLD = 0.006   # 降低阈值，捕获轻声细语（原0.008太高）
RMS_SILENCE_SECONDS = 0.8      # ★ 改用秒：连续0.8秒静音才认为句子结束（原3帧≈0.24秒太短）
RMS_NOISE_FLOOR = 0.003        # 噪声地板，低于此值视为纯静音

# ★ 语音累积 — 把短块攒成大段再识别，提高完整性
MAX_SPEECH_DURATION = 15.0     # ★ 加长到15秒（原5秒太短，正常句子5-8秒）
MIN_SPEECH_DURATION = 0.8      # 稍微降低最低门槛（原1.2秒），捕捉短句

SILENCE_TIMEOUT = 8.0     # ★ 延长上下文锁定时间（原4秒太短）
QUEUE_SIZE = 100
TRANSLATE_QUEUE_SIZE = 20  # 翻译队列上限（背压：翻译 API 慢时防止无限积压）
POLL_INTERVAL = 150       # 界面刷新间隔（毫秒）


# =========================== 依赖检查 ===========================
def check_audio_deps():
    """检查音频翻译所需依赖，返回缺失列表"""
    missing = []
    for mod, pkg in [("faster_whisper", "faster-whisper"), ("numpy", "numpy")]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(f"pip install {pkg}")
    try:
        from pycaw.pycaw import AudioUtilities  # noqa: F401
    except ImportError:
        missing.append("pip install pycaw comtypes")
    return missing


# =========================== 音频捕获 ===========================
class AudioCapture:
    """WASAPI 环回捕获系统音频输出"""

    def __init__(self, callback):
        self.callback = callback
        self._run = False
        self._device_name = ""
        self._thread = None
        self._client = None
        self._capture = None

    def start(self):
        """启动音频捕获，返回 (成功标志, 设备名称)"""
        CoInitializeEx(0)
        self._run = True
        try:
            from pycaw.pycaw import (
                AudioUtilities, IAudioClient,
                WAVEFORMATEX, PROPERTYKEY
            )
            dev = AudioUtilities.GetDeviceEnumerator().GetDefaultAudioEndpoint(0, 0)
            try:
                pk = PROPERTYKEY()
                pk.fmtid = GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}")
                pk.pid = 14
                self._device_name = str(
                    dev.OpenPropertyStore(0).GetValue(pk).GetValue()
                )
            except Exception:
                self._device_name = "系统音频"

            cl = dev.Activate(
                GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"),
                CLSCTX_ALL, None
            ).QueryInterface(IAudioClient)

            wf = ctypes.cast(cl.GetMixFormat(), POINTER(WAVEFORMATEX)).contents
            self._channels = wf.nChannels
            self._device_sr = wf.nSamplesPerSec
            cl.Initialize(0, 0x00020000, ctypes.c_int64(1000000), 0, byref(wf), None)

            cp = cl.GetService(GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"))
            vt = ctypes.cast(cp, POINTER(POINTER(ctypes.c_void_p))).contents

            GB = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p,
                POINTER(POINTER(ctypes.c_ubyte)), POINTER(c_uint32),
                POINTER(c_ulong), ctypes.c_uint64, ctypes.c_uint64
            )
            RB = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, c_uint32)
            NP = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, POINTER(c_uint32)
            )

            self._gb = GB(ctypes.cast(vt[3], ctypes.c_void_p).value)
            self._rb = RB(ctypes.cast(vt[4], ctypes.c_void_p).value)
            self._np = NP(ctypes.cast(vt[5], ctypes.c_void_p).value)
            self._capture = cp
            self._client = cl

            cl.Start()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

            print(f"[音频捕获] {self._device_name} "
                  f"{self._channels}声道 {self._device_sr}Hz")
            return True, self._device_name
        except Exception as e:
            self._run = False
            import traceback
            traceback.print_exc()
            return False, str(e)

    def _loop(self):
        # 局部捕获 COM 回调与接口引用，stop() 置 None 不影响在途循环
        np_fn, gb_fn, rb_fn = self._np, self._gb, self._rb
        capture = self._capture
        ch = self._channels
        sr = self._device_sr
        buf = np.array([], dtype=np.float32)
        first = True
        ratio = SR / sr
        while self._run:
            try:
                psz = c_uint32()
                np_fn(capture, byref(psz))
                if psz.value == 0:
                    time.sleep(0.005)
                    continue
                dp = POINTER(ctypes.c_ubyte)()
                fr = c_uint32()
                fl = c_ulong()
                gb_fn(capture, byref(dp), byref(fr), byref(fl), 0, 0)
                if fr.value > 0:
                    raw = (ctypes.c_float * (fr.value * ch)).from_address(
                        ctypes.addressof(dp.contents)
                    )
                    arr = np.ctypeslib.as_array(raw).reshape(-1, ch)
                    mono = np.mean(arr, axis=1).astype(np.float32)
                    if first:
                        print(f"[音频捕获] 振幅={np.max(np.abs(mono)):.4f}")
                        first = False
                    buf = np.concatenate([buf, mono])
                    while len(buf) >= int(sr * 0.03):
                        chunk = buf[:int(sr * 0.08)]
                        buf = buf[len(chunk) // 2:]
                        out_len = max(1, int(len(chunk) * ratio))
                        idx = np.linspace(0, len(chunk) - 1, out_len)
                        resampled = np.interp(
                            idx, np.arange(len(chunk)), chunk
                        ).astype(np.float32)
                        if self.callback:
                            self.callback(resampled)
                rb_fn(capture, fr.value)
            except Exception as e:
                if self._run:
                    print(f"[音频捕获] 错误: {e}")
                time.sleep(0.01)

    def stop(self):
        """停止音频捕获 — 非阻塞版本
        ★ 先 Stop COM 客户端（解除捕获循环的阻塞），再设标志位。
        不 join 线程（它是 daemon，进程退出时自动清理），避免卡 UI。"""
        try:
            if self._client:
                self._client.Stop()  # ★ 先停 COM，解除 _loop 阻塞
        except Exception:
            pass
        self._run = False
        # 显式释放 COM 引用（_loop 已局部捕获，在途循环不受影响）
        self._capture = None
        self._client = None
        self._gb = None
        self._rb = None
        self._np = None

    @property
    def device_name(self):
        return self._device_name


# =========================== 语音识别（升级版） ===========================

# =========================== 模型管理（国内源下载 + 完整缓存检测） ===========================

# HF 缓存目录名映射
_MODEL_CACHE_MAP = {
    "tiny":     "models--Systran--faster-whisper-tiny",
    "base":     "models--Systran--faster-whisper-base",
    "small":    "models--Systran--faster-whisper-small",
    "medium":   "models--Systran--faster-whisper-medium",
    "large-v3": "models--Systran--faster-whisper-large-v3",
}

# 模型显示名称（中文）
_MODEL_DISPLAY_NAMES = {
    "tiny":     "🤏 Tiny（极速·低精度）",
    "base":     "📦 Base（快速·基础精度）",
    "small":    "⭐ Small（推荐·较好精度）",
    "medium":   "⚖️ Medium（平衡·高精度）",
    "large-v3": "🚀 Large-v3（最高精度）",
}

# 模型详细信息
_MODEL_INFO = {
    "tiny":     {"size": "~75MB", "vram": "~1GB", "speed": "实时比≈0.1x", "desc": "极速识别，适合低配设备"},
    "base":     {"size": "~145MB", "vram": "~1GB", "speed": "实时比≈0.15x", "desc": "快速识别，基础精度"},
    "small":    {"size": "~480MB", "vram": "~1GB", "speed": "实时比≈0.2x", "desc": "速度与精度平衡，推荐日常使用"},
    "medium":   {"size": "~1.5GB", "vram": "~3GB", "speed": "实时比≈0.5x", "desc": "精度≈large的95%，GPU友好"},
    "large-v3": {"size": "~2.9GB", "vram": "~6GB", "speed": "实时比≈1x", "desc": "最高精度，需6GB+显存(RTX 4050+)"},
}

# modelscope 模型 ID 映射（国内源，无需翻墙）
_MODELSCOPE_MAP = {
    "tiny":     "keepitsimple/faster-whisper-tiny",
    "base":     "keepitsimple/faster-whisper-base",
    "small":    "keepitsimple/faster-whisper-small",
    "medium":   "keepitsimple/faster-whisper-medium",
    "large-v3": "keepitsimple/faster-whisper-large-v3",
}

# 模型大小参考
_MODEL_SIZES = {
    "tiny": "~75MB", "base": "~145MB", "small": "~480MB",
    "medium": "~1.5GB", "large-v3": "~2.9GB",
}


def _get_cache_root():
    """获取 HF 缓存根目录"""
    return os.path.join(
        os.environ.get("HF_HOME",
                       os.path.join(os.path.expanduser("~"),
                                    ".cache", "huggingface")),
        "hub"
    )


def _verify_model_complete(snapshot_dir):
    """验证模型文件是否完整（检查 model.bin 是否存在且 >1MB）"""
    if not os.path.isdir(snapshot_dir):
        return False
    model_bin = os.path.join(snapshot_dir, "model.bin")
    if os.path.isfile(model_bin) and os.path.getsize(model_bin) > 1_000_000:
        return True
    for f in os.listdir(snapshot_dir):
        if f.endswith((".bin", ".safetensors", ".pt")):
            fpath = os.path.join(snapshot_dir, f)
            if os.path.getsize(fpath) > 1_000_000:
                return True
    return False


def list_cached_models():
    """扫描所有已下载且完整的模型，返回 [(模型名, 显示名, 详情字典), ...]

    可在 UI 中调用，展示可选模型列表。
    """
    cache_root = _get_cache_root()
    available = []
    for name in ["tiny", "base", "small", "medium", "large-v3"]:
        mdir = _MODEL_CACHE_MAP.get(name)
        mroot = os.path.join(cache_root, mdir) if mdir else None
        if mroot and os.path.isdir(mroot):
            sdir = os.path.join(mroot, "snapshots")
            if os.path.isdir(sdir):
                for entry in sorted(os.listdir(sdir), reverse=True):
                    full = os.path.join(sdir, entry)
                    if os.path.isdir(full) and _verify_model_complete(full):
                        available.append((
                            name,
                            _MODEL_DISPLAY_NAMES.get(name, name),
                            _MODEL_INFO.get(name, {})
                        ))
                        break
    return available


def _find_model_path(model_name):
    """自动查找模型路径：环境变量 > HF缓存(验证完整性) > 按优先级回退

    回退顺序: 指定模型 → large-v3 → medium → small → base → tiny
    如果都没有，自动触发国内源下载
    """
    env_key = "WHISPER_MODEL_PATH"
    if os.environ.get(env_key):
        env_path = os.environ[env_key]
        if _verify_model_complete(env_path):
            print(f"[模型] 使用环境变量路径: {env_path}")
            return env_path
        print(f"[模型] ⚠️ 环境变量路径不完整: {env_path}")

    cache_root = _get_cache_root()

    def _try_find(name):
        """在HF缓存中查找指定模型的完整快照"""
        mdir = _MODEL_CACHE_MAP.get(
            name, f"models--Systran--faster-whisper-{name}"
        )
        mroot = os.path.join(cache_root, mdir)
        if os.path.isdir(mroot):
            sdir = os.path.join(mroot, "snapshots")
            if os.path.isdir(sdir):
                for entry in sorted(os.listdir(sdir), reverse=True):
                    full = os.path.join(sdir, entry)
                    if os.path.isdir(full) and _verify_model_complete(full):
                        return full, name
                return "incomplete", name
        return None, None

    # 按优先级尝试已缓存模型
    fallback_order = [model_name, "large-v3", "medium", "small", "base", "tiny"]
    tried = set()
    incomplete_models = []

    for name in fallback_order:
        if name in tried:
            continue
        tried.add(name)
        path, found_name = _try_find(name)
        if path == "incomplete":
            incomplete_models.append(found_name)
            print(f"[模型] ⚠️ 「{found_name}」缓存不完整，需重新下载")
            continue
        if path:
            if name != model_name:
                print(f"[模型] 「{model_name}」未缓存，回退到「{found_name}」(已验证完整)")
            else:
                print(f"[模型] ✅ 已缓存: {found_name}")
            return path

    # 没有完整模型 → 触发国内源下载
    target = incomplete_models[0] if incomplete_models else model_name
    if incomplete_models:
        print(f"[模型] 修复不完整的缓存: {target}")
    print(f"[模型] 未找到本地模型，开始下载: {target} ({_MODEL_SIZES.get(target, '?')})")
    return _download_model_smart(target)


def _download_model_smart(model_name="small"):
    """智能下载：优先 modelscope（国内最稳），其次 hf-mirror，最后官方源

    返回模型路径，失败返回 None
    """
    # ---- 方法1: modelscope（推荐国内用户）----
    if MODELSCOPE_ENABLED:
        path = _download_via_modelscope(model_name)
        if path and _verify_model_complete(path):
            print(f"[下载] ✅ modelscope 下载成功: {model_name}")
            return path
        print("[下载] modelscope 失败，尝试 HF 镜像...")

    # ---- 方法2: hf-mirror.com ----
    os.environ.setdefault("HF_ENDPOINT", HF_ENDPOINT)
    try:
        from faster_whisper import WhisperModel
        print(f"[下载] HF镜像下载 {model_name} ({_MODEL_SIZES.get(model_name, '?')})...")
        print(f"       端点: {os.environ.get('HF_ENDPOINT', '官方')}")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        path = _find_cached_snapshot(model_name)
        if path:
            print(f"[下载] ✅ {model_name} 下载完成")
            del model
            return path
    except Exception as e:
        print(f"[下载] HF 镜像失败: {e}")

    # ---- 方法3: 官方源（最后手段）----
    try:
        os.environ.pop("HF_ENDPOINT", None)
        from faster_whisper import WhisperModel
        print(f"[下载] 尝试官方源（可能较慢）...")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        path = _find_cached_snapshot(model_name)
        if path:
            print(f"[下载] ✅ 官方源下载成功")
            del model
            return path
    except Exception as e:
        print(f"[下载] 官方源失败: {e}")

    return None


def _find_cached_snapshot(model_name):
    """下载后重新查找模型路径"""
    cache_root = _get_cache_root()
    mdir = _MODEL_CACHE_MAP.get(
        model_name, f"models--Systran--faster-whisper-{model_name}"
    )
    mroot = os.path.join(cache_root, mdir)
    if os.path.isdir(mroot):
        sdir = os.path.join(mroot, "snapshots")
        if os.path.isdir(sdir):
            for entry in sorted(os.listdir(sdir), reverse=True):
                full = os.path.join(sdir, entry)
                if os.path.isdir(full) and _verify_model_complete(full):
                    return full
    return None


def _download_via_modelscope(model_name):
    """通过 modelscope（魔搭社区）下载模型到 HF 缓存目录"""
    try:
        from modelscope import snapshot_download
    except ImportError:
        print("[modelscope] 未安装，请运行: pip install modelscope")
        return None

    ms_id = _MODELSCOPE_MAP.get(model_name)
    if not ms_id:
        print(f"[modelscope] 不支持的模型: {model_name}")
        return None

    try:
        print(f"[modelscope] 下载 {ms_id} ({_MODEL_SIZES.get(model_name, '?')})...")
        local_dir = snapshot_download(ms_id, cache_dir=None)
        print(f"[modelscope] 下载到: {local_dir}")

        # 构造 HF 兼容的缓存目录结构
        cache_root = _get_cache_root()
        mdir_name = _MODEL_CACHE_MAP.get(
            model_name, f"models--Systran--faster-whisper-{model_name}"
        )
        snapshot_dir = os.path.join(cache_root, mdir_name, "snapshots", "modelscope")
        os.makedirs(snapshot_dir, exist_ok=True)

        import shutil
        for fname in os.listdir(local_dir):
            src = os.path.join(local_dir, fname)
            dst = os.path.join(snapshot_dir, fname)
            if os.path.isfile(src) and not os.path.exists(dst):
                try:
                    os.link(src, dst)
                except (OSError, NotImplementedError):
                    shutil.copy2(src, dst)

        if _verify_model_complete(snapshot_dir):
            print(f"[modelscope] ✅ 模型已就绪: {snapshot_dir}")
            return snapshot_dir

        print("[modelscope] 模型文件不完整")
        return None
    except Exception as e:
        print(f"[modelscope] 下载失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def download_model(model_name="small"):
    """
    通过国内源下载 Whisper 模型（命令行工具）

    用法:
        python -c "from audio_translator import download_model; download_model('small')"
        python -c "from audio_translator import download_model; download_model('large-v3')"

    下载优先级: modelscope → hf-mirror → huggingface 官方
    """
    print(f"\n{'='*55}")
    print(f"  Whisper 模型下载（国内源加速）")
    print(f"{'='*55}")
    print(f"  目标: {model_name} ({_MODEL_SIZES.get(model_name, '?')})")
    print(f"  顺序: modelscope → hf-mirror → 官方")
    print(f"{'='*55}\n")

    path = _download_model_smart(model_name)

    if path and _verify_model_complete(path):
        print(f"\n{'='*55}")
        print(f"  ✅ 下载成功!")
        print(f"  路径: {path}")
        print(f"{'='*55}\n")
        return True
    else:
        print(f"\n{'='*55}")
        print(f"  ❌ 下载失败")
        print(f"  请检查网络，或手动安装 modelscope:")
        print(f"    pip install modelscope")
        print(f"{'='*55}\n")
        return False


class ASRProcessor:
    """faster-whisper 语音识别处理器（升级版）
    ★ 流式输出  ★ VAD过滤  ★ 语言锁定  ★ 上下文提示  ★ 音频归一化
    """

    def __init__(self, model_name=None):
        self._model = None
        self._device = "cpu"
        self._model_name = model_name or MODEL
        # 语言锁定
        self._locked_lang = None
        self._last_speech_time = 0
        # 上下文记忆
        self._context = ""

    # ========== 模型加载 ==========

    def load(self):
        """加载 Whisper 模型并启用 GPU 加速（自动检测最佳配置）

        GPU 推理流程（自动回退）:
          1. float16  — 最高精度（large-v3 需 6GB VRAM）
          2. int8_float16 — 最佳速度/精度平衡 ★推荐
          3. int8  — CPU 回退方案
        如果全部失败 → 自动下载模型后重试
        """
        try:
            from faster_whisper import WhisperModel
            import ctranslate2

            # 查找/下载模型
            model_path = _find_model_path(self._model_name)
            if not model_path or not _verify_model_complete(model_path):
                print(f"[语音识别] 模型路径无效: {model_path}")
                print(f"[语音识别] 尝试下载 {self._model_name}...")
                model_path = _download_model_smart(self._model_name)
                if not model_path:
                    print("[语音识别] ❌ 模型下载失败")
                    return False

            print(f"[语音识别] 模型={self._model_name}  路径={model_path}")

            # ======= GPU 加速检测 =======
            if not FORCE_CPU:
                gpu_count = self._detect_gpu_count()

                if gpu_count > 0:
                    # ★ 预检 cublas DLL 是否存在
                    if self._check_cublas_available():
                        result = self._try_load_gpu(WhisperModel, model_path, gpu_count)
                        if result:
                            return True
                        print("[语音识别] GPU 全部失败，回退到 CPU...")
                    else:
                        print("[语音识别] ⚠️ cublas DLL 缺失，跳过 GPU，回退 CPU")
                        print("[语音识别] 如需 GPU 加速请安装 CUDA Toolkit:")
                        print("           https://developer.nvidia.com/cuda-downloads")
                else:
                    print("[语音识别] 未检测到 CUDA 设备，使用 CPU")

            # ======= CPU 回退 =======
            return self._load_cpu(WhisperModel, model_path)

        except Exception as e:
            print(f"[语音识别] ❌ 加载失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _detect_gpu_count(self):
        """检测可用 GPU 数量"""
        try:
            import ctranslate2
            n = ctranslate2.get_cuda_device_count()
            if n > 0:
                print(f"[GPU] 检测到 {n} 个 CUDA 设备")
            return n
        except Exception as e:
            print(f"[GPU] 检测失败: {e}")
            return 0

    def _check_cublas_available(self):
        """★ 预检 cublas DLL 是否可用，避免加载时崩溃"""
        import ctypes
        dll_names = [
            "cublas64_12.dll",
            "cublas64_11.dll",
            "cublas64_10.dll",
            "cublas.dll",
        ]
        for name in dll_names:
            try:
                ctypes.CDLL(name)
                print(f"[GPU] ✅ 找到 {name}")
                return True
            except OSError:
                continue
        # 也检查 PATH 中是否有 CUDA
        import shutil
        for exe in ["nvcc.exe", "nvidia-smi.exe"]:
            if shutil.which(exe):
                print(f"[GPU] 找到 {exe}，但 cublas DLL 缺失")
                break
        return False

    def _try_load_gpu(self, WhisperModel, model_path, gpu_count):
        """尝试 GPU 加载，按优先级回退 compute_type"""
        # 根据配置决定尝试顺序
        if GPU_COMPUTE_TYPE == "auto":
            compute_types = ["int8_float16", "float16", "int8"]
        elif GPU_COMPUTE_TYPE == "float16":
            compute_types = ["float16", "int8_float16", "int8"]
        elif GPU_COMPUTE_TYPE == "int8_float16":
            compute_types = ["int8_float16", "float16", "int8"]
        else:
            compute_types = [GPU_COMPUTE_TYPE]

        cuda_errors = {
            "cublas64_12.dll": "缺少 CUDA 运行时 DLL，请安装 CUDA Toolkit 12.x 或更新 NVIDIA 驱动",
            "cublas": "cuBLAS 库错误，请检查 CUDA 安装",
            "out of memory": "GPU 显存不足，请换用小模型（如 small）或使用 CPU",
            "no cuda": "未检测到 CUDA 支持",
        }

        for ct in compute_types:
            try:
                t0 = time.time()
                print(f"[GPU] 尝试 device=cuda compute_type={ct} ...")
                self._model = WhisperModel(
                    model_path, device="cuda", compute_type=ct
                )
                elapsed = time.time() - t0
                self._device = "cuda"
                self._compute_type = ct
                print(f"[GPU] ✅ 加载成功! compute_type={ct}  耗时 {elapsed:.1f}s")
                self._warmup_gpu()
                return True

            except Exception as e:
                err_str = str(e).lower()
                hint = ""
                for keyword, msg in cuda_errors.items():
                    if keyword.lower() in err_str:
                        hint = f"  → {msg}"
                        break
                print(f"[GPU] {ct} 失败: {e}{hint}")

                # 清理失败模型引用
                self._model = None
                import gc
                gc.collect()

        return False

    def _load_cpu(self, WhisperModel, model_path):
        """CPU 模式加载"""
        try:
            threads = CPU_THREADS or min(8, os.cpu_count() or 4)
            print(f"[CPU] 加载中... compute_type=int8 threads={threads}")
            t0 = time.time()
            self._model = WhisperModel(
                model_path, device="cpu", compute_type="int8",
                cpu_threads=threads
            )
            elapsed = time.time() - t0
            self._device = "cpu"
            self._compute_type = "int8"
            print(f"[CPU] ✅ 加载成功! 耗时 {elapsed:.1f}s")
            return True
        except Exception as e:
            print(f"[CPU] ❌ 加载失败: {e}")
            return False

    def _warmup_gpu(self):
        """GPU 预热 — 减少首次推理延迟"""
        try:
            import numpy as np
            dummy = np.zeros(int(SR * 1.0), dtype=np.float32)
            t0 = time.time()
            list(self._model.transcribe(
                dummy, language="en", beam_size=1,
                without_timestamps=True,
                vad_filter=True,
                vad_parameters=dict(
                    threshold=0.5,
                    min_speech_duration_ms=100,
                    min_silence_duration_ms=50,
                ),
            )[0])
            elapsed = time.time() - t0
            print(f"[GPU] 预热完成 ({elapsed:.1f}s)")
        except Exception as e:
            print(f"[GPU] 预热跳过: {e}")

    def reload_cpu(self):
        """运行时强制切换到 CPU 模式"""
        from faster_whisper import WhisperModel
        model_path = _find_model_path(self._model_name)
        if not model_path:
            model_path = _download_model_smart(self._model_name)
        if model_path:
            self._model = None
            import gc
            gc.collect()
            return self._load_cpu(WhisperModel, model_path)
        return False

    @property
    def compute_type(self):
        """返回当前使用的 compute_type"""
        return getattr(self, '_compute_type', 'unknown')

    @property
    def device(self):
        return self._device

    @property
    def model_name(self):
        return self._model_name

    # ========== 音频预处理 ==========

    @staticmethod
    def _preprocess(audio):
        """音频预处理：静音裁剪 + RMS 归一化 + 峰值限幅
        ★ 新增：自动裁剪首尾静音，让 Whisper 获得干净的句子边界
        """
        audio = np.asarray(audio, dtype=np.float32)
        audio = np.nan_to_num(audio, nan=0.0, posinf=1.0, neginf=-1.0)

        # ★ 静音裁剪：去除首尾静音段（基于能量阈值）
        energy = np.abs(audio)
        threshold = 0.005  # 能量阈值
        mask = energy > threshold
        if np.any(mask):
            start = np.argmax(mask)
            end = len(mask) - np.argmax(mask[::-1])
            # 保留前后各 0.1 秒缓冲
            margin = int(0.1 * SR)
            start = max(0, start - margin)
            end = min(len(audio), end + margin)
            audio = audio[start:end]

        # RMS 归一化
        rms = np.sqrt(np.mean(audio ** 2))
        if rms > 1e-6:
            target_rms = 0.25  # 稍微降低（原0.3），避免过爆
            audio = audio * (target_rms / max(rms, target_rms))
        audio = np.clip(audio, -1.0, 1.0)
        return audio

    # ========== 流式转写 ==========

    def transcribe(self, audio, stream_callback=None):
        """
        流式转写。每识别出一句就调用 stream_callback(text, is_final)
        返回 {"text": full_text, "lang": detected_lang}

        ★ 关键修复：
          - beam_size 1→5（大幅提升精度）
          - best_of=5（多轮采样取最佳）
          - condition_on_previous_text=True（上下文连贯）
          - VAD 静音窗口 100→350ms（减少句子被切碎）
        """
        audio = self._preprocess(audio)

        # 丢弃过短的音频（<0.3秒基本是噪声）
        if len(audio) < int(0.3 * SR):
            return {"text": "", "lang": "unknown"}

        kwargs = dict(
            beam_size=5,                    # ★ 1→5：波束搜索大幅提升精度
            best_of=5,                      # ★ 新增：5轮采样取最佳
            condition_on_previous_text=True, # ★ 新增：利用前文提升连贯性
            compression_ratio_threshold=2.4, # ★ 新增：过滤乱码输出
            log_prob_threshold=-1.0,         # ★ 新增：过滤低置信度
            no_speech_threshold=0.6,         # ★ 新增：更好的静音检测
            vad_filter=True,                 # ★ 开启 VAD（静音窗口加大）
            vad_parameters=dict(
                threshold=0.5,
                min_speech_duration_ms=300,    # ★ 250→300：过滤更短噪声
                min_silence_duration_ms=350,   # ★ 100→350：关键！更长的静音才分割句子
                speech_pad_ms=400,             # ★ 新增：语音前后各留400ms缓冲
            ),
            without_timestamps=True,    # 保持关闭以提速
        )

        # 语言锁定
        now = time.time()
        if self._locked_lang and (now - self._last_speech_time < SILENCE_TIMEOUT):
            kwargs["language"] = self._locked_lang

        # ★ 上下文提示：扩展到 400 字符（原200），让 Whisper 有更多上下文
        if self._context.strip():
            kwargs["initial_prompt"] = self._context[-400:]

        segments, info = self._model.transcribe(audio, **kwargs)

        detected_lang = info.language
        if detected_lang and detected_lang != "nn":
            self._locked_lang = detected_lang
            self._last_speech_time = now

        # ★ 流式：逐句回调
        parts = []
        for seg in segments:
            t = seg.text.strip()
            if t:
                parts.append(t)
                if stream_callback:
                    stream_callback(" ".join(parts), False)

        full_text = " ".join(parts)

        if full_text.strip():
            self._context = (self._context + " " + full_text)[-600:]  # ★ 保留更长上下文
            if stream_callback:
                stream_callback(full_text, True)

        return {"text": full_text, "lang": detected_lang}

    def reset_context(self):
        """重置上下文和语言锁定（话题切换 / 清空时调用）"""
        self._context = ""
        self._locked_lang = None
        self._last_speech_time = 0


# =========================== 音频翻译窗口（中文界面） ===========================
class AudioWindow(tk.Toplevel):
    """音频翻译独立窗口 — 全中文界面，支持流式实时更新"""

    def __init__(self, master, on_close=None, on_model_change=None,
                 available_models=None, current_model=None):
        super().__init__(master)
        self._on_close = on_close
        self._on_model_change = on_model_change
        self._paused = False
        self._segment_count = 0
        self._recording_start = time.time()
        self._model_selector_var = None  # tk.StringVar 用于下拉框
        self._available_models = available_models or []  # [(name, display, info), ...]
        self._current_model = current_model or "large-v3"
        self._model_switching = False  # 模型切换锁，防止重复触发

        self.configure(bg="#f0f2f5")
        self.title("🎙 音频翻译")
        self.resizable(True, True)
        self.minsize(520, 420)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda e: self._close())

        self._build_ui()
        self._position_window()

    def _build_ui(self):
        """构建中文界面"""
        main = tk.Frame(self, bg="#f0f2f5")
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        # —— 顶部标题栏（双行：标题+模型选择） ——
        header = tk.Frame(main, bg="#f0f2f5", height=56)
        header.pack(fill=tk.X, pady=(0, 4))
        header.pack_propagate(False)

        # 第一行：标题 + 状态灯 + 音量 + 计时
        row1 = tk.Frame(header, bg="#f0f2f5")
        row1.pack(fill=tk.X)

        tk.Label(
            row1, text="🎙 音频翻译",
            font=("Microsoft YaHei", 13, "bold"),
            bg="#f0f2f5", fg="#1a73e8"
        ).pack(side=tk.LEFT)

        # 状态指示灯
        sf = tk.Frame(row1, bg="#f0f2f5")
        sf.pack(side=tk.RIGHT)
        self._status_canvas = tk.Canvas(
            sf, width=10, height=10, bg="#f0f2f5", highlightthickness=0
        )
        self._status_canvas.pack(side=tk.LEFT, padx=(0, 4))
        self._status_dot = self._status_canvas.create_oval(
            1, 1, 9, 9, fill="#ea4335", outline=""
        )
        self._status_label = tk.Label(
            sf, text="初始化中...",
            font=("Microsoft YaHei", 9), bg="#f0f2f5", fg="#ea4335"
        )
        self._status_label.pack(side=tk.LEFT)

        # 音量电平表
        self._level_canvas = tk.Canvas(
            row1, width=40, height=10,
            bg="#f0f2f5", highlightthickness=1,
            highlightbackground="#d0d5dd"
        )
        self._level_canvas.pack(side=tk.RIGHT, padx=6)
        self._level_bar = self._level_canvas.create_rectangle(
            1, 1, 1, 9, fill="#34a853", outline=""
        )

        # 计时
        self._time_label = tk.Label(
            row1, text="00:00",
            font=("Consolas", 9), bg="#f0f2f5", fg="#5f6368"
        )
        self._time_label.pack(side=tk.RIGHT, padx=(0, 4))

        # 第二行：模型选择器
        row2 = tk.Frame(header, bg="#f0f2f5")
        row2.pack(fill=tk.X, pady=(3, 0))

        tk.Label(
            row2, text="🧠 模型:",
            font=("Microsoft YaHei", 9),
            bg="#f0f2f5", fg="#5f6368"
        ).pack(side=tk.LEFT, padx=(0, 4))

        # 模型详情提示（必须在 _build_model_selector 之前创建，因为后者会调用 _show_model_info）
        self._model_info_label = tk.Label(
            row2, text="",
            font=("Microsoft YaHei", 8),
            bg="#f0f2f5", fg="#80868b"
        )
        self._model_info_label.pack(side=tk.LEFT, padx=(6, 0))

        # 构建模型选择下拉框
        self._build_model_selector(row2)

        # —— 识别原文区域 ——
        tk.Label(
            main, text="📝 识别原文（流式）",
            font=("Microsoft YaHei", 10, "bold"),
            bg="#f0f2f5", fg="#202124"
        ).pack(anchor=tk.W, pady=(0, 2))

        of = tk.Frame(
            main, bg="#ffffff",
            highlightbackground="#dadce0", highlightthickness=1
        )
        of.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        self._orig_text = tk.Text(
            of, font=("Microsoft YaHei", 12),
            bg="#ffffff", fg="#202124",
            wrap=tk.WORD, state=tk.DISABLED,
            relief=tk.FLAT, padx=10, pady=8, height=6
        )
        os_ = tk.Scrollbar(of, command=self._orig_text.yview, width=6)
        self._orig_text.configure(yscrollcommand=os_.set)
        self._orig_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        os_.pack(side=tk.RIGHT, fill=tk.Y)

        # —— 翻译结果区域 ——
        th = tk.Frame(main, bg="#f0f2f5")
        th.pack(fill=tk.X, pady=(4, 2))
        tk.Label(
            th, text="🌐 翻译结果",
            font=("Microsoft YaHei", 10, "bold"),
            bg="#f0f2f5", fg="#1a73e8"
        ).pack(side=tk.LEFT)
        self._spinner_label = tk.Label(
            th, text="",
            font=("Microsoft YaHei", 9), bg="#f0f2f5", fg="#f9ab00"
        )
        self._spinner_label.pack(side=tk.RIGHT)

        tf = tk.Frame(
            main, bg="#e8f0fe",
            highlightbackground="#1a73e8", highlightthickness=1
        )
        tf.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        self._trans_text = tk.Text(
            tf, font=("Microsoft YaHei", 12, "bold"),
            bg="#e8f0fe", fg="#1a73e8",
            wrap=tk.WORD, state=tk.DISABLED,
            relief=tk.FLAT, padx=10, pady=8, height=5
        )
        ts = tk.Scrollbar(tf, command=self._trans_text.yview, width=6)
        self._trans_text.configure(yscrollcommand=ts.set)
        self._trans_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ts.pack(side=tk.RIGHT, fill=tk.Y)

        # —— 底部操作栏 ——
        bottom = tk.Frame(main, bg="#f0f2f5", height=32)
        bottom.pack(fill=tk.X, pady=(2, 0))
        bottom.pack_propagate(False)

        self._pause_btn = tk.Label(
            bottom, text="⏸ 暂停",
            font=("Microsoft YaHei", 9),
            bg="#fff3cd", fg="#856404",
            cursor="hand2", padx=12, pady=3
        )
        self._pause_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._pause_btn.bind("<Button-1>", lambda e: self._toggle_pause())

        cb = tk.Label(
            bottom, text="🗑 清空",
            font=("Microsoft YaHei", 9),
            bg="#fce8e6", fg="#c5221f",
            cursor="hand2", padx=12, pady=3
        )
        cb.pack(side=tk.LEFT, padx=4)
        cb.bind("<Button-1>", lambda e: self.clear())

        cpb = tk.Label(
            bottom, text="📋 复制译文",
            font=("Microsoft YaHei", 9),
            bg="#e8f0fe", fg="#1a73e8",
            cursor="hand2", padx=12, pady=3
        )
        cpb.pack(side=tk.RIGHT, padx=4)
        cpb.bind("<Button-1>", lambda e: self._copy_translation())

        clb = tk.Label(
            bottom, text="✕ 关闭",
            font=("Microsoft YaHei", 9),
            bg="#fce8e6", fg="#ea4335",
            cursor="hand2", padx=14, pady=3
        )
        clb.pack(side=tk.RIGHT, padx=(4, 0))
        clb.bind("<Button-1>", lambda e: self._close())

    def _build_model_selector(self, parent):
        """构建模型下拉选择框"""
        try:
            import tkinter.ttk as ttk
        except ImportError:
            # ttk 不可用时的回退方案
            self._model_selector_var = tk.StringVar(master=self, value="(ttk 不可用)")
            self._model_combo = None
            return

        # 准备下拉选项：[(显示名, 模型名), ...]
        model_options = []
        default_idx = 0
        for i, (name, display, info) in enumerate(self._available_models):
            size_str = info.get("size", "?") if isinstance(info, dict) else "?"
            model_options.append(f"{display}  [{size_str}]")
            if name == self._current_model:
                default_idx = i

        if not model_options:
            # 没有已下载模型时显示提示
            self._model_selector_var = tk.StringVar(
                master=self, value="未检测到已下载模型"
            )
            self._model_combo = ttk.Combobox(
                parent, textvariable=self._model_selector_var,
                values=["未检测到已下载模型"],
                state="disabled", width=30, font=("Microsoft YaHei", 8)
            )
            self._model_combo.pack(side=tk.LEFT)
            return

        self._model_selector_var = tk.StringVar(
            master=self, value=model_options[default_idx]
        )
        self._model_combo = ttk.Combobox(
            parent, textvariable=self._model_selector_var,
            values=model_options,
            state="readonly", width=30, font=("Microsoft YaHei", 8)
        )
        self._model_combo.pack(side=tk.LEFT)
        self._model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)

        # 显示当前模型详情
        if 0 <= default_idx < len(self._available_models):
            info = self._available_models[default_idx][2]
            self._show_model_info(info)

    def _on_model_selected(self, event=None):
        """模型下拉框选择事件"""
        if self._model_switching:
            return
        idx = self._model_combo.current()
        if idx < 0 or idx >= len(self._available_models):
            return
        new_model = self._available_models[idx][0]
        if new_model == self._current_model:
            return

        # 显示详情
        info = self._available_models[idx][2]
        self._show_model_info(info)

        # 触发回调
        if self._on_model_change:
            self._model_switching = True
            self._model_combo.configure(state="disabled")
            self.set_status(f"🔄 切换模型中...", "#f9ab00")
            self._on_model_change(new_model, self._on_switch_done)

    def _on_switch_done(self, success, message=""):
        """模型切换完成回调"""
        self._model_switching = False
        if self._model_combo.winfo_exists():
            self._model_combo.configure(state="readonly")
        if success:
            idx = self._model_combo.current()
            if 0 <= idx < len(self._available_models):
                self._current_model = self._available_models[idx][0]
        else:
            # 恢复旧选择
            for i, (name, display, info) in enumerate(self._available_models):
                if name == self._current_model:
                    self._model_selector_var.set(
                        self._model_combo["values"][i]
                    )
                    self._model_combo.current(i)
                    self._show_model_info(info)
                    break
        if message and self.winfo_exists():
            self.set_status(message, "#34a853" if success else "#ea4335")

    def _show_model_info(self, info):
        """显示模型详情"""
        if not hasattr(self, '_model_info_label') or not self._model_info_label.winfo_exists():
            return
        if not info:
            self._model_info_label.configure(text="")
            return
        vram = info.get("vram", "?") if isinstance(info, dict) else "?"
        speed = info.get("speed", "?") if isinstance(info, dict) else "?"
        desc = info.get("desc", "") if isinstance(info, dict) else ""
        self._model_info_label.configure(
            text=f"VRAM {vram} | {speed} | {desc}"
        )

    def _position_window(self):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w, h = 520, 560
        self.geometry(f"{w}x{h}+{sw - w - 30}+{sh - h - 60}")

    # ========== 公共接口 ==========

    def set_status(self, text, color="#ea4335"):
        if not self.winfo_exists():
            return
        self._status_label.configure(text=text, fg=color)
        self._status_canvas.itemconfig(self._status_dot, fill=color)

    def set_level(self, amplitude):
        if not self.winfo_exists():
            return
        w = max(3, int(min(amplitude, 1.0) * 36))
        color = ("#34a853" if amplitude < 0.4
                 else ("#f9ab00" if amplitude < 0.7 else "#ea4335"))
        self._level_canvas.coords(self._level_bar, 2, 1, w + 2, 9)
        self._level_canvas.itemconfig(self._level_bar, fill=color)

    def update_time(self):
        if not self.winfo_exists() or self._paused:
            return
        elapsed = int(time.time() - self._recording_start)
        mins, secs = divmod(elapsed, 60)
        self._time_label.configure(text=f"{mins:02d}:{secs:02d}")

    def show_partial_original(self, text, lang):
        """★ 流式更新原文（增量显示，不换段）"""
        if not self.winfo_exists() or not text:
            return
        self._orig_text.configure(state=tk.NORMAL)
        # 找到当前段落的起始位置并替换
        content = self._orig_text.get("1.0", tk.END)
        # 删除最后一个段落，重新写入
        lines = content.strip().split("\n\n")
        if lines and lines[-1].startswith(("🇨🇳", "🇺🇸", "[", "⏳")):
            lines = lines[:-1]
        prefix = "\n\n".join(lines)
        lang_label = {"zh": "🇨🇳", "en": "🇺🇸"}.get(lang, f"[{lang}]")

        self._orig_text.delete("1.0", tk.END)
        if prefix:
            self._orig_text.insert("1.0", prefix + "\n\n")
        self._orig_text.insert(tk.END, f"⏳ {lang_label}  {text}")
        self._orig_text.see(tk.END)
        self._orig_text.configure(state=tk.DISABLED)

    def show_original(self, text, lang):
        """识别完成，显示最终原文"""
        if not self.winfo_exists() or not text:
            return
        self._segment_count += 1
        lang_label = {"zh": "🇨🇳", "en": "🇺🇸"}.get(lang, f"[{lang}]")
        timestamp = time.strftime("%H:%M:%S")

        self._orig_text.configure(state=tk.NORMAL)
        # 替换流式占位
        content = self._orig_text.get("1.0", tk.END).strip()
        lines = content.split("\n\n")
        if lines and "⏳" in lines[-1]:
            lines = lines[:-1]
        prefix = "\n\n".join(lines)

        self._orig_text.delete("1.0", tk.END)
        if prefix:
            self._orig_text.insert("1.0", prefix + "\n\n")
        self._orig_text.insert(tk.END, f"{timestamp} {lang_label}  {text}")
        self._orig_text.see(tk.END)
        self._orig_text.configure(state=tk.DISABLED)

        self._show_translating()

    def show_translation(self, translated_text):
        if not self.winfo_exists():
            return
        self._trans_text.configure(state=tk.NORMAL)
        current = self._trans_text.get("1.0", tk.END).strip()
        # 移除可能残留的流式占位符（含 "已有译文 + 占位符" 的情况）
        if current.endswith("⏳ 翻译中..."):
            self._trans_text.delete("1.0", tk.END)
            current = current[:-len("⏳ 翻译中...")].rstrip()
            if current:
                self._trans_text.insert("1.0", current)
        elif current in ("", "⏳ 翻译中..."):
            self._trans_text.delete("1.0", tk.END)
        if self._segment_count > 1 and current:
            self._trans_text.insert(tk.END, "\n\n")
        self._trans_text.insert(tk.END, translated_text)
        self._trans_text.see(tk.END)
        self._trans_text.configure(state=tk.DISABLED)
        self._spinner_label.configure(text="")

    def _show_translating(self):
        if not self.winfo_exists():
            return
        current = self._trans_text.get("1.0", tk.END).strip()
        self._trans_text.configure(state=tk.NORMAL)
        if not current or current == "⏳ 翻译中...":
            self._trans_text.delete("1.0", tk.END)
            self._trans_text.insert("1.0", "⏳ 翻译中...")
        else:
            self._trans_text.insert(tk.END, "\n\n⏳ 翻译中...")
        self._trans_text.see(tk.END)
        self._trans_text.configure(state=tk.DISABLED)
        self._spinner_label.configure(
            text=f"翻译中{['.', '..', '...'][int(time.time() * 2) % 3]}"
        )

    def log_message(self, message):
        if not self.winfo_exists():
            return
        self._orig_text.configure(state=tk.NORMAL)
        self._orig_text.insert(tk.END, f"{message}\n")
        self._orig_text.see(tk.END)
        self._orig_text.configure(state=tk.DISABLED)

    def clear(self):
        if not self.winfo_exists():
            return
        self._segment_count = 0
        self._recording_start = time.time()
        self._time_label.configure(text="00:00")
        self._orig_text.configure(state=tk.NORMAL)
        self._orig_text.delete("1.0", tk.END)
        self._orig_text.configure(state=tk.DISABLED)
        self._trans_text.configure(state=tk.NORMAL)
        self._trans_text.delete("1.0", tk.END)
        self._trans_text.configure(state=tk.DISABLED)
        self._spinner_label.configure(text="")

    def _copy_translation(self):
        text = self._trans_text.get("1.0", tk.END).strip()
        text = text.replace("⏳ 翻译中...", "").strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._status_label.configure(text="✅ 已复制", fg="#34a853")
            self.after(1500, lambda: self._status_label.configure(
                text="监听中..." if not self._paused else "已暂停",
                fg="#34a853" if not self._paused else "#f9ab00"
            ))

    def _toggle_pause(self):
        self._paused = not self._paused
        if self._paused:
            self._pause_btn.configure(text="▶ 继续", bg="#d4edda", fg="#155724")
            self.set_status("已暂停", "#f9ab00")
        else:
            self._pause_btn.configure(text="⏸ 暂停", bg="#fff3cd", fg="#856404")
            self.set_status("监听中...", "#34a853")

    @property
    def paused(self):
        return self._paused

    def _close(self):
        if self._on_close:
            self._on_close()
        self.destroy()


# =========================== 音频翻译主控 ===========================
class AudioTranslator:
    """音频翻译主控制器"""

    def __init__(self, engine):
        self.engine = engine
        self.capture = None
        self.asr = None
        self.window = None
        self._running = False
        self._parent = None
        # ★ 线程协调：Event 区分"整体停止"与"暂停（模型切换）"，避免 _running 一旗多义
        self._stop_event = threading.Event()    # 永久停止
        self._pause_event = threading.Event()   # 模型切换时暂停识别/翻译（初始 set = 未暂停）
        self._pause_event.set()
        self._asr_idle = threading.Event()      # 识别线程空闲标志（安全点：切换/停止前等待在途推理完成）
        self._asr_idle.set()
        self._model_lock = threading.Lock()     # 串行化 _init_pipeline 与 _reload_model
        self._generation = 0                    # 线程代际：stop() 时 +1，旧线程检测到不匹配即退出
        self._audio_queue = queue.Queue(QUEUE_SIZE)
        self._result_queue = queue.Queue()
        self._translate_queue = queue.Queue(TRANSLATE_QUEUE_SIZE)
        self._asr_thread = None
        self._translate_thread = None
        self._init_thread = None        # 模型初始化线程（防 start 重入）
        self._init_gen = 0              # _init_thread 对应的代际
        self._segment_counter = 0

        # ★ RMS 能量门控 + 语音累积
        self._speech_buffer = []        # 累积的语音块
        self._silence_duration = 0.0    # ★ 改用浮点秒数（原 _silence_count 帧计数）
        self._speech_duration = 0.0     # 当前累积语音时长
        self._speech_rms_sum = 0.0      # ★ 新增：累积 RMS（用于自适应阈值）
        self._speech_rms_count = 0      # ★ 新增：RMS 采样计数
        self._chunk_times = collections.deque(maxlen=100)  # 处理耗时记录（有界）

    def _push_translate_task(self, text, from_lang, to_lang, q=None):
        """翻译任务入队（非阻塞，队列满时丢弃最旧任务形成背压）"""
        q = q or self._translate_queue
        task = {"text": text, "from_lang": from_lang, "to_lang": to_lang}
        try:
            q.put_nowait(task)
        except queue.Full:
            try:
                q.get_nowait()  # 丢最旧
            except queue.Empty:
                pass
            try:
                q.put_nowait(task)
            except queue.Full:
                pass

    def start(self, parent):
        # 防重入：运行中、当前代际的初始化进行中、或停止清理未完成时不允许再次启动
        if (self._running
                or (self._init_thread is not None
                    and self._init_thread.is_alive()
                    and self._init_gen == self._generation)
                or (not self._stop_event.is_set() and self._asr_thread is not None)):
            return
        self._stop_event.clear()
        self._parent = parent
        missing = check_audio_deps()
        if missing:
            messagebox.showwarning(
                "缺少依赖",
                "音频翻译需要以下依赖，请先安装：\n\n" + "\n".join(missing)
            )
            return

        # ★ 扫描已下载模型
        available = list_cached_models()
        if available:
            print(f"[模型] 检测到 {len(available)} 个已下载模型: "
                  f"{[m[0] for m in available]}")
        else:
            print("[模型] 未检测到已下载模型，将自动下载默认模型")

        self.window = AudioWindow(
            parent,
            on_close=self.stop,
            on_model_change=self._on_user_model_change,
            available_models=available,
            current_model=MODEL
        )
        self.window.deiconify()
        self.window.lift()
        self.window.log_message(f"🚀 正在加载语音模型 {MODEL}...")
        self._init_gen = self._generation
        self._init_thread = threading.Thread(target=self._init_pipeline, daemon=True)
        self._init_thread.start()

    def _on_user_model_change(self, new_model, done_callback):
        """用户在 UI 中切换模型 → 后台线程执行重载"""
        print(f"[模型切换] 用户选择: {new_model}")
        if self.window and self.window.winfo_exists():
            self.window.log_message(f"🔄 正在切换到 {new_model}...")
        threading.Thread(
            target=self._reload_model,
            args=(new_model, done_callback),
            daemon=True
        ).start()

    def _safe_after(self, fn):
        """把回调投递到主线程（窗口已销毁时静默丢弃，避免 TclError）"""
        try:
            if self._parent is not None:
                self._parent.after(0, fn)
        except Exception:
            pass

    def _ensure_workers(self):
        """确保捕获与识别/翻译工作线程存在（初始加载失败后切换成功时补启动）"""
        if self._stop_event.is_set():
            return False
        if self.capture is None:
            self.capture = AudioCapture(self._on_audio_chunk)
            ok, msg = self.capture.start()
            if not ok:
                self._safe_after(lambda: self._safe_log(f"❌ 音频捕获失败: {msg}"))
                return False
        if self._asr_thread is None or not self._asr_thread.is_alive():
            gen = self._generation
            audio_q, result_q = self._audio_queue, self._result_queue
            translate_q = self._translate_queue
            self._asr_thread = threading.Thread(
                target=self._recognition_loop,
                args=(audio_q, result_q, translate_q, gen),
                daemon=True
            )
            self._asr_thread.start()
        if self._translate_thread is None or not self._translate_thread.is_alive():
            gen = self._generation
            result_q = self._result_queue
            translate_q = self._translate_queue
            self._translate_thread = threading.Thread(
                target=self._translation_loop,
                args=(translate_q, result_q, gen),
                daemon=True
            )
            self._translate_thread.start()
        if self._stop_event.is_set():
            # 补启动期间会话被停止：回收刚启动的捕获
            if self.capture:
                self.capture.stop()
            return False
        self._running = True
        return True

    def _reload_model(self, new_model_name, done_callback):
        """后台重载模型（暂停识别 → 安全点等待在途推理 → 替换模型 → 恢复）

        ★ 关键改进：
          - 用 _pause_event 暂停工作线程（线程不退出，切换后无需重启）
          - 等待 _asr_idle（在途推理完成）后才释放旧模型，避免与 ctranslate2 并发崩溃
          - 与 _init_pipeline 用 _model_lock 串行化，避免互相覆盖状态
        """
        global MODEL
        gen = self._generation  # 会话代际：stop()→start() 交错时旧切换任务失效

        def _stale():
            """当前切换任务是否已过期（会话被停止/重启）"""
            return self._stop_event.is_set() or gen != self._generation

        if _stale():
            return

        with self._model_lock:
            if _stale():
                return
            # 1. 暂停识别/翻译
            self._pause_event.clear()

            # 2. 等待在途推理完成（安全点）
            if not self._asr_idle.wait(timeout=15):
                self._safe_log("⚠️ 等待当前识别完成超时，继续切换")

            # 3. 清空队列
            for q in [self._audio_queue, self._result_queue, self._translate_queue]:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except Exception:
                        break

            # 4. 释放旧模型（此时无推理进行中）
            old_asr = self.asr
            if old_asr:
                try:
                    old_asr.reset_context()
                except Exception:
                    pass
                old_asr._model = None
            self.asr = None
            import gc
            gc.collect()

            # 5. 创建新 ASR 并加载
            new_asr = ASRProcessor(model_name=new_model_name)
            ok = new_asr.load()
            note = ""  # 结果备注（用于回退提示）

            if not ok:
                # 加载失败，尝试回退到默认模型
                print(f"[模型切换] ❌ {new_model_name} 加载失败")
                fallback_name = MODEL
                fallback_asr = ASRProcessor(model_name=fallback_name)
                if fallback_asr.load():
                    self.asr = fallback_asr
                    MODEL = fallback_name  # 同步全局模型名，与下拉框显示一致
                    note = "⚠️ 已回退到 "
                    self._safe_log(f"⚠️ 已回退到默认模型 {fallback_name}")
                else:
                    # 新模型和回退模型都失败：恢复暂停事件（不悬挂），给出明确提示
                    self._pause_event.set()
                    self._safe_log("❌ 模型加载失败且回退失败，已暂停。请关闭窗口重试。")
                    if not _stale():
                        self._safe_after(lambda: done_callback(
                            False, f"❌ {new_model_name} 加载失败，回退也失败"
                        ))
                        self._safe_after(lambda: self.window.set_status(
                            "模型加载失败", "#ea4335"
                        ) if self.window else None)
                    return
            else:
                # 6. 更新全局 MODEL
                MODEL = new_model_name
                self.asr = new_asr
                self._safe_log(f"✅ 模型已切换: {new_model_name}")

            # 切换期间用户关闭/重启了会话：释放新加载的模型，避免悬挂
            if _stale():
                if self.asr:
                    self.asr._model = None
                    self.asr = None
                return

            self._speech_buffer = []
            self._speech_duration = 0.0
            self._silence_duration = 0.0

            # 7. 恢复识别/翻译；若工作线程缺失（初始加载失败后首次切换成功）则补启动
            if self.asr is not None:
                self._ensure_workers()
            self._pause_event.set()

            # 通知 UI 切换完成（asr_final 局部引用，避免回调执行时已停止）
            asr_final = self.asr
            if asr_final:
                device_emoji = "🎮" if asr_final.device == "cuda" else "💻"
                self._safe_after(lambda: done_callback(
                    True,
                    f"{note}{device_emoji} {asr_final.model_name}/{asr_final.device} 就绪"
                ))
            print(f"[模型切换] ✅ {new_model_name} 加载成功")

    def _init_pipeline(self):
        with self._model_lock:
            if self._stop_event.is_set():
                return
            self.asr = ASRProcessor()
            if not self.asr.load():
                self._parent.after(
                    0, lambda: self._safe_log("❌ 语音模型加载失败，请检查模型文件")
                )
                self._parent.after(
                    0, lambda: self.window.set_status("模型加载失败", "#ea4335")
                    if self.window else None
                )
                return
            # GPU加速状态显示
            device_emoji = "🎮" if self.asr.device == "cuda" else "💻"
            ct_info = f" ({self.asr.compute_type})" if hasattr(self.asr, 'compute_type') else ""
            self._parent.after(
                0, lambda: self._safe_log(
                    f"{device_emoji} 模型就绪: {self.asr.model_name}/{self.asr.device}{ct_info}\n"
                    f"   正在启动音频捕获..."
                )
            )
            self.capture = AudioCapture(self._on_audio_chunk)
            ok, msg = self.capture.start()
            if not ok:
                self._parent.after(
                    0, lambda: self._safe_log(f"❌ 音频捕获失败: {msg}")
                )
                return
            if self._stop_event.is_set():
                # 初始化期间用户已关闭窗口
                self.capture.stop()
                return
            self._running = True
            # 线程启动时捕获队列与代际的局部引用：stop 后重建队列/递增代际，
            # 旧线程只会操作旧对象并在代际不匹配时退出，杜绝双消费者。
            gen = self._generation
            audio_q, result_q = self._audio_queue, self._result_queue
            translate_q = self._translate_queue
            self._asr_thread = threading.Thread(
                target=self._recognition_loop,
                args=(audio_q, result_q, translate_q, gen),
                daemon=True
            )
            self._asr_thread.start()
            self._translate_thread = threading.Thread(
                target=self._translation_loop,
                args=(translate_q, result_q, gen),
                daemon=True
            )
            self._translate_thread.start()
            self._parent.after(
                0, lambda: self.window.set_status("监听中...", "#34a853")
                if self.window else None
            )
            self._parent.after(POLL_INTERVAL, self._display_loop)
            self._parent.after(1000, self._update_timer)

    # ========== 音频回调（★ RMS 门控 + 语音累积） ==========

    def _on_audio_chunk(self, data):
        """
        ★ 修复版 RMS 能量检测 → 语音累积 → 攒大段再送 Whisper

        核心改进：
          1. 基于时间（秒）而非帧数的静音检测 — 0.8秒静音才切句
          2. 自适应阈值 — 动态追踪噪声地板
          3. 最大累积 15 秒 — 足够容纳完整长句
        """
        win = self.window  # 局部引用，防止 stop() 置 None 后中途变空
        if not self._running or self._stop_event.is_set() or (win and win.paused):
            return
        amp = float(np.max(np.abs(data)))
        if win and win.winfo_exists():
            self._parent.after(
                0, lambda a=amp, w=win: (
                    w.set_level(min(a * 3, 1.0)) if w.winfo_exists() else None
                )
            )

        chunk_sec = len(data) / SR  # 当前块时长（秒）

        # ★ RMS 能量检测
        rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
        is_speech = rms > RMS_SPEECH_THRESHOLD

        if is_speech:
            self._speech_buffer.append(data.copy())
            self._speech_duration += chunk_sec
            self._silence_duration = 0.0   # ★ 使用浮点秒数
            self._speech_rms_sum += rms
            self._speech_rms_count += 1
            # 达到最大时长 → 立即送识别
            if self._speech_duration >= MAX_SPEECH_DURATION:
                self._flush_speech_buffer()
        else:
            # 真正的静音（低于噪声地板）才计时
            if rms < RMS_NOISE_FLOOR:
                self._silence_duration += chunk_sec
            # ★ 关键修复：连续静音超过 0.8 秒 + 有足够语音 → 送识别
            if (self._silence_duration >= RMS_SILENCE_SECONDS
                    and self._speech_duration >= MIN_SPEECH_DURATION):
                self._flush_speech_buffer()

    def _flush_speech_buffer(self):
        """合并累积语音块，送入识别队列
        ★ 修复：更大的重叠（1.5秒）+ 首尾静音修剪
        """
        if not self._speech_buffer or self._speech_duration < MIN_SPEECH_DURATION:
            self._speech_buffer = []
            self._speech_duration = 0.0
            self._silence_duration = 0.0
            self._speech_rms_sum = 0.0
            self._speech_rms_count = 0
            return

        segment = np.concatenate(self._speech_buffer)
        dur = len(segment) / SR

        # ★ 保留末尾 1.5 秒作为下一段的重叠（原0.4秒太短）
        overlap_sec = OVERLAP_DURATION
        overlap_samples = int(overlap_sec * SR)
        if len(segment) > overlap_samples:
            overlap = segment[-overlap_samples:].copy()
        else:
            overlap = np.array([], dtype=np.float32)

        self._speech_buffer = [overlap] if len(overlap) > 0 else []
        self._speech_duration = len(overlap) / SR if len(overlap) > 0 else 0.0
        self._silence_duration = 0.0
        self._speech_rms_sum = 0.0
        self._speech_rms_count = 0

        self._segment_counter += 1
        rms = float(np.sqrt(np.mean(segment.astype(np.float64) ** 2)))
        print(f"[音频] 第{self._segment_counter}段 {dur:.1f}秒 "
              f"RMS={rms:.4f} 振幅={np.max(np.abs(segment)):.4f}")
        try:
            self._audio_queue.put_nowait(segment)
        except queue.Full:
            print("[音频] 队列满，丢弃旧段")
            try:
                self._audio_queue.get_nowait()
                self._audio_queue.put_nowait(segment)
            except queue.Empty:
                pass

    # ========== 识别循环（★ 流式版） ==========

    def _recognition_loop(self, audio_q, result_q, translate_q, gen):
        """识别循环 — 流式回调：逐句显示原文

        ★ 线程协调：_stop_event 控制整体停止，_pause_event 控制模型切换暂停。
        切换时线程不退出（无需重启），恢复后动态读取 self.asr 获取新模型。
        队列与代际使用启动时捕获的局部引用，与 stop 后的新实例状态隔离。
        """
        def _alive():
            return (not self._stop_event.is_set()) and gen == self._generation

        while _alive():
            # 模型切换期间暂停（wait 返回后必须校验 is_set，否则暂停失效）
            self._pause_event.wait(timeout=0.5)
            if not _alive():
                break
            if not self._pause_event.is_set():
                continue
            try:
                audio = audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if not _alive():
                break
            if not self._pause_event.is_set():
                # 暂停在 get 阻塞期间生效：放回（非阻塞，满则丢弃——切换时队列本就会被清空）
                try:
                    audio_q.put_nowait(audio)
                except queue.Full:
                    pass
                continue
            # 在开始推理前立即标记"识别忙碌"（安全点窗口最小化，
            # 防止 _reload_model 在 pause 检查通过后误判空闲而释放模型）
            self._asr_idle.clear()
            # 动态获取当前模型（切换后自动用新模型）
            asr = self.asr
            if asr is None:
                self._asr_idle.set()
                continue
            try:
                t0 = time.time()
                dur = len(audio) / SR
                print(f"[识别] 转写 {dur:.1f}秒...")

                # ★ 流式回调（闭包捕获本次迭代的 asr 局部引用，stop() 后不会 AttributeError）
                def on_partial(partial_text, is_final):
                    result = {
                        "type": "original" if is_final else "partial_original",
                        "text": partial_text,
                        "lang": asr._locked_lang or "unknown"
                    }
                    try:
                        result_q.put_nowait(result)
                    except queue.Full:
                        pass

                try:
                    result = asr.transcribe(audio, stream_callback=on_partial)
                finally:
                    self._asr_idle.set()
                elapsed = time.time() - t0
                text = result["text"].strip()
                lang = result["lang"]

                # 记录耗时
                self._chunk_times.append(elapsed)
                rt = dur / elapsed if elapsed > 0 else 0
                avg = (sum(self._chunk_times)
                       / max(1, len(self._chunk_times)))
                print(f"[识别] {dur:.1f}s音频 → {elapsed:.1f}s处理 "
                      f"(实时比 {rt:.1f}x) 语言={lang} "
                      f"平均{avg:.1f}s/段 文本={text[:100]}")
                if not text:
                    continue

                # 送入翻译队列
                target_lang = "en" if lang.startswith("zh") else "zh"
                self._push_translate_task(text, lang, target_lang, q=translate_q)

            except Exception as e:
                err_msg = str(e)
                print(f"[识别] 错误: {err_msg}")
                if "cublas" in err_msg.lower() or "cuda" in err_msg.lower():
                    print("[识别] 检测到 CUDA 错误，自动切换到 CPU...")
                    try:
                        asr.reload_cpu()
                        result = asr.transcribe(audio)
                        text = result["text"].strip()
                        lang = result["lang"]
                        print(f"[识别] CPU模式 语言={lang} 文本={text[:120]}")
                        if text:
                            result_q.put({
                                "type": "original",
                                "text": text, "lang": lang
                            })
                            target_lang = "en" if lang.startswith("zh") else "zh"
                            self._push_translate_task(text, lang, target_lang, q=translate_q)
                        continue
                    except Exception as e2:
                        print(f"[识别] CPU 回退也失败: {e2}")
                import traceback
                traceback.print_exc()

    # ========== 翻译循环 ==========

    def _translation_loop(self, translate_q, result_q, gen):
        def _alive():
            return (not self._stop_event.is_set()) and gen == self._generation

        while _alive():
            # 模型切换期间暂停（wait 返回后必须校验 is_set）
            self._pause_event.wait(timeout=0.5)
            if not _alive():
                break
            if not self._pause_event.is_set():
                continue
            try:
                task = translate_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if not _alive():
                break
            if not self._pause_event.is_set():
                # 暂停在 get 阻塞期间生效：放回（非阻塞，满则丢弃）
                try:
                    translate_q.put_nowait(task)
                except queue.Full:
                    pass
                continue
            try:
                print(f"[翻译] {task['from_lang']}→{task['to_lang']} "
                      f"{task['text'][:60]}...")
                translated = self.engine.translate(
                    task["text"], task["from_lang"], task["to_lang"]
                )
                print(f"[翻译] 结果: {translated[:120]}")
                result_q.put({"type": "translation", "text": translated})
            except Exception as e:
                print(f"[翻译] 错误: {e}")
                result_q.put({
                    "type": "translation", "text": f"[翻译失败] {e}"
                })

    # ========== 界面显示 ==========

    def _display_loop(self):
        if self._stop_event.is_set():
            return
        try:
            while True:
                r = self._result_queue.get_nowait()
                if not self.window or not self.window.winfo_exists():
                    continue
                t = r["type"]
                if t == "partial_original":
                    self.window.show_partial_original(r["text"], r["lang"])
                elif t == "original":
                    self.window.show_original(r["text"], r["lang"])
                elif t == "translation":
                    self.window.show_translation(r["text"])
        except queue.Empty:
            pass

        # 更新实时处理速度
        if self.window and self.window.winfo_exists():
            c = self.window._trans_text.get("1.0", tk.END).strip()
            if "翻译中" in c:
                dots = [".", "..", "..."]
                self.window._spinner_label.configure(
                    text=f"翻译中{dots[int(time.time() * 2) % 3]}"
                )
            # 显示平均处理耗时（GPU加速状态；局部引用避免与切换/停止竞态）
            asr = self.asr
            if self._chunk_times and asr:
                avg = sum(self._chunk_times) / max(1, len(self._chunk_times))
                device_icon = "🎮" if asr.device == "cuda" else "💻"
                rt_str = f" 实时比{avg/1.5:.1f}x" if avg < 3 else ""
                self.window.set_status(
                    f"{device_icon} {asr.model_name}/{asr.device}"
                    f" | 平均 {avg:.1f}s/段{rt_str}",
                    "#34a853" if avg < 2 else ("#f9ab00" if avg < 5 else "#ea4335")
                )

        if not self._stop_event.is_set():
            self._parent.after(POLL_INTERVAL, self._display_loop)

    def _update_timer(self):
        if self._stop_event.is_set():
            return
        if self.window and self.window.winfo_exists():
            self.window.update_time()
            self._parent.after(1000, self._update_timer)

    def _safe_log(self, msg):
        if self.window and self.window.winfo_exists():
            self.window.log_message(msg)

    def stop(self):
        """停止音频翻译 — 非阻塞版本
        先停捕获（解除 COM 阻塞），设置 _stop_event 让识别/翻译线程退出。
        线程 join 放到后台线程中执行，不阻塞主线程 UI。"""
        if self._stop_event.is_set():
            return  # 防重入
        print("[音频翻译] 停止中...")
        self._running = False
        self._stop_event.set()
        self._generation += 1  # 让仍在途中的旧工作线程退出

        # 第一步：停音频捕获（COM Stop 可解除 _loop 阻塞）
        if self.capture:
            self.capture.stop()

        # 第二步：主线程不 join，放到后台线程去等（超时打警告，避免静默泄漏）
        # 立即捕获线程引用：快速重启后新线程写入 self._asr_thread 时，清理的仍是旧线程
        asr_t, tr_t = self._asr_thread, self._translate_thread

        def _cleanup_threads():
            for t, name in [(asr_t, "识别"), (tr_t, "翻译")]:
                if t and t.is_alive():
                    t.join(timeout=5)
                    if t.is_alive():
                        print(f"[音频翻译] ⚠️ {name}线程未在5秒内退出（可能在处理长音频）")
            print("[音频翻译] 后台线程已清理")

        threading.Thread(target=_cleanup_threads, daemon=True).start()
        self._asr_thread = None
        self._translate_thread = None

        # 第三步：重建队列（隔离仍在退出途中的旧线程，防止快速重启后双消费者）
        self._audio_queue = queue.Queue(QUEUE_SIZE)
        self._result_queue = queue.Queue()
        self._translate_queue = queue.Queue(TRANSLATE_QUEUE_SIZE)

        # 第四步：重置状态（不依赖线程 join 结果）
        if self.asr:
            try:
                self.asr.reset_context()
            except Exception:
                pass
        self.window = None
        self.capture = None
        self.asr = None
        self._speech_buffer = []
        self._speech_duration = 0.0
        self._silence_duration = 0.0
        self._speech_rms_sum = 0.0
        self._speech_rms_count = 0
        self._chunk_times.clear()
        self._pause_event.set()
        print("[音频翻译] 已停止")
