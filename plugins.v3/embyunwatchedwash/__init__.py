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
from app.core.event import eventmanager
from app.log import logger
from app.modules.emby import Emby
from app.modules.jellyfin import Jellyfin
from app.plugins import _PluginBase
from app.schemas.types import MediaType, EventType

lock = RLock()


class EmbyUnwatchedWash(_PluginBase):
    # 插件名称
    plugin_name = "未看洗版"
    # 插件描述
    plugin_desc = "Jellyfin/Emby 扫描未观看的影视，自动订阅洗版（升级更高画质版本）。支持手动指定只对部分影视洗版。"
    # 插件版本
    plugin_version = "1.2"
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
    _selected_items: List[int] = []

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
            self._selected_items = config.get("selected_items") or []

        if self._only_once:
            self._only_once = False
            self.update_config({
                "enabled": self._enabled,
                "cron": self._cron,
                "notify": self._notify,
                "only_once": self._only_once,
                "include_series": self._include_series,
                "selected_items": self._selected_items,
            })
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(self.sync, 'date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="立即运行未看洗版")
            logger.info("【未看洗版】已设置『立即运行一次』任务，约 3 秒后执行")
            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/emby_wash",
            "event": EventType.PluginAction,
            "desc": "立即执行未看洗版",
            "category": "未看洗版"
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        """
        return [
            {
                "path": "/history",
                "endpoint": self.get_history,
                "methods": ["GET"],
                "summary": "获取未看洗版历史记录"
            },
            {
                "path": "/medias",
                "endpoint": self.get_medias,
                "methods": ["GET"],
                "summary": "获取媒体库未观看影视列表（供手动选择洗版）"
            }
        ]

    def get_history(self) -> List[dict]:
        """
        API 端点：返回已洗版历史记录
        """
        return self.get_data('history') or []

    def get_medias(self) -> List[dict]:
        """
        API 端点：返回媒体库未观看影视可选项（标题 + tmdbid）
        """
        return self._get_library_options()

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
        # 动态拉取媒体库未观看影视，供手动选择洗版
        options = self._get_library_options()

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
                                            'hint': '保存配置后立即运行一次（不受启用开关管控）',
                                            'persistent-hint': True
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
                                            'hint': '关闭则仅对电影洗版',
                                            'persistent-hint': True
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'selected_items',
                                            'label': '指定洗版影视（留空=全部未观看）',
                                            'items': options,
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'filterable': True,
                                            'hideSelected': True,
                                            'persistent-hint': True,
                                            'hint': f'从媒体库未观看列表中手动勾选要洗版的影视'
                                                    f'（当前共 {len(options)} 部可选项）。'
                                                    f'不选则对媒体库内所有未观看影视洗版。'
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
                                                    '你也可以在上方「指定洗版影视」中手动选择只洗版部分影视；不选则默认对全部未观看影视洗版。'
                                                    '已处理的条目会写入缓存，不会重复订阅。'
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
            "include_series": True,
            "selected_items": []
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        contents = []

        # 媒体库未观看影视（供查看 / 手动选择参考）
        try:
            options = self._get_library_options()
        except Exception:
            options = []
        if options:
            items_content = []
            for opt in options[:100]:
                items_content.append({
                    'component': 'VListItem',
                    'props': {
                        'title': opt.get('title'),
                        'density': 'compact',
                    }
                })
            contents.append({
                'component': 'VCard',
                'props': {'class': 'mb-3'},
                'content': [
                    {
                        'component': 'VCardTitle',
                        'props': {'class': 'text-subtitle-1'},
                        'text': f'媒体库未观看影视（共 {len(options)} 部，此处显示前 100）'
                    },
                    {
                        'component': 'VList',
                        'content': items_content
                    }
                ]
            })
        else:
            contents.append({
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'variant': 'tonal',
                    'text': '未能从媒体服务器读取未观看列表（可能未配置 settings.MEDIASERVER 或服务器不可达）。'
                }
            })

        # 当前洗版模式
        selected = self._selected_items or []
        if selected:
            mode_text = f"当前为【手动选择模式】，已指定 {len(selected)} 部影视进行洗版。"
        else:
            mode_text = "当前为【全量模式】，将对媒体库内所有未观看影视洗版。"
        contents.append({
            'component': 'VAlert',
            'props': {
                'type': 'info',
                'variant': 'tonal',
                'text': mode_text + " 可在插件配置页『指定洗版影视』中手动选择。"
            }
        })

        # 洗版历史
        historys = self.get_data('history')
        if not historys:
            contents.append({
                'component': 'div',
                'text': '暂无洗版历史',
                'props': {
                    'class': 'text-center',
                }
            })
            return [{
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }]

        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
        for history in historys[:50]:
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

    @eventmanager.register(EventType.PluginAction)
    def emby_wash_command(self, event):
        """
        响应远程命令 /emby_wash，立即执行一次扫描
        """
        if event.event_data and event.event_data.get("cmd") == "/emby_wash":
            self.sync()

    def sync(self):
        """
        通过流媒体管理工具未观看列表，自动洗版。
        若配置了 selected_items（手动选择），则只对这些 tmdbid 洗版；
        否则扫描媒体库全部未观看影视洗版。
        """
        washed_count = 0
        skipped_count = 0
        failed_count = 0

        # 获取锁
        _is_lock: bool = lock.acquire(timeout=60)
        if not _is_lock:
            logger.warning("【未看洗版】获取任务锁超时，已有实例在运行，本次跳过")
            return
        try:
            logger.info("【未看洗版】========== 开始扫描任务 ==========")
            # 读取缓存
            caches = self._cache_path.read_text().split("\n") if self._cache_path.exists() else []
            caches = [c for c in caches if c]
            # 读取历史记录
            history = self.get_data('history') or []

            # 手动选择模式
            selected = [str(x) for x in (self._selected_items or [])]

            if selected:
                logger.info(f"【未看洗版】运行模式：手动选择（指定 {len(selected)} 个 tmdbid 洗版）")
                for tid in selected:
                    if tid in caches:
                        logger.debug(f"【未看洗版】手动模式：tmdbid={tid} 已在缓存，跳过")
                        continue
                    # 指定 tmdbid 直接识别，无需媒体服务器
                    logger.info(f"【未看洗版】手动模式：正在处理 tmdbid={tid}")
                    mediainfo: MediaInfo = self.chain.recognize_media(tmdbid=int(tid))
                    if not mediainfo:
                        logger.warning(f"【未看洗版】手动模式：tmdbid={tid} 识别失败")
                        failed_count += 1
                        continue
                    status = self._wash_one(mediainfo, caches, history)
                    if status == "added":
                        washed_count += 1
                    elif status == "failed":
                        failed_count += 1
                    elif status == "skipped":
                        skipped_count += 1
            else:
                logger.info(f"【未看洗版】运行模式：全量扫描 | 包含剧集={self._include_series} | "
                            f"媒体服务器={settings.MEDIASERVER or '未配置'}")
                # 全量模式：扫描媒体库未观看影视
                if not settings.MEDIASERVER:
                    logger.warning("【未看洗版】未配置媒体服务器 settings.MEDIASERVER，无法全量扫描")
                    return
                media_servers = settings.MEDIASERVER.split(',')

                # 读取未观看条目（已分页拉全）
                all_items = {}
                for media_server in media_servers:
                    if media_server == 'jellyfin':
                        items = self.jellyfin_get_items()
                        logger.info(f"【未看洗版】Jellyfin 获取到 {len(items)} 条未观看条目")
                        all_items['jellyfin'] = items
                    elif media_server == 'emby':
                        items = self.emby_get_items()
                        logger.info(f"【未看洗版】Emby 获取到 {len(items)} 条未观看条目")
                        all_items['emby'] = items
                    else:
                        logger.warning(f"【未看洗版】暂不支持的媒体服务器类型：{media_server}")

                def function(y, x):
                    return y if (x['Name'] in [i['Name'] for i in y]) else (lambda z, u: (z.append(u), z))(y, x)[1]

                # 处理所有结果
                for server, all_item in all_items.items():
                    # all_item 根据影视名去重
                    result = reduce(function, all_item, [])
                    logger.info(f"【未看洗版】{server} 去重后待处理 {len(result)} 部影视")
                    for data in result:
                        name = data.get("Name")
                        _type = data.get("Type")
                        # 仅接受 Movie / Series（剧集按配置）
                        if _type == 'Movie':
                            mtype = MediaType.MOVIE
                        elif _type == 'Series' and self._include_series:
                            mtype = MediaType.TV
                        else:
                            logger.debug(f"【未看洗版】跳过（类型不匹配/未开启剧集）：{name} type={_type}")
                            continue

                        # 获取详情
                        if server == 'jellyfin':
                            item_info_resp = Jellyfin().get_iteminfo(itemid=data.get('Id'))
                        else:
                            item_info_resp = Emby().get_iteminfo(itemid=data.get('Id'))
                        if not item_info_resp:
                            logger.warning(f"【未看洗版】获取详情失败，跳过：{name}")
                            continue

                        # 获取tmdb_id
                        tmdb_id = self._tmdbid_of(item_info_resp)
                        if not tmdb_id:
                            logger.debug(f"【未看洗版】无 tmdbid，跳过：{name}")
                            continue
                        # 已处理过的条目（按 tmdbid 去重）跳过
                        if str(tmdb_id) in caches:
                            logger.debug(f"【未看洗版】已在缓存中，跳过：{name} (tmdbid={tmdb_id})")
                            continue
                        # 识别媒体信息
                        logger.info(f"【未看洗版】正在处理：{name} (tmdbid={tmdb_id})")
                        mediainfo: MediaInfo = self.chain.recognize_media(tmdbid=tmdb_id, mtype=mtype)
                        if not mediainfo:
                            logger.warning(f"【未看洗版】媒体识别失败，跳过：{name} (tmdbid={tmdb_id})")
                            failed_count += 1
                            continue
                        status = self._wash_one(mediainfo, caches, history)
                        if status == "added":
                            washed_count += 1
                        elif status == "failed":
                            failed_count += 1
                        elif status == "skipped":
                            skipped_count += 1

            # 任务完成汇总
            logger.info(f"【未看洗版】========== 扫描完成 ========== | "
                        f"新建订阅 {washed_count} 个 | 失败 {failed_count} 个 | "
                        f"跳过（已处理/剧集未开）{skipped_count} 个")
            # 保存历史记录
            self.save_data('history', history)
            # 保存缓存
            self._cache_path.write_text("\n".join(caches))
            # 发送完成通知
            if self._notify:
                if failed_count == 0:
                    self.post_message(
                        title="『未看洗版』任务完成",
                        text=f"本次处理 {washed_count} 个未观看影视（跳过 {skipped_count} 个），已创建洗版订阅。"
                    )
                else:
                    self.post_message(
                        title="『未看洗版』部分失败",
                        text=f"成功 {washed_count} 个，失败 {failed_count} 个（跳过 {skipped_count} 个）。"
                              f"失败通常因系统未开启『允许洗版』、缺少下载器/订阅配置或媒体识别失败，请检查 MoviePilot 订阅设置与日志。"
                    )
        finally:
            lock.release()

    def _wash_one(self, mediainfo: MediaInfo, caches: List[str], history: List[dict]) -> str:
        """
        对单个媒体创建洗版订阅，并写入缓存与历史。
        返回：added（已添加）/ skipped（被类型开关跳过）/ failed（创建失败）
        """
        # 前置校验：剧集开关
        if mediainfo.type == MediaType.TV and not self._include_series:
            logger.info(f"EmbyUnwatchedWash 跳过剧集（未开启包含剧集）：{mediainfo.title}")
            return "skipped"

        # 前置校验：创建洗版（best_version=True）订阅
        sid, msg = self.subscribechain.add(
            mtype=mediainfo.type,
            title=mediainfo.title,
            year=mediainfo.year,
            tmdbid=mediainfo.tmdb_id,
            best_version=True,
            username="未看洗版",
            exist_ok=True,
        )
        if sid is None:
            # 订阅创建失败：通常是系统未开启『允许洗版』、缺少下载器/订阅配置或识别失败
            logger.warning(f"【未看洗版】创建洗版订阅失败：{mediainfo.title} ({mediainfo.year}) - {msg}")
            return "failed"

        # 订阅创建成功
        logger.info(f"【未看洗版】已创建洗版订阅：{mediainfo.title} ({mediainfo.year}) [{mediainfo.type.value}]")

        # 加入缓存（按 tmdbid 去重，避免同名影视误判）
        tid = str(mediainfo.tmdb_id)
        if tid not in caches:
            caches.append(tid)
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
        return "added"

    def jellyfin_get_items(self) -> List[dict]:
        # 获取所有user
        users_url = "[HOST]Users?&apikey=[APIKEY]"
        users = self.get_users(Jellyfin().get_data(users_url))
        if not users:
            logger.info(f"EmbyUnwatchedWash/users_url: {users_url}")
            return []
        all_items = []
        limit = 500
        for user in users:
            # 分页拉全：按加入日期降序，仅取未观看
            start = 0
            while True:
                url = ("[HOST]Users/" + user + "/Items"
                       "?SortBy=DateCreated%2CSortName"
                       "&SortOrder=Descending"
                       "&Filters=IsUnplayed"
                       "&Recursive=true"
                       "&Fields=PrimaryImageAspectRatio%2CBasicSyncInfo%2CProviderIds"
                       "&CollapseBoxSetItems=false"
                       "&ExcludeLocationTypes=Virtual"
                       "&EnableTotalRecordCount=true"
                       f"&Limit={limit}&StartIndex={start}"
                       "&apikey=[APIKEY]")
                resp = self.get_items(Jellyfin().get_data(url))
                if not resp:
                    break
                items = resp
                all_items.extend(items)
                # 判断是否已经拉完
                if len(items) < limit:
                    break
                start += limit
        return all_items

    def emby_get_items(self) -> List[dict]:
        # 获取所有user
        get_users_url = "[HOST]Users?&api_key=[APIKEY]"
        users = self.get_users(Emby().get_data(get_users_url))
        if not users:
            return []
        all_items = []
        limit = 500
        for user in users:
            # 分页拉全：按加入日期降序，仅取未观看
            start = 0
            while True:
                url = ("[HOST]emby/Users/" + user + "/Items"
                       "?SortBy=DateCreated%2CSortName"
                       "&SortOrder=Descending"
                       "&Filters=IsUnplayed"
                       "&Recursive=true"
                       "&Fields=PrimaryImageAspectRatio%2CBasicSyncInfo%2CProviderIds"
                       "&CollapseBoxSetItems=false"
                       "&ExcludeLocationTypes=Virtual"
                       "&EnableTotalRecordCount=true"
                       f"&Limit={limit}&StartIndex={start}"
                       "&api_key=[APIKEY]")
                resp = self.get_items(Emby().get_data(url))
                if not resp:
                    break
                items = resp
                all_items.extend(items)
                # 判断是否已经拉完
                if len(items) < limit:
                    break
                start += limit
        return all_items

    def _get_library_options(self) -> List[dict]:
        """
        构建媒体库未观看影视的可选项（标题 + tmdbid），用于设置页手动选择与 /medias API。
        通过 Items 的 ProviderIds 直接取 Tmdb，避免逐条 get_iteminfo。
        """
        options = []
        try:
            if not settings.MEDIASERVER:
                return options
            seen = set()
            cap = 500
            for server in settings.MEDIASERVER.split(','):
                if server == 'jellyfin':
                    items = self.jellyfin_get_items()
                elif server == 'emby':
                    items = self.emby_get_items()
                else:
                    continue
                for it in items:
                    name = it.get('Name')
                    if not name or name in seen:
                        continue
                    t = it.get('Type')
                    if t not in ('Movie', 'Series'):
                        continue
                    if t == 'Series' and not self._include_series:
                        continue
                    pid = (it.get('ProviderIds') or {}).get('Tmdb')
                    if not pid:
                        continue
                    year = it.get('ProductionYear') or ''
                    typelabel = '电影' if t == 'Movie' else '剧集'
                    options.append({
                        'title': f"{name} ({year}) [{typelabel}]",
                        'value': int(pid),
                    })
                    seen.add(name)
                    if len(options) >= cap:
                        logger.info(f"EmbyUnwatchedWash 媒体库选项已截断至 {cap} 条")
                        return options
        except Exception as e:
            logger.error(f"EmbyUnwatchedWash 构建媒体库选项失败：{e}")
        return options

    @staticmethod
    def _tmdbid_of(resp) -> Optional[int]:
        """
        安全获取媒体详情中的 tmdbid（兼容属性与字典两种返回形式）
        """
        if resp is None:
            return None
        tid = getattr(resp, 'tmdbid', None)
        if tid:
            return tid
        if isinstance(resp, dict):
            return resp.get('tmdbid')
        return None

    @staticmethod
    def get_items(resp: Response):
        try:
            if resp:
                return resp.json().get("Items") or []
            else:
                return []
        except Exception as e:
            logger.error(f"解析Items数据出错：{str(e)}")
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
