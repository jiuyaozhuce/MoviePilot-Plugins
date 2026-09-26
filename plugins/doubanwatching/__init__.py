import threading
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List

from app.chain.media import MediaChain
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfo
from app.plugins import _PluginBase
from app.plugins.doubanwatching.DoubanHelper import DoubanHelper
from app.schemas import WebhookEventInfo, MediaInfo
from app.schemas.types import EventType, MediaType, MediaSource
import re
from app.log import logger

lock = threading.Lock()


class DouBanWatching(_PluginBase):
    # 插件名称
    plugin_name = "豆瓣书影音档案"
    # 插件描述
    plugin_desc = "将剧集电影的在看、看完状态同步到豆瓣书影音档案。"
    # 插件图标
    plugin_icon = "douban.png"
    # 插件版本
    plugin_version = "v1.9.11"
    # 插件作者
    plugin_author = "honue"
    # 作者主页
    author_url = "https://github.com/honue"
    # 插件配置项ID前缀
    plugin_config_prefix = "doubanwatching_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    _enable = False
    _private = True
    _first = True
    _user = ""
    _exclude = ""
    _cookie = ""

    _pc_month = None
    _pc_num = None
    _mobile_month = None
    _mobile_num = None

    _wait_process: Dict = None

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enable = config.get("enable", False)
        self._private = config.get("private", True)
        self._first = config.get("first", True)
        self._user = config.get("user", "")
        self._exclude = config.get("exclude", "")
        self._cookie = config.get("cookie", "")

        self._pc_month = int(config.get("pc_month")) if config.get("pc_month", None) else 3
        self._pc_num = int(config.get("pc_num", 50)) if config.get("pc_num", None) else 50
        self._mobile_month = int(config.get("mobile_month")) if config.get("mobile_month", None) else 2
        self._mobile_num = int(config.get("mobile_num")) if config.get("mobile_num", None) else 15

        if self.get_data("processed"):
            from app.db.plugindata_oper import PluginDataOper
            PluginDataOper().del_data(plugin_id="DouBanWatching")
            logger.warn("检测到本插件旧版本数据，删除旧版本数据，避免报错...")

    @eventmanager.register(EventType.WebhookMessage)
    def sync_log(self, event: Event, played: bool = False):
        event_info: WebhookEventInfo = event.event_data
        play_start = {"playback.start", "media.play", "PlaybackStart"}
        path = event_info.item_path
        processed_items: Dict = self.get_data('data') or {}
        self._wait_process: Dict = self.get_data('wait') or {}

        if (event_info.event in play_start and event_info.user_name in self._user.split(',')) or played:
            logger.info(" ")
            if played:
                logger.info(f"标记播放完成 {event_info.item_name}")

            if not self.exclude_keyword(path=path, keywords=self._exclude).get("ret", False):
                logger.info(self.exclude_keyword(path=path, keywords=self._exclude).get("message", ""))
                return

            if event_info.item_type == "TV":
                self._process_tv_show(event_info, processed_items, played=played)
            elif event_info.item_type == "MOV":
                self._process_movie(event_info, processed_items, played=played)
            else:
                return

    @eventmanager.register(EventType.WebhookMessage)
    def sync_played(self, event: Event):
        event_info: WebhookEventInfo = event.event_data
        played = {'item.markplayed', 'media.scrobble'}
        is_played = event_info.event in played
        if event_info.channel == "jellyfin":
            # this is a temporary solution for jellyfin.
            # a better solution should be resolving the
            # played information in the jellyfin module
            is_played = event_info.event == 'UserDataSaved' and event_info.save_reason == 'TogglePlayed'

        if is_played and event_info.user_name in self._user.split(','):
            with lock:
                self.sync_log(event=event, played=True)

    def _process_tv_show(self, event_info: WebhookEventInfo, processed_items: Dict, played: bool = False):
        index = event_info.item_name.index(" S")
        title = event_info.item_name[:index]
        season_id, episode_id = map(int, [event_info.season_id, event_info.episode_id])
        tmdb_id = event_info.tmdb_id

        if not played:
            logger.info(f"开始播放 {title} 第{season_id}季 第{episode_id}集")

        if episode_id < 2 and self._first:
            logger.info(f"剧集第1集的活动不同步到豆瓣档案，跳过")
            return

        meta = MetaInfo(title)
        meta.begin_season = season_id
        meta.type = MediaType("电视剧")
        mediainfo = self._recognize_media(meta, tmdb_id)

        if not mediainfo:
            logger.warn(f'标题：{title}，tmdbid：{tmdb_id}，指定tmdbid未识别到媒体信息，尝试仅使用标题识别')
            meta.tmdbid = None
            mediainfo = self._recognize_media(meta, None)
            if not mediainfo:
                logger.error(f'仍然未识别到媒体信息，请检查TMDB网络连接...')
                return

        episodes = mediainfo.seasons.get(season_id, [])

        title = self.format_title(title, season_id)
        status = "collect" if len(episodes) == episode_id else "do"

        if processed_items.get(title) and len(episodes) != episode_id:
            logger.info(f"{title} 已同步到豆瓣在看，不处理")
            return

        sync_ret = self._sync_to_douban(title, status, event_info.item_type, processed_items, mediainfo.poster_path)
        # 尝试同步之前同步失败的
        if sync_ret:
            logger.info(f"尝试同步之前同步失败的条目")
            self._wait_process: Dict = self.get_data('wait') or {}
            for key, value in self._wait_process.items():
                logger.info(f"尝试同步: {key}")
                self._sync_to_douban(key, value["status"], value["type"], processed_items, value["poster_path"])

    def _process_movie(self, event_info: WebhookEventInfo, processed_items: Dict, played: bool = False):
        title = event_info.item_name

        if not played:
            logger.info(f"开始播放 {title}")

        meta = MetaInfo(title)
        meta.type = MediaType("电影")
        mediainfo = self._recognize_media(meta, event_info.tmdb_id)

        if not mediainfo:
            logger.warn(f'标题：{title}，tmdbid：{event_info.tmdb_id}，指定tmdbid未识别到媒体信息，尝试仅使用标题识别')
            meta.tmdbid = None
            mediainfo = self._recognize_media(meta, None)
            if not mediainfo:
                logger.error(f'仍然未识别到媒体信息，请检查TMDB网络连接...')
                return

        if processed_items.get(title):
            logger.info(f"{title} 已同步到豆瓣在看，不处理")
            return

        self._sync_to_douban(title, "collect", event_info.item_type, processed_items, mediainfo.poster_path)

    def _recognize_media(self, meta: MetaInfo, tmdb_id: Optional[int]) -> Optional[MediaInfo]:
        if tmdb_id:
            return MediaChain().recognize_media(meta=meta, mtype=meta.type,
                                                media_source=MediaSource.TMDB,
                                                media_id=str(tmdb_id), cache=True)
        return MediaChain().recognize_media(meta=meta, mtype=meta.type, cache=True)

    def _sync_to_douban(self, title: str, status: str, mediaType: str, processed_items: Dict,
                        poster_path: str) -> bool:
        logger.info(f"开始尝试获取 {title} 豆瓣id")
        douban_helper = DoubanHelper(user_cookie=self._cookie)
        subject_name, subject_id = douban_helper.get_subject_id(title=title)

        if subject_id:
            logger.info(f"查询：{title} => 匹配豆瓣：{subject_name} https://movie.douban.com/subject/{subject_id}/")
            ret = douban_helper.set_watching_status(subject_id=subject_id, status=status, private=self._private)
            if ret:
                processed_items[title] = {
                    "subject_id": subject_id,
                    "subject_name": subject_name,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "poster_path": poster_path,
                    "type": "电视剧" if mediaType == "TV" else "电影"
                }

                if title in self._wait_process:
                    del self._wait_process[title]

                self.save_data('data', processed_items)
                self.save_data('wait', self._wait_process)
                logger.info(f"{title} 同步到档案成功")
                return True
            else:
                logger.error(f"{title} 同步到档案失败")
                if title not in self._wait_process:
                    self._wait_process[title] = {
                        "subject_id": subject_id,
                        "subject_name": subject_name,
                        "status": status,
                        "poster_path": poster_path,
                        "type": mediaType
                    }
                    self.save_data('wait', self._wait_process)
                    logger.error(f"{title} 添加到待同步列表")
        else:
            logger.warn(f"获取 {title} subject_id 失败，本条目不存在于豆瓣，或请检查cookie")

        return False

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            }, {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'private',
                                            'label': '仅自己可见',
                                        }
                                    }
                                ]
                            }, {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'first',
                                            'label': '不标记第一集',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'user',
                                            'label': '媒体库用户名',
                                            'placeholder': '多个关键词以,分隔',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'exclude',
                                            'label': '媒体路径排除关键词',
                                            'placeholder': '多个关键词以,分隔',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cookie',
                                            'label': '豆瓣cookie',
                                            'placeholder': '留空则每次从cookiecloud获取',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'pc_month',
                                            'label': '大屏幕显示月份数',
                                            'placeholder': '默认3个月，最少两个月',
                                        }
                                    }
                                ]
                            }, {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'pc_num',
                                            'label': '大屏幕每月最多显示数',
                                            'placeholder': '50',
                                        }
                                    }
                                ]
                            }, {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'mobile_month',
                                            'label': '小屏幕屏幕显示月份数',
                                            'placeholder': '默认2个月，最少两个月',
                                        }
                                    }
                                ]
                            }, {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'mobile_num',
                                            'label': '小屏幕每月最多显示数',
                                            'placeholder': '15',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '需要开启媒体服务器的webhook，需要浏览器登录豆瓣，将豆瓣的cookie同步到cookiecloud，也可以手动将cookie填写到此处，不异地登陆有效期很久。'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'v1.8+ 解决了容易提示cookie失效，导致同步失败的问题，现在用cookiecloud应该不用填保活了,建议使用cookiecloud。'
                                        }
                                    }
                                ]
                            }, {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'v1.9.0 支持标记已观看同步，播放自动同步。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enable": False,
            "private": True,
            "first": True,
            "user": '',
            "exclude": '',
            "cookie": "",
            "pc_month": 3,
            "pc_num": 50,
            "mobile_month": 2,
            "mobile_num": 15,
        }

    def get_dashboard(self, **kwargs) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], List[dict]]]:
        cols = {
            "cols": 12, "md": 12
        }
        mobile = self.is_mobile(kwargs.get('user_agent'))
        attrs = {"refresh": 600, "border": False}
        line_items = self.get_line_item(mobile=mobile)
        if line_items:
            # 海报墙：CSS Grid 自适应列数，剩余宽度均摊到每列，
            # 每行精确填满容器（避免最后一行右侧剩下不到一张海报的空隙）
            gap = "6px" if mobile else "8px"
            min_w = "44px" if mobile else "66px"
            grid_style = (f"display:grid; grid-template-columns:repeat(auto-fill, minmax({min_w}, 1fr)); "
                          f"gap:{gap}; width:100%;")
            elements = [
                {
                    'component': 'VRow',
                    'props': {
                        'no-gutters': True,
                        'style': grid_style
                    },
                    'content': line_items
                }
            ]
        else:
            # 空状态：不渲染时间线，给出一行提示，避免白板
            elements = [
                {
                    'component': 'VRow',
                    'props': {
                        'no-gutters': True
                    },
                    'content': [
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 12
                            },
                            'content': [
                                {
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'info',
                                        'variant': 'tonal',
                                        'density': 'compact',
                                        'text': '暂无观影记录，播放媒体后将自动同步到豆瓣书影音档案'
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]

        return cols, attrs, elements

    def get_line_item(self, mobile: bool = False):
        """
        海报墙布局：全部海报按观看时间从新到旧、从左到右平铺，自动换行；
        跨月处插入一张与海报同尺寸的“月份卡”（显示月份与当月观看总数）。
        """
        data: Dict = self.get_data('data') or {}
        content = []

        # 限制显示月数
        limit_month = self._mobile_month if mobile else self._pc_month
        # 限制每月最多显示数
        limit_num = self._mobile_num if mobile else self._pc_num

        month_text_class = "text-subtitle-2 font-weight-bold" if mobile else "text-subtitle-1 font-weight-bold"
        num_text_class = "text-caption"

        def month_card(label: int, total: int) -> dict:
            """与海报同尺寸的月份卡：撑满所在网格列，2:3 比例与海报等宽等高。"""
            return {
                "component": "VCard",
                "props": {
                    "variant": "tonal",
                    "color": "#AF85FD",
                    "class": "rounded-lg",
                    "style": "width:100%; aspect-ratio:2/3;"
                },
                "content": [
                    {
                        "component": "VCol",
                        "props": {
                            "style": "height:100%; display:flex; flex-direction:column;"
                                     "align-items:center; justify-content:center; padding:0;"
                        },
                        "content": [
                            {
                                "component": "div",
                                "props": {
                                    "class": month_text_class
                                },
                                "html": f"{label}月"
                            },
                            {
                                "component": "div",
                                "props": {
                                    "class": num_text_class
                                },
                                "html": f"{total}部"
                            }
                        ]
                    }
                ]
            }

        def poster_item(poster: str, val: dict) -> dict:
            return {
                "component": "a",
                "props": {
                    "href": "https://www.douban.com/doubanapp/dispatch?uri=/movie/" + val.get(
                        "subject_id") + "?from=mdouban&open=app",
                    "target": "_blank",
                    "title": val.get("subject_name"),
                    "style": "display:block; width:100%;"
                },
                "content": [
                    {
                        "component": "VCard",
                        "props": {
                            "class": "elevation-4 rounded-lg"
                        },
                        "content": [
                            {
                                "component": "VImg",
                                "props": {
                                    "src": poster.replace("/original/", "/w200/"),
                                    "style": "width:100%; display:block;",
                                    "aspect-ratio": "2/3",
                                    "cover": True
                                }
                            }
                        ]
                    }
                ]
            }

        # 将字典按照 timestamp 排序（从新到旧）
        sorted_data = sorted(data.items(),
                             key=lambda item: datetime.strptime(item[1]['timestamp'], "%Y-%m-%d %H:%M:%S"))

        def resolve_poster(val: dict):
            """返回条目的有效海报路径（original 尺寸），无效返回 None。"""
            if not val.get('poster_path', ''):
                meta = MetaInfo(val.get("subject_name"))
                meta.type = MediaType("电视剧" if not val.get("type", '') else val.get("type"))
                # 识别媒体信息（cache=True，主循环再次调用时命中缓存）
                mediainfo: MediaInfo = MediaChain().recognize_media(meta=meta, mtype=meta.type,
                                                                    cache=True)
                if not mediainfo:
                    return None
                return mediainfo.poster_path
            return val.get('poster_path')

        # 预扫描：统计每月观看总数（月份卡需要提前知道“看过N部”）
        month_totals: Dict[int, int] = {}
        for key, val in sorted_data[::-1]:
            if not isinstance(val, dict):
                continue
            poster = resolve_poster(val)
            if not poster or (poster.count('original') < 1):
                continue
            m = datetime.strptime(val.get('timestamp'), "%Y-%m-%d %H:%M:%S").month
            month_totals[m] = month_totals.get(m, 0) + 1

        last_month = None
        month_shown = 0   # 当月已展示海报数（受 limit_num 限制）

        for key, val in sorted_data[::-1]:
            if not isinstance(val, dict):
                continue
            poster_path = resolve_poster(val)
            if not poster_path or (poster_path.count('original') < 1):
                continue

            time_object = datetime.strptime(val.get('timestamp'), "%Y-%m-%d %H:%M:%S")

            # 跨月：在组头插入月份卡
            if time_object.month != last_month:
                if last_month is not None:
                    limit_month -= 1
                    if limit_month < 1:
                        break
                last_month = time_object.month
                month_shown = 0
                content.append(month_card(time_object.month, month_totals.get(time_object.month, 0)))

            if month_shown < limit_num:
                month_shown += 1
                content.append(poster_item(poster_path, val))

        return content

    @staticmethod
    def is_mobile(user_agent):
        mobile_keywords = [
            'Mobile', 'Android', 'Silk/', 'Kindle', 'BlackBerry', 'Opera Mini', 'Opera Mobi', 'iPhone', 'iPad'
        ]
        for keyword in mobile_keywords:
            if re.search(keyword, user_agent, re.IGNORECASE):
                return True
        return False

    def get_page(self) -> List[dict]:
        pass

    def get_state(self) -> bool:
        return self._enable

    def stop_service(self):
        pass

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    @staticmethod
    def exclude_keyword(path: str, keywords: str) -> Dict[str, Any]:
        if not keywords:
            return {"ret": True, "message": "空关键词"}

        if not path:
            logger.warn('媒体路径为空,不执行过滤操作')
            return {"ret": True, "message": "媒体路径为空,不执行过滤操作"}

        keywords_list = re.split(r'[，,]', keywords)
        if any(k in path for k in keywords_list):
            return {"ret": False, "message": f"路径 {path} 包含 {keywords}"}

        return {"ret": True, "message": f"路径 {path} 不包含任何关键词 {keywords}"}

    @staticmethod
    def format_title(title: str, season_id: int) -> str:
        if season_id > 1:
            return f"{title} 第{season_id}季"
        else:
            return title
