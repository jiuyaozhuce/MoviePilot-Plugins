# -*- coding: utf-8 -*-
"""播放进度洗版（MoviePilot V3）

功能：
    按媒体服务器（Emby / Jellyfin）上的播放进度，决定一个订阅是否还要继续洗版。

    - 电视剧：某个目标集已经看过（判定方式见配置 mode），就把该集的洗版优先级写成 100，
      这是宿主定义的「该集洗版完成」口径（app/application/subscription/priority.py），
      后续洗版搜索不会再为这一集下载更高优先级的版本；
    - 电影：已经看过就把 current_priority 写成 100，整部停止洗版；
    - 全集洗版（best_version_full）：宿主按整部判断完成，因此只有目标集全部看过才写 100。

    这样「已经看完的内容」不会被反复升级替换：省带宽、省磁盘，也不会把用户已经看过的
    剧集重洗成另一个版本。

工作方式：
    1. 定时（默认 30 分钟）、手动，或媒体服务器 Webhook 即时触发一次；
    2. 从订阅库取「订阅中（state=R）+ 已开启洗版」的订阅；
    3. 用 MediaServerHelper 取启用的 Emby/Jellyfin 实例，按订阅名称/年份/TMDB ID 在服务器上找到对应条目；
    4. 读取条目下每一集的 UserData（Played / PlaybackPositionTicks / PlayedPercentage）；
    5. 命中的集写回订阅的 episode_priority（保留既有值），并按宿主口径重算 current_priority；
    6. 可选发送通知，并在插件详情页展示最近一次执行结果。

设计说明：
    - 判定口径全部可配置：percentage（默认，进度 >= 阈值）/ any_play（进度 > 0）/ played（已看标记）；
    - 支持「试运行」：只写日志不写库，确认口径无误后再正式启用；
    - 只读媒体服务器、只写订阅表，不删除、不下载任何内容；
    - 不注册宿主的 get_service，自己在 init_plugin 里起 BackgroundScheduler，
      保存配置后立刻按新配置重新排程，不必重启宿主；
    - Webhook 即时触发（默认开启）：Emby / Jellyfin 的「播放停止」「标记为已播放」
      「标记为未播放」三类会改变观看状态的事件，经去抖合并后触发一次检查，
      不必等下一轮定时。事件本身不携带、也不改写任何订阅数据，实际动作仍由
      run() 按媒体服务器事实决定，因此重复投递无副作用。
"""
import datetime
import re
import threading
from html import escape
import traceback
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app.db.oper.subscribe import SubscribeOper
from app.schemas.types import EventType, MediaType, NotificationChannel
from app.sdk.config import settings
from app.sdk.events import eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import MediaServerHelper, ServiceConfigHelper

# 宿主定义的「洗版完成」优先级：某集/整部达到该值即不再搜索更高优先级的版本
BEST_VERSION_DONE_PRIORITY = 100
# 宿主对「未设置优先级」的默认取值
UNSET_PRIORITY = 0
# 本插件能读取播放进度的媒体服务器类型（客户端都提供 get_data / get_user）
SUPPORTED_SERVER_TYPES = ("emby", "jellyfin")
# 判定方式：进度百分比 / 有过播放 / 已看标记
MODE_PERCENTAGE = "percentage"
MODE_ANY_PLAY = "any_play"
MODE_PLAYED = "played"

# 详情页「播放进度」列最多逐个列出的未看完集数，其余只给总数（避免剧集多时把表格撑长）
WATCHED_DETAIL_LIMIT = 3
# 详情页「播放进度」列的文本长度上限，超出后只保留区间概览，保证单元格不会被撑到需要横向滚动
WATCHED_TEXT_MAX = 60

# 详情页逐集状态条：每位代表一集，按媒体库与订阅事实取最靠后的状态
TIMELINE_MISSING = "0"     # 媒体库里没有、也没有下载记录
TIMELINE_DOWNLOADED = "1"  # 已经有一份，但还没洗到最高版本
TIMELINE_UPGRADED = "2"    # 已洗到最高版本（宿主口径：优先级 100）
TIMELINE_WATCHED = "3"     # 已看过，宿主侧不会再为它洗版
# 每个状态对应的方块配色与文字说明：填充色 / 边框色 / 悬停与图例文案
TIMELINE_STYLES = {
    TIMELINE_MISSING: ("rgba(158,158,158,.3)", "rgba(158,158,158,.7)", "未下载"),
    TIMELINE_DOWNLOADED: ("#42A5F5", "#2E8BD6", "已下载"),
    TIMELINE_UPGRADED: ("#7E57C2", "#6A45AE", "已洗版"),
    TIMELINE_WATCHED: ("#43A047", "#358A39", "已观看"),
}
# 方块条最大宽度（px）：480 刚好容纳 30 集一行（12px + 3px 间距），再长就换行
TIMELINE_MAX_WIDTH = 480
# 单个方块边长与间距（px）
TIMELINE_BOX_SIZE = 12
TIMELINE_GAP = 3
# 详情页状态徽章的圆点颜色（中性底 + 语义色点，深浅色主题都不需要改）
BADGE_DOT = {
    "done": "#43A047",   # 已写入跳过标记
    "dry": "#42A5F5",    # 试运行
    "error": "#E53935",  # 读取失败
    "muted": "#757575",  # 无变化
}

# 订阅「最近活跃」的判定来源，按可信度从高到低：最近观看 → 订阅最近更新 → 订阅创建时间。
# 详情页按它倒序排列，最近在看的排在前面。
ACTIVITY_WATCH = "最近观看"
ACTIVITY_UPDATE = "最近更新"
ACTIVITY_CREATED = "订阅于"
# 媒体服务器活动日志的取样条数：Emby/Jellyfin 的列表接口不返回 LastPlayedDate，
# 只能靠活动日志（含播放/标记已看事件）反推「最近观看时间」。
ACTIVITY_LOG_LIMIT = 500
# 活动日志里算作「观看活动」的事件类型（统一小写比对，Emby 带点、Jellyfin 不带点）
ACTIVITY_LOG_EVENTS = {
    "playback.start", "playbackstart",
    "playback.stop", "playbackstop",
    "item.markplayed", "markplayed",
}

# 读取播放进度失败的原因（写进日志与详情页，避免静默跳过）
PLAYBACK_STATUS_TEXT = {
    "no_user": "没有可统计的用户",
    "not_found": "找不到对应条目",
}

# Webhook 即时触发：只关心会改变观看状态的事件。
# Emby 原生事件名带点（playback.stop / item.markplayed），Jellyfin 多为无点写法
# （PlaybackStop / MarkPlayed），统一转小写后比对；播放开始 / 暂停 / 心跳一律忽略，
# 避免短时间内刷爆检查任务。
WEBHOOK_WATCH_EVENTS = {
    "playback.stop", "playbackstop", "media.stop",
    "item.markplayed", "markplayed",
    "item.markunplayed", "markunplayed",
}
# Webhook 命中的媒体类型（Emby: TV / MOV / AUD；Jellyfin 偶见 SHOW）
WEBHOOK_ITEM_TYPES = ("TV", "SHOW", "MOV")
# 去抖延迟默认值（秒）
WEBHOOK_DEFAULT_DELAY = 20


class PlaybackBestVersion(_PluginBase):
    """播放进度洗版插件。

    按媒体服务器上的播放进度决定订阅是否继续洗版：已看过的剧集/电影不再重复洗版。
    """

    # 插件名称
    plugin_name = "播放进度洗版"
    # 插件描述
    plugin_desc = "按媒体服务器的播放进度决定订阅是否继续洗版：已看过的剧集/电影不再重复洗版。"
    # 插件图标
    plugin_icon = "playbackbestversion.svg"
    # 插件版本
    plugin_version = "3.4.0"
    # 插件作者
    plugin_author = "jiuyaozhuce"
    # 作者主页
    author_url = "https://github.com/jiuyaozhuce"
    # 插件配置项 ID 前缀
    plugin_config_prefix = "playbackbestversion_"
    # 加载顺序
    plugin_order = 31
    # 可使用的用户级别
    auth_level = 1

    # 日志前缀
    LOG_TAG = "[PlaybackBestVersion] "

    # 通知渠道类型（宿主通知配置的 type）-> 通知渠道枚举
    NOTIFY_TYPE_MAP = {
        "telegram": NotificationChannel.Telegram,
        "wechat": NotificationChannel.Wechat,
        "feishu": NotificationChannel.Feishu,
        "wechatclawbot": NotificationChannel.WechatClawBot,
        "slack": NotificationChannel.Slack,
        "discord": NotificationChannel.Discord,
        "dingtalk": NotificationChannel.DingTalk,
        "synologychat": NotificationChannel.SynologyChat,
        "vocechat": NotificationChannel.VoceChat,
        "web": NotificationChannel.Web,
        "webpush": NotificationChannel.WebPush,
        "apppush": NotificationChannel.AppPush,
        "qqbot": NotificationChannel.QQ,
    }

    def __init__(self):
        """初始化运行时状态，配置项由 init_plugin 注入。"""
        super().__init__()
        # 是否启用
        self._enabled = False
        # 保存配置后是否立即运行一次（跑完自动复位）
        self._onlyonce = False
        # 是否发送通知
        self._notify = False
        # 通知渠道（渠道类型，如 telegram / wechat）
        self._notify_channel: List[str] = []
        # 检测间隔（分钟）
        self._interval = 30
        # 参与检测的媒体服务器名称，空表示全部支持的启用服务器
        self._servers: List[str] = []
        # 只统计这些用户名的播放进度，空表示使用服务器配置的用户
        self._usernames = ""
        # 判定方式
        self._mode = MODE_PERCENTAGE
        # 进度阈值（百分比）
        self._threshold = 90
        # 是否处理电视剧
        self._handle_tv = True
        # 是否处理电影
        self._handle_movie = True
        # 试运行：只记录不写库
        self._dry_run = False
        # 是否接收媒体服务器 Webhook 即时触发
        self._webhook_trigger = True
        # Webhook 去抖延迟（秒）：窗口内多次事件合并成一次检查，0 表示立即
        self._webhook_delay = WEBHOOK_DEFAULT_DELAY
        # Webhook 去抖定时器与保护锁（独立于定时调度器，保存配置重排不会丢任务）
        self._webhook_timer: Optional[threading.Timer] = None
        self._webhook_timer_lock = threading.Lock()
        # 定时调度器
        self._scheduler: Optional[BackgroundScheduler] = None
        # 互斥锁：避免定时与手动触发同时执行
        self._lock = threading.Lock()
        # 最近一次执行结果（详情页与通知用）
        self._last_run: Dict[str, Any] = {}
        # 活动日志缓存（每轮检查只拉一次，键 = 服务器名），用于「最近观看时间」
        self._activity_cache: Dict[str, Dict[str, float]] = {}

    def init_plugin(self, config: dict = None):
        """初始化插件：读取配置、按需立即运行一次，并启动定时调度。

        :param config: 插件配置字典，来自宿主的插件配置存储
        """
        # 先停掉旧调度器，避免保存配置后出现两份定时任务
        self.stop_service()


        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._notify = bool(config.get("notify"))
        self._notify_channel = self._as_list(config.get("notify_channel"))
        self._interval = max(1, self._as_int(config.get("interval_minutes"), 30))
        self._servers = self._as_list(config.get("servers"))
        self._usernames = (config.get("usernames") or "").strip() \
            if isinstance(config.get("usernames"), str) else ""
        self._mode = config.get("mode") or MODE_PERCENTAGE
        self._threshold = min(100, max(1, self._as_int(config.get("threshold"), 90)))
        self._handle_tv = bool(config.get("handle_tv", True))
        self._handle_movie = bool(config.get("handle_movie", True))
        self._dry_run = bool(config.get("dry_run"))
        self._webhook_trigger = bool(config.get("webhook_trigger", True))
        self._webhook_delay = max(0, self._as_int(config.get("webhook_delay"), WEBHOOK_DEFAULT_DELAY))

        # 恢复上次执行结果，让详情页在重启后仍有内容
        self._last_run = self.get_data("last_run") or {}

        if not self._enabled:
            logger.info(f"{self.LOG_TAG}插件未启用")
        else:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._onlyonce:
                run_date = datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                self._scheduler.add_job(
                    self._run_once,
                    "date",
                    run_date=run_date,
                    name="播放进度洗版-立即运行一次",
                )
                logger.info(f"{self.LOG_TAG}已注册「立即运行一次」任务：{run_date}")
            self._scheduler.add_job(
                self._run_once,
                "interval",
                minutes=self._interval,
                name="播放进度洗版-定时检测",
            )
            self._scheduler.start()
            logger.info(f"{self.LOG_TAG}定时检测已启动，间隔 {self._interval} 分钟")

        # 把规范化后的配置写回，避免表单里残留空值/非法值
        if self._current_config() != config:
            self.update_config(self._current_config())

    def get_state(self) -> bool:
        """返回插件当前是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """没有远程命令，返回空列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """提供「立即执行一次」的插件接口，便于在详情页手动验证口径。"""
        return [
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["GET"],
                "summary": "立即执行一次检查",
                "description": "按当前配置扫描一遍订阅与播放进度，返回本轮执行摘要",
            }
        ]

    def get_page(self) -> Optional[List[dict]]:
        """插件详情页：执行摘要 + 逐订阅卡片 + 折叠的未匹配清单。

        不用表格：Vuetify 的 VTable 在手机竖屏会把「跳过洗版」这类窄表头压成竖排、
        把标题和方块条挤成碎行（实测 390px 下表格列宽只剩 52~119px，行高 192px）。
        改成 flex 卡片后，宽屏一行放下标题/徽章/方块条，窄屏自动堆叠，永不横向滚动。
        """
        last = self._last_run or {}
        records = self._sorted_records(last.get("records") or [])
        matched = [record for record in records if record.get("server")]
        unmatched = [record for record in records if not record.get("server")]

        page: List[dict] = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "div",
                                "props": {"style": "max-width: 960px; margin: 0 auto"},
                                "html": self._summary_html(last),
                            }
                        ],
                    }
                ],
            }
        ]

        blocks: List[str] = []
        if records:
            blocks.append(self._legend_html())
            if matched:
                blocks.append(
                    '<div style="display:flex;flex-direction:column;gap:8px;margin-top:10px">'
                    + "".join(self._card_html(record) for record in matched)
                    + "</div>"
                )
            else:
                blocks.append(
                    '<div style="margin-top:10px;font-size:13px;line-height:1.6;'
                    'opacity:.7">本轮没有任何订阅匹配到媒体库条目，先确认名称/年份/TMDB ID '
                    '与媒体服务器一致，或该订阅尚未下载入库。</div>'
                )
            if unmatched:
                blocks.append(self._unmatched_html(unmatched))
            blocks.append(self._explain_html())

        if blocks:
            page.append(
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"style": "max-width: 960px; margin: 0 auto"},
                                    "html": "".join(blocks),
                                }
                            ],
                        }
                    ],
                }
            )
        return page

    def _summary_html(self, last: Dict[str, Any]) -> str:
        """顶部状态条：自建而不是 VAlert —— VAlert 会把长文本块整体居中，
        中文还会被拆成「· 检 / 查 28 个订阅」这种断词。这里逐段 nowrap + 左对齐。"""
        ok = bool(last.get("success")) and bool(last)
        tone = "info" if ok else "warning"
        items: List[str] = []
        if not last:
            items.append("尚未执行过检查")
        else:
            items.append(f"最近一次执行 {last.get('time') or '-'}")
            if last.get("message"):
                items.append(str(last["message"]))
            if last.get("records"):
                items.append(f"写入 {last.get('changed') or 0} 个跳过标记")
            if self._dry_run:
                items.append("试运行，未写库")
        spans = "".join(
            f'<span style="white-space:nowrap">{escape(item)}</span>' for item in items
        )
        return (
            f'<div style="display:flex;flex-wrap:wrap;gap:2px 12px;align-items:center;'
            f'padding:8px 12px;border-radius:8px;font-size:13px;line-height:1.6;'
            f'background:rgba(var(--v-theme-{tone},33,150,243),.12);'
            f'border:1px solid rgba(var(--v-theme-{tone},33,150,243),.3);'
            f'color:rgba(var(--v-theme-on-surface,0,0,0),.85)">' + spans + "</div>"
        )

    def stop_service(self):
        """停用插件时关闭定时调度器，并取消尚未触发的 Webhook 去抖任务。"""
        with self._webhook_timer_lock:
            if self._webhook_timer:
                try:
                    self._webhook_timer.cancel()
                except Exception as err:
                    logger.debug(f"{self.LOG_TAG}取消 Webhook 去抖任务失败：{err}")
                self._webhook_timer = None
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception as err:
                logger.error(f"{self.LOG_TAG}停止调度器失败：{err}")
        self._scheduler = None

    def get_form(self) -> List[dict]:
        """插件配置页面（Vuetify JSON 配置），返回 (表单, 默认值)。"""
        # 媒体服务器下拉：仅列出 Emby / Jellyfin
        server_items: List[dict] = []
        try:
            for name, service in (MediaServerHelper().get_services() or {}).items():
                server_type = (getattr(service, "type", "") or "").lower()
                if server_type in SUPPORTED_SERVER_TYPES and name:
                    server_items.append({"title": f"{name}（{server_type}）", "value": name})
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}读取媒体服务器配置失败：{err}")

        # 通知渠道下拉：已启用渠道的类型去重（如 telegram / wechat）
        notify_items: List[dict] = []
        try:
            seen_types = set()
            for conf in (ServiceConfigHelper.get_notification_configs() or []):
                if not getattr(conf, "enabled", False):
                    continue
                conf_type = getattr(conf, "type", "") or ""
                conf_name = getattr(conf, "name", "") or conf_type
                if conf_type and conf_type not in seen_types:
                    seen_types.add(conf_type)
                    notify_items.append({"title": conf_name, "value": conf_type})
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}读取通知渠道配置失败：{err}")

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "title": "⏯ 播放进度洗版",
                                            "text": "按媒体服务器的播放进度决定订阅是否继续洗版：已看过的剧集不再为它洗版，"
                                                    "已看过的电影整部停止洗版。只读媒体服务器、只写订阅表，"
                                                    "不删除、不下载任何内容。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "dry_run", "label": "试运行（只记日志）"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "servers",
                                            "label": "媒体服务器",
                                            "placeholder": "留空表示全部已启用的 Emby / Jellyfin",
                                            "items": server_items,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "usernames",
                                            "label": "统计哪些用户",
                                            "placeholder": "留空表示服务器配置的用户，多个用英文逗号分隔",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "mode",
                                            "label": "判定方式",
                                            "items": [
                                                {"title": "播放进度达到阈值", "value": MODE_PERCENTAGE},
                                                {"title": "只要播放过", "value": MODE_ANY_PLAY},
                                                {"title": "标记为已看", "value": MODE_PLAYED},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "threshold",
                                            "label": "进度阈值（%）",
                                            "placeholder": "仅「播放进度达到阈值」时生效，默认 90",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "interval_minutes",
                                            "label": "检测间隔（分钟）",
                                            "placeholder": "默认 30",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "handle_tv", "label": "处理电视剧"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "handle_movie", "label": "处理电影"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "notify_channel",
                                            "label": "通知渠道",
                                            "placeholder": "留空表示不发送通知",
                                            "items": notify_items,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "title": "Webhook 即时触发",
                                            "text": "在 Emby「设置 → Webhook」新增 Webhook，URL 填 "
                                                    "http://<MoviePilot地址>/api/v1/webhook/?token=<API_TOKEN>&source=<媒体服务器名>，"
                                                    "请求方式 POST，事件勾选「播放停止」「标记为已播放」「标记为未播放」。"
                                                    "收到后按去抖延迟合并多次事件只跑一次检查；定时检测仍然保留。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "webhook_trigger",
                                            "label": "接收 Webhook 触发检查",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "webhook_delay",
                                            "label": "去抖延迟（秒）",
                                            "placeholder": "默认 20；0 表示立即执行",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], self._current_config()

    def api_run(self) -> Dict[str, Any]:
        """插件 API：立即执行一次检查。"""
        return self.run(force=True)

    # ---- 调度入口 ----

    def _run_once(self):
        """调度入口：执行一次检查，并把「立即运行一次」标记复位。"""
        try:
            self.run()
        except Exception as err:
            logger.error(f"{self.LOG_TAG}执行异常：{err}\n{traceback.format_exc()}")
        finally:
            if self._onlyonce:
                self._onlyonce = False
                try:
                    self.update_config(self._current_config())
                except Exception as err:
                    logger.error(f"{self.LOG_TAG}复位「立即运行一次」失败：{err}")

    # ---- Webhook 即时触发 ----

    @eventmanager.register(EventType.WebhookMessage)
    def on_webhook_message(self, event):
        """接收 Emby / Jellyfin Webhook，仅处理会改变观看状态的事件。

        event_data 是 WebhookEventInfo（兼容 dict），字段用 getattr 取值。
        事件本身不携带、也不改写任何订阅数据，只安排一次去抖检查；
        实际写入仍由 run() 按媒体服务器事实决定，重复投递无副作用。
        """
        try:
            if not self._enabled or not self._webhook_trigger:
                return
            info = getattr(event, "event_data", None)
            if info is None:
                return

            def g(name, default=None):
                if isinstance(info, dict):
                    return info.get(name, default)
                return getattr(info, name, default)

            channel = str(g("channel") or "").strip().lower()
            server_name = str(g("server_name") or "").strip()
            # 只认 Emby / Jellyfin；没有 channel 时至少要求有服务器名，避免误收其它来源
            if channel:
                if channel not in SUPPORTED_SERVER_TYPES:
                    return
            elif not server_name:
                return
            # 配置了参与检测的服务器名单时，其它服务器的事件不触发全量检查
            if self._servers and server_name and server_name not in self._servers:
                logger.debug(f"{self.LOG_TAG}忽略 Webhook：服务器 {server_name} 不在检测名单内")
                return

            raw_event = str(g("event") or "").strip().lower()
            if raw_event not in WEBHOOK_WATCH_EVENTS:
                logger.debug(f"{self.LOG_TAG}忽略 Webhook 事件：{raw_event or '(空)'}")
                return
            item_type = str(g("item_type") or "").strip().upper()
            if item_type and item_type not in WEBHOOK_ITEM_TYPES:
                return

            reason = "Webhook[%s] event=%s item=%s 用户=%s 进度=%s" % (
                channel or server_name or "?", raw_event,
                g("item_name") or "-", g("user_name") or "-", g("percentage") or "-")
            self._schedule_webhook_run(reason)
        except Exception as err:
            logger.error(f"{self.LOG_TAG}处理 Webhook 事件异常：{err}\n{traceback.format_exc()}")

    def _schedule_webhook_run(self, reason: str) -> None:
        """去抖：窗口内多次事件只保留最后一次，延迟到期后跑一轮检查。"""
        wait = max(0, self._as_int(self._webhook_delay, WEBHOOK_DEFAULT_DELAY))
        with self._webhook_timer_lock:
            # 取消上一个尚未触发的定时器，实现「合并多次事件」
            if self._webhook_timer:
                try:
                    self._webhook_timer.cancel()
                except Exception:
                    pass
            timer = threading.Timer(wait, self._webhook_run, args=(reason, wait))
            timer.daemon = True
            self._webhook_timer = timer
            try:
                timer.start()
            except Exception as err:
                self._webhook_timer = None
                logger.error(f"{self.LOG_TAG}安排 Webhook 检查失败：{err}")
                return
        logger.info(f"{self.LOG_TAG}{reason}｜{wait} 秒后执行一次检查"
                    + ("（试运行）" if self._dry_run else ""))

    def _webhook_run(self, reason: str = "", wait: int = 0) -> None:
        """去抖到期后执行一轮检查（复用 run 的任务锁，不与定时任务并发）。"""
        with self._webhook_timer_lock:
            self._webhook_timer = None
        try:
            result = self.run()
            logger.info(f"{self.LOG_TAG}Webhook 检查完成（{reason}）：{result.get('message') or ''}")
        except Exception as err:
            logger.error(f"{self.LOG_TAG}Webhook 检查异常：{err}\n{traceback.format_exc()}")

    # ---- 主流程 ----

    def run(self, force: bool = False) -> Dict[str, Any]:
        """执行一轮检查。

        :param force: 手动触发时为 True，可绕过「插件未启用」判断（便于先用试运行观察）
        :return: 本轮执行摘要
        """
        if not self._enabled and not force:
            return {"success": False, "message": "插件未启用", "records": []}
        if not self._lock.acquire(blocking=False):
            logger.info(f"{self.LOG_TAG}上一轮检查尚未结束，跳过本轮")
            return {"success": False, "message": "上一轮检查尚未结束", "records": []}

        started = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 每轮重新拉活动日志，保证「最近观看」跟着实际播放走
        self._activity_cache = {}
        try:
            subscribes = self._get_best_version_subscribes()
            servers = self._get_servers()
            if not subscribes:
                summary: Dict[str, Any] = {
                    "success": True,
                    "message": "没有需要检查的洗版订阅",
                    "time": started,
                    "records": [],
                    "changed": 0,
                }
            elif not servers:
                summary = {
                    "success": False,
                    "message": "没有可用的 Emby / Jellyfin 媒体服务器",
                    "time": started,
                    "records": [],
                    "changed": 0,
                }
            else:
                records = []
                for subscribe in subscribes:
                    record = self._process(subscribe, servers)
                    if record:
                        records.append(record)
                # 最近活跃的排前面（观看时间 > 订阅更新时间 > 订阅创建时间）
                records = self._sorted_records(records)
                changed = sum(len(record.get("changed") or []) for record in records)
                summary = {
                    "success": True,
                    "message": f"检查 {len(records)} 个订阅",
                    "time": started,
                    "records": records,
                    "changed": changed,
                }
                logger.info(f"{self.LOG_TAG}{summary['message']}，本轮共写入 {changed} 个跳过标记")
                if self._notify and changed:
                    self._send_notify(records)

            self._last_run = summary
            self.save_data("last_run", summary)
            return summary
        except Exception as err:
            logger.error(f"{self.LOG_TAG}检查失败：{err}\n{traceback.format_exc()}")
            return {"success": False, "message": f"检查失败：{err}", "time": started, "records": []}
        finally:
            self._lock.release()

    def _get_best_version_subscribes(self) -> List[Any]:
        """取「订阅中 + 已开启洗版 + 尚未完成」的订阅。"""
        try:
            subscribes = SubscribeOper().list(state="R") or []
        except Exception as err:
            logger.error(f"{self.LOG_TAG}读取订阅失败：{err}")
            return []

        result = []
        for subscribe in subscribes:
            if not getattr(subscribe, "best_version", False):
                continue
            media_type = self._media_type(subscribe)
            if media_type is None:
                continue
            if media_type == MediaType.TV and not self._handle_tv:
                continue
            if media_type == MediaType.MOVIE and not self._handle_movie:
                continue
            if self._is_done(subscribe, media_type):
                continue
            result.append(subscribe)
        return result

    def _process(self, subscribe, servers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """处理单个订阅：找到媒体服务器条目、读取进度、命中则写库。

        :param subscribe: 订阅记录
        :param servers: 可用的媒体服务器列表
        :return: 执行记录（供详情页与通知使用）
        """
        media_type = self._media_type(subscribe)
        active_ts, active_label = self._activity_of(subscribe, {})
        record: Dict[str, Any] = {
            "title": self._title(subscribe),
            "type": media_type.value if media_type else "",
            "season": getattr(subscribe, "season", None) or "",
            "server": "",
            "watched": "",
            "timeline": None,
            "changed": [],
            "status": "未找到条目",
            # 排序键：拿不到播放进度时先按「订阅最近更新 / 创建时间」占位，读到进度后再换成观看时间
            "active_ts": active_ts,
            "active_label": active_label,
        }

        for server in servers:
            try:
                playback = self._fetch_playback(server, subscribe, media_type)
            except Exception as err:
                logger.warning(f"{self.LOG_TAG}{record['title']} 读取 {server['name']} 播放进度失败：{err}")
                record["status"] = f"{server['name']} 读取失败：{err}"
                continue
            status = (playback or {}).get("status")
            if status != "ok":
                # 以前这里是静默 continue，出了问题完全查不到原因；
                # 现在把原因写进日志和详情页（no_user / not_found / 读取异常）
                reason = PLAYBACK_STATUS_TEXT.get(status, f"读取播放进度失败（{status or '无返回'}）")
                logger.info(f"{self.LOG_TAG}{record['title']} 在 {server['name']} 上{reason}，跳过")
                record["status"] = f"{server['name']} {reason}"
                continue

            record["server"] = server["name"]
            record["watched"] = self._describe_watched(playback)
            record["timeline"] = self._build_timeline(subscribe, media_type, playback)
            # 读到进度后，活跃时间改用「最近观看」（比订阅更新时间更贴近用户真正在追什么）
            record["active_ts"], record["active_label"] = self._activity_of(subscribe, playback)
            changed = self._apply(subscribe, media_type, playback)
            if changed:
                record["changed"] = changed
                record["status"] = "已跳过" + ("（试运行）" if self._dry_run else "")
            else:
                record["status"] = "无变化"
            return record

        return record

    def _apply(self, subscribe, media_type: MediaType, playback: Dict[str, Any]) -> List[Any]:
        """把命中结果写回订阅。

        :return: 本次写入的跳过标记（剧集号列表，或 ["整部"]），无变化时返回空列表
        """
        if media_type == MediaType.TV:
            return self._apply_tv(subscribe, playback.get("episodes") or {})
        return self._apply_movie(subscribe, playback.get("movie") or {})

    def _apply_tv(self, subscribe, episodes: Dict[int, Dict[str, Any]]) -> List[int]:
        """电视剧：把已看过的集写成洗版完成优先级。

        :param subscribe: 订阅记录
        :param episodes: 集号 -> 播放进度
        :return: 本次写入的集号列表
        """
        target = self._target_episodes(subscribe)
        if not target:
            logger.info(f"{self.LOG_TAG}{self._title(subscribe)} 缺少集数信息，跳过")
            return []

        hit = [episode for episode in target if episode in episodes and self._hit(episodes[episode])]
        if not hit:
            return []

        current_priority = self._as_int(getattr(subscribe, "current_priority", None), UNSET_PRIORITY)
        if getattr(subscribe, "best_version_full", False):
            # 全集洗版：宿主按整部判断完成，只有目标集全部看过才停止洗版
            if len(hit) < len(target) or current_priority >= BEST_VERSION_DONE_PRIORITY:
                return []
            self._update_subscribe(subscribe, {"current_priority": BEST_VERSION_DONE_PRIORITY})
            return ["整部"]

        priorities = self._episode_priorities(subscribe, target)
        changed = []
        for episode in hit:
            if priorities.get(str(episode), UNSET_PRIORITY) < BEST_VERSION_DONE_PRIORITY:
                priorities[str(episode)] = BEST_VERSION_DONE_PRIORITY
                changed.append(episode)
        if not changed:
            return []

        # 按宿主口径重算整部优先级：目标集范围内的最小值
        # （app/application/subscription/priority.py:get_best_version_current_priority）
        new_priority = min(
            (priorities.get(str(episode), UNSET_PRIORITY) for episode in target),
            default=UNSET_PRIORITY,
        )
        self._update_subscribe(
            subscribe,
            {"episode_priority": priorities, "current_priority": new_priority},
        )
        return changed

    def _apply_movie(self, subscribe, movie: Dict[str, Any]) -> List[str]:
        """电影：已看过则整部停止洗版。

        :param subscribe: 订阅记录
        :param movie: 电影的播放进度
        :return: 命中时返回 ["整部"]
        """
        if not movie or not self._hit(movie):
            return []
        current_priority = self._as_int(getattr(subscribe, "current_priority", None), UNSET_PRIORITY)
        if current_priority >= BEST_VERSION_DONE_PRIORITY:
            return []
        self._update_subscribe(subscribe, {"current_priority": BEST_VERSION_DONE_PRIORITY})
        return ["整部"]

    def _update_subscribe(self, subscribe, payload: Dict[str, Any]):
        """写回订阅；试运行时只记录日志。

        :param subscribe: 订阅记录
        :param payload: 要更新的字段
        """
        payload = dict(payload)
        payload.setdefault("last_update", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        title = self._title(subscribe)
        if self._dry_run:
            logger.info(f"{self.LOG_TAG}[试运行] {title} 将写入：{payload}")
            return
        try:
            SubscribeOper().update(subscribe.id, payload)
            logger.info(f"{self.LOG_TAG}{title} 已写入：{payload}")
        except Exception as err:
            logger.error(f"{self.LOG_TAG}{title} 写回订阅失败：{err}")

    # ---- 媒体服务器读取 ----

    def _get_servers(self) -> List[Dict[str, Any]]:
        """取参与检测的媒体服务器实例。"""
        try:
            services = MediaServerHelper().get_services() or {}
        except Exception as err:
            logger.error(f"{self.LOG_TAG}读取媒体服务器失败：{err}")
            return []

        result = []
        for name, service in services.items():
            server_type = (getattr(service, "type", "") or "").lower()
            if server_type not in SUPPORTED_SERVER_TYPES:
                continue
            if self._servers and name not in self._servers:
                continue
            client = getattr(service, "instance", None)
            if client is None:
                continue
            result.append({"name": name, "type": server_type, "client": client})

        if not result:
            logger.warning(f"{self.LOG_TAG}没有匹配到可用的 Emby / Jellyfin 服务器（当前配置：{self._servers or '全部'}）")
        return result

    def _fetch_playback(self, server: Dict[str, Any], subscribe, media_type: MediaType) -> Dict[str, Any]:
        """读取订阅对应条目在媒体服务器上的播放进度。

        :param server: 媒体服务器信息（name / type / client）
        :param subscribe: 订阅记录
        :param media_type: 媒体类型
        :return: {"status": "ok"|"not_found"|"no_user", "episodes"|"movie": ...}
        """
        client = server["client"]
        # Emby 客户端的内部路径带 emby/ 前缀，Jellyfin 不带
        prefix = "emby" if server["type"] == "emby" else ""
        user_ids = self._resolve_users(client)
        if not user_ids:
            return {"status": "no_user"}

        item = self._find_item(client, prefix, user_ids[0], subscribe, media_type)
        if not item or not item.get("Id"):
            return {"status": "not_found"}

        if media_type == MediaType.MOVIE:
            movie: Dict[str, Any] = {}
            last_ts = 0.0
            for user_id in user_ids:
                data = self._get_json(
                    client,
                    f"[HOST]{prefix}/Users/{user_id}/Items/{item['Id']}?Fields=UserData&api_key=[APIKEY]",
                )
                progress = self._progress_of(data or {})
                last_ts = max(last_ts, float(progress.get("last_ts") or 0))
                if progress["percentage"] > float(movie.get("percentage") or 0) \
                        or (progress["played"] and not movie.get("played")):
                    movie = progress
            if not last_ts:
                last_ts = self._watch_ts_from_activity(
                    getattr(subscribe, "name", ""),
                    self._activity_index(client, prefix, server["name"]),
                )
            return {"status": "ok", "movie": movie, "last_ts": last_ts}
        episodes: Dict[int, Dict[str, Any]] = {}
        last_ts = 0.0
        for user_id in user_ids:
            data = self._get_json(
                client,
                f"[HOST]{prefix}/Users/{user_id}/Items?ParentId={item['Id']}&Recursive=true"
                f"&IncludeItemTypes=Episode&Fields=UserData,IndexNumber,ParentIndexNumber&api_key=[APIKEY]",
            )
            for episode_item in (data or {}).get("Items") or []:
                if not self._season_match(subscribe, episode_item):
                    continue
                number = self._as_int(episode_item.get("IndexNumber"), 0)
                if number <= 0:
                    continue
                progress = self._progress_of(episode_item)
                # 整季最近一次观看时间 = 各集 LastPlayedDate 的最大值
                last_ts = max(last_ts, float(progress.get("last_ts") or 0))
                exists = episodes.get(number)
                if not exists or progress["percentage"] > exists["percentage"] \
                        or (progress["played"] and not exists["played"]):
                    episodes[number] = progress
        if not last_ts:
            last_ts = self._watch_ts_from_activity(
                getattr(subscribe, "name", ""),
                self._activity_index(client, prefix, server["name"]),
            )
        return {"status": "ok", "episodes": episodes, "last_ts": last_ts}

    def _find_item(self, client, prefix: str, user_id, subscribe, media_type: MediaType) -> Optional[Dict[str, Any]]:
        """在媒体服务器上按名称/年份/TMDB ID 找订阅对应的剧集或电影条目。"""
        name = (getattr(subscribe, "name", "") or "").strip()
        if not name:
            return None

        item_type = "Series" if media_type == MediaType.TV else "Movie"
        data = self._get_json(
            client,
            f"[HOST]{prefix}/Users/{user_id}/Items?Recursive=true&IncludeItemTypes={item_type}"
            f"&SearchTerm={quote(name)}&Fields=ProductionYear,ProviderIds&Limit=20&api_key=[APIKEY]",
        )
        items = (data or {}).get("Items") or []
        if not items:
            return None

        year = self._as_int(getattr(subscribe, "year", None), 0)
        tmdbid = self._tmdbid(subscribe)
        best, best_score = None, 0
        for item in items:
            score = 0
            if (item.get("Name") or "").strip() == name:
                score += 4
            if year and self._as_int(item.get("ProductionYear"), 0) == year:
                score += 2
            if tmdbid and str(((item.get("ProviderIds") or {}).get("Tmdb")) or "") == str(tmdbid):
                score += 8
            if score > best_score:
                best, best_score = item, score
        return best if best_score > 0 else None

    def _activity_index(self, client, prefix: str, server_name: str) -> Dict[str, float]:
        """取「活动日志条目名 → 最近一次观看时间」索引（每个媒体服务器每轮只拉一次）。

        Emby/Jellyfin 的条目列表接口不返回 LastPlayedDate（实测 Emby 4.x 只给 Played / PlayCount），
        所以「看过但没有时间」。活动日志里每条播放 / 标记已看都带 Date 和片名，
        用片名做包含匹配即可回填「最近观看」；读不到（接口不存在 / 无权限）时返回空字典，
        调用方自然退回订阅更新时间，不影响任何洗版判定。
        """
        cached = self._activity_cache.get(server_name)
        if cached is not None:
            return cached

        index: Dict[str, float] = {}
        data = self._get_json(
            client,
            f"[HOST]{prefix}/System/ActivityLog/Entries?HasUserId=true"
            f"&Limit={ACTIVITY_LOG_LIMIT}&api_key=[APIKEY]",
        )
        for entry in (data or {}).get("Items") or []:
            if str(entry.get("Type") or "").lower() not in ACTIVITY_LOG_EVENTS:
                continue
            name = str(entry.get("Name") or "").strip()
            ts = self._parse_ts(entry.get("Date"))
            if not name or not ts:
                continue
            if ts > index.get(name, 0):
                index[name] = ts
        self._activity_cache[server_name] = index
        return index

    @staticmethod
    def _watch_ts_from_activity(title: str, index: Dict[str, float]) -> float:
        """在活动日志索引里找这个片名的最近观看时间：先精确匹配，再退化为包含匹配。"""
        target = (title or "").strip()
        if not target or not index:
            return 0.0
        exact = index.get(target)
        if exact:
            return exact
        best = 0.0
        for label, ts in index.items():
            if target in label and ts > best:
                best = ts
        return best

    def _resolve_users(self, client) -> List[Any]:
        """解析要统计的用户 ID 列表；未配置用户名时用服务器配置的用户。"""
        names = [name.strip() for name in (self._usernames or "").split(",") if name.strip()]
        if not names:
            user_id = getattr(client, "user", None)
            return [user_id] if user_id else []

        user_ids = []
        for name in names:
            try:
                user_id = client.get_user(name)
            except Exception as err:
                logger.warning(f"{self.LOG_TAG}解析媒体服务器用户 {name} 失败：{err}")
                user_id = None
            if user_id:
                user_ids.append(user_id)
        return user_ids

    @staticmethod
    def _get_json(client, url: str) -> Optional[Dict[str, Any]]:
        """发起一次媒体服务器 API 请求并解析 JSON。"""
        try:
            response = client.get_data(url)
        except Exception as err:
            logger.debug(f"{PlaybackBestVersion.LOG_TAG}请求媒体服务器失败：{err}")
            return None
        if response is None:
            return None
        try:
            return response.json()
        except Exception:
            return None

    @staticmethod
    def _progress_of(item: Dict[str, Any]) -> Dict[str, Any]:
        """把 Emby/Jellyfin 的 UserData 归一化成 {played, percentage, last_ts}。

        last_ts 是 UserData.LastPlayedDate 的 epoch 秒（没看过则为 0），详情页用它排「最近活跃」。
        """
        user_data = (item or {}).get("UserData") or {}
        played = bool(user_data.get("Played"))
        percentage = user_data.get("PlayedPercentage")
        if percentage is None:
            position = user_data.get("PlaybackPositionTicks") or 0
            runtime = (item or {}).get("RunTimeTicks") or 0
            percentage = (position / runtime * 100) if runtime else 0
        try:
            percentage = float(percentage or 0)
        except (TypeError, ValueError):
            percentage = 0.0
        if played:
            percentage = 100.0
        return {
            "played": played,
            "percentage": round(percentage, 1),
            "last_ts": PlaybackBestVersion._parse_ts(user_data.get("LastPlayedDate")),
        }

    @staticmethod
    def _parse_ts(value: Any) -> float:
        """把 ISO 字符串 / datetime 转成 epoch 秒；解析不出来返回 0（排序时沉底）。"""
        if not value:
            return 0.0
        if isinstance(value, datetime.datetime):
            return value.timestamp()
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if not text:
            return 0.0
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        # Emby 给的秒内小数是 7 位（2026-11-16T13:00:00.0000000Z），fromisoformat 只吃 6 位
        trimmed = re.match(r"^(.*\.\d{6})\d+(.*)$", text)
        if trimmed:
            text = trimmed.group(1) + trimmed.group(2)
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError:
            return 0.0
        # 不带时区的值（订阅表的 last_update / date）是宿主本地时间，按本地解释，
        # 不能当 UTC，否则会整体偏移一个时区。
        return parsed.timestamp()

    @staticmethod
    def _humanize_ts(ts: float) -> str:
        """epoch 秒 → 人话（刚刚 / N 分钟前 / N 小时前 / N 天前 / 具体日期）。"""
        if not ts:
            return ""
        moment = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        seconds = datetime.datetime.now(datetime.timezone.utc).timestamp() - ts
        if seconds < 60:
            return "刚刚"
        if seconds < 3600:
            return f"{int(seconds // 60)} 分钟前"
        if seconds < 86400:
            return f"{int(seconds // 3600)} 小时前"
        if seconds < 30 * 86400:
            return f"{int(seconds // 86400)} 天前"
        return moment.astimezone().strftime("%Y-%m-%d")

    def _activity_of(self, subscribe, playback: Dict[str, Any]) -> Tuple[float, str]:
        """订阅的「最近活跃」时间与来源：最近观看 > 订阅最近更新 > 订阅创建时间。"""
        watched = float((playback or {}).get("last_ts") or 0)
        if watched > 0:
            return watched, ACTIVITY_WATCH
        updated = self._parse_ts(getattr(subscribe, "last_update", None))
        if updated > 0:
            return updated, ACTIVITY_UPDATE
        return self._parse_ts(getattr(subscribe, "date", None)), ACTIVITY_CREATED

    @staticmethod
    def _record_ts(record: Dict[str, Any]) -> float:
        """从执行记录里取活跃时间戳（兼容旧数据：没有该字段就当 0）。"""
        try:
            return float(record.get("active_ts") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _sorted_records(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """最近活跃的排前面；时间相同按订阅名，保证每次刷新顺序稳定。"""
        return sorted(
            records or [],
            key=lambda item: (-self._record_ts(item), str(item.get("title") or "")),
        )

    def _hit(self, progress: Dict[str, Any]) -> bool:
        """按配置的判定方式判断这一集/这部电影是否算「看过」。"""
        played = bool(progress.get("played"))
        percentage = float(progress.get("percentage") or 0)
        if self._mode == MODE_PLAYED:
            return played
        if self._mode == MODE_ANY_PLAY:
            return played or percentage > 0
        return played or percentage >= self._threshold

    # ---- 订阅数据口径（与宿主 app/application/subscription/priority.py 保持一致） ----

    def _target_episodes(self, subscribe) -> List[int]:
        """订阅的目标集号范围（宿主口径：range(start_episode or 1, total_episode + 1)）。"""
        start = self._as_int(getattr(subscribe, "start_episode", None), 1) or 1
        total = self._as_int(getattr(subscribe, "total_episode", None), 0)
        if total < start:
            return []
        return list(range(start, total + 1))

    def _episode_priorities(self, subscribe, target: List[int]) -> Dict[str, int]:
        """取分集洗版优先级，并按宿主口径补齐。

        宿主在 episode_priority 为空且非全集洗版时，会用 current_priority 回退填充所有目标集
        （app/application/subscription/priority.py:get_episode_priority）。这里保持一致：
        否则一旦写入非空 map，宿主的回退失效，未写入的集会被当成 0，反而放宽洗版准入。
        """
        stored = getattr(subscribe, "episode_priority", None) or {}
        priorities: Dict[str, int] = {}
        for key, value in stored.items():
            try:
                priorities[str(int(key))] = self._as_int(value, UNSET_PRIORITY)
            except (TypeError, ValueError):
                continue

        if not priorities and not getattr(subscribe, "best_version_full", False):
            current = getattr(subscribe, "current_priority", None)
            if current is not None:
                priorities = {str(episode): self._as_int(current, UNSET_PRIORITY) for episode in target}
        return priorities

    def _is_done(self, subscribe, media_type: MediaType) -> bool:
        """订阅是否已经洗版完成（完成的不必再检查）。"""
        if media_type == MediaType.TV and not getattr(subscribe, "best_version_full", False):
            target = self._target_episodes(subscribe)
            if not target:
                # 宿主在拿不到目标集时回退成整部优先级判断
                return self._as_int(getattr(subscribe, "current_priority", None), UNSET_PRIORITY) \
                    >= BEST_VERSION_DONE_PRIORITY
            priorities = self._episode_priorities(subscribe, target)
            return all(
                priorities.get(str(episode), UNSET_PRIORITY) >= BEST_VERSION_DONE_PRIORITY
                for episode in target
            )
        return self._as_int(getattr(subscribe, "current_priority", None), UNSET_PRIORITY) \
            >= BEST_VERSION_DONE_PRIORITY

    @staticmethod
    def _media_type(subscribe) -> Optional[MediaType]:
        """订阅的媒体类型。"""
        value = getattr(subscribe, "type", None)
        if value == MediaType.TV.value:
            return MediaType.TV
        if value == MediaType.MOVIE.value:
            return MediaType.MOVIE
        return None

    @staticmethod
    def _tmdbid(subscribe) -> Optional[int]:
        """订阅的 TMDB ID（V3 用 media_source + media_id，V2 用 tmdbid）。"""
        media_source = getattr(subscribe, "media_source", None)
        value = getattr(subscribe, "media_id", None)
        if media_source not in (None, "", "themoviedb"):
            value = None
        if not value:
            value = getattr(subscribe, "tmdbid", None)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _season_match(self, subscribe, item: Dict[str, Any]) -> bool:
        """按订阅的季号过滤分集（未指定季时不限）。"""
        season = self._as_int(getattr(subscribe, "season", None), 0)
        if season <= 0:
            return True
        return self._as_int(item.get("ParentIndexNumber"), 0) == season

    # ---- 展示与通知 ----

    def _build_timeline(self, subscribe, media_type: MediaType,
                        playback: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """构造逐集状态条数据：每集编码成一个字符，详情页再渲染成小方块。

        状态取「最靠后」的事实：看过 > 洗到最高版本 > 已有一份 > 没有。
        Emby 上不存在但订阅下载记录里有、或已写入过优先级的集，同样算「已有一份」，
        避免刚下载还没入库时全被当成未下载。

        :param subscribe: 订阅记录
        :param media_type: 媒体类型
        :param playback: 媒体服务器读取结果
        :return: {"start": 起始集, "total": 结束集, "codes": 逐集状态串}，拿不到集数时返回 None
        """
        if media_type == MediaType.MOVIE:
            movie = playback.get("movie") or {}
            if not movie:
                return None
            return {
                "start": 1,
                "total": 1,
                "codes": TIMELINE_WATCHED if self._hit(movie) else TIMELINE_DOWNLOADED,
            }

        target = self._target_episodes(subscribe)
        if not target:
            return None

        episodes = playback.get("episodes") or {}
        priorities = self._stored_priorities(subscribe)
        noted = self._note_episodes(subscribe)
        codes: List[str] = []
        for episode in target:
            progress = episodes.get(episode)
            priority = priorities.get(str(episode), UNSET_PRIORITY)
            downloaded = progress is not None or episode in noted or priority > UNSET_PRIORITY
            if not downloaded:
                codes.append(TIMELINE_MISSING)
            elif progress is not None and self._hit(progress):
                codes.append(TIMELINE_WATCHED)
            elif priority >= BEST_VERSION_DONE_PRIORITY:
                codes.append(TIMELINE_UPGRADED)
            else:
                codes.append(TIMELINE_DOWNLOADED)
        return {"start": target[0], "total": target[-1], "codes": "".join(codes)}

    @staticmethod
    def _stored_priorities(subscribe) -> Dict[str, int]:
        """订阅里已经写入的分集优先级（不做 current_priority 回退填充，只认真实记录的集）。"""
        result: Dict[str, int] = {}
        for key, value in (getattr(subscribe, "episode_priority", None) or {}).items():
            try:
                result[str(int(key))] = PlaybackBestVersion._as_int(value, UNSET_PRIORITY)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _note_episodes(subscribe) -> set:
        """订阅下载记录（note）里出现过的集号。"""
        raw = getattr(subscribe, "note", None) or []
        if isinstance(raw, dict):
            raw = list(raw.keys())
        elif isinstance(raw, str):
            raw = raw.split(",")
        result = set()
        for item in raw:
            try:
                result.add(int(item))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _timeline_box_html(code: str, title: str = "") -> str:
        """单个小方块的 HTML。"""
        fill, border, _ = TIMELINE_STYLES.get(code, TIMELINE_STYLES[TIMELINE_MISSING])
        title_attr = f' title="{title}"' if title else ""
        return (
            f'<span{title_attr} style="display:inline-block;width:{TIMELINE_BOX_SIZE}px;'
            f'height:{TIMELINE_BOX_SIZE}px;border-radius:2px;box-sizing:border-box;'
            f'background:{fill};border:1px solid {border}"></span>'
        )

    def _timeline_block_html(self, timeline: Dict[str, Any]) -> str:
        """逐集方块条 + 起止集号刻度。

        width:fit-content 让容器正好包住方块，刻度行才能与方块两端对齐；
        超过 max-width 就换行，窄屏自动收缩，不需要媒体查询。
        """
        codes = str(timeline.get("codes") or "")
        start = self._as_int(timeline.get("start"), 1) or 1
        end = start + max(len(codes) - 1, 0)
        boxes = []
        for offset, code in enumerate(codes):
            label = TIMELINE_STYLES.get(code, TIMELINE_STYLES[TIMELINE_MISSING])[2]
            boxes.append(self._timeline_box_html(code, f"E{start + offset} {label}"))
        return (
            f'<div style="width:fit-content;'
            f'max-width:min({TIMELINE_MAX_WIDTH}px,100%);margin-top:8px">'
            f'<div style="display:flex;flex-wrap:wrap;gap:{TIMELINE_GAP}px">'
            + "".join(boxes) +
            '</div>'
            '<div style="display:flex;justify-content:space-between;font-size:10px;'
            'line-height:1.4;margin-top:2px;'
            'color:rgba(var(--v-theme-on-surface,0,0,0),.45)">'
            f'<span>E{start}</span><span>E{end}</span></div>'
            '</div>'
        )

    @staticmethod
    def _legend_html() -> str:
        """方块图例（跟着方块条走，别放到表格外面隔一屏）。"""
        items = [
            '<span style="margin-right:10px;'
            'color:rgba(var(--v-theme-on-surface,0,0,0),.7)">每格代表一集</span>'
        ]
        for code in (TIMELINE_MISSING, TIMELINE_DOWNLOADED, TIMELINE_UPGRADED, TIMELINE_WATCHED):
            fill, border, label = TIMELINE_STYLES[code]
            items.append(
                '<span style="display:inline-flex;align-items:center;gap:5px;margin-right:12px">'
                f'<span style="display:inline-block;width:{TIMELINE_BOX_SIZE}px;height:{TIMELINE_BOX_SIZE}px;'
                f'border-radius:2px;box-sizing:border-box;background:{fill};'
                f'border:1px solid {border}"></span>'
                f'<span style="color:rgba(var(--v-theme-on-surface,0,0,0),.7)">{label}</span></span>'
            )
        items.append(
            '<span style="margin-left:auto;color:rgba(var(--v-theme-on-surface,0,0,0),.45)">'
            '最近活跃优先</span>'
        )
        return ('<div style="display:flex;flex-wrap:wrap;align-items:center;gap:2px 0;'
                'font-size:12px">' + "".join(items) + "</div>")

    @staticmethod
    def _badge_html(kind: str, text: str, detail: str = "") -> str:
        """状态徽章：中性底 + 语义色圆点，深浅色主题都不用改色。"""
        dot = BADGE_DOT.get(kind, BADGE_DOT["muted"])
        detail_attr = f' title="{escape(detail, quote=True)}"' if detail else ""
        return (
            '<span' + detail_attr + ' style="display:inline-flex;align-items:center;gap:6px;'
            'flex:0 0 auto;padding:0 8px;height:22px;border-radius:11px;font-size:12px;'
            'line-height:22px;white-space:nowrap;'
            'background:rgba(var(--v-theme-on-surface,0,0,0),.06);'
            'border:1px solid rgba(var(--v-theme-on-surface,0,0,0),.14);'
            'color:rgba(var(--v-theme-on-surface,0,0,0),.78)">'
            f'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;'
            f'background:{dot}"></span>{escape(text)}</span>'
        )

    def _badge_of(self, record: Dict[str, Any]) -> Tuple[str, str, str]:
        """从执行记录推出徽章的（类型, 短文案, 悬停详情）。"""
        status = str(record.get("status") or "")
        changed = record.get("changed") or []
        if changed:
            detail = PlaybackBestVersion._describe_changed(changed)
            if self._dry_run:
                return "dry", f"试运行 {detail}", f"试运行：本应跳过 {detail} 的洗版，未写库"
            return "done", f"已跳过 {detail}", f"已把 {detail} 写成分集洗版优先级 100"
        if "失败" in status:
            return "error", "读取失败", status
        return "muted", "无变化", status

    def _card_html(self, record: Dict[str, Any]) -> str:
        """单个订阅卡片：标题 + 徽章 / 小字 / 方块条 / 进度。"""
        title = escape(str(record.get("title") or "-"))
        meta = escape(self._record_meta(record))
        kind, text, detail = self._badge_of(record)
        parts = [
            '<div style="border:1px solid rgba(var(--v-theme-on-surface,0,0,0),.12);'
            'border-radius:8px;padding:10px 12px">',
            '<div style="display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap">',
            '<div style="flex:1 1 180px;min-width:0">',
            f'<div style="font-size:14px;font-weight:600;line-height:1.4">{title}</div>',
            f'<div style="font-size:12px;line-height:1.5;margin-top:2px;'
            f'color:rgba(var(--v-theme-on-surface,0,0,0),.6)">{meta}</div>',
            '</div>',
            self._badge_html(kind, text, detail),
            '</div>',
        ]
        timeline = record.get("timeline")
        if timeline:
            parts.append(self._timeline_block_html(timeline))
        watched = str(record.get("watched") or "")
        if watched:
            parts.append(
                f'<div style="font-size:12px;line-height:1.5;margin-top:6px;'
                f'color:rgba(var(--v-theme-on-surface,0,0,0),.75)">{escape(watched)}</div>'
            )
        parts.append('</div>')
        return "".join(parts)

    def _unmatched_html(self, records: List[Dict[str, Any]]) -> str:
        """未匹配到媒体库条目的订阅：默认折叠成一行，点开再看。

        这类订阅通常占多数（本轮 28 个里 26 个），展开成三列表格会让整页 80% 都是
        「- / - / 找不到对应条目」的噪音。
        """
        rows = []
        for record in records:
            title = escape(str(record.get("title") or "-"))
            meta = escape(self._record_meta(record))
            rows.append(
                '<div style="display:flex;gap:10px;align-items:baseline;font-size:12px;'
                'line-height:1.6;padding:2px 0">'
                f'<span style="flex:1 1 auto;min-width:0">{title}</span>'
                f'<span style="flex:0 0 auto;color:rgba(var(--v-theme-on-surface,0,0,0),.55)">'
                f'{meta}</span></div>'
            )
        return (
            '<details style="margin-top:12px;border:1px solid '
            'rgba(var(--v-theme-on-surface,0,0,0),.12);border-radius:8px;padding:8px 12px">'
            '<summary style="cursor:pointer;font-size:12px;line-height:1.6;'
            'color:rgba(var(--v-theme-on-surface,0,0,0),.7)">'
            f'{len(records)} 个订阅未在媒体库找到条目，点开查看</summary>'
            # auto-fill + minmax：宽屏自动铺成多列，手机仍是一列，不用写媒体查询
            '<div style="margin-top:8px;display:grid;gap:2px 20px;'
            'grid-template-columns:repeat(auto-fill,minmax(240px,1fr))">'
            + "".join(rows) +
            '</div></details>'
        )

    @staticmethod
    def _explain_html() -> str:
        """口径说明：默认折叠，不占版面。"""
        points = [
            "命中集写入订阅的分集洗版优先级 100（宿主口径：该集洗版完成），"
            "后续洗版搜索不再为它下载更高优先级的版本",
            "全集洗版（best_version_full）只在目标集全部看过时才写 100",
            "开启 Webhook 后，播放结束 / 标记已看会即时触发一次检查",
            "标记未看不会撤销已写入的 100；拿不准时先开「试运行」只看日志",
        ]
        items = "".join(f"<li>{escape(point)}</li>" for point in points)
        return (
            '<details style="margin-top:12px;border:1px solid '
            'rgba(var(--v-theme-on-surface,0,0,0),.12);border-radius:8px;padding:8px 12px">'
            '<summary style="cursor:pointer;font-size:12px;line-height:1.6;'
            'color:rgba(var(--v-theme-on-surface,0,0,0),.7)">判定口径与写库规则</summary>'
            '<ul style="margin:8px 0 0;padding-left:18px;font-size:12px;line-height:1.8;'
            'color:rgba(var(--v-theme-on-surface,0,0,0),.7)">' + items + "</ul>"
            '</details>'
        )

    @staticmethod
    def _record_meta(record: Dict[str, Any]) -> str:
        """订阅名下方那行小字：类型 · 服务器 · 最近活跃（季已写在标题里，不重复）。"""
        parts: List[str] = []
        media_type = str(record.get("type") or "")
        parts.append({"TV": "电视剧", "MOVIE": "电影"}.get(media_type, media_type))
        server = record.get("server")
        if server:
            parts.append(str(server))
        label = str(record.get("active_label") or "")
        try:
            active_ts = float(record.get("active_ts") or 0)
        except (TypeError, ValueError):
            active_ts = 0.0
        if label and active_ts:
            if label == ACTIVITY_CREATED:
                # 「订阅于」直接给日期，避免「订阅于 3 天前」这种别扭说法
                moment = datetime.datetime.fromtimestamp(
                    active_ts, datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
            else:
                moment = PlaybackBestVersion._humanize_ts(active_ts)
            if moment:
                parts.append(f"{label} {moment}")
        return " · ".join(part for part in parts if part)

    def _title(self, subscribe) -> str:
        """订阅的展示名：名称（年份）Sxx。"""
        name = getattr(subscribe, "name", "") or ""
        year = getattr(subscribe, "year", None)
        title = f"{name}（{year}）" if year else name
        if self._media_type(subscribe) == MediaType.TV:
            season = self._as_int(getattr(subscribe, "season", None), 0)
            if season > 0:
                title += f" 第 {season} 季"
        return title

    def _describe_watched(self, playback: Dict[str, Any]) -> str:
        """把读取到的进度整理成一行文字。

        集号会合并成区间（如 E1-33），否则剧集一多这一列就会很长、把表格顶得需要横向滚动。
        """
        movie = playback.get("movie")
        if movie is not None:
            percentage = float(movie.get("percentage") or 0)
            if self._hit(movie):
                return "已看"
            # 有进度但还没到阈值：说「看到 X%」而不是「未看完 X%」，避免和「已看」混淆
            return f"看到 {percentage:.0f}%" if percentage > 0 else "未看"

        episodes = playback.get("episodes") or {}
        if not episodes:
            return "无播放记录"

        watched: List[int] = []
        unfinished: List[Tuple[int, float]] = []
        for episode in sorted(episodes):
            progress = episodes[episode] or {}
            number = self._as_int(episode, 0)
            if self._hit(progress):
                watched.append(number)
            else:
                unfinished.append((number, float(progress.get("percentage") or 0)))

        total = len(watched) + len(unfinished)
        parts: List[str] = []
        if watched:
            if len(watched) == total:
                parts.append(f"已看全部 {total} 集")
            else:
                parts.append(f"已看 {len(watched)}/{total} 集（{self._episode_ranges(watched)}）")
        if unfinished:
            parts.append(self._describe_unfinished(unfinished))

        text = "；".join(part for part in parts if part)
        if len(text) > WATCHED_TEXT_MAX and watched:
            # 兜底：仍然过长时只保留区间概览，不逐集列百分比
            text = f"已看 {len(watched)}/{total} 集（{self._episode_ranges(watched)}）；其余 {len(unfinished)} 集未看完"
        return text or "无播放记录"

    @staticmethod
    def _describe_unfinished(unfinished: List[Tuple[int, float]]) -> str:
        """还没到看过线的集：百分比一致时合并，否则只列前几集，其余给总数。"""
        if all(percentage <= 0 for _, percentage in unfinished):
            # 一点进度都没有的集，直接说「未看」，别和「看了一半」混在一起
            return f"未看 {len(unfinished)} 集"
        percentages = {round(percentage) for _, percentage in unfinished}
        if len(percentages) == 1:
            return f"{len(unfinished)} 集看到 {percentages.pop()}%"
        shown = unfinished[:WATCHED_DETAIL_LIMIT]
        detail = "、".join(
            f"E{number} 未看" if percentage <= 0 else f"E{number} {percentage:.0f}%"
            for number, percentage in shown
        )
        if len(unfinished) > len(shown):
            detail += f" 等 {len(unfinished)} 集"
        return f"未看完 {detail}"

    @staticmethod
    def _episode_ranges(episodes: List[int]) -> str:
        """把集号压缩成区间文本：1,2,3,5,7,8 -> "E1-3、E5、E7-8"。

        每段都带 E 前缀：只写数字时「已看 1/26 集（1）」会被读成「已看 1 集」，与括号里的集号混淆。
        """
        values = sorted({int(value) for value in episodes})
        if not values:
            return ""
        parts: List[str] = []
        start = prev = values[0]
        for value in values[1:]:
            if value == prev + 1:
                prev = value
                continue
            parts.append(f"E{start}" if start == prev else f"E{start}-{prev}")
            start = prev = value
        parts.append(f"E{start}" if start == prev else f"E{start}-{prev}")
        return "、".join(parts)

    @classmethod
    def _describe_changed(cls, changed) -> str:
        """把写入的跳过标记整理成文字（集号同样合并成区间）。"""
        if not changed:
            return ""
        if changed == ["整部"]:
            return "整部"
        numbers: List[int] = []
        others: List[str] = []
        for item in changed:
            number = cls._as_int(item, 0)
            if number > 0:
                numbers.append(number)
            else:
                others.append(str(item))
        parts: List[str] = []
        if numbers:
            parts.append(cls._episode_ranges(numbers))
        parts.extend(others)
        return "、".join(parts)

    def _send_notify(self, records: List[Dict[str, Any]]):
        """把本轮写入的跳过标记发到配置的通知渠道。"""
        lines = []
        for record in records:
            changed = record.get("changed") or []
            if not changed:
                continue
            lines.append(
                f"{record.get('title')}（{record.get('type')}）：已跳过 "
                f"{self._describe_changed(changed)} 的洗版"
            )
        if not lines:
            return

        text = "\n".join(lines)
        for channel in (self._notify_channel or []):
            # 配置里存的是宿主通知配置的 type，宿主的消息模型只认通知渠道枚举
            notify_channel = self.NOTIFY_TYPE_MAP.get(str(channel).lower())
            if not notify_channel:
                logger.warning(f"{self.LOG_TAG}未知的通知渠道 {channel}，跳过")
                continue
            try:
                self.post_message(
                    channel=notify_channel,
                    title="【播放进度洗版】已跳过已看内容的洗版",
                    text=text,
                )
            except Exception as err:
                logger.error(f"{self.LOG_TAG}发送通知失败（{channel}）：{err}")

    # ---- 配置辅助 ----

    def _current_config(self) -> Dict[str, Any]:
        """当前配置的规范化快照（用于回写与表单默认值）。"""
        return {
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "notify_channel": self._notify_channel,
            "interval_minutes": self._interval,
            "servers": self._servers,
            "usernames": self._usernames,
            "mode": self._mode,
            "threshold": self._threshold,
            "handle_tv": self._handle_tv,
            "handle_movie": self._handle_movie,
            "dry_run": self._dry_run,
            "webhook_trigger": self._webhook_trigger,
            "webhook_delay": self._webhook_delay,
        }

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        """把配置值转成整数，失败时返回默认值。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_list(value: Any) -> List[str]:
        """把配置值转成字符串列表（兼容逗号分隔的字符串）。"""
        if not value:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if str(item).strip()]
        return []
