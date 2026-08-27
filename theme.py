"""
颜色主题模块
==============
提供应用程序的亮色和暗色配色方案，
以及主题切换功能。
"""

class Theme:
    """现代化配色方案 — 亮色 + 暗色双主题"""

    # ==================== 亮色主题 ====================
    # 基础色
    BG = "#f5f6f8"
    CARD_BG = "#ffffff"
    BORDER = "#e0e3e8"
    TEXT = "#202124"
    TEXT_SEC = "#5f6368"
    TEXT_HINT = "#9aa0a6"

    # 功能色
    PRIMARY = "#1a73e8"
    PRIMARY_BG = "#e8f0fe"
    ACCENT = "#ea4335"
    ACCENT_BG = "#fce8e6"
    SUCCESS = "#34a853"

    # ==================== 暗色主题 ====================
    DARK_BG = "#1e1e2a"
    DARK_CARD_BG = "#282836"
    DARK_BORDER = "#3d3d50"
    DARK_TEXT = "#e8e8f0"
    DARK_TEXT_SEC = "#b0b0c0"
    DARK_TEXT_HINT = "#707080"
    DARK_PRIMARY = "#8ab4f8"
    DARK_PRIMARY_BG = "#1e2a3a"

    # ==================== 当前主题状态 ====================
    _is_dark = False

    @classmethod
    def is_dark(cls) -> bool:
        """当前是否为暗色主题"""
        return cls._is_dark

    @classmethod
    def toggle(cls) -> bool:
        """切换亮色/暗色主题，返回切换后的暗色状态"""
        cls._is_dark = not cls._is_dark
        return cls._is_dark

    @classmethod
    def set_dark(cls, dark: bool) -> None:
        """强制设置主题模式"""
        cls._is_dark = dark

    @classmethod
    def get(cls, light_attr: str, dark_attr: str | None = None) -> str:
        """
        根据当前主题返回对应颜色值。

        Args:
            light_attr: 亮色主题的属性名（如 'BG'）
            dark_attr: 暗色主题的属性名（如 'DARK_BG'），
                       默认在 light_attr 前加 'DARK_' 前缀

        Returns:
            颜色十六进制值字符串
        """
        if cls._is_dark:
            attr = dark_attr or f"DARK_{light_attr}"
            return getattr(cls, attr, getattr(cls, light_attr))
        return getattr(cls, light_attr)
