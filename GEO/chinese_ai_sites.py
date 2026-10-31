# -*- coding: utf-8 -*-
"""国内 AI 对话平台站点定义（GEO 批量抓取）。"""
from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class ChineseAISite:
    id: str
    name: str
    url: str
    extractor: str = "generic"  # kimi | generic | deepseek | doubao 等，解析器逐步扩展
    notes: str = ""


# 任务要求的 6 个平台（Ds = DeepSeek）
CHINESE_AI_SITES: Dict[str, ChineseAISite] = {
    "deepseek": ChineseAISite(
        id="deepseek",
        name="DeepSeek",
        url="https://chat.deepseek.com",
        notes="Ds；支持联网搜索需在界面开启",
    ),
    "doubao": ChineseAISite(
        id="doubao",
        name="豆包",
        url="https://www.doubao.com/chat",
        extractor="doubao",
        notes="字节跳动；网页版需登录抖音/手机号",
    ),
    "yiyan": ChineseAISite(
        id="yiyan",
        name="文心一言",
        url="https://yiyan.baidu.com",
        extractor="yiyan",
        notes="百度账号登录",
    ),
    "yuanbao": ChineseAISite(
        id="yuanbao",
        name="腾讯元宝",
        url="https://yuanbao.tencent.com/chat",
        extractor="generic",
        notes="微信/QQ/手机号；可切换混元/DeepSeek",
    ),
    "qianwen": ChineseAISite(
        id="qianwen",
        name="通义千问",
        url="https://tongyi.aliyun.com/qianwen",
        extractor="qianwen",
        notes="阿里云账号；千问 App 同账号",
    ),
    "kimi": ChineseAISite(
        id="kimi",
        name="Kimi",
        url="https://www.kimi.com",
        extractor="kimi",
        notes="登录后 kimi.com；旧域名 moonshot.cn",
    ),
}

DEFAULT_SITE_ORDER: List[str] = [
    "deepseek",
    "doubao",
    "yiyan",
    "yuanbao",
    "qianwen",
    "kimi",
]
