from datetime import datetime, timedelta
from functools import reduce
from pathlib import Path
from threading import RLock
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from requests import Response

from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.log import logger
from app.modules.emby import Emby
from app.modules.jellyfin import Jellyfin
from app.plugins import _PluginBase
from app.schemas.types import MediaType

lock = RLock()


class EmbyUnwatchedWash(_PluginBase):
    # 插件名称
    plugin_name = "未看洗版"
    # 插件描述
    plugin_desc = "Jellyfin/Emby 扫描未观看的影视，自动订阅洗版（升级更高画质版本）。"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "forked-from-bestfilmversion(wlj)"
    # 作者主页
    author_url = "https://github.com/jxxghp/MoviePilot-Plugins/tree/main/plugins/bestfilmversion"
    # 插件配置项ID前缀
    plugin_config_prefix = "embyunwatchedwash_"
    # 加载顺序
    plugin_order = 14
    # 可使用的用户级别
    auth_level = 2

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None
    _cache_path: Optional[Path] = None
    subscribechain = None

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _notify: bool = False
    _only_once: bool = False
    _include_series: bool = True

    def init_plugin(self, config: dict = None):
        self._cache_path = settings.TEMP_PATH / "__emby_unwatched_wash_cache__"
        self.subscribechain = SubscribeChain()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._only_once = config.get("only_once")
            self._include_series = config.get("include_series")

        if self._only_once:
            self._only_once = False
            self.update_config({
                "enabled": self._enabled,
                "cron": self._cron,
                "notify": self._notify,
                "only_once": self._only_once,
                "include_series": self._include_series
            })
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(self.sync, 'date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="立即运行未看洗版")
            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        """
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled:
            if self._cron:
                return [{
                    "id": "EmbyUnwatchedWash",
                    "name": "未看洗版定时服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sync,
                    "kwargs": {}
                }]
            return [
                {
                    "id": "EmbyUnwatchedWash",
                    "name": "未看洗版定时服务",
                    "trigger": "interval",
                    "func": self.sync,
                    "kwargs": {
                        "minutes": 30
                    }
                }
            ]
        return []

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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'only_once',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'include_series',
                                            'label': '包含剧集',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空每30分钟'
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
                                            'text': '扫描媒体服务器中未观看（IsUnplayed）的影视，自动创建「洗版」订阅以升级更高画质版本。'
                                                    '已处理的条目会写入缓存，不会重复订阅。剧集开关可控制是否对电视剧执行洗版。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": False,
            "cron": "",
            "only_once": False,
            "include_series": True
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]
        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            title = history.get("title")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            tmdbid = history.get("tmdbid")
            contents.append(
                {
                    'component': 'VCard',
                    'content': [
                        {
                            'component': 'div',
                            'props': {
                                'class': 'd-flex justify-space-start flex-nowrap flex-row',
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': poster,
                                                'height': 120,
                                                'width': 80,
                                                'aspect-ratio': '2/3',
                                                'class': 'object-cover shadow ring-gray-500',
                                                'cover': True
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VCardTitle',
                                            'props': {
                                                'class': 'ps-1 pe-5 break-words whitespace-break-spaces'
                                            },
                                            'content': [
                                                {
                                                    'component': 'a',
                                                    'props': {
                                                        'href': f"https://www.themoviedb.org/movie/{tmdbid}",
                                                        'target': '_blank'
                                                    },
                                                    'text': title
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'类型：{mtype}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'时间：{time_str}'
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )

        return [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }
        ]

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    def sync(self):
        """
        通过流媒体管理工具未观看列表，自动洗版
        """
        # 获取锁
        _is_lock: bool = lock.acquire(timeout=60)
        if not _is_lock:
            return
        try:
            # 读取缓存
            caches = self._cache_path.read_text().split("\n") if self._cache_path.exists() else []
            # 读取历史记录
            history = self.get_data('history') or []

            # 媒体服务器类型，多个以,分隔
            if not settings.MEDIASERVER:
                return
            media_servers = settings.MEDIASERVER.split(',')

            # 读取未观看条目
            all_items = {}
            for media_server in media_servers:
                if media_server == 'jellyfin':
                    all_items['jellyfin'] = self.jellyfin_get_items()
                elif media_server == 'emby':
                    all_items['emby'] = self.emby_get_items()
                else:
                    logger.info(f"EmbyUnwatchedWash 暂不支持的媒体服务器：{media_server}")

            def function(y, x):
                return y if (x['Name'] in [i['Name'] for i in y]) else (lambda z, u: (z.append(u), z))(y, x)[1]

            # 处理所有结果
            for server, all_item in all_items.items():
                # all_item 根据影视名去重
                result = reduce(function, all_item, [])
                for data in result:
                    # 检查缓存
                    if data.get('Name') in caches:
                        continue

                    # 获取详情
                    if server == 'jellyfin':
                        item_info_resp = Jellyfin().get_iteminfo(itemid=data.get('Id'))
                    else:
                        item_info_resp = Emby().get_iteminfo(itemid=data.get('Id'))
                    logger.debug(f'EmbyUnwatchedWash插件 item打印 {item_info_resp}')
                    if not item_info_resp:
                        continue

                    # 仅接受 Movie / Series（剧集按配置）
                    _type = data.get('Type')
                    if _type == 'Movie':
                        mtype = MediaType.MOVIE
                    elif _type == 'Series' and self._include_series:
                        mtype = MediaType.TV
                    else:
                        continue

                    # 获取tmdb_id
                    tmdb_id = item_info_resp.tmdbid
                    if not tmdb_id:
                        continue
                    # 识别媒体信息
                    mediainfo: MediaInfo = self.chain.recognize_media(tmdbid=tmdb_id, mtype=mtype)
                    if not mediainfo:
                        logger.warn(f'未识别到媒体信息，标题：{data.get("Name")}，tmdbid：{tmdb_id}')
                        continue
                    # 添加洗版订阅（best_version=True 即升级更高画质）
                    self.subscribechain.add(mtype=mtype,
                                            title=mediainfo.title,
                                            year=mediainfo.year,
                                            tmdbid=mediainfo.tmdb_id,
                                            best_version=True,
                                            username="未看洗版",
                                            exist_ok=True)
                    # 加入缓存
                    caches.append(data.get('Name'))
                    # 存储历史记录
                    if mediainfo.tmdb_id not in [h.get("tmdbid") for h in history]:
                        history.append({
                            "title": mediainfo.title,
                            "type": mediainfo.type.value,
                            "year": mediainfo.year,
                            "poster": mediainfo.get_poster_image(),
                            "overview": mediainfo.overview,
                            "tmdbid": mediainfo.tmdb_id,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
            # 保存历史记录
            self.save_data('history', history)
            # 保存缓存
            self._cache_path.write_text("\n".join(caches))
        finally:
            lock.release()

    def jellyfin_get_items(self) -> List[dict]:
        # 获取所有user
        users_url = "[HOST]Users?&apikey=[APIKEY]"
        users = self.get_users(Jellyfin().get_data(users_url))
        if not users:
            logger.info(f"EmbyUnwatchedWash/users_url: {users_url}")
            return []
        all_items = []
        for user in users:
            # 根据加入日期 降序排序，仅取未观看
            url = "[HOST]Users/" + user + "/Items?SortBy=DateCreated%2CSortName" \
                                          "&SortOrder=Descending" \
                                          "&Filters=IsUnplayed" \
                                          "&Recursive=true" \
                                          "&Fields=PrimaryImageAspectRatio%2CBasicSyncInfo" \
                                          "&CollapseBoxSetItems=false" \
                                          "&ExcludeLocationTypes=Virtual" \
                                          "&EnableTotalRecordCount=false" \
                                          "&Limit=500" \
                                          "&apikey=[APIKEY]"
            resp = self.get_items(Jellyfin().get_data(url))
            if not resp:
                continue
            all_items.extend(resp)
        return all_items

    def emby_get_items(self) -> List[dict]:
        # 获取所有user
        get_users_url = "[HOST]Users?&api_key=[APIKEY]"
        users = self.get_users(Emby().get_data(get_users_url))
        if not users:
            return []
        all_items = []
        for user in users:
            # 根据加入日期 降序排序，仅取未观看
            url = "[HOST]emby/Users/" + user + "/Items?SortBy=DateCreated%2CSortName" \
                                               "&SortOrder=Descending" \
                                               "&Filters=IsUnplayed" \
                                               "&Recursive=true" \
                                               "&Fields=PrimaryImageAspectRatio%2CBasicSyncInfo" \
                                               "&CollapseBoxSetItems=false" \
                                               "&ExcludeLocationTypes=Virtual" \
                                               "&EnableTotalRecordCount=false" \
                                               "&Limit=500&api_key=[APIKEY]"
            resp = self.get_items(Emby().get_data(url))
            if not resp:
                continue
            all_items.extend(resp)
        return all_items

    @staticmethod
    def get_items(resp: Response):
        try:
            if resp:
                return resp.json().get("Items") or []
            else:
                return []
        except Exception as e:
            print(str(e))
            return []

    @staticmethod
    def get_users(resp: Response):
        try:
            if resp:
                return [data['Id'] for data in resp.json()]
            else:
                logger.error(f"EmbyUnwatchedWash/Users 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接EmbyUnwatchedWash/Users 出错：" + str(e))
            return []
