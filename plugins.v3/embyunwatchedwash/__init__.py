import inspect
import time
import traceback
from datetime import datetime, timedelta
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
try:
    # MoviePilot v3
    from app.sdk.services import MediaServerHelper
except ImportError:
    # MoviePilot v2
    from app.helper.mediaserver import MediaServerHelper
from app.plugins import _PluginBase
from app.schemas.types import MediaType, EventType
try:
    from app.schemas.types import MediaSource
except ImportError:
    MediaSource = None

lock = RLock()


class EmbyUnwatchedWash(_PluginBase):
    # 插件名称
    plugin_name = "未看洗版"
    # 插件描述
    plugin_desc = "Jellyfin/Emby 扫描未观看的影视，自动订阅洗版（升级更高画质版本）。支持手动指定只对部分影视洗版。"
    # 插件版本
    plugin_version = "1.8"
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
    # 剧集按「未观看集」精确定位：只从该季第一个未看的集开始洗版（设置订阅的开始集数）
    _series_episode_level: bool = True
    # 单次运行最多处理的影视数量（0 = 不限），用于避免大库一次性建过多订阅
    _limit: int = 0

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
            self._series_episode_level = config.get("series_episode_level")
            if self._series_episode_level is None:
                self._series_episode_level = True
            try:
                self._limit = int(config.get("limit") or 0)
            except (TypeError, ValueError):
                self._limit = 0

        if self._only_once:
            self._only_once = False
            self.update_config({
                "enabled": self._enabled,
                "cron": self._cron,
                "notify": self._notify,
                "only_once": self._only_once,
                "include_series": self._include_series,
                "selected_items": self._selected_items,
                "series_episode_level": self._series_episode_level,
                "limit": self._limit,
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'series_episode_level',
                                            'label': '剧集按未观看集洗版',
                                            'hint': '开启：剧集按季订阅，并把「开始集数」设为该季第一个未看的集（已看的集不洗）；'
                                                    '关闭：整部剧洗版',
                                            'persistent-hint': True
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
                                            'model': 'limit',
                                            'label': '单次最多处理数量',
                                            'placeholder': '0 = 不限；建议先设小值试跑',
                                            'hint': '大库建议先设 5~20，确认无误后再放开，避免一次创建上千订阅',
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
            "selected_items": [],
            "series_episode_level": True,
            "limit": 0
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
                    'text': '未能从媒体服务器读取未观看列表（可能未配置媒体服务器或服务器不可达）。'
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
                                                    + (f' 第{history.get("season")}季'
                                                       if history.get("season") is not None else '')
                                                    + (f' 开始集数 {history.get("start_episode")}'
                                                       if history.get("start_episode") else '')
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
                # 开启剧集集粒度时，读取媒体库以定位所选剧集「未观看的集」
                plan_by_tmdb: Dict[str, List[dict]] = {}
                if self._include_series and self._series_episode_level:
                    plan_by_tmdb = self._plan_by_tmdb()
                for tid in selected:
                    tasks = plan_by_tmdb.get(str(tid)) or [{
                        "tmdb_id": tid,
                        "mtype": None,
                        "name": None,
                        "season": None,
                        "start_episode": None,
                    }]
                    for task in tasks:
                        status = self._process_task(task, caches, history)
                        if status == "added":
                            washed_count += 1
                        elif status == "failed":
                            failed_count += 1
                        elif status == "skipped":
                            skipped_count += 1
            else:
                servers = self._get_server_instances()
                logger.info(f"【未看洗版】运行模式：全量扫描 | 包含剧集={self._include_series} | "
                            f"剧集集粒度={self._series_episode_level} | 单次上限={self._limit or '不限'} | "
                            f"媒体服务器={','.join(n for _, n, _ in servers) or '未检测到已配置服务器'}")
                # 全量模式：扫描媒体库未观看影视
                if not servers:
                    logger.warning("【未看洗版】未检测到已配置/已连接的媒体服务器，无法全量扫描。"
                                   "请在 MoviePilot『设置 → 媒体 → 媒体服务器』中添加服务器并确保连接正常")
                    return

                # 读取未观看条目（已分页拉全）
                raw_items = []
                for stype, name, inst in servers:
                    try:
                        if stype == 'jellyfin':
                            items = self.jellyfin_get_items(inst)
                        else:
                            items = self.emby_get_items(inst)
                        logger.info(f"【未看洗版】{name}({stype}) 获取到 {len(items)} 条未观看条目")
                        raw_items.extend(items or [])
                    except Exception as e:
                        logger.error(f"【未看洗版】读取媒体服务器 {name}({stype}) 未观看列表失败：{e}")

                if not raw_items:
                    logger.info("【未看洗版】媒体服务器未返回任何未观看条目，本次无可执行任务")

                # 构建洗版任务：电影整部；剧集按季，起始集=该季第一个未观看的集
                tasks = self._build_wash_tasks(raw_items)
                logger.info(f"【未看洗版】待处理任务：电影 {sum(1 for t in tasks if t['mtype'] == MediaType.MOVIE)} 部 | "
                            f"剧集 {sum(1 for t in tasks if t['mtype'] == MediaType.TV)} 个")

                limit = self._limit if isinstance(self._limit, int) and self._limit > 0 else 0
                for task in tasks:
                    # 单次上限保护（避免大库一次创建上千订阅）
                    if limit and (washed_count + failed_count) >= limit:
                        logger.info(f"【未看洗版】已达到单次处理上限 {limit}，本次停止（剩余任务下次运行继续）")
                        break
                    status = self._process_task(task, caches, history)
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
        except Exception as e:
            # 兜底：任何未捕获异常都要打出来，否则任务会像“卡住”一样静默结束（无任何完成日志）
            logger.error(f"【未看洗版】扫描过程发生未捕获异常：{e}\n{traceback.format_exc()}")
        finally:
            lock.release()

    def _process_task(self, task: dict, caches: List[str], history: List[dict]) -> str:
        """
        处理单个洗版任务：命中缓存则跳过 → 识别媒体 → 创建洗版订阅。
        返回：added（已添加）/ failed（失败）/ skipped（缓存跳过）
        """
        tmdb_id = task.get("tmdb_id")
        season = task.get("season")
        start_episode = task.get("start_episode")

        label = task.get("name") or str(tmdb_id)
        if season is not None:
            label = f"{label} 第{season}季"
        # 缓存键：电影按 tmdbid；剧集按 tmdbid+季（同一季只处理一次）
        cache_key = str(tmdb_id) if season is None else f"{tmdb_id}:S{season}"

        if cache_key in caches:
            logger.debug(f"【未看洗版】已在缓存中，跳过：{label} (key={cache_key})")
            return "skipped"

        _ep_log = "" if start_episode is None else f"，开始集数={start_episode}"
        logger.info(f"【未看洗版】正在处理：{label} (tmdbid={tmdb_id}{_ep_log})")
        try:
            _t0 = time.time()
            mediainfo: MediaInfo = self._recognize_auto(tmdb_id, mtype=task.get("mtype"))
            logger.info(f"【未看洗版】{label} 识别耗时 {time.time() - _t0:.1f}s")
        except Exception as e:
            logger.error(f"【未看洗版】识别异常：{label} (tmdbid={tmdb_id})：{e}\n{traceback.format_exc()}")
            return "failed"
        if not mediainfo:
            logger.warning(f"【未看洗版】媒体识别失败，跳过：{label} (tmdbid={tmdb_id})")
            return "failed"
        return self._wash_one(mediainfo, caches, history,
                              season=season, start_episode=start_episode, cache_key=cache_key)

    def _plan_by_tmdb(self) -> Dict[str, List[dict]]:
        """
        读取媒体库未观看条目并按 tmdbid 归组（供手动选择模式定位剧集未观看的季/集）。
        读取失败时返回空字典，调用方会退化为「整部洗版」。
        """
        result: Dict[str, List[dict]] = {}
        try:
            raw_items = []
            for stype, name, inst in self._get_server_instances():
                try:
                    if stype == 'jellyfin':
                        raw_items.extend(self.jellyfin_get_items(inst) or [])
                    else:
                        raw_items.extend(self.emby_get_items(inst) or [])
                except Exception as e:
                    logger.error(f"【未看洗版】读取媒体服务器 {name}({stype}) 未观看列表失败：{e}")
            for task in self._build_wash_tasks(raw_items):
                result.setdefault(str(task.get("tmdb_id")), []).append(task)
        except Exception as e:
            logger.warning(f"【未看洗版】读取媒体库以定位未观看集失败（将按整部洗版）：{e}")
        return result

    def _build_wash_tasks(self, items: List[dict]) -> List[dict]:
        """
        把媒体服务器返回的未观看条目转换成洗版任务列表：
        - 电影：整部洗版（season=None）
        - 剧集（开启集粒度）：按季拆分任务，start_episode = 该季第一个未观看的集
          （已观看的集不会被洗版，MoviePilot 会从该集开始搜索/下载）
        - 剧集（关闭集粒度，或拿不到集明细）：整剧洗版（season=None）
        """
        movies: Dict[int, dict] = {}
        series_meta: Dict[str, dict] = {}
        episodes: Dict[str, set] = {}

        for data in items or []:
            if not isinstance(data, dict):
                continue
            _type = data.get("Type")
            if _type == "Movie":
                tid = self._tmdbid_of_item(data)
                if tid and tid not in movies:
                    movies[tid] = {"name": data.get("Name"), "year": data.get("ProductionYear")}
            elif _type == "Series":
                sid = data.get("Id")
                if sid:
                    series_meta[sid] = {
                        "name": data.get("Name"),
                        "year": data.get("ProductionYear"),
                        "tmdb_id": self._tmdbid_of_item(data),
                    }
            elif _type == "Episode":
                # 单集：SeriesId 归属剧集 + 季号(ParentIndexNumber) + 集号(IndexNumber)
                sid = data.get("SeriesId")
                season = data.get("ParentIndexNumber")
                ep = data.get("IndexNumber")
                if sid and season is not None and ep is not None:
                    try:
                        episodes.setdefault(sid, set()).add((int(season), int(ep)))
                    except (TypeError, ValueError):
                        continue

        tasks: List[dict] = []
        # 电影任务
        for tid, m in movies.items():
            tasks.append({
                "tmdb_id": tid,
                "mtype": MediaType.MOVIE,
                "name": m.get("name"),
                "season": None,
                "start_episode": None,
            })

        if not self._include_series:
            if series_meta or episodes:
                logger.info(f"【未看洗版】未开启『包含剧集』，跳过 {len(series_meta) or len(episodes)} 部剧集")
            return tasks

        # 剧集：按季 + 未观看集定位开始集数
        handled = set()
        if self._series_episode_level:
            for sid, eps in episodes.items():
                meta = series_meta.get(sid) or {}
                tmdb_id = meta.get("tmdb_id")
                if not tmdb_id:
                    logger.debug(f"【未看洗版】剧集缺少 tmdbid，跳过：{meta.get('name') or sid}")
                    continue
                handled.add(sid)
                by_season: Dict[int, List[int]] = {}
                for season, ep in eps:
                    by_season.setdefault(season, []).append(ep)
                for season in sorted(by_season.keys()):
                    eps_list = sorted(by_season[season])
                    tasks.append({
                        "tmdb_id": tmdb_id,
                        "mtype": MediaType.TV,
                        "name": meta.get("name"),
                        "season": season,
                        "start_episode": eps_list[0],
                        "unplayed": eps_list,
                    })
                    logger.info(f"【未看洗版】剧集任务：{meta.get('name')} 第{season}季 未观看 {len(eps_list)} 集"
                                f"（{eps_list[0]}~{eps_list[-1]}）→ 开始集数={eps_list[0]}")

        # 未拿到集明细的剧集 / 关闭集粒度 → 整剧洗版
        for sid, meta in series_meta.items():
            if sid in handled:
                continue
            if not meta.get("tmdb_id"):
                continue
            tasks.append({
                "tmdb_id": meta["tmdb_id"],
                "mtype": MediaType.TV,
                "name": meta.get("name"),
                "season": None,
                "start_episode": None,
            })
        return tasks

    def _wash_one(self, mediainfo: MediaInfo, caches: List[str], history: List[dict],
                  season: Optional[int] = None, start_episode: Optional[int] = None,
                  cache_key: str = None) -> str:
        """
        对单个媒体创建洗版订阅，并写入缓存与历史。
        season/start_episode 用于剧集按季、按未观看集定位开始集数。
        返回：added（已添加）/ skipped（被类型开关跳过）/ failed（创建失败）
        """
        # 前置校验：剧集开关
        if mediainfo.type == MediaType.TV and not self._include_series:
            logger.info(f"EmbyUnwatchedWash 跳过剧集（未开启包含剧集）：{mediainfo.title}")
            return "skipped"

        tid_num = self._tmdb_of(mediainfo)

        # 剧集按季/集的参数（会随订阅一起保存：season 为显式参数，start_episode 经 kwargs 落到订阅字段）
        extra = {}
        if season is not None:
            extra["season"] = season
        if start_episode is not None:
            extra["start_episode"] = start_episode

        # 创建洗版（best_version=True）订阅，兼容 v3（media_source/media_id）与 v2（tmdbid）
        try:
            try:
                params = inspect.signature(self.subscribechain.add).parameters
            except Exception:
                params = {}
            if "media_source" in params and MediaSource is not None and tid_num:
                # v3：tmdbid 参数已被移除，传了会被 **kwargs 静默吞掉
                sid, msg = self.subscribechain.add(
                    mtype=mediainfo.type,
                    title=mediainfo.title,
                    year=mediainfo.year,
                    best_version=True,
                    username="未看洗版",
                    exist_ok=True,
                    media_source=MediaSource.TMDB,
                    media_id=str(tid_num),
                    **extra,
                )
            else:
                sid, msg = self.subscribechain.add(
                    mtype=mediainfo.type,
                    title=mediainfo.title,
                    year=mediainfo.year,
                    tmdbid=tid_num,
                    best_version=True,
                    username="未看洗版",
                    exist_ok=True,
                    **extra,
                )
        except Exception as e:
            logger.error(f"【未看洗版】创建洗版订阅异常：{mediainfo.title} - {e}\n{traceback.format_exc()}")
            return "failed"
        if sid is None:
            # 订阅创建失败：通常是系统未开启『允许洗版』、缺少下载器/订阅配置或识别失败
            logger.warning(f"【未看洗版】创建洗版订阅失败：{mediainfo.title} ({mediainfo.year}) - {msg}")
            return "failed"

        # 订阅创建成功
        _extra_log = ""
        if season is not None:
            _extra_log += f" 第{season}季"
        if start_episode is not None:
            _extra_log += f" 开始集数={start_episode}"
        logger.info(f"【未看洗版】已创建洗版订阅：{mediainfo.title} ({mediainfo.year}) "
                    f"[{mediainfo.type.value}]{_extra_log}")

        # 加入缓存（电影按 tmdbid；剧集按 tmdbid+季，避免同一季重复订阅）
        tid = str(tid_num) if tid_num else str(mediainfo.tmdb_id)
        key = cache_key or tid
        if key not in caches:
            caches.append(key)
        # 存储历史记录
        if key not in [h.get("key") for h in history]:
            history.append({
                "title": mediainfo.title,
                "type": mediainfo.type.value,
                "year": mediainfo.year,
                "poster": mediainfo.get_poster_image(),
                "overview": mediainfo.overview,
                "tmdbid": tid_num,
                "season": season,
                "start_episode": start_episode,
                "key": key,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
        return "added"

    def jellyfin_get_items(self, instance=None) -> List[dict]:
        try:
            client = instance or Jellyfin()
            # 获取所有user
            users_url = "[HOST]Users?&apikey=[APIKEY]"
            users = self.get_users(client.get_data(users_url))
            if not users:
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
                    resp = self.get_items(client.get_data(url))
                    if not resp:
                        break
                    items = resp
                    all_items.extend(items)
                    # 判断是否已经拉完
                    if len(items) < limit:
                        break
                    start += limit
            return all_items
        except Exception as e:
            logger.error(f"【未看洗版】读取 Jellyfin 未观看列表失败：{e}")
            return []

    def emby_get_items(self, instance=None) -> List[dict]:
        try:
            client = instance or Emby()
            # 获取所有user
            get_users_url = "[HOST]Users?&api_key=[APIKEY]"
            users = self.get_users(client.get_data(get_users_url))
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
                    resp = self.get_items(client.get_data(url))
                    if not resp:
                        break
                    items = resp
                    all_items.extend(items)
                    # 判断是否已经拉完
                    if len(items) < limit:
                        break
                    start += limit
            return all_items
        except Exception as e:
            logger.error(f"【未看洗版】读取 Emby 未观看列表失败：{e}")
            return []

    def _recognize_media(self, tmdb_id, mtype=None) -> Optional[MediaInfo]:
        """
        按 tmdbid 识别媒体信息，自动兼容 MoviePilot v2 / v3 两种签名。
        - v3：recognize_media(mtype=..., media_source=MediaSource.TMDB, media_id=str(id))
              （v3 核心已移除 tmdbid 参数，直接传会抛 TypeError）
        - v2：recognize_media(mtype=..., tmdbid=int(id))
        """
        try:
            params = inspect.signature(self.chain.recognize_media).parameters
        except Exception:
            params = {}
        if "media_source" in params and MediaSource is not None:
            return self.chain.recognize_media(
                mtype=mtype,
                media_source=MediaSource.TMDB,
                media_id=str(tmdb_id),
            )
        # v2 旧签名
        return self.chain.recognize_media(mtype=mtype, tmdbid=int(tmdb_id))

    def _recognize_auto(self, tmdb_id, mtype=None) -> Optional[MediaInfo]:
        """
        识别媒体并自动兜底类型：v3 仅凭 media_id 无法判断电影/剧集，必须给 mtype。
        优先用已知 mtype；未知或识别失败时依次尝试 电影 → 剧集。
        """
        if mtype:
            mediainfo = self._recognize_media(tmdb_id, mtype=mtype)
            if mediainfo:
                return mediainfo
        for _t in (MediaType.MOVIE, MediaType.TV):
            try:
                mediainfo = self._recognize_media(tmdb_id, mtype=_t)
            except Exception as e:
                logger.debug(f"【未看洗版】tmdbid={tmdb_id} 按 {_t.value} 识别异常：{e}")
                continue
            if mediainfo:
                return mediainfo
        return None

    def _get_server_instances(self) -> List[Tuple[str, str, Any]]:
        """
        通过 MediaServerHelper 获取已配置且已连接的媒体服务器客户端实例。
        返回 [(类型, 名称, 客户端实例), ...]，实例自带 host/apikey（v3 中裸 Emby()/Jellyfin() 无连接信息），
        可直接调用 get_data / get_iteminfo。兼容 v2（app.helper.mediaserver）与 v3（app.sdk.services）。
        """
        result: List[Tuple[str, str, Any]] = []
        try:
            services = MediaServerHelper().get_services()
        except Exception as e:
            logger.error(f"【未看洗版】获取媒体服务器服务失败：{e}")
            return result
        for name, info in (services or {}).items():
            try:
                inst = getattr(info, "instance", None)
                stype = (getattr(info, "type", "") or "").lower()
                if not inst:
                    continue
                if stype not in ("emby", "jellyfin"):
                    logger.info(f"【未看洗版】跳过暂不支持的媒体服务器：{name} ({stype})")
                    continue
                if hasattr(inst, "is_inactive") and inst.is_inactive():
                    logger.warning(f"【未看洗版】媒体服务器 {name} 未连接，请检查其 Host/API Key 配置")
                    continue
                result.append((stype, name, inst))
            except Exception as e:
                logger.error(f"【未看洗版】处理媒体服务器 {name} 失败：{e}")
        return result

    def _get_library_options(self) -> List[dict]:
        """
        构建媒体库未观看影视的可选项（标题 + tmdbid），用于设置页手动选择与 /medias API。
        通过 Items 的 ProviderIds 直接取 Tmdb，避免逐条 get_iteminfo。
        """
        options = []
        try:
            servers = self._get_server_instances()
            if not servers:
                return options
            seen = set()
            cap = 500
            for stype, name, inst in servers:
                try:
                    if stype == 'jellyfin':
                        items = self.jellyfin_get_items(inst)
                    else:
                        items = self.emby_get_items(inst)
                except Exception as e:
                    logger.error(f"【未看洗版】读取 {name}({stype}) 未观看列表失败：{e}")
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
    def _tmdb_of(mediainfo) -> Optional[int]:
        """
        从 MediaInfo 取 tmdbid：v3 优先 tmdb_id，回退 media_source+media_id。
        """
        if mediainfo is None:
            return None
        tid = getattr(mediainfo, "tmdb_id", None)
        if tid:
            try:
                return int(tid)
            except (TypeError, ValueError):
                pass
        src = getattr(mediainfo, "media_source", None)
        mid = getattr(mediainfo, "media_id", None)
        if mid:
            src_val = str(getattr(src, "value", src) or "").lower()
            if src_val in ("themoviedb", "tmdb", "none", ""):
                try:
                    return int(str(mid).strip())
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _tmdbid_of_item(data: dict) -> Optional[int]:
        """
        从列表条目的 ProviderIds 中取 tmdbid（列表请求已含 ProviderIds 字段，无需再查详情）。
        """
        if not isinstance(data, dict):
            return None
        pid = (data.get('ProviderIds') or data.get('Provider_Ids') or {}).get('Tmdb')
        if not pid:
            return None
        try:
            return int(str(pid).strip())
        except (TypeError, ValueError):
            return None

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
