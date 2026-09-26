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

# 详情页「媒体库未观看清单」每页条数。
# 详情页的点击事件由官方渲染器处理：每次动作都会重载整页并回到顶部，因此这里
# 用「服务端记住页码 + 固定每页条数」的方式，让勾选后仍停留在同一页、同一屏内。
_LIST_PAGE_SIZE = 12


class EmbyUnwatchedWash(_PluginBase):
    # 插件名称
    plugin_name = "未看洗版"
    # 插件描述
    plugin_desc = "Jellyfin/Emby 扫描未观看的影视，自动订阅洗版（升级更高画质版本）。支持手动指定只对部分影视洗版。"
    # 插件版本
    plugin_version = "1.24"
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
    # 排除与 Dry-run 标记
    _exclude_libraries: List[str] = []
    _exclude_keywords: List[str] = []
    _dry_run: bool = False
    # 媒体库未观看选项的 TTL 缓存（配置页/详情页频繁调用，避免每次全量扫描）
    _options_cache: List[dict] = []
    _options_cache_time: float = 0.0
    _options_cache_ttl: int = 60
    # 媒体库名称缓存（用于排除媒体库 VSelect）
    _library_names_cache: List[str] = []
    _library_names_cache_time: float = 0.0
    # 清单里被「排除规则」隐去的条目数 / 涉及库名 / 被隐去的 tmdbid 集合
    # （详情页要把这件事告诉用户，否则「共 N 部」与设置页看到的库对不上）
    _options_hidden: int = 0
    _options_hidden_libs: List[str] = []
    _options_excluded_ids: set = set()

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
            # 新增的排除与 Dry-Run 配置
            self._exclude_libraries = self._normalize_str_list(config.get("exclude_libraries", []))
            # 兼容旧版 exclude_library_names（UI 选择存储）
            if config.get("exclude_library_names"):
                self._exclude_libraries = self._normalize_str_list(config.get("exclude_library_names", []))
            self._exclude_keywords = self._normalize_str_list(config.get("exclude_keywords", []))
            self._dry_run = bool(config.get("dry_run", False))

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
                "exclude_libraries": self._exclude_libraries,
                "exclude_keywords": self._exclude_keywords,
                "dry_run": self._dry_run,
                "exclude_library_names": self._exclude_libraries,
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
            },
            {
                "path": "/libraries",
                "endpoint": self.get_libraries,
                "methods": ["GET"],
                "summary": "获取媒体服务器库列表（供排除媒体库选择）"
            },
            {
                "path": "/clear_cache",
                "endpoint": self.clear_cache,
                "methods": ["GET"],
                "summary": "清除洗版缓存（重置后会重新处理已洗版条目）"
            },
            {
                "path": "/clear_history",
                "endpoint": self.clear_history,
                "methods": ["GET"],
                "summary": "清除洗版历史记录（仅清 UI 列表，不影响已建订阅）"
            },
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除单条洗版历史记录（key 为历史记录唯一标识）"
            },
            {
                "path": "/select_set",
                "endpoint": self.select_set,
                "methods": ["GET"],
                "summary": "勾选/取消勾选单个未观看影视（详情页点条目即保存，on=1 勾选 / on=0 取消）"
            },
            {
                "path": "/select_page",
                "endpoint": self.select_page,
                "methods": ["GET"],
                "summary": "记住未观看清单页码（详情页每次动作都会重载，靠它回到同一页）"
            },
            {
                "path": "/select_bulk",
                "endpoint": self.select_bulk,
                "methods": ["GET"],
                "summary": "批量操作洗版清单（mode=page_all 全选本页 / page_none 取消本页 / clear_all 清空恢复全量）"
            }
        ]

    @staticmethod
    def _api_response(success: bool, message: str = "", data: Any = None) -> Dict[str, Any]:
        """
        构造 MoviePilot 标准接口响应信封。

        ⚠️ 响应体必须**恰好**是 {success, message, data} 三个键：
        详情页事件由前端 PageRender 用「数据客户端」发起（源码里是 `import api from '@/api'`，
        取默认导出，而非 pluginApi），该客户端的响应拦截器会做**严格信封校验**——键数必须为 3、
        success 必须是 bool、message 必须是 str、且必须存在 data 键。少一个键就会被判为
        invalid-envelope，前端随即弹出「服务器返回了无效响应」并 reject，**即使 HTTP 状态码是 200、
        服务端数据也已正确落库**（点一次勾选，服务端存了，页面却报错且不刷新，就是这么来的）。

        官方插件同此约定，例如 doubanrank 返回的是 schemas.Response(success=..., message=...)，
        序列化后即为三键结构（data 默认为 null）。
        """
        return {"success": bool(success), "message": str(message or ""), "data": data}

    def get_history(self) -> Dict[str, Any]:
        """
        API 端点：返回已洗版历史记录
        """
        return self._api_response(True, "获取成功", self.get_data('history') or [])

    def get_medias(self) -> Dict[str, Any]:
        """
        API 端点：返回媒体库未观看影视可选项（标题 + tmdbid）
        """
        return self._api_response(True, "获取成功", self._get_library_options())

    def get_libraries(self) -> Dict[str, Any]:
        """
        API 端点：返回所有媒体服务器的库列表，供排除媒体库 UI 选择。
        data 格式：[{"title": "电影", "value": "电影"}, ...]
        """
        return self._api_response(True, "获取成功", self._get_library_list_options())

    def clear_cache(self) -> Dict[str, Any]:
        """
        API 端点：清除洗版缓存文件（GET）。清除后已处理条目会重新进入洗版队列。
        """
        try:
            if self._cache_path and self._cache_path.exists():
                self._cache_path.unlink()
                logger.info("【未看洗版】已清除洗版缓存")
            return self._api_response(True, "缓存已清除")
        except Exception as e:
            logger.error(f"【未看洗版】清除缓存失败：{e}")
            return self._api_response(False, str(e))

    def clear_history(self) -> Dict[str, Any]:
        """
        API 端点：清除洗版历史记录（GET）。仅清 UI 列表，不影响已创建的订阅。

        注：插件动态路由默认走 apikey 鉴权，而详情页事件只会把参数拼进 query，
        因此这些维护类端点统一声明为 GET（与官方插件 delete_history 的做法一致）。
        """
        try:
            self.save_data('history', [])
            logger.info("【未看洗版】已清除洗版历史")
            return self._api_response(True, "历史已清除")
        except Exception as e:
            logger.error(f"【未看洗版】清除历史失败：{e}")
            return self._api_response(False, str(e))

    def delete_history(self, key: str) -> Dict[str, Any]:
        """
        API 端点：按唯一 key 删除单条洗版历史记录（GET），供详情页卡片右上角按钮调用。
        """
        try:
            history = self.get_data('history') or []
            remain = [item for item in history if str(item.get('key')) != str(key)]
            if len(remain) == len(history):
                return self._api_response(False, "未找到对应的历史记录")
            self.save_data('history', remain)
            logger.info(f"【未看洗版】已删除单条洗版历史（key={key}）")
            return self._api_response(True, "已删除该条历史")
        except Exception as e:
            logger.error(f"【未看洗版】删除单条历史失败：{e}")
            return self._api_response(False, str(e))

    # ------------------------------------------------------------------
    # 勾选洗版清单：详情页「媒体库未观看清单」逐条勾选，点击即保存
    # ------------------------------------------------------------------
    def _normalized_selected(self) -> List[int]:
        """把配置里的 selected_items 归一化为去重的 int 列表（保持顺序）。"""
        out: List[int] = []
        for item in (self._selected_items or []):
            try:
                tid = int(item)
            except (TypeError, ValueError):
                continue
            if tid not in out:
                out.append(tid)
        return out

    def _persist_selected(self, items: List[int]) -> None:
        """
        把勾选结果写回插件配置。
        update_config 需要完整字段（否则未传的键会丢），所以整份回写。
        """
        self._selected_items = items
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "notify": self._notify,
            "only_once": False,
            "include_series": self._include_series,
            "selected_items": items,
            "series_episode_level": self._series_episode_level,
            "limit": self._limit,
            "exclude_libraries": self._exclude_libraries,
            "exclude_keywords": self._exclude_keywords,
            "dry_run": self._dry_run,
            "exclude_library_names": self._exclude_libraries,
        })

    def _sorted_options(self) -> List[dict]:
        """未观看清单按标题排序，保证分页顺序稳定（否则翻页会串行）。会按需扫描媒体库。"""
        try:
            options = self._get_library_options() or []
        except Exception as e:
            logger.error(f"【未看洗版】读取未观看清单失败：{e}")
            return []
        return sorted(options, key=lambda x: (str(x.get('title') or ''), str(x.get('value') or '')))

    def _cached_options_snapshot(self) -> List[dict]:
        """
        返回「不触发媒体库扫描」的未观看清单快照（按标题排序，与详情页分页口径一致）。

        为什么交互端点必须走它：详情页的点击事件由前端渲染器发起，请求会经过浏览器
        Service Worker 的 NetworkFirst（networkTimeoutSeconds=5）。一旦响应超过 5 秒，
        SW 就回退到本地缓存，把旧的响应体喂给页面（表现为莫名其妙的报错或页面不刷新）。
        扫描媒体库动辄十几秒，所以勾选 / 翻页 / 批量这几条「点一下就要立刻返回」的路径
        只读缓存快照，缓存为空时返回空列表由调用方降级，绝不同步扫描。

        快照内容 == 详情页上一次渲染时用的那份数据，因此分页与屏幕上看到的完全一致
        （比点击时重新扫描更不会出现「页码跳动」）。真正需要扫描的是打开详情页这一下。
        """
        return sorted(list(self._options_cache or []),
                      key=lambda x: (str(x.get('title') or ''), str(x.get('value') or '')))

    def _hidden_info(self) -> Tuple[int, List[str]]:
        """
        返回（被排除规则从清单中隐去的条目数, 涉及到的媒体库名）。

        值来自上一次扫描（与 `_options_cache` 同源），所以详情页展示的条数
        与屏幕上那份清单永远自洽。
        """
        count = 0
        try:
            count = int(getattr(self, '_options_hidden', 0) or 0)
        except (TypeError, ValueError):
            count = 0
        libs = list(getattr(self, '_options_hidden_libs', []) or [])
        return count, libs

    def _title_of(self, tid: int) -> str:
        """按 tmdbid 反查标题，仅用于日志/提示文案（只读快照，不触发扫描）。"""
        for opt in self._cached_options_snapshot():
            try:
                if int(opt.get('value')) == int(tid):
                    return self._clean_title(opt.get('title'))
            except (TypeError, ValueError):
                continue
        return ''

    def _saved_page(self) -> int:
        """读取上次停留的未观看清单页码。"""
        try:
            return int(self.get_data('list_page') or 1)
        except (TypeError, ValueError):
            return 1

    def _save_page(self, page: int) -> None:
        """记住未观看清单页码：详情页每次点击都会整页重载，靠它回到同一页。"""
        try:
            self.save_data('list_page', int(page))
        except Exception as e:
            logger.warning(f"【未看洗版】记住清单页码失败：{e}")

    def select_set(self, value: int = 0, on: int = 1, page: int = 1) -> Dict[str, Any]:
        """
        API 端点：勾选 / 取消勾选单个未观看影视（GET）。

        传「期望状态」（on=1 勾选、on=0 取消）而不是「翻转」：这样即使前端把事件
        触发两次，结果也一致，不会把勾选弄反。
        """
        try:
            tid = int(value)
        except (TypeError, ValueError):
            return self._api_response(False, "无效的影视 ID")
        try:
            current = self._normalized_selected()
            if int(on) == 1:
                if tid not in current:
                    current.append(tid)
                act = "已加入洗版清单"
            else:
                current = [x for x in current if x != tid]
                act = "已移出洗版清单"
            self._persist_selected(current)
            self._save_page(page)
            name = self._title_of(tid) or str(tid)
            logger.info(f"【未看洗版】{act}：{name}（当前共 {len(current)} 部）")
            return self._api_response(True, f"{act}：{name}（当前共 {len(current)} 部）",
                                      {"count": len(current), "value": tid, "on": int(on)})
        except Exception as e:
            logger.error(f"【未看洗版】更新洗版清单失败：{e}")
            return self._api_response(False, str(e))

    def select_page(self, page: int = 1) -> Dict[str, Any]:
        """API 端点：记住未观看清单页码（GET），供详情页上一页/下一页使用。"""
        _, page_no, _, _ = self._page_info(self._cached_options_snapshot(), page)
        self._save_page(page_no)
        return self._api_response(True, f"已切换到第 {page_no} 页", {"page": page_no})

    def select_bulk(self, mode: str = "page_all", page: int = 1) -> Dict[str, Any]:
        """
        API 端点：批量操作洗版清单（GET）。
        mode=page_all 全选本页 / page_none 取消本页 / clear_all 清空（恢复处理全部未观看）。
        """
        try:
            current = self._normalized_selected()
            if mode == "clear_all":
                self._persist_selected([])
                self._save_page(page)
                logger.info("【未看洗版】已清空洗版清单，恢复处理全部未观看")
                return self._api_response(True, "已清空洗版清单，恢复『处理全部未观看』",
                                          {"count": 0})
            page_items, _, _, _ = self._page_info(self._cached_options_snapshot(), page)
            page_ids: List[int] = []
            for opt in page_items:
                try:
                    page_ids.append(int(opt.get('value')))
                except (TypeError, ValueError):
                    continue
            if mode == "page_all":
                merged = current + [x for x in page_ids if x not in current]
                msg = f"本页 {len(page_ids)} 部已全部加入洗版清单（当前共 {len(merged)} 部）"
            elif mode == "page_none":
                merged = [x for x in current if x not in page_ids]
                msg = f"已取消本页勾选（当前共 {len(merged)} 部）"
            else:
                return self._api_response(False, f"未知操作：{mode}")
            self._persist_selected(merged)
            self._save_page(page)
            logger.info(f"【未看洗版】{msg}")
            return self._api_response(True, msg, {"count": len(merged), "page_size": len(page_ids)})
        except Exception as e:
            logger.error(f"【未看洗版】批量更新洗版清单失败：{e}")
            return self._api_response(False, str(e))

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

        排版：采用官方插件推荐的「单 VRow 栅格 + 分区标题（分隔线 + 小标题）」写法，
        自上而下按使用时的逻辑顺序排列：
        基础设置 -> 执行计划 -> 洗版范围 -> 排除规则 -> 试运行与手动触发。

        注意：配置页只保留「配置类」字段，媒体库未观看清单属「查看类」内容，
        统一在「数据查看」页展示，避免两处重复。
        """
        # 只拉取媒体库列表供「排除媒体库」选择。
        # 未观看清单不再在配置页拉取（那会每次打开配置页都全量扫描媒体库）。
        try:
            library_items = self._get_library_list_options()
        except Exception as e:
            logger.error(f"【未看洗版】读取媒体库列表失败，排除项将为空：{e}")
            library_items = []

        def cell(content: dict, md: int = 12) -> dict:
            """把一个组件包进统一样式的栅格单元（md 断点下按 md 值自动分栏）。"""
            return {
                'component': 'VCol',
                'props': {'cols': 12, 'md': md, 'class': 'pa-2'},
                'content': [content]
            }

        def control(component: str, model: str, label: str, md: int = 12, **props) -> dict:
            """生成一个带标签的表单项；开关若没有提示文字则隐藏详情区，保证同行开关高度一致。"""
            node_props: Dict[str, Any] = {'model': model, 'label': label}
            if component == 'VSwitch' and not props.get('hint'):
                node_props['hide-details'] = True
            node_props.update(props)
            return cell({'component': component, 'props': node_props}, md=md)

        def section(title: str) -> dict:
            """分区标题：顶部分隔线 + 小标题，用于把配置项按逻辑分组。"""
            return cell({
                'component': 'div',
                'props': {'class': 'pt-2'},
                'content': [
                    {'component': 'VDivider', 'props': {'class': 'mb-3'}},
                    {'component': 'h3', 'props': {'class': 'text-subtitle-1'}, 'text': title},
                ]
            })

        return [{
            'component': 'VForm',
            'content': [{
                'component': 'VRow',
                'props': {'class': 'ma-0'},
                'content': [
                    cell({
                        'component': 'VAlert',
                        'props': {
                            'type': 'info',
                            'variant': 'tonal',
                            'text': '扫描媒体服务器中未观看（IsUnplayed）的影视，自动创建「洗版」订阅以升级为更高画质版本。'
                                    '已处理的条目会写入缓存，不会重复订阅；建议先开启「Dry-run 预览」试跑确认，再正式运行。'
                        }
                    }),

                    # ---------- 1. 基础设置 ----------
                    section('基础设置'),
                    control('VSwitch', 'enabled', '启用插件', md=6),
                    control('VSwitch', 'notify', '发送通知', md=6),

                    # ---------- 2. 执行计划 ----------
                    section('执行计划'),
                    control('VTextField', 'cron', '执行周期', md=6,
                            placeholder='留空则每 30 分钟运行一次',
                            hint='5 位 cron 表达式，留空按 30 分钟间隔；例：0 3 * * * 表示每天 03:00 运行',
                            **{'persistent-hint': True}),
                    control('VTextField', 'limit', '单次最多处理数量', md=6,
                            placeholder='0 = 不限',
                            hint='大库建议先设 5~20 试跑，确认无误后再放开，避免一次创建上千订阅',
                            **{'persistent-hint': True}),

                    # ---------- 3. 洗版范围 ----------
                    section('洗版范围'),
                    control('VSwitch', 'include_series', '包含剧集', md=6,
                            hint='关闭则仅对电影洗版',
                            **{'persistent-hint': True}),
                    control('VSwitch', 'series_episode_level', '剧集按未观看集洗版', md=6,
                            hint='开启：按季订阅并把「开始集数」设为该季第一个未看的集（已看集不洗）；关闭：整部剧洗版',
                            **{'persistent-hint': True}),

                    # ---------- 4. 排除规则 ----------
                    section('排除规则'),
                    control('VSelect', 'exclude_libraries', '排除媒体库', md=6,
                            items=library_items, multiple=True, chips=True, clearable=True,
                            filterable=True, hideSelected=True,
                            hint='按库名精确匹配（忽略大小写），勾选后该库的未观看内容将被跳过',
                            **{'persistent-hint': True}),
                    control('VTextField', 'exclude_keywords', '排除关键字（每行一个）', md=6,
                            placeholder='例：children\nkids\nbaby',
                            rows=3, multiline=True,
                            hint='对库名做子串匹配（忽略大小写），命中即跳过该库',
                            **{'persistent-hint': True}),

                    # ---------- 5. 试运行与手动触发 ----------
                    section('试运行与手动触发'),
                    control('VSwitch', 'dry_run', 'Dry-run 预览模式', md=6,
                            hint='只打印待洗版清单，不真正创建订阅，适合正式运行前试跑',
                            **{'persistent-hint': True}),
                    control('VSwitch', 'only_once', '保存后立即运行一次', md=6,
                            hint='保存配置后立刻执行一次（不受启用开关管控），执行后自动关闭',
                            **{'persistent-hint': True}),
                ]
            }]
        }], {
            "enabled": False,
            "notify": False,
            "cron": "",
            "only_once": False,
            "include_series": True,
            # 兼容旧配置：手动指定清单的配置项已从配置页移除（未观看清单统一在
            # 「数据查看」页查看），此处保留键位以免历史配置读出异常。
            "selected_items": [],
            "series_episode_level": True,
            "limit": 0,
            "exclude_libraries": [],
            "exclude_keywords": [],
            "dry_run": False,
            "exclude_library_names": []  # 兼容旧版：UI 选择时存储的库名列表
        }

    # ------------------------------------------------------------------
    # 详情页（数据查看）构件
    # ------------------------------------------------------------------
    @staticmethod
    def _api_key() -> str:
        """
        详情页按钮调用插件 API 时需要在 query 中携带的 apikey。
        插件动态路由默认走 apikey 鉴权（Header 或 Query），而详情页事件只会把 params
        拼进 query/body，因此统一用 GET + query apikey 的方式调用。
        """
        return str(getattr(settings, "API_TOKEN", "") or "")

    @staticmethod
    def _section_title(text: str, hint: str = "") -> dict:
        """
        详情页分区标题：全宽一行，主标题 + 次要说明。
        """
        content = [
            {'component': 'span', 'props': {'class': 'text-subtitle-1 font-weight-bold'}, 'text': text}
        ]
        if hint:
            content.append({
                'component': 'span',
                'props': {'class': 'text-caption text-medium-emphasis ms-2'},
                'text': hint
            })
        return {'component': 'div', 'props': {'class': 'd-flex align-center flex-wrap mb-2'}, 'content': content}

    @staticmethod
    def _grid(cards: List[dict]) -> dict:
        """
        官方详情页栅格容器：grid + grid-info-card（自适应列宽 15rem，明暗主题通用）。
        """
        return {'component': 'div', 'props': {'class': 'grid gap-3 grid-info-card mb-2'}, 'content': cards}

    @staticmethod
    def _stat_card(label: str, value: str, caption: str = "", color: str = "primary",
                   icon: Optional[str] = None) -> dict:
        """
        概览统计卡：小标题 + 主数值 + 说明。颜色一律取主题色，保证浅色/深色主题都可读。
        """
        head = [{'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis'}, 'text': label}]
        if icon:
            head.append({'component': 'VIcon', 'props': {'icon': icon, 'size': 'small', 'color': color}})
        inner = [
            {'component': 'div', 'props': {'class': 'd-flex align-center justify-space-between'}, 'content': head},
            {'component': 'div', 'props': {'class': 'text-h6 font-weight-bold mt-1'}, 'text': value},
        ]
        if caption:
            inner.append({
                'component': 'div',
                'props': {'class': 'text-caption text-medium-emphasis mt-1'},
                'text': caption
            })
        return {
            'component': 'VCard',
            'props': {'variant': 'tonal', 'color': color},
            'content': [{'component': 'VCardText', 'content': inner}]
        }

    def _history_card(self, history: dict) -> dict:
        """
        洗版历史卡片：海报 + 标题（跳 TMDB）+ 类型/季集/时间，右上角可删除单条记录。
        """
        title = history.get("title")
        poster = history.get("poster")
        mtype = history.get("type")
        tmdbid = history.get("tmdbid")
        # 电影走 /movie/，剧集走 /tv/，避免历史卡片跳错页
        tmdb_path = "tv" if mtype == "电视剧" else "movie"
        tmdb_url = f"https://www.themoviedb.org/{tmdb_path}/{tmdbid}" if tmdbid else None

        # 详情行：类型（年份）→ 季/起始集（有才显示）→ 时间
        detail_lines = [f"类型：{mtype or '未知'}" + (f"（{history.get('year')}）" if history.get('year') else "")]
        season = history.get("season")
        start_episode = history.get("start_episode")
        if season is not None or start_episode:
            ep_line = f"第{season}季" if season is not None else ""
            if start_episode:
                ep_line += f" 起始集 {start_episode}"
            detail_lines.append(ep_line.strip())
        detail_lines.append(f"时间：{history.get('time') or '-'}")

        card_content: List[dict] = []
        # 单条删除：官方写法 VDialogCloseBtn + events 调插件 API，执行后页面自动刷新
        record_key = history.get("key")
        if record_key:
            card_content.append({
                'component': 'VDialogCloseBtn',
                'props': {'innerClass': 'absolute top-0 right-0'},
                'events': {
                    'click': {
                        'api': 'plugin/EmbyUnwatchedWash/delete_history',
                        'method': 'get',
                        'params': {'key': str(record_key), 'apikey': self._api_key()}
                    }
                }
            })
        card_content.append({
            'component': 'div',
            'props': {'class': 'd-flex justify-space-start flex-nowrap flex-row'},
            'content': [
                {
                    'component': 'div',
                    'content': [{
                        'component': 'VImg',
                        'props': {
                            'src': poster,
                            'height': 120,
                            'width': 80,
                            'aspect-ratio': '2/3',
                            'class': 'object-cover shadow ring-gray-500',
                            'cover': True,
                            # 海报为空时不渲染图片占位，避免控制台 404
                            'srcset': '',
                            'alt': title or '',
                        }
                    }]
                },
                {
                    'component': 'div',
                    'content': [
                        {
                            'component': 'VCardTitle',
                            'props': {'class': 'ps-1 pe-5 break-words whitespace-break-spaces'},
                            'content': [{
                                'component': 'a',
                                'props': {'href': tmdb_url, 'target': '_blank'} if tmdb_url else {},
                                'text': title
                            }]
                        },
                        *[{'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': line}
                          for line in detail_lines],
                    ]
                }
            ]
        })
        return {'component': 'VCard', 'content': card_content}

    @staticmethod
    def _clean_title(raw: str) -> str:
        """整理标题：媒体项缺年份时会出现「名称 () [剧集]」，统一收成「名称 [剧集]」。"""
        text = str(raw or '').strip()
        text = text.replace(' () [', ' [').replace('() [', ' [')
        if text.endswith(' ()'):
            text = text[:-3]
        return text.strip()

    @classmethod
    def _split_title(cls, raw: str) -> Tuple[str, str]:
        """把「名称 (年份) [电影/剧集]」拆成（显示名, 类型标签）。"""
        text = cls._clean_title(raw)
        label = ''
        for suffix, name in ((' [电影]', '电影'), (' [剧集]', '剧集')):
            if text.endswith(suffix):
                text = text[:-len(suffix)]
                label = name
                break
        return text.strip(), label

    def _page_info(self, options: List[dict], page: int) -> Tuple[List[dict], int, int, int]:
        """按固定每页条数切片，返回（本页条目, 实际页码, 总页数, 总条数）。"""
        total = len(options)
        pages = max(1, (total + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
        try:
            page_no = int(page)
        except (TypeError, ValueError):
            page_no = 1
        page_no = min(max(1, page_no), pages)
        start = (page_no - 1) * _LIST_PAGE_SIZE
        return options[start:start + _LIST_PAGE_SIZE], page_no, pages, total

    def _unwatched_card(self, options: List[dict], page: int = 1) -> dict:
        """
        媒体库未观看清单（可勾选）：分页展示，点条目即勾选/取消并立即保存。

        官方详情页渲染器（PageRender）是无状态的：任何点击都会整页重载。因此勾选状态
        直接落在插件配置 selected_items 上（点一次存一次，重载后按服务端状态回显），
        页码也存在插件数据里，保证重载后仍停在同一页。
        """
        apikey = self._api_key()
        page_items, page_no, page_total, total = self._page_info(options, page)
        selected_set = set(self._normalized_selected())

        def action(api: str, params: Dict[str, Any]) -> dict:
            """官方事件写法：GET + query 参数（插件路由默认 apikey 鉴权）。"""
            payload = dict(params)
            payload['apikey'] = apikey
            return {'click': {'api': api, 'method': 'get', 'params': payload}}

        rows: List[dict] = []
        for opt in page_items:
            try:
                tid = int(opt.get('value'))
            except (TypeError, ValueError):
                continue
            name, type_label = self._split_title(opt.get('title'))
            checked = tid in selected_set
            # 文字必须放在配置的**顶层** text（渲染器把它塞进默认插槽）；
            # 若写进 props.text，会被 VBtn 的「插槽优先」逻辑吃掉（s.default?.() ?? t.text），
            # 按钮会渲染成一个没有标签的空按钮。
            # 未勾选行额外加 text-high-emphasis：卡片内的继承色是 medium-emphasis（偏暗），
            # 提到高强调度可保证深浅色主题下都清晰可读。
            btn_props: Dict[str, Any] = {
                'class': 'flex-grow-1 justify-start text-none'
                         + ('' if checked else ' text-high-emphasis'),
                'density': 'compact',
                'size': 'small',
                'variant': 'tonal' if checked else 'text',
                'prepend-icon': 'mdi-checkbox-marked' if checked else 'mdi-checkbox-blank-outline',
            }
            if checked:
                btn_props['color'] = 'primary'
            row: List[dict] = [{
                'component': 'VBtn',
                'props': btn_props,
                'text': name or str(tid),
                'events': action('plugin/EmbyUnwatchedWash/select_set', {
                    'value': tid, 'on': 0 if checked else 1, 'page': page_no
                })
            }]
            if type_label:
                row.append({
                    'component': 'span',
                    'props': {'class': 'text-caption text-medium-emphasis ms-2 flex-shrink-0'},
                    'text': type_label
                })
            tmdb_path = 'tv' if type_label == '剧集' else 'movie'
            row.append({
                'component': 'a',
                'props': {'class': 'text-caption ms-3 flex-shrink-0 text-decoration-none',
                          'href': f"https://www.themoviedb.org/{tmdb_path}/{tid}",
                          'target': '_blank'},
                'text': 'TMDB'
            })
            rows.append({'component': 'div', 'props': {'class': 'd-flex align-center px-2'},
                         'content': row})

        def toolbar_btn(label: str, api: str, params: Dict[str, Any],
                        disabled: bool = False, color: str = "") -> dict:
            # 同上：标签放顶层 text，别放 props（VBtn 只要默认插槽存在就忽略 props.text）
            btn: Dict[str, Any] = {
                'size': 'small',
                'variant': 'tonal',
                'class': 'text-none' + ('' if color else ' text-high-emphasis'),
            }
            if disabled:
                btn['disabled'] = True
            if color:
                btn['color'] = color
            return {'component': 'VBtn', 'props': btn, 'text': label,
                    'events': action(api, params)}

        selected_count = len(selected_set)
        if selected_count:
            hint = (f'已勾选 {selected_count} 部：运行时只对这批影视创建洗版订阅；'
                    f'点「清空全部」恢复处理全部未观看。')
        else:
            hint = ('未勾选任何项时处理全部未观看影视（排除规则命中的除外）；'
                    '点击条目即勾选，勾选结果会立即保存。')
        hidden_count, hidden_libs = self._hidden_info()

        # 抬头一行：总数 + 分页 + 「被排除规则隐去的条数」。
        # 隐去数量必须显式说出来 —— 否则设置页里勾了「排除儿童」，清单却看不出少在哪，
        # 只会让人怀疑过滤没生效。
        count_text = (f"共 {total} 部未观看候选 · 第 {page_no}/{page_total} 页 · "
                      f"每页 {_LIST_PAGE_SIZE} 部")
        if hidden_count:
            count_text += f" · 已按排除规则隐藏 {hidden_count} 条"
            if hidden_libs:
                count_text += f"（{'、'.join(hidden_libs)}）"

        body: List[dict] = []
        if rows:
            body.append({'component': 'VCardText', 'props': {'class': 'pa-0 py-1'}, 'content': rows})
        else:
            body.append({
                'component': 'VCardText',
                'props': {'class': 'text-center text-caption text-medium-emphasis py-6'},
                'text': '当前页没有可勾选的条目。'
            })

        return {
            'component': 'VCard',
            'props': {'class': 'mb-3'},
            'content': [
                {
                    'component': 'VCardText',
                    'props': {'class': 'text-caption text-medium-emphasis pb-1'},
                    'text': count_text
                },
                {
                    'component': 'VAlert',
                    'props': {'type': 'warning' if selected_count else 'info',
                              'variant': 'tonal', 'density': 'compact', 'class': 'mb-2',
                              'text': hint}
                },
                {'component': 'VDivider'},
                *body,
                {'component': 'VDivider'},
                {'component': 'VCardActions', 'props': {'class': 'flex-wrap ga-1 px-2'}, 'content': [
                    toolbar_btn('上一页', 'plugin/EmbyUnwatchedWash/select_page',
                                {'page': page_no - 1}, disabled=page_no <= 1),
                    {'component': 'span',
                     'props': {'class': 'text-caption text-medium-emphasis px-2'},
                     'text': f"{page_no} / {page_total}"},
                    toolbar_btn('下一页', 'plugin/EmbyUnwatchedWash/select_page',
                                {'page': page_no + 1}, disabled=page_no >= page_total),
                    toolbar_btn('全选本页', 'plugin/EmbyUnwatchedWash/select_bulk',
                                {'mode': 'page_all', 'page': page_no}),
                    toolbar_btn('取消本页', 'plugin/EmbyUnwatchedWash/select_bulk',
                                {'mode': 'page_none', 'page': page_no}),
                    toolbar_btn('清空全部', 'plugin/EmbyUnwatchedWash/select_bulk',
                                {'mode': 'clear_all', 'page': page_no},
                                disabled=selected_count == 0, color='error'),
                ]},
            ]
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面（数据查看）。
        自上而下按使用逻辑排列：
        运行概览 → 运行提示 → 媒体库未观看清单（可勾选）→ 洗版历史 → 维护操作。

        未观看清单排在历史之前：清单是可选可改的「操作区」，而详情页每次点击都会整页
        重载并回到顶部，把它放在靠前的位置可以少滚一点。
        """
        # ---------- 数据准备（任何一步失败都降级为空，避免详情页打不开） ----------
        # 清单按标题排序（分页顺序才稳定）；勾选状态来自插件配置 selected_items
        options = self._sorted_options()
        selected = self._normalized_selected()
        history = self.get_data('history') or []
        history = sorted(history, key=lambda x: x.get('time', ''), reverse=True)

        contents: List[dict] = []

        # ---------- 1. 运行概览：5 张统计卡，一屏一行 ----------
        contents.append(self._section_title('运行概览', '数据来自媒体服务器扫描结果与插件本地记录'))
        hidden_count, hidden_libs = self._hidden_info()
        contents.append(self._grid([
            self._stat_card('未观看候选', f"{len(options)} 部",
                            (f"已按排除规则隐藏 {hidden_count} 部"
                             + (f"（{'、'.join(hidden_libs)}）" if hidden_libs else ''))
                            if hidden_count else '媒体库中未观看的影视',
                            'primary', 'mdi-movie-open-outline'),
            self._stat_card('洗版历史', f"{len(history)} 条",
                            '本地最多保留 500 条', 'success', 'mdi-history'),
            self._stat_card('洗版范围', '电影 + 剧集' if self._include_series else '仅电影',
                            ('剧集按未观看集洗版' if self._series_episode_level else '剧集按整部洗版')
                            if self._include_series else '不处理剧集',
                            'info', 'mdi-movie-filter'),
            self._stat_card('试运行', '已开启' if self._dry_run else '已关闭',
                            '仅输出清单，不创建订阅' if self._dry_run else '按规则正常创建订阅',
                            'warning' if self._dry_run else 'success', 'mdi-test-tube'),
            self._stat_card('排除规则', f"{len(self._exclude_libraries)} 个库",
                            f"关键字 {len(self._exclude_keywords)} 条" if self._exclude_keywords
                            else '未设置排除关键字',
                            'warning' if (self._exclude_libraries or self._exclude_keywords) else 'primary',
                            'mdi-filter-off-outline'),
        ]))

        # ---------- 2. 运行提示：按需出现，不常驻 ----------
        if self._dry_run:
            contents.append({'component': 'VAlert', 'props': {
                'type': 'warning', 'variant': 'tonal', 'class': 'mb-2', 'prepend-icon': 'mdi-test-tube',
                'text': 'Dry-run 预览已开启：运行只会把待洗版清单写入日志，不会创建订阅。'}})
        if not options:
            contents.append({'component': 'VAlert', 'props': {
                'type': 'info', 'variant': 'tonal', 'class': 'mb-2',
                'prepend-icon': 'mdi-alert-circle-outline',
                'text': '暂未读取到未观看清单，请检查媒体服务器配置与连通性；下方历史记录不受影响。'}})
        if selected:
            extra = ''
            if options:
                visible_ids = set()
                for opt in options:
                    try:
                        visible_ids.add(int(opt.get('value')))
                    except (TypeError, ValueError):
                        continue
                out_of_list = [x for x in selected if x not in visible_ids]
                if out_of_list:
                    extra = (f'其中 {len(out_of_list)} 部已不在下方清单里'
                             f'（所属媒体库被排除，或已不再未观看），运行时同样会跳过。')
            contents.append({'component': 'VAlert', 'props': {
                'type': 'warning', 'variant': 'tonal', 'class': 'mb-2',
                'prepend-icon': 'mdi-format-list-checks',
                'text': f'已勾选 {len(selected)} 部影视：运行时只对这批创建洗版订阅，'
                        f'其余未观看内容会跳过；在下方清单点「清空全部」可恢复处理全部未观看。'
                        + extra}})
        if self._exclude_libraries or self._exclude_keywords:
            rules = []
            if self._exclude_libraries:
                rules.append('排除媒体库：' + '、'.join(self._exclude_libraries))
            if self._exclude_keywords:
                rules.append('排除关键字：' + '、'.join(self._exclude_keywords))
            tail = '（命中即跳过该库，下方清单中也不显示这些条目）'
            contents.append({'component': 'VAlert', 'props': {
                'type': 'info', 'variant': 'tonal', 'class': 'mb-2',
                'prepend-icon': 'mdi-filter-off-outline',
                'text': '；'.join(rules) + tail}})

        # ---------- 3. 媒体库未观看清单（可勾选，点击即保存） ----------
        contents.append(self._section_title(
            '媒体库未观看清单', '点条目即勾选并立即保存 · 未勾选任何项则处理全部未观看'))
        contents.append(self._unwatched_card(options, page=self._saved_page()))

        # ---------- 4. 洗版历史 ----------
        contents.append(self._section_title(
            '洗版历史', f"共 {len(history)} 条 · 按时间倒序 · 右上角可删除单条"))
        if history:
            contents.append(self._grid([self._history_card(item) for item in history[:50]]))
            if len(history) > 50:
                contents.append({
                    'component': 'div',
                    'props': {'class': 'text-caption text-medium-emphasis mb-3'},
                    'text': f"仅展示最近 50 条，本地共保留 {len(history)} 条。"
                })
        else:
            contents.append({
                'component': 'VCard',
                'props': {'variant': 'tonal', 'class': 'mb-3'},
                'content': [{
                    'component': 'VCardText',
                    'props': {'class': 'text-center text-caption text-medium-emphasis py-6'},
                    'text': '暂无洗版历史，运行一次未看洗版后这里会出现记录。'
                }]
            })

        # ---------- 5. 维护操作：危险操作垫底，点击后自动刷新页面 ----------
        apikey = self._api_key()
        contents.append(self._section_title('维护操作', '点击后页面会自动刷新'))
        contents.append({
            'component': 'VCard',
            'props': {'variant': 'tonal', 'color': 'warning', 'class': 'mb-3'},
            'content': [
                {'component': 'VCardText', 'props': {'class': 'text-caption'},
                 'text': '清除洗版缓存：重置已处理记录，下次运行会重新对这批影视创建订阅。'},
                {'component': 'VCardText', 'props': {'class': 'text-caption pt-0'},
                 'text': '清除历史记录：仅清空上方列表，已创建的订阅不受影响。'},
                {'component': 'VCardActions', 'content': [
                    {'component': 'VBtn',
                     'props': {'color': 'warning', 'variant': 'tonal', 'size': 'small',
                               'prepend-icon': 'mdi-refresh'},
                     'text': '清除洗版缓存',
                     'events': {'click': {'api': 'plugin/EmbyUnwatchedWash/clear_cache',
                                          'method': 'get', 'params': {'apikey': apikey}}}},
                    {'component': 'VBtn',
                     'props': {'color': 'error', 'variant': 'tonal', 'size': 'small',
                               'prepend-icon': 'mdi-delete-sweep'},
                     'text': '清除历史记录',
                     'events': {'click': {'api': 'plugin/EmbyUnwatchedWash/clear_history',
                                          'method': 'get', 'params': {'apikey': apikey}}}},
                ]},
            ]
        })

        return contents


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
            # ---------- Dry-run 预览模式：只列出计划，不创建订阅 ----------
            if self._dry_run:
                logger.info("【未看洗版】Dry-run 模式已开启，仅打印待洗版条目，不会创建订阅")
                try:
                    plan_items = self._build_plan()
                except Exception as e:
                    logger.error(f"【未看洗版】Dry-run 构建计划失败：{e}\n{traceback.format_exc()}")
                    plan_items = []
                if not plan_items:
                    logger.info("【未看洗版】Dry-run 无待处理条目（可能被排除规则过滤或媒体库无未观看）")
                else:
                    logger.info(f"【未看洗版】Dry-run 待处理条目共 {len(plan_items)} 条：")
                    for idx, itm in enumerate(plan_items, 1):
                        tmdb = itm.get("tmdb_id")
                        mtype = itm.get("mtype")
                        title = itm.get("name") or str(tmdb)
                        season = itm.get("season")
                        start = itm.get("start_episode")
                        label = f"{title} [{mtype}]"
                        season_str = f" 第{season}季" if season is not None else ""
                        ep_str = f" 开始集数={start}" if start is not None else ""
                        logger.info(f"  {idx}. {label}{season_str}{ep_str}")
                    logger.info("【未看洗版】Dry-run 结束（仅列出以上条目，未实际创建订阅）")
                return
            # ---------- 正常模式 ----------
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
                # 手动模式同样受 limit 保护（避免一次勾选几百部直接爆订阅）
                limit = self._limit if isinstance(self._limit, int) and self._limit > 0 else 0
                for tid in selected:
                    if limit and (washed_count + failed_count) >= limit:
                        logger.info(f"【未看洗版】已达到单次处理上限 {limit}，本次停止（剩余选择下次运行继续）")
                        break
                    tasks = plan_by_tmdb.get(str(tid)) or [{
                        "tmdb_id": tid,
                        "mtype": None,
                        "name": None,
                        "season": None,
                        "start_episode": None,
                    }]
                    for task in tasks:
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

    def _build_plan(self) -> List[dict]:
        """
        构建本次运行的完整任务计划（供 Dry-run 预览与全量模式共用）。
        返回 List[dict]，每项含 tmdb_id / mtype / name / season / start_episode。
        手动模式会额外按 selected_items 过滤。
        """
        try:
            selected = [str(x) for x in (self._selected_items or [])]
            if selected:
                # 手动模式：读取媒体库定位剧集未观看的季/集
                plan_by_tmdb: Dict[str, List[dict]] = {}
                if self._include_series and self._series_episode_level:
                    plan_by_tmdb = self._plan_by_tmdb()
                plan_items: List[dict] = []
                for tid in selected:
                    tasks = plan_by_tmdb.get(str(tid)) or [{
                        "tmdb_id": tid,
                        "mtype": None,
                        "name": None,
                        "season": None,
                        "start_episode": None,
                    }]
                    for task in tasks:
                        plan_items.append({
                            "tmdb_id": task.get("tmdb_id"),
                            "mtype": task.get("mtype"),
                            "name": task.get("name"),
                            "season": task.get("season"),
                            "start_episode": task.get("start_episode"),
                        })
                return plan_items
            # 全量模式：扫描媒体库未观看条目
            servers = self._get_server_instances()
            raw_items = []
            for stype, name, inst in servers:
                try:
                    if stype == 'jellyfin':
                        raw_items.extend(self.jellyfin_get_items(inst) or [])
                    else:
                        raw_items.extend(self.emby_get_items(inst) or [])
                except Exception as e:
                    logger.error(f"【未看洗版】读取 {name}({stype}) 未观看列表失败：{e}")
            return self._build_wash_tasks(raw_items)
        except Exception as e:
            logger.error(f"【未看洗版】构建计划失败：{e}\n{traceback.format_exc()}")
            return []

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

    def _is_excluded_item(self, item: dict) -> bool:
        """
        按「排除媒体库 / 排除关键字」判断某条未观看条目是否要跳过。
        基于条目的 LibraryName（库名）：
        - exclude_libraries：库名精确匹配（去空格、忽略大小写）
        - exclude_keywords：库名子串匹配（忽略大小写）
        没有配置任何排除规则时返回 False（不排除）。
        """
        if not self._exclude_libraries and not self._exclude_keywords:
            return False
        lib = (item.get("LibraryName") or "").strip()
        if not lib:
            return False
        lib_lc = lib.lower()
        for lib_name in self._exclude_libraries:
            if lib_name and lib_name.strip().lower() == lib_lc:
                logger.debug(f"【未看洗版】命中排除媒体库，跳过：{item.get('Name')} (库={lib})")
                return True
        for kw in self._exclude_keywords:
            if kw and kw.strip().lower() in lib_lc:
                logger.debug(f"【未看洗版】命中排除关键字 '{kw}'，跳过：{item.get('Name')} (库={lib})")
                return True
        return False

    def _library_entries(self, instance, stype: str = 'emby') -> List[dict]:
        """
        读取媒体库清单：[{'name': '电影', 'id': '库根 ItemId', 'type': 'movies'}, ...]

        为什么需要它：Emby/Jellyfin 的 Items 接口**不返回 LibraryName** —— 即使显式写进
        `Fields=...LibraryName` 也照样被忽略（本机实测 200/200 条全为空）。因此「排除媒体库」
        无法靠条目自带字段判断，只能**按库分别拉取**（ParentId=库根 ItemId），
        入库时就把库名打在条目上，后续 `_is_excluded_item` 才有依据。
        """
        entries: List[dict] = []
        try:
            if stype == 'jellyfin':
                urls = ["[HOST]jellyfin/Libraries?api_key=[APIKEY]",
                        "[HOST]jellyfin/Library/VirtualFolders?api_key=[APIKEY]"]
            else:
                urls = ["[HOST]emby/Library/VirtualFolders?api_key=[APIKEY]"]
            for url in urls:
                resp = instance.get_data(url)
                if not resp or resp.status_code != 200:
                    continue
                try:
                    data = resp.json()
                except Exception:
                    continue
                if not isinstance(data, list):
                    continue
                for lib in data:
                    if not isinstance(lib, dict):
                        continue
                    lib_name = (lib.get('Name') or '').strip()
                    lib_id = str(lib.get('ItemId') or lib.get('Id') or '').strip()
                    if lib_name and lib_id and not any(e['name'] == lib_name for e in entries):
                        entries.append({
                            'name': lib_name,
                            'id': lib_id,
                            'type': lib.get('CollectionType') or '',
                        })
                if entries:
                    break
        except Exception as e:
            logger.error(f"【未看洗版】读取媒体库清单失败：{e}")
        if entries:
            logger.info(f"【未看洗版】读取到 {len(entries)} 个媒体库："
                        + '、'.join(e['name'] for e in entries))
        return entries

    def _get_library_list_options(self) -> List[dict]:
        """
        获取媒体服务器库列表，供排除媒体库选择使用。
        通过 Emby/Jellyfin 的 VirtualFolders API 获取库名列表。
        返回格式：[{"title": "电影", "value": "电影"}, ...]
        """
        import time as _time
        now = _time.time()
        # TTL 缓存 5 分钟
        if self._library_names_cache and (now - self._library_names_cache_time) < 300:
            logger.info(f"【未看洗版】从缓存返回 {len(self._library_names_cache)} 个库名")
            return [{'title': name, 'value': name} for name in self._library_names_cache]
        library_names = []
        try:
            # 尝试从已有的媒体服务器实例获取库列表
            servers = self._get_server_instances()
            logger.info(f"【未看洗版】获取到 {len(servers)} 个媒体服务器实例")
            for stype, name, inst in servers:
                try:
                    logger.info(f"【未看洗版】处理 {name}({stype}) 获取库列表")
                    if stype == 'emby':
                        # Emby: /emby/Library/VirtualFolders?api_key=...
                        resp = inst.get_data("[HOST]emby/Library/VirtualFolders?api_key=[APIKEY]")
                        logger.info(f"【未看洗版】Emby VirtualFolders 响应: {resp.status_code if resp else None}")
                        if resp and resp.status_code == 200:
                            try:
                                data = resp.json()
                                if isinstance(data, list):
                                    for lib in data:
                                        lib_name = lib.get('Name')
                                        if lib_name and lib_name not in library_names:
                                            library_names.append(lib_name)
                                    logger.info(f"【未看洗版】从 {name} 获取到 {len(library_names)} 个库名")
                            except Exception as e:
                                logger.error(f"【未看洗版】解析 Emby 库列表失败：{e}")
                    else:
                        # Jellyfin: /jellyfin/Libraries?api_key=...
                        resp = inst.get_data("[HOST]jellyfin/Libraries?api_key=[APIKEY]")
                        if resp and resp.status_code == 200:
                            try:
                                data = resp.json()
                                if isinstance(data, list):
                                    for lib in data:
                                        lib_name = lib.get('Name')
                                        if lib_name and lib_name not in library_names:
                                            library_names.append(lib_name)
                            except Exception:
                                pass
                except Exception as e:
                    logger.error(f"【未看洗版】读取 {name}({stype}) 库列表失败：{e}")
            # 如果实例获取失败，尝试直接从数据库读取 Emby 配置并调用 API
            if not library_names:
                logger.warning("【未看洗版】无法从实例获取库列表，尝试从数据库读取配置")
                try:
                    import psycopg2
                    from app.sdk.config import settings
                    # 获取数据库连接参数
                    db_url = getattr(settings, 'DATABASE_URL', '')
                    if db_url:
                        conn = psycopg2.connect(db_url)
                        cur = conn.cursor()
                        cur.execute("SELECT value FROM systemconfig WHERE key='MediaServers'")
                        row = cur.fetchone()
                        if row:
                            import json
                            servers_config = json.loads(row[0])
                            for srv in servers_config:
                                if srv.get('type') == 'emby' and srv.get('enabled'):
                                    config = srv.get('config', {})
                                    host = config.get('host', '').rstrip('/')
                                    apikey = config.get('apikey', '')
                                    if host and apikey:
                                        try:
                                            import requests
                                            url = f"{host}/emby/Library/VirtualFolders?api_key={apikey}"
                                            r = requests.get(url, timeout=10)
                                            if r.status_code == 200:
                                                data = r.json()
                                                if isinstance(data, list):
                                                    for lib in data:
                                                        lib_name = lib.get('Name')
                                                        if lib_name and lib_name not in library_names:
                                                            library_names.append(lib_name)
                                                    logger.info(f"【未看洗版】从数据库配置获取到 {len(library_names)} 个库名")
                                        except Exception as e:
                                            logger.error(f"【未看洗版】请求 Emby API 失败：{e}")
                        cur.close()
                        conn.close()
                except Exception as e:
                    logger.error(f"【未看洗版】从数据库读取配置失败：{e}")
        except Exception as e:
            logger.error(f"【未看洗版】获取媒体库列表失败：{e}")
        # 写缓存
        self._library_names_cache = sorted(library_names)
        self._library_names_cache_time = now
        logger.info(f"【未看洗版】最终返回 {len(library_names)} 个库名")
        return [{'title': name, 'value': name} for name in self._library_names_cache]

    @staticmethod
    def _normalize_str_list(value) -> List[str]:
        """
        归一化配置里的字符串列表：支持换行分隔的多行字符串、逗号分隔、列表。
        前端 VTextField(multiline) 保存的是换行分隔的字符串。
        """
        if not value:
            return []
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            # 先按换行拆，再按逗号拆，去重
            parts = []
            for line in value.splitlines():
                for piece in line.split(","):
                    piece = piece.strip()
                    if piece and piece not in parts:
                        parts.append(piece)
            return parts
        return []

    def _build_wash_tasks(self, items: List[dict]) -> List[dict]:
        """
        把媒体服务器返回的未观看条目转换成洗版任务列表：
        - 电影：整部洗版（season=None）
        - 剧集（开启集粒度）：按季拆分任务，start_episode = 该季第一个未观看的集
          （已观看的集不会被洗版，MoviePilot 会从该集开始搜索/下载）
        - 剧集（关闭集粒度，或拿不到集明细）：整剧洗版（season=None）
        若配置了「排除媒体库 / 排除关键字」，命中者直接跳过。
        """
        movies: Dict[int, dict] = {}
        series_meta: Dict[str, dict] = {}
        episodes: Dict[str, set] = {}

        for data in items or []:
            if not isinstance(data, dict):
                continue
            # 排除媒体库 / 排除关键字：命中则整条跳过（LibraryName 由拉取 Fields 带回）
            if self._is_excluded_item(data):
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
                    # 第 0 集通常是特番/特别篇，不作为开始集数
                    positive = [e for e in eps_list if e > 0]
                    start_ep = positive[0] if positive else 1
                    tasks.append({
                        "tmdb_id": tmdb_id,
                        "mtype": MediaType.TV,
                        "name": meta.get("name"),
                        "season": season,
                        "start_episode": start_ep,
                        "unplayed": eps_list,
                    })
                    logger.info(f"【未看洗版】剧集任务：{meta.get('name')} 第{season}季 未观看 {len(eps_list)} 集"
                                f"（{eps_list[0]}~{eps_list[-1]}）→ 开始集数={start_ep}")

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
        # 存储历史记录（带上限，避免无限增长）
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
            # 历史记录上限：超过 500 条时按时间淘汰最旧的，避免 plugindata 无限膨胀
            if len(history) > 500:
                history.sort(key=lambda x: x.get("time", ""), reverse=True)
                del history[500:]
        return "added"

    def jellyfin_get_items(self, instance=None) -> List[dict]:
        """拉取 Jellyfin 未观看条目；同样按媒体库逐个拉取并打上 LibraryName（理由见 emby_get_items）。"""
        try:
            client = instance or Jellyfin()
            # 获取所有user
            users_url = "[HOST]Users?&apikey=[APIKEY]"
            users = self.get_users(client.get_data(users_url))
            if not users:
                return []
            libs = self._library_entries(client, 'jellyfin')
            if libs:
                targets = [(lib['name'], str(lib['id'])) for lib in libs]
            else:
                logger.warning("【未看洗版】未能读取 Jellyfin 媒体库清单，退化为全局拉取："
                               "本轮无法判定条目所属媒体库，排除媒体库不会命中")
                targets = [('', '')]
            all_items = []
            limit = 500
            for lib_name, parent_id in targets:
                lib_count = 0
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
                               + (f"&ParentId={parent_id}" if parent_id else "")
                               + "&apikey=[APIKEY]")
                        resp = self.get_items(client.get_data(url))
                        if not resp:
                            break
                        if lib_name:
                            for it in resp:
                                if isinstance(it, dict):
                                    it['LibraryName'] = lib_name
                        all_items.extend(resp)
                        lib_count += len(resp)
                        # 判断是否已经拉完
                        if len(resp) < limit:
                            break
                        start += limit
                if lib_name:
                    logger.info(f"【未看洗版】Jellyfin 媒体库「{lib_name}」未观看条目 {lib_count} 条")
            return all_items
        except Exception as e:
            logger.error(f"【未看洗版】读取 Jellyfin 未观看列表失败：{e}")
            return []

    def emby_get_items(self, instance=None) -> List[dict]:
        """
        拉取 Emby 未观看条目，**按媒体库逐个拉取**并给每条打上 LibraryName。

        用 ParentId=库根 ItemId 逐库查询是唯一可靠的库归属办法：Emby 的 Items 接口
        不返回 LibraryName（写进 Fields 也无效）。拿不到库清单时退化为全局拉取一次，
        此时条目没有 LibraryName，「排除媒体库」自然不命中（并在日志里提示）。
        """
        try:
            client = instance or Emby()
            # 获取所有user
            get_users_url = "[HOST]Users?&api_key=[APIKEY]"
            users = self.get_users(client.get_data(get_users_url))
            if not users:
                return []
            libs = self._library_entries(client, 'emby')
            if libs:
                targets = [(lib['name'], str(lib['id'])) for lib in libs]
            else:
                logger.warning("【未看洗版】未能读取 Emby 媒体库清单，退化为全局拉取："
                               "本轮无法判定条目所属媒体库，排除媒体库不会命中")
                targets = [('', '')]
            all_items = []
            limit = 500
            for lib_name, parent_id in targets:
                lib_count = 0
                for user in users:
                    # 分页拉全：按加入日期降序，仅取未观看
                    start = 0
                    while True:
                        url = ("[HOST]emby/Users/" + user + "/Items"
                               "?SortBy=DateCreated%2CSortName"
                               "&SortOrder=Descending"
                               "&Filters=IsUnplayed"
                               "&Recursive=true"
                               # LibraryName 不在这里申请：Emby 根本不返回该字段，靠 ParentId 逐库取
                               "&Fields=PrimaryImageAspectRatio%2CBasicSyncInfo%2CProviderIds"
                               "&CollapseBoxSetItems=false"
                               "&ExcludeLocationTypes=Virtual"
                               "&EnableTotalRecordCount=true"
                               f"&Limit={limit}&StartIndex={start}"
                               + (f"&ParentId={parent_id}" if parent_id else "")
                               + "&api_key=[APIKEY]")
                        resp = self.get_items(client.get_data(url))
                        if not resp:
                            break
                        if lib_name:
                            for it in resp:
                                if isinstance(it, dict):
                                    it['LibraryName'] = lib_name
                        all_items.extend(resp)
                        lib_count += len(resp)
                        # 判断是否已经拉完
                        if len(resp) < limit:
                            break
                        start += limit
                if lib_name:
                    logger.info(f"【未看洗版】Emby 媒体库「{lib_name}」未观看条目 {lib_count} 条")
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
        结果带 TTL 缓存（默认 60 秒）：配置页与详情页都会调用，避免每次全量分页扫描。
        同时提取并缓存媒体库名称，供排除媒体库 VSelect 使用。
        """
        import time as _time
        now = _time.time()
        if self._options_cache and (now - self._options_cache_time) < self._options_cache_ttl:
            return self._options_cache
        options = []
        library_names = set()
        hidden = 0
        hidden_libs = set()
        excluded_ids = set()
        try:
            servers = self._get_server_instances()
            if not servers:
                return options
            seen = set()
            cap = 500
            for stype, srv_name, inst in servers:
                try:
                    if stype == 'jellyfin':
                        items = self.jellyfin_get_items(inst)
                    else:
                        items = self.emby_get_items(inst)
                except Exception as e:
                    logger.error(f"【未看洗版】读取 {srv_name}({stype}) 未观看列表失败：{e}")
                    continue
                for it in items:
                    item_name = it.get('Name')
                    # 库名来自「按库拉取」时打的标记（Emby 的 Items 接口没有这个字段）
                    lib_name = (it.get('LibraryName') or '').strip()
                    if lib_name:
                        library_names.add(lib_name)
                    if not item_name or item_name in seen:
                        continue
                    t = it.get('Type')
                    if t not in ('Movie', 'Series'):
                        continue
                    # 命中「排除媒体库 / 排除关键字」的条目直接从清单里隐去：
                    # 它们在运行阶段本来就会被 `_build_wash_tasks` 跳过，
                    # 留在清单里只会造成「能勾选、勾了却没反应」的误导。
                    if self._is_excluded_item(it):
                        hidden += 1
                        if lib_name:
                            hidden_libs.add(lib_name)
                        tid_hidden = self._tmdbid_of_item(it)
                        if tid_hidden:
                            excluded_ids.add(tid_hidden)
                        continue
                    if t == 'Series' and not self._include_series:
                        continue
                    pid = (it.get('ProviderIds') or {}).get('Tmdb')
                    if not pid:
                        continue
                    year = it.get('ProductionYear') or ''
                    typelabel = '电影' if t == 'Movie' else '剧集'
                    options.append({
                        'title': f"{item_name} ({year}) [{typelabel}]",
                        'value': int(pid),
                    })
                    seen.add(item_name)
                    if len(options) >= cap:
                        logger.info(f"EmbyUnwatchedWash 媒体库选项已截断至 {cap} 条")
                        break
        except Exception as e:
            logger.error(f"EmbyUnwatchedWash 构建媒体库选项失败：{e}")
        # 写缓存（即使为空也写，避免持续打爆媒体服务器）
        self._options_cache = options
        self._options_cache_time = now
        # 记录被隐去的条目，供详情页说明「为什么清单里的库比设置页选的少」
        self._options_hidden = hidden
        self._options_hidden_libs = sorted(hidden_libs)
        self._options_excluded_ids = excluded_ids
        if hidden:
            logger.info(f"【未看洗版】未观看清单已按排除规则隐藏 {hidden} 条"
                        f"（媒体库：{'、'.join(sorted(hidden_libs)) or '未知'}）")
        # 写库名缓存
        self._library_names_cache = sorted(library_names)
        self._library_names_cache_time = now
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
