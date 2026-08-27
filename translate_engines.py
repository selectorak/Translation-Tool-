#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多翻译引擎模块
================
支持的翻译引擎：
  • MyMemory        — 免费，无需API Key（国内稳定）
  • Google Translate — 免费，无需API Key（质量高）
  • 百度翻译         — 需 app_id + secret_key（免费额度）
  • 腾讯翻译         — 需 secret_id + secret_key（免费额度）
  • 阿里翻译(通用版)  — 需 access_key_id + access_key_secret（免费额度）
  • 有道翻译         — 需 app_key + app_secret（免费额度）
  • 讯飞翻译         — 需 app_id + api_key + api_secret（免费额度）
  • 微软翻译         — 需 subscription_key + region（免费额度）

字符限制：每个引擎单次请求有不同限制，本模块自动分块处理长文本。
"""

import hashlib
import hmac
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from copy import deepcopy
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from abc import ABC, abstractmethod
from urllib.parse import quote

from translate_cache import TranslationCache

# =========================== 配置管理 ===========================

def _get_app_dir():
    """获取应用数据目录（配置文件与缓存所在位置）。

    打包成 exe 后 __file__ 指向 _MEIPASS 临时解压目录（每次启动重建、退出删除），
    写入即丢失，因此 frozen 时改用 %APPDATA%/TranslatorApp。
    """
    if getattr(sys, "frozen", False):
        app_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TranslatorApp")
        os.makedirs(app_dir, exist_ok=True)
        return app_dir
    return os.path.dirname(os.path.abspath(__file__))


def _get_config_file():
    """获取配置文件路径"""
    return os.path.join(_get_app_dir(), "api_keys.json")


def _get_cache_dir():
    """获取翻译缓存目录"""
    return os.path.join(_get_app_dir(), ".translation_cache")


CONFIG_FILE = _get_config_file()
CACHE_DIR = _get_cache_dir()

DEFAULT_CONFIG = {
    "selected_engine": "auto",  # auto / mymemory / google / baidu / tencent / alibaba / youdao / xunfei / microsoft
    "fallback_enabled": True,   # 主引擎失败时是否回退到其他引擎
    "api_keys": {
        "baidu": {"app_id": "", "secret_key": ""},
        "tencent": {"secret_id": "", "secret_key": "", "region": "ap-guangzhou"},
        "alibaba": {"access_key_id": "", "access_key_secret": ""},
        "youdao": {"app_key": "", "app_secret": ""},
        "xunfei": {"app_id": "", "api_key": "", "api_secret": ""},
        "microsoft": {"subscription_key": "", "region": "eastasia"},
    },
    "max_chunk_size": 5000,  # 单次请求最大字符数（自动分块）
}


def load_config():
    """加载API密钥配置"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            # 合并默认配置（确保新增字段存在）
            merged = deepcopy(DEFAULT_CONFIG)
            _deep_merge(merged, config)
            return merged
        except json.JSONDecodeError as e:
            print(f"[配置] {CONFIG_FILE} 解析失败({e.msg})，已使用默认配置。请修复该文件或删除后重新配置。")
        except OSError as e:
            print(f"[配置] 读取失败: {e}，已使用默认配置。")
    return deepcopy(DEFAULT_CONFIG)


def save_config(config):
    """保存API密钥配置（临时文件 + 原子替换，避免中断损坏配置）"""
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        tmp_path = CONFIG_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CONFIG_FILE)
        return True
    except Exception as e:
        print(f"[配置] 保存失败: {e}")
        return False


def _deep_merge(base, override):
    """深度合并两个字典"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


# =========================== 基础翻译引擎 ===========================

class BaseEngine(ABC):
    """翻译引擎基类"""

    name = "base"
    display_name = "基础引擎"
    max_chars_per_request = 5000  # 单次请求最大字符数
    requires_key = False
    LANG_MAP: dict = {}  # 语言代码差异表（子类覆盖）
    SUPPORTED_LANGS: set | None = None  # 支持的语言代码集（None=不限制，用于回退时自动跳过）

    def __init__(self, session: requests.Session | None = None):
        self._session = session or requests.Session()

    @property
    def translation_cache_token(self) -> str:
        """缓存令牌：配置变更后返回不同值，使旧缓存自动失效（借鉴 TranslationPlugin）"""
        return "1"

    def _request_json(self, method: str, url: str, **kwargs) -> Any:
        """统一请求 + 状态码检查 + JSON 解析，失败时抛出带上下文的 RuntimeError。

        各引擎不应再直接访问 resp.json()，这样限流/鉴权失败（4xx/5xx）
        能给出可读的错误信息，而不是晦涩的 JSONDecodeError。
        """
        resp = self._session.request(method, url, **kwargs)
        if resp.status_code >= 400:
            snippet = resp.text[:300].replace("\n", " ")
            raise RuntimeError(
                f"{self.display_name} 请求失败(HTTP {resp.status_code}): {snippet}"
            )
        try:
            return resp.json()
        except ValueError:
            snippet = resp.text[:300].replace("\n", " ")
            raise RuntimeError(f"{self.display_name} 返回无效 JSON: {snippet}")

    @abstractmethod
    def translate(self, text: str, from_lang: str = "auto", to_lang: str = "zh") -> str:
        """执行翻译，返回译文"""
        pass

    def is_available(self) -> bool:
        """检查引擎是否可用"""
        return True

    def _map_lang(self, lang: str) -> str:
        """语言代码映射（子类通过覆盖 LANG_MAP 声明差异表）"""
        return self.LANG_MAP.get(lang, lang)


class FreeEngine(BaseEngine):
    """免费引擎基类（无需API Key）"""
    requires_key = False

    def is_available(self):
        return True


class KeyEngine(BaseEngine):
    """需要API Key的引擎基类"""
    requires_key = True

    def __init__(self, config: dict, session: requests.Session | None = None):
        super().__init__(session)
        self.config = config
        self.api_keys = config.get("api_keys", {}).get(self.name, {})

    @property
    def translation_cache_token(self) -> str:
        """缓存令牌 = API 密钥配置的哈希：改密钥后旧缓存自动失效"""
        payload = json.dumps(self.api_keys, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    def is_available(self):
        """检查API密钥是否已配置"""
        return bool(self._check_keys())

    @abstractmethod
    def _check_keys(self) -> bool:
        """检查密钥是否完整"""
        pass


# =========================== MyMemory 翻译 ===========================

class MyMemoryEngine(FreeEngine):
    """MyMemory 翻译 — 免费，国内访问稳定"""
    name = "mymemory"
    display_name = "MyMemory"
    max_chars_per_request = 5000

    def translate(self, text: str, from_lang: str = "auto", to_lang: str = "zh") -> str:
        if from_lang == "auto":
            from_lang = "zh" if re.search(r'[\u4e00-\u9fff]', text) else "en"
        langpair = f"{from_lang}|{to_lang}"
        data = self._request_json(
            "GET",
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": langpair},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        # MyMemory 业务失败时 HTTP 仍为 200，需检查 responseStatus
        if data.get("responseStatus") != 200:
            raise RuntimeError(
                f"MyMemory 翻译错误: {data.get('responseDetails', '未知错误')}"
            )
        return data["responseData"]["translatedText"].strip()


# =========================== Google 翻译 ===========================

class GoogleTranslateEngine(FreeEngine):
    """Google 翻译 — 免费，翻译质量高"""
    name = "google"
    display_name = "Google 翻译"
    max_chars_per_request = 5000

    def translate(self, text: str, from_lang: str = "auto", to_lang: str = "zh") -> str:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": from_lang,
            "tl": to_lang,
            # dt 需要重复参数：t=译文, bd=词典（requests 会展开为 dt=t&dt=bd）
            "dt": ["t", "bd"],
            "q": text
        }
        data = self._request_json(url, "GET", params=params, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }, timeout=8)
        parts = []
        for seg in (data[0] if data and data[0] else []):
            if seg and seg[0]:
                parts.append(seg[0])
        return "".join(parts).strip()


# =========================== 百度翻译 ===========================

class BaiduTranslateEngine(KeyEngine):
    """百度翻译 — 需 app_id + secret_key
    注册地址: https://fanyi-api.baidu.com/
    免费额度: 每月100万字符（标准版）
    """
    name = "baidu"
    display_name = "百度翻译"
    max_chars_per_request = 8000
    LANG_MAP = {"ja": "jp", "ko": "kor", "fr": "fra", "de": "de", "es": "spa", "ru": "ru"}
    SUPPORTED_LANGS = {"auto", "zh", "en", "ja", "ko", "fr", "de", "es", "ru"}

    # 百度业务错误码 → 中文文案（借鉴 TranslationPlugin 的可操作错误映射）
    BAIDU_ERROR_MAP = {
        "52001": "请求超时，请重试",
        "52002": "百度系统错误，请重试",
        "52003": "未授权用户，请检查 APP ID 是否正确",
        "54000": "请求参数为空，请检查调用方式",
        "54001": "签名错误，请检查密钥是否正确",
        "54003": "访问频率受限，请降低请求频率或稍后重试",
        "54004": "账户余额不足，请前往控制台充值",
        "54005": "长文本请求过于频繁，请降低发送频率",
        "58000": "客户端 IP 非法，请检查 IP 白名单设置",
        "58001": "译文语言方向不支持，请更换语言对",
        "58002": "百度翻译服务当前已关闭，请稍后再试",
        "90107": "认证未通过或未生效，请检查 APP ID 与密钥",
    }

    def _check_keys(self):
        return bool(self.api_keys.get("app_id") and self.api_keys.get("secret_key"))

    def translate(self, text: str, from_lang: str = "auto", to_lang: str = "zh") -> str:
        app_id = self.api_keys["app_id"]
        secret_key = self.api_keys["secret_key"]

        sl = self._map_lang(from_lang)
        tl = self._map_lang(to_lang)

        salt = str(random.randint(32768, 65536))
        sign_str = app_id + text + salt + secret_key
        sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest()

        url = "https://fanyi-api.baidu.com/api/trans/vip/translate"
        params = {
            "q": text,
            "from": sl,
            "to": tl,
            "appid": app_id,
            "salt": salt,
            "sign": sign,
        }
        # 百度 API 支持 POST，长文本走 GET 会超出 URL 长度限制且原文进入访问日志
        data = self._request_json("POST", url, data=params, timeout=10)
        if "trans_result" in data:
            parts = [item["dst"] for item in data["trans_result"]]
            return "".join(parts)
        else:
            error_code = str(data.get("error_code", ""))
            error_msg = BaiduTranslateEngine.BAIDU_ERROR_MAP.get(error_code) \
                or data.get("error_msg", "未知错误")
            raise RuntimeError(f"百度翻译错误[{error_code}]: {error_msg}")


# =========================== 腾讯翻译 ===========================

class TencentTranslateEngine(KeyEngine):
    """腾讯云机器翻译(TMT) — 需 secret_id + secret_key
    注册地址: https://console.cloud.tencent.com/tmt
    免费额度: 每月500万字符
    """
    name = "tencent"
    display_name = "腾讯翻译"
    max_chars_per_request = 8000
    SUPPORTED_LANGS = {"auto", "zh", "en", "ja", "ko", "fr", "de", "es", "ru"}

    def _check_keys(self):
        return bool(self.api_keys.get("secret_id") and self.api_keys.get("secret_key"))

    def _sign(self, secret_key, sign_str, method="HmacSHA256"):
        """腾讯云签名 V1"""
        if method == "HmacSHA256":
            return hmac.new(
                secret_key.encode("utf-8"), sign_str.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
        return hmac.new(
            secret_key.encode("utf-8"), sign_str.encode("utf-8"),
            hashlib.sha1
        ).hexdigest()

    def translate(self, text: str, from_lang: str = "auto", to_lang: str = "zh") -> str:
        secret_id = self.api_keys["secret_id"]
        secret_key = self.api_keys["secret_key"]
        region = self.api_keys.get("region", "ap-guangzhou")

        sl = self._map_lang(from_lang)
        tl = self._map_lang(to_lang)

        # 构建请求
        params = {
            "Action": "TextTranslate",
            "Version": "2018-03-21",
            "Region": region,
            "SourceText": text,
            "Source": sl,
            "Target": tl,
            "ProjectId": 0,
            "Timestamp": int(time.time()),
            "Nonce": random.randint(10000, 99999),
            "SecretId": secret_id,
        }

        # 签名 V1
        sorted_keys = sorted(params.keys())
        sign_str = "&".join([f"{k}={params[k]}" for k in sorted_keys])
        sign_str = f"POST/tmt.tencentcloudapi.com/?{sign_str}"
        params["Signature"] = self._sign(secret_key, sign_str, "HmacSHA1")

        # 腾讯云签名 V1 按 form 参数计算，必须用 data=（form 编码）而非 json=
        data = self._request_json(
            "POST",
            "https://tmt.tencentcloudapi.com/",
            data=params,
            timeout=10
        )
        if "Response" in data:
            if "TargetText" in data["Response"]:
                return data["Response"]["TargetText"]
            else:
                error = data["Response"].get("Error", {})
                raise RuntimeError(f"腾讯翻译错误: {error.get('Message', '未知错误')}")
        raise RuntimeError(f"腾讯翻译错误: 未知响应格式")


# =========================== 阿里翻译(通用版) ===========================

class AliTranslateEngine(KeyEngine):
    """阿里云机器翻译(通用版) — 需 access_key_id + access_key_secret
    注册地址: https://www.aliyun.com/product/ai/base_alimt
    免费额度: 每月100万字符
    """
    name = "alibaba"
    display_name = "阿里翻译"
    max_chars_per_request = 5000
    SUPPORTED_LANGS = {"auto", "zh", "en", "ja", "ko", "fr", "de", "es", "ru"}

    def _check_keys(self):
        return bool(self.api_keys.get("access_key_id") and self.api_keys.get("access_key_secret"))

    def translate(self, text: str, from_lang: str = "auto", to_lang: str = "zh") -> str:
        access_key_id = self.api_keys["access_key_id"]
        access_key_secret = self.api_keys["access_key_secret"]

        sl = self._map_lang(from_lang)
        tl = self._map_lang(to_lang)

        # 阿里云签名
        from urllib.parse import quote
        import base64

        # 业务参数 + RPC 公共参数（老版 RPC 签名规范必需）
        params = {
            "Action": "TranslateGeneral",
            "Version": "2018-10-12",
            "Format": "JSON",
            "AccessKeyId": access_key_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "FormatType": "text",
            "SourceLanguage": sl,
            "TargetLanguage": tl,
            "SourceText": text,
            "Scene": "general",
        }

        # 构建规范化查询字符串
        sorted_params = sorted(params.items())
        canonicalized_query = "&".join([
            f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in sorted_params
        ])

        string_to_sign = f"GET&{quote('/', safe='')}&{quote(canonicalized_query, safe='')}"
        signature = base64.b64encode(
            hmac.new(
                (access_key_secret + "&").encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1
            ).digest()
        ).decode("utf-8")

        url = (
            f"https://mt.cn-hangzhou.aliyuncs.com/?"
            f"{canonicalized_query}&Signature={quote(signature, safe='')}"
        )

        data = self._request_json("GET", url, timeout=10)
        if "Code" in data:
            if data["Code"] == "200":
                return data.get("Data", {}).get("Translated", "")
            else:
                raise RuntimeError(f"阿里翻译错误: {data.get('Message', '未知错误')}")
        raise RuntimeError(f"阿里翻译错误: 未知响应格式")


# =========================== 有道翻译 ===========================

class YoudaoTranslateEngine(KeyEngine):
    """有道智云翻译 — 需 app_key + app_secret
    注册地址: https://ai.youdao.com/
    免费额度: 每月100万字符（文本翻译）
    """
    name = "youdao"
    display_name = "有道翻译"
    max_chars_per_request = 5000
    LANG_MAP = {"zh": "zh-CHS"}
    SUPPORTED_LANGS = {"auto", "zh", "en", "ja", "ko", "fr", "de", "es", "ru"}

    def _check_keys(self):
        return bool(self.api_keys.get("app_key") and self.api_keys.get("app_secret"))

    def translate(self, text: str, from_lang: str = "auto", to_lang: str = "zh") -> str:
        app_key = self.api_keys["app_key"]
        app_secret = self.api_keys["app_secret"]

        sl = self._map_lang(from_lang)
        tl = self._map_lang(to_lang)

        salt = str(uuid.uuid4())
        curtime = str(int(time.time()))

        # 有道签名：SHA256(app_key + input + salt + curtime + app_secret)
        input_text = text
        if len(input_text) > 20:
            input_text = input_text[:10] + str(len(input_text)) + input_text[-10:]
        sign_str = app_key + input_text + salt + curtime + app_secret
        sign = hashlib.sha256(sign_str.encode("utf-8")).hexdigest()

        url = "https://openapi.youdao.com/api"
        data = {
            "q": text,
            "from": sl,
            "to": tl,
            "appKey": app_key,
            "salt": salt,
            "sign": sign,
            "signType": "v3",
            "curtime": curtime,
        }
        data = self._request_json("POST", url, data=data, timeout=10)
        if data.get("errorCode") == "0":
            return "".join(data.get("translation", []))
        else:
            raise RuntimeError(f"有道翻译错误: {data.get('errorCode', '未知错误')}")


# =========================== 讯飞翻译 ===========================

class XunfeiTranslateEngine(KeyEngine):
    """讯飞翻译 — 需 app_id + api_key + api_secret
    注册地址: https://www.xfyun.cn/services/its
    免费额度: 每日500次
    """
    name = "xunfei"
    display_name = "讯飞翻译"
    max_chars_per_request = 5000
    LANG_MAP = {"zh": "cn"}
    SUPPORTED_LANGS = {"auto", "zh", "en", "ja", "ko", "fr", "de", "es", "ru"}

    def _check_keys(self):
        return bool(self.api_keys.get("app_id") and self.api_keys.get("api_key")
                    and self.api_keys.get("api_secret"))

    def translate(self, text: str, from_lang: str = "auto", to_lang: str = "zh") -> str:
        app_id = self.api_keys["app_id"]
        api_key = self.api_keys["api_key"]
        api_secret = self.api_keys["api_secret"]

        sl = self._map_lang(from_lang)
        tl = self._map_lang(to_lang)

        host = "itrans.xfyun.cn"
        path = "/v2/its"
        url = f"https://{host}{path}"
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())

        # 构建请求体
        body = {
            "header": {
                "app_id": app_id,
                "status": 3,  # 3=流式完成
            },
            "parameter": {
                "its": {
                    "from": sl,
                    "to": tl,
                    "result": {},
                }
            },
            "payload": {
                "input_data": {
                    "encoding": "utf8",
                    "status": 3,
                    "text": base64_encode(text),
                }
            }
        }
        body_str = json.dumps(body)
        # 讯飞规范：digest = base64(md5(body 二进制))
        import base64 as _b64
        body_digest_b64 = _b64.b64encode(
            hashlib.md5(body_str.encode("utf-8")).digest()
        ).decode("utf-8")

        # 签名（header 与签名串使用同一个 digest 值）
        sign_str = f"host: {host}\ndate: {date}\nPOST {path} HTTP/1.1\ndigest: {body_digest_b64}"
        signature = hmac.new(
            api_secret.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256
        ).digest()
        signature_b64 = _b64.b64encode(signature).decode("utf-8")

        auth_header = (
            f'api_key="{api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line digest", '
            f'signature="{signature_b64}"'
        )

        headers = {
            "Host": host,
            "Date": date,
            "Digest": f"SHA-256={body_digest_b64}",
            "Authorization": auth_header,
            "Content-Type": "application/json",
        }

        data = self._request_json("POST", url, data=body_str, headers=headers, timeout=15)
        code = data.get("header", {}).get("code", -1)
        if code == 0:
            payload = data.get("payload", {})
            result = payload.get("result", {})
            text_result = result.get("text", "")
            # 解码 base64
            try:
                text_result = base64_decode(text_result)
            except Exception:
                pass
            return text_result
        else:
            raise RuntimeError(f"讯飞翻译错误: code={code}, message={data.get('header', {}).get('message', '未知错误')}")


def base64_encode(s: str) -> str:
    """Base64 编码"""
    import base64
    return base64.b64encode(s.encode("utf-8")).decode("utf-8")


def base64_decode(s: str) -> str:
    """Base64 解码"""
    import base64
    return base64.b64decode(s).decode("utf-8")


# =========================== 微软翻译 ===========================

class MicrosoftTranslateEngine(KeyEngine):
    """微软翻译(Azure Cognitive Services) — 需 subscription_key + region
    注册地址: https://portal.azure.com/ → 创建"Translator"资源
    免费额度: 每月200万字符
    """
    name = "microsoft"
    display_name = "微软翻译"
    max_chars_per_request = 5000
    LANG_MAP = {"zh": "zh-Hans"}

    def _check_keys(self):
        return bool(self.api_keys.get("subscription_key") and self.api_keys.get("region"))

    def translate(self, text: str, from_lang: str = "auto", to_lang: str = "zh") -> str:
        subscription_key = self.api_keys["subscription_key"]
        region = self.api_keys.get("region", "eastasia")

        tl = self._map_lang(to_lang)

        url = "https://api.cognitive.microsofttranslator.com/translate"
        params = {
            "api-version": "3.0",
            "to": tl,
        }
        if from_lang != "auto":
            params["from"] = self._map_lang(from_lang)

        headers = {
            "Ocp-Apim-Subscription-Key": subscription_key,
            "Ocp-Apim-Subscription-Region": region,
            "Content-Type": "application/json",
        }
        body = [{"Text": text}]

        data = self._request_json("POST", url, params=params, headers=headers, json=body, timeout=10)
        if isinstance(data, list) and len(data) > 0:
            translations = data[0].get("translations", [])
            if translations:
                return translations[0].get("text", "")
        error = data.get("error", {}) if isinstance(data, dict) else {}
        raise RuntimeError(f"微软翻译错误: {error.get('message', '未知错误')}")


# =========================== 翻译引擎管理器 ===========================

class _Flight:
    """在途翻译请求（同 key 并发去重合并用）"""

    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.error = None


class TranslateEngineManager:
    """翻译引擎管理器 — 统一管理所有引擎，处理引擎选择、回退、分块"""

    # 默认重试配置
    RETRY_TOTAL = 2        # 最多重试次数
    RETRY_BACKOFF = 0.5    # 重试间隔（秒，指数退避基数）
    RETRY_STATUSES = [408, 429, 500, 502, 503, 504]  # 可重试的HTTP状态码

    def __init__(self):
        self.config = load_config()
        self._session = self._create_session()
        self._cache = TranslationCache(CACHE_DIR)
        self._in_flight: dict = {}   # 在途请求去重表：cache_key -> _Flight
        self._in_flight_lock = threading.Lock()
        self._init_engines()

    def _create_session(self) -> requests.Session:
        """创建带重试机制的 requests Session"""
        session = requests.Session()
        retry_strategy = Retry(
            total=self.RETRY_TOTAL,
            backoff_factor=self.RETRY_BACKOFF,
            status_forcelist=self.RETRY_STATUSES,
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _init_engines(self):
        """初始化所有引擎实例"""
        self.engines = {}

        # 免费引擎（无需API Key）
        self.engines["mymemory"] = MyMemoryEngine(session=self._session)
        self.engines["google"] = GoogleTranslateEngine(session=self._session)

        # 需要API Key的引擎
        key_engine_classes = {
            "baidu": BaiduTranslateEngine,
            "tencent": TencentTranslateEngine,
            "alibaba": AliTranslateEngine,
            "youdao": YoudaoTranslateEngine,
            "xunfei": XunfeiTranslateEngine,
            "microsoft": MicrosoftTranslateEngine,
        }
        for name, cls in key_engine_classes.items():
            self.engines[name] = cls(self.config, session=self._session)

    def reload_config(self):
        """重新加载配置"""
        self.config = load_config()
        self._init_engines()

    def get_available_engines(self) -> list:
        """获取所有可用的引擎列表"""
        available = []
        for name, engine in self.engines.items():
            available.append({
                "name": name,
                "display_name": engine.display_name,
                "available": engine.is_available(),
                "requires_key": engine.requires_key,
            })
        return available

    def translate(self, text: str, from_lang: str = "auto", to_lang: str = "zh",
                  engine_name: str = None) -> str:
        """
        执行翻译（自动分块、自动回退）
        
        Args:
            text: 待翻译文本（无字符数限制，自动分块）
            from_lang: 源语言
            to_lang: 目标语言
            engine_name: 指定引擎名称（None则使用配置中的 selected_engine）
        
        Returns:
            翻译后的文本
        """
        if not text or not text.strip():
            return ""

        text = text.strip()

        # 确定使用的引擎列表
        if engine_name is None:
            engine_name = self.config.get("selected_engine", "auto")

        engine_order = self._get_engine_order(engine_name)

        # 分块上限取：配置值 与 所有候选引擎限制 的最小值（防止回退到限制更小的引擎时超限失败）
        max_chunk = self.config.get("max_chunk_size", 5000)
        available_limits = [
            self.engines[n].max_chars_per_request
            for n in engine_order
            if n in self.engines and self.engines[n].is_available()
        ]
        if available_limits:
            max_chunk = min(max_chunk, *available_limits)

        # 分块翻译
        if len(text) > max_chunk:
            return self._translate_chunked(text, from_lang, to_lang, engine_order, max_chunk)
        else:
            return self._translate_single(text, from_lang, to_lang, engine_order)

    def _get_engine_order(self, engine_name: str) -> list:
        """获取引擎尝试顺序"""
        fallback_order = ["mymemory", "google", "microsoft", "baidu", "youdao",
                          "tencent", "alibaba", "xunfei"]

        if engine_name == "auto":
            # 自动模式：优先使用有key的引擎，然后免费引擎
            order = []
            # 先加已配置key的付费引擎
            for name in ["baidu", "tencent", "alibaba", "youdao", "xunfei", "microsoft"]:
                engine = self.engines.get(name)
                if engine and engine.is_available():
                    order.append(name)
            # 再加免费引擎
            for name in ["mymemory", "google"]:
                engine = self.engines.get(name)
                if engine and engine.is_available():
                    order.append(name)
            return order if order else ["mymemory", "google"]
        elif engine_name in self.engines:
            engine = self.engines[engine_name]
            if engine.is_available():
                if self.config.get("fallback_enabled", True):
                    order = [engine_name]
                    for name in fallback_order:
                        if name != engine_name and name in self.engines and self.engines[name].is_available():
                            order.append(name)
                    return order
                return [engine_name]
            else:
                # 主引擎不可用，使用回退
                if self.config.get("fallback_enabled", True):
                    order = []
                    for name in fallback_order:
                        if self.engines[name].is_available():
                            order.append(name)
                    return order if order else ["mymemory", "google"]
                raise RuntimeError(f"翻译引擎 '{engine_name}' 不可用，请先配置API密钥")
        else:
            raise RuntimeError(f"未知翻译引擎: {engine_name}")

    def _translate_single(self, text: str, from_lang: str, to_lang: str,
                          engine_order: list) -> str:
        """单次翻译尝试（缓存查询 → 同 key 去重 → 请求 → 写缓存 → 失败回退）"""
        errors = []
        for name in engine_order:
            engine = self.engines.get(name)
            if engine is None or not engine.is_available():
                continue
            # 语言子集校验：引擎不支持该语言时自动回退下一引擎
            if engine.SUPPORTED_LANGS is not None:
                if from_lang != "auto" and from_lang not in engine.SUPPORTED_LANGS:
                    continue
                if to_lang not in engine.SUPPORTED_LANGS:
                    continue

            key = TranslationCache.make_key(
                text, from_lang, to_lang, name, engine.translation_cache_token
            )

            # 1. 查缓存（内存 LRU → 磁盘）
            cached = self._cache.get(key)
            if cached is not None:
                return cached

            # 2. 在途请求去重合并
            try:
                result = self._execute_with_dedup(engine, key, text, from_lang, to_lang)
            except Exception as e:
                errors.append(f"{engine.display_name}: {e}")
                continue

            # 3. 写缓存
            if result:
                self._cache.put(key, result)
            return result

        if errors:
            raise RuntimeError("所有翻译引擎均失败:\n" + "\n".join(errors))
        raise RuntimeError("所有翻译服务暂时不可用，请检查网络后重试")

    def _execute_with_dedup(self, engine, key: str, text: str,
                            from_lang: str, to_lang: str) -> str:
        """执行翻译并合并同 key 并发请求（借鉴 TranslationPlugin 的 listener 合并）

        同一缓存键的在途请求只有一个真正发网络请求，其余线程等待共享结果。
        """
        with self._in_flight_lock:
            flight = self._in_flight.get(key)
            if flight is None:
                flight = self._in_flight[key] = _Flight()
                owner = True
            else:
                owner = False

        if owner:
            try:
                result = engine.translate(text, from_lang, to_lang)
                flight.result = result
            except Exception as e:
                flight.error = e
            finally:
                flight.done.set()
                with self._in_flight_lock:
                    self._in_flight.pop(key, None)
        else:
            flight.done.wait()

        if flight.error is not None:
            raise flight.error
        return flight.result

    def _translate_chunked(self, text: str, from_lang: str, to_lang: str,
                           engine_order: list, chunk_size: int) -> str:
        """分块翻译长文本"""
        # 按句子边界分割
        chunks = self._split_text_into_chunks(text, chunk_size)
        results = []

        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            try:
                result = self._translate_single(chunk, from_lang, to_lang, engine_order)
                results.append(result)
            except Exception as e:
                # 单块失败，标记错误
                results.append(f"[翻译失败: {e}]")

        return "\n\n".join(results)

    def _split_text_into_chunks(self, text: str, max_size: int) -> list:
        """将文本按句子边界分块，尽量保持语义完整"""
        if len(text) <= max_size:
            return [text]

        chunks = []
        # 按段落分割
        paragraphs = text.split("\n")
        current_chunk = ""

        for para in paragraphs:
            if not para.strip():
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                continue

            if len(current_chunk) + len(para) + 1 <= max_size:
                if current_chunk:
                    current_chunk += "\n" + para
                else:
                    current_chunk = para
            else:
                # 当前段落会导致超限
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""

                # 如果段落本身超过 max_size，按句子分割
                if len(para) > max_size:
                    sentences = re.split(r'(?<=[.!?。！？])', para)
                    for sent in sentences:
                        sent = sent.strip()
                        if not sent:
                            continue
                        if len(current_chunk) + len(sent) + 1 <= max_size:
                            current_chunk = (current_chunk + " " + sent).strip() if current_chunk else sent
                        else:
                            if current_chunk:
                                chunks.append(current_chunk.strip())
                            current_chunk = sent
                else:
                    current_chunk = para

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks if chunks else [text]

    @staticmethod
    def detect_lang(text: str) -> str:
        """简单语言检测"""
        if re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text):
            return "zh"
        return "en"


# =========================== 全局单例 ===========================

_engine_manager = None


def get_engine_manager() -> TranslateEngineManager:
    """获取全局翻译引擎管理器实例"""
    global _engine_manager
    if _engine_manager is None:
        _engine_manager = TranslateEngineManager()
    return _engine_manager


def translate(text: str, from_lang: str = "auto", to_lang: str = "zh",
              engine_name: str = None) -> str:
    """便捷翻译函数"""
    return get_engine_manager().translate(text, from_lang, to_lang, engine_name)


def detect_lang(text: str) -> str:
    """便捷语言检测函数"""
    return TranslateEngineManager.detect_lang(text)


def get_available_engines() -> list:
    """获取可用引擎列表"""
    return get_engine_manager().get_available_engines()


def reload_config():
    """重新加载配置"""
    get_engine_manager().reload_config()
