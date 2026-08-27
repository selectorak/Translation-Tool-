"""
应用程序配置模块
==================
集中管理所有全局配置常量，包括界面尺寸、功能开关等。
"""

# ==================== 应用信息 ====================
APP_TITLE = "中英翻译助手"
APP_VERSION = "1.0"

# ==================== 剪贴板监控 ====================
CHECK_CLIPBOARD_INTERVAL = 0.6       # 剪贴板检测间隔（秒）

# ==================== 任务与轮询 ====================
CLIPBOARD_PREVIEW_CHARS = 2000       # 浮窗预览截断字符数
SELECTION_CHECK_DELAY_MS = 300       # 划词选中检测延迟（毫秒）
TASK_QUEUE_POLL_MS = 200             # 任务队列轮询间隔（毫秒）

# ==================== 浮动弹窗 ====================
FLOAT_POPUP_AUTO_HIDE = 8            # 浮动弹窗自动隐藏时间（秒）— 已改为不自动关闭，保留用于扩展
FLOAT_POPUP_WIDTH = 380
FLOAT_POPUP_HEIGHT = 220

# ==================== 主窗口 ====================
MAIN_WIN_WIDTH = 820
MAIN_WIN_HEIGHT = 620

# ==================== 翻译引擎元数据（设置对话框渲染用） ====================
ENGINE_INFO = {
    "baidu": {
        "name": "百度翻译",
        "url": "https://fanyi-api.baidu.com/",
        "fields": [("app_id", "APP ID", 30), ("secret_key", "密钥(Secret Key)", 30)],
        "note": "免费额度: 每月100万字符（标准版）"
    },
    "tencent": {
        "name": "腾讯翻译(TMT)",
        "url": "https://console.cloud.tencent.com/tmt",
        "fields": [("secret_id", "SecretId", 30), ("secret_key", "SecretKey", 30), ("region", "地域", 15)],
        "note": "免费额度: 每月500万字符"
    },
    "alibaba": {
        "name": "阿里翻译(通用版)",
        "url": "https://www.aliyun.com/product/ai/base_alimt",
        "fields": [("access_key_id", "AccessKey ID", 25), ("access_key_secret", "AccessKey Secret", 25)],
        "note": "免费额度: 每月100万字符"
    },
    "youdao": {
        "name": "有道翻译",
        "url": "https://ai.youdao.com/",
        "fields": [("app_key", "应用ID(App Key)", 25), ("app_secret", "应用密钥(App Secret)", 25)],
        "note": "免费额度: 每月100万字符"
    },
    "xunfei": {
        "name": "讯飞翻译",
        "url": "https://www.xfyun.cn/services/its",
        "fields": [("app_id", "APPID", 20), ("api_key", "APIKey", 25), ("api_secret", "APISecret", 25)],
        "note": "免费额度: 每日500次"
    },
    "microsoft": {
        "name": "微软翻译(Azure)",
        "url": "https://portal.azure.com/",
        "fields": [("subscription_key", "订阅密钥", 30), ("region", "区域(如 eastasia)", 15)],
        "note": "免费额度: 每月200万字符"
    },
}
