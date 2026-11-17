# -*- coding: utf-8 -*-
"""下载通知洗版标注（MoviePilot V3）

需求：
    宿主在「订阅洗版（升级）」触发下载时，发出的下载通知与首次订阅的下载通知完全一样，
    看不出这次是在洗版。本插件在通知标题前加一个标记（默认【洗版】），并在正文首行注明
    这次升级了哪些集，方便一眼区分「新入库」和「洗版升级」。

为什么用运行期补丁，而不是改宿主代码：
    1. 下载通知的标题/正文由 app/chain/download/processing.py 的
       DownloadProcessingOwner._build_download_notification 用通知模板渲染（Jinja2），
       模板渲染时的可用变量只有 meta / mediainfo / torrentinfo / download_episodes 等，
       拿不到「这次是不是洗版」的上下文，所以单改模板做不出条件标记；
    2. 「是不是洗版」的事实由 app/application/subscription/priority.py 的
       annotate_subscription_download 写进 Context.subscription_download，
       而 _build_download_notification 的调用方（DownloadHistoryOwner 的记录方法）手里
       正好有 context，只是没有把它传给通知构造函数；
    3. 直接改容器里的宿主文件，容器重建或镜像升级就丢；写成插件在 init_plugin 里
       打一次补丁，则每次宿主启动、插件加载时都会自动生效。

补丁做了什么（只加内容，不改判定）：
    - 只在 subscription_download.submission_type == "upgrade" 时动手；
    - 给 Message.title 加前缀（默认【洗版】，已带则不重复加）；
    - 给 Message.text 首行加一行说明：部分集洗版 -> 「洗版：升级 E03-E26」，
      全集洗版 -> 「洗版：全集覆盖」，电影 -> 「洗版：覆盖已有版本」；
    - 任何异常都只在 debug 日志里留痕，绝不阻断下载或通知。

边界：
    - 只影响「下载通知」，不改动订阅、下载记录、洗版优先级等任何数据；
    - 宿主若改了 _build_download_notification 的名字，补丁装不上，插件会在日志里告警，
      但下载与通知本身照常工作（只是没有标记）。
"""
import inspect
import threading
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

# 日志前缀
LOG_TAG = "[DownloadWashMark] "
# 默认标题前缀
DEFAULT_PREFIX = "【洗版】"
# 被补丁的目标：模块、类、方法
TARGET_MODULE = "app.chain.download.processing"
TARGET_CLASS = "DownloadProcessingOwner"
TARGET_METHOD = "_build_download_notification"

# 运行期状态（前缀/是否注明集数由配置写入）
_STATE: Dict[str, Any] = {
    "prefix": DEFAULT_PREFIX,
    "detail": True,
    "installed": False,
    "original": None,
}
_LOCK = threading.Lock()


def _episode_text(values: Any) -> str:
    """把集号集合压成 E01、E03-E05 这种紧凑写法。"""
    numbers = []
    for value in values or []:
        try:
            numbers.append(int(value))
        except (TypeError, ValueError):
            continue
    if not numbers:
        return ""
    numbers = sorted(set(numbers))
    spans: List[Tuple[int, int]] = []
    start = prev = numbers[0]
    for number in numbers[1:]:
        if number == prev + 1:
            prev = number
            continue
        spans.append((start, prev))
        start = prev = number
    spans.append((start, prev))
    parts = []
    for begin, end in spans:
        if begin == end:
            parts.append("E%02d" % begin)
        else:
            parts.append("E%02d-E%02d" % (begin, end))
    return "、".join(parts)


def _detail_line(submission: Dict[str, Any]) -> str:
    """生成正文首行的洗版说明。"""
    episodes = _episode_text(submission.get("upgrade_episodes"))
    if submission.get("best_version_full"):
        return "洗版：全集覆盖" + ("（%s）" % episodes if episodes else "")
    if episodes:
        return "洗版：升级 %s" % episodes
    # 电影，或宿主没记录具体集数
    return "洗版：覆盖已有版本"


def _decorate(message: Any, submission: Any) -> Any:
    """给洗版下载的通知加标记；非洗版或异常时原样返回。"""
    if message is None or not isinstance(submission, dict):
        return message
    if submission.get("submission_type") != "upgrade":
        return message

    prefix = _STATE.get("prefix") or DEFAULT_PREFIX
    title = getattr(message, "title", None) or ""
    if title and not title.lstrip().startswith(prefix):
        try:
            message.title = "%s%s" % (prefix, title)
        except Exception as err:  # 通知对象不可写时只记录，不阻断发送
            logger.debug("%s标题标注失败（忽略）：%s" % (LOG_TAG, err))

    if _STATE.get("detail", True):
        line = _detail_line(submission)
        if line:
            text = getattr(message, "text", None) or ""
            try:
                message.text = ("%s\n%s" % (line, text)) if text else line
            except Exception as err:
                logger.debug("%s正文标注失败（忽略）：%s" % (LOG_TAG, err))
    return message


def _find_context(depth: int = 8) -> Any:
    """从调用栈里找到带 subscription_download 的 Context。

    _build_download_notification 本身拿不到 context，它的调用方
    （DownloadHistoryOwner 的记录方法）局部变量里就有。
    """
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame else None
        for _ in range(depth):
            if frame is None:
                return None
            context = frame.f_locals.get("context")
            if context is not None and hasattr(context, "subscription_download"):
                return context
            frame = frame.f_back
    finally:
        del frame
    return None


def _install_patch() -> bool:
    """安装下载通知标注补丁（幂等）。"""
    with _LOCK:
        if _STATE.get("installed"):
            return True
        try:
            module = __import__(TARGET_MODULE, fromlist=[TARGET_CLASS])
            owner = getattr(module, TARGET_CLASS)
        except Exception as err:
            logger.error("%s找不到宿主下载链模块，洗版标注未启用：%s" % (LOG_TAG, err))
            return False

        original = getattr(owner, TARGET_METHOD, None)
        if not callable(original):
            logger.error("%s宿主下载通知方法不存在，洗版标注未启用" % LOG_TAG)
            return False
        if getattr(original, "_washmark", False):
            _STATE.update(installed=True, original=getattr(original, "_washmark_original", original))
            logger.info("%s补丁已存在，跳过重复安装" % LOG_TAG)
            return True

        def wrapper(self, *args, **kwargs):
            message = original(self, *args, **kwargs)
            try:
                context = _find_context()
                message = _decorate(message, getattr(context, "subscription_download", None))
            except Exception as err:
                logger.debug("%s标注流程异常（忽略）：%s" % (LOG_TAG, err))
            return message

        wrapper.__name__ = getattr(original, "__name__", TARGET_METHOD)
        wrapper.__doc__ = getattr(original, "__doc__", None)
        wrapper._washmark = True
        wrapper._washmark_original = original
        setattr(owner, TARGET_METHOD, wrapper)
        _STATE.update(installed=True, original=original)
        logger.info("%s已安装下载通知洗版标注补丁" % LOG_TAG)
        return True


def _uninstall_patch() -> None:
    """还原宿主方法（插件被停用时调用）。"""
    with _LOCK:
        if not _STATE.get("installed"):
            return
        original = _STATE.get("original")
        try:
            module = __import__(TARGET_MODULE, fromlist=[TARGET_CLASS])
            owner = getattr(module, TARGET_CLASS)
            if callable(original):
                setattr(owner, TARGET_METHOD, original)
                logger.info("%s已卸载下载通知洗版标注补丁" % LOG_TAG)
        except Exception as err:
            logger.warning("%s卸载补丁失败：%s" % (LOG_TAG, err))
        finally:
            _STATE.update(installed=False, original=None)


def _sample_message() -> Dict[str, Any]:
    """详情页/接口用的示例，验证标注效果（不发送通知）。"""
    class _Sample:
        title = "示例剧集 S01 E03-E26 开始下载"
        text = "站点：示例站\n质量：2160p DV"

    sample = _Sample()
    marked_tv = _decorate(sample, {
        "submission_type": "upgrade",
        "best_version_full": False,
        "upgrade_episodes": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
                             17, 18, 19, 20, 21, 22, 23, 24, 25, 26],
    })
    return {"title": marked_tv.title, "text": marked_tv.text}


class DownloadWashMark(_PluginBase):
    """下载通知洗版标注插件。"""

    # 插件名称
    plugin_name = "下载通知洗版标注"
    # 插件描述
    plugin_desc = "订阅洗版（升级）触发下载时，在下载通知标题前加【洗版】标记，并注明升级了哪些集。"
    # 插件图标
    plugin_icon = "downloadwashmark.svg"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "jiuyaozhuce"
    # 作者主页
    author_url = "https://github.com/jiuyaozhuce"
    # 插件配置项 ID 前缀
    plugin_config_prefix = "downloadwashmark_"
    # 加载顺序
    plugin_order = 32
    # 可使用的用户级别
    auth_level = 1

    def __init__(self):
        """初始化运行时状态。"""
        super().__init__()
        self._enabled = False

    def init_plugin(self, config: dict = None):
        """按配置装载或卸载补丁；保存配置后立即生效，不必重启宿主。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        prefix = (config.get("title_prefix") or DEFAULT_PREFIX)
        _STATE["prefix"] = prefix.strip() or DEFAULT_PREFIX
        _STATE["detail"] = bool(config.get("show_detail", True))
        if self._enabled:
            _install_patch()
        else:
            _uninstall_patch()

    def get_state(self) -> bool:
        """插件是否启用。"""
        return bool(self._enabled)

    def stop_service(self):
        """插件停用时还原宿主方法。"""
        _uninstall_patch()

    def get_command(self) -> List[Dict[str, Any]]:
        """无远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """提供一个只读状态接口，方便确认补丁是否生效。"""
        return [
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "summary": "查看洗版标注补丁状态",
                "description": "返回补丁是否已安装，以及一条示例标注效果。",
                "auth": "bear",
            }
        ]

    def api_status(self) -> Dict[str, Any]:
        """返回补丁状态与示例。"""
        return {
            "enabled": bool(self._enabled),
            "patched": bool(_STATE.get("installed")),
            "title_prefix": _STATE.get("prefix") or DEFAULT_PREFIX,
            "show_detail": bool(_STATE.get("detail", True)),
            "sample": _sample_message(),
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """插件配置页面。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "cols": 12,
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "density": "comfortable",
                                        },
                                        "content": [
                                            {
                                                "component": "div",
                                                "text": "只给「洗版（升级）」触发的下载通知加标记：标题前加前缀，"
                                                        "正文首行写明升级了哪些集。首次订阅的下载不受影响。",
                                            }
                                        ],
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "cols": 12,
                                "md": 4,
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "modelValue": self._enabled,
                                            "label": "启用洗版标注",
                                            "color": "primary",
                                            "hideDetails": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "cols": 12,
                                "md": 4,
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "modelValue": _STATE.get("prefix") or DEFAULT_PREFIX,
                                            "label": "标题前缀",
                                            "placeholder": DEFAULT_PREFIX,
                                            "hideDetails": True,
                                            "persistentHint": True,
                                            "hint": "默认【洗版】",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "cols": 12,
                                "md": 4,
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "modelValue": bool(_STATE.get("detail", True)),
                                            "label": "正文注明升级集数",
                                            "color": "primary",
                                            "hideDetails": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ], {
            "enabled": False,
            "title_prefix": DEFAULT_PREFIX,
            "show_detail": True,
        }

    def get_page(self) -> List[dict]:
        """插件详情页：显示补丁状态与标注示例。"""
        patched = bool(_STATE.get("installed"))
        enabled = bool(self._enabled)
        sample = _sample_message()
        if patched:
            state_text, state_color = "补丁已生效", "#43A047"
        elif enabled:
            state_text, state_color = "已启用，但补丁未安装（看日志）", "#FB8C00"
        else:
            state_text, state_color = "未启用", "#757575"
        body = (
            '<div style="max-width:760px;margin:0 auto;font-size:13px;line-height:1.7">'
            '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">'
            '<span style="width:8px;height:8px;border-radius:50%;background:%s;'
            'display:inline-block"></span>'
            '<span style="font-size:15px;font-weight:600">%s</span></div>'
            '<div style="padding:12px 14px;border-radius:10px;'
            'border:1px solid rgba(var(--v-theme-on-surface,0,0,0),.12);margin-bottom:12px">'
            '<div>标题前缀：<b>%s</b>　正文注明集数：<b>%s</b></div>'
            '<div style="opacity:.7;margin-top:4px">只标注「洗版升级」的下载通知，'
            '首次订阅的下载不受影响。</div></div>'
            '<div style="padding:12px 14px;border-radius:10px;'
            'border:1px solid rgba(var(--v-theme-on-surface,0,0,0),.12)">'
            '<div style="opacity:.7;margin-bottom:6px">标注示例</div>'
            '<div style="font-size:15px;font-weight:600">%s</div>'
            '<pre style="margin:6px 0 0;white-space:pre-wrap;word-break:break-word;'
            'font-family:inherit;font-size:13px">%s</pre></div>'
            "</div>"
        ) % (
            state_color,
            state_text,
            escape(self._current_prefix()),
            "是" if _STATE.get("detail", True) else "否",
            escape(str(sample.get("title") or "")),
            escape(str(sample.get("text") or "")),
        )
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {"component": "div", "props": {"style": "margin-top:4px"}, "html": body}
                        ],
                    }
                ],
            }
        ]

    @staticmethod
    def _current_prefix() -> str:
        """当前标题前缀。"""
        return _STATE.get("prefix") or DEFAULT_PREFIX

    @staticmethod
    def _esc(value: Any) -> str:
        """HTML 转义。"""
        return escape("" if value is None else str(value))
