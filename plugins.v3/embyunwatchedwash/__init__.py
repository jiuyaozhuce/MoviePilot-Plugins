import traceback
from datetime import datetime, timedelta
from threading import RLock
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.subscribe import SubscribeChain
from app.chain.search import SearchChain
from app.chain.download import DownloadChain
from app.core.config import settings
from app.core.event import eventmanager
from app.log import logger
try:
    # MoviePilot v3
    from app.sdk.services import MediaServerHelper
except ImportError:
    # MoviePilot v2
    from app.helper.mediaserver import MediaServerHelper
from app.plugins import _PluginBase
from app.schemas.types import MediaType, EventType
from app.core.metainfo import MetaInfo
try:
    from app.schemas.types import SystemConfigKey
except ImportError:
    SystemConfigKey = None

lock = RLock()

# 存量订阅的归属标记：v1.41 及之前本插件创建的订阅都以它作为 username。
# v1.42 起插件不再自建订阅（订阅创建交给 MP 原生「订阅 + 逐集洗版」），
# 该标记仅用于识别**存量**插件订阅：整季看完时的「取消订阅」动作只对它们生效；
# 用户自己在 MP 里建的订阅（best_version=1）只做 start_episode 推进，绝不删除。
WASH_USERNAME = "未看洗版"
# v1.46 电影洗版订阅的归属标记（与剧集未看洗版区分：电影洗版订阅不因「已观看」而取消，
# 只在其洗到目标画质前持续存在；电影出库时由「资源出库回收」取消）。
WASH_USERNAME_MOVIE = "未看洗版·电影"


class EmbyUnwatchedWash(_PluginBase):
    # 插件名称
    plugin_name = "未看洗版"
    # 插件描述
    plugin_desc = "v1.48 精简重构：剧集部分只把 Emby 观看事实写回 MP 订阅（开始集数 / 总集数 / 已看集优先级），" \
                 "搜索下载、画质比较、旧版去重全部交给 MoviePilot 官方路径，插件不再自己搜种下载；" \
                 "电影部分仍按目标画质自动建 best_version=1 洗版订阅（MP 原生洗版）。" \
                 "并保留旧版清理与资源出库回收。"
    # 插件版本
    plugin_version = "1.48"
    # 插件作者
    plugin_author = "jiuyaozhuce"
    # 作者主页
    author_url = "https://github.com/jiuyaozhuce/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "embyunwatchedwash_"
    # 加载顺序
    plugin_order = 14
    # 可使用的用户级别
    auth_level = 2

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None
    subscribechain = None
    searchchain = None
    downloadchain = None
    # Dry-run 标记：观看联动只打印「将推进」、旧版清理只打印「将删除」，均不实际执行
    _dry_run: bool = False
    # 洗版后清理：订阅覆盖的内容在媒体库出现更高画质版本时，删除低画质旧条目
    # （只调 Emby 删除接口删软链，不碰下载器种子与原始文件）
    _delete_washed_old: bool = False
    # SubscribeComplete 事件兜底删除链的延迟分钟数
    _delete_delay_minutes: int = 10
    # 剧集逐集核对旧版（v1.42）：True=对订阅覆盖季的**全部集**逐集核对（追更期即时清理）；
    # False=仅订阅完成事件指定的集核对（v1.41 行为，更保守）
    _tv_full_sweep: bool = True
    # ------------------------------------------------------------------
    # 观看联动（v1.42 核心）：按观看记录自动后移逐集洗版订阅的开始集数
    # ------------------------------------------------------------------
    #   True  = 开启（默认）：所有 best_version=1 的剧集订阅，按观看进度把
    #           start_episode 推进到第一个未观看集（只进不退，非破坏性）；
    #           username==未看洗版 的存量订阅整季看完时取消
    #   False = 关闭：插件不碰任何订阅参数
    _cancel_watched: bool = True
    # 「已观看」判定所依据的媒体服务器；为空表示所有已配置且连接的服务器
    _watched_check_servers: List[str] = []
    # 资源出库回收（v1.45 新增，默认关闭）：媒体库里已不存在的「未看洗版」存量订阅
    # 自动取消，避免 MP 继续为空库资源搜刮下载。用户自建订阅（best_version=1）只提示不代删；
    # 媒体服务器状态未知时一律不动（区分「确定出库」与「查询异常」，绝不误删）。
    _cancel_removed: bool = False
    # ------------------------------------------------------------------
    # v1.48 重构：剧集「只调订阅字段」，搜索/下载/删旧版全部交给 MP 官方路径
    # ------------------------------------------------------------------
    # 电影洗版：库内低于目标画质的电影，自动建 best_version=1 订阅（MP 原生洗版）。
    _movie_wash: bool = False
    # 洗版目标画质（分辨率），如 1080P / 4K / Remux；低于该画质才触发洗版。
    _wash_resolution: str = "1080P"
    _wash_resolution_height: int = 1080

    def init_plugin(self, config: dict = None):
        self.subscribechain = SubscribeChain()
        self.searchchain = SearchChain()
        self.downloadchain = DownloadChain()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._only_once = config.get("only_once")
            # 观看联动（v1.42 核心功能，默认开启；start_episode 推进只进不退，非破坏性）
            self._cancel_watched = bool(config.get("cancel_watched", True))
            self._watched_check_servers = self._normalize_str_list(
                config.get("watched_check_servers", []))
            self._cancel_removed = bool(config.get("cancel_removed", False))
            self._movie_wash = bool(config.get("movie_wash", False))
            self._wash_resolution = str(config.get("wash_resolution", "1080P") or "1080P")
            self._wash_resolution_height = self._res_height(self._wash_resolution)
            self._dry_run = bool(config.get("dry_run", False))
            # 旧版清理（v1.41 引入、v1.42 即时化）：默认关闭——这是「删除」动作，误删代价高
            self._delete_washed_old = bool(config.get("delete_washed_old", False))
            try:
                self._delete_delay_minutes = max(1, int(config.get("delete_delay_minutes") or 10))
            except (TypeError, ValueError):
                self._delete_delay_minutes = 10
            # 剧集逐集核对（v1.42 新增）：默认开启，追更期旧版软料即时清理
            self._tv_full_sweep = bool(config.get("tv_full_sweep", True))

        if self._only_once:
            self._only_once = False
            self.update_config({
                "enabled": self._enabled,
                "cron": self._cron,
                "only_once": self._only_once,
                "cancel_watched": self._cancel_watched,
                "watched_check_servers": self._watched_check_servers,
                "cancel_removed": self._cancel_removed,
                "movie_wash": self._movie_wash,
                "wash_resolution": self._wash_resolution,
                "dry_run": self._dry_run,
                "delete_washed_old": self._delete_washed_old,
                "delete_delay_minutes": self._delete_delay_minutes,
                "tv_full_sweep": self._tv_full_sweep,
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
        获取插件API（v1.42 收敛为 1 个端点：订阅概览刷新）
        """
        return [
            {
                "path": "/overview",
                "endpoint": self._api_overview,
                "methods": ["GET"],
                "summary": "刷新洗版订阅概览（详情页按钮点击后整页自动重渲染，读取最新订阅状态）"
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

    def _api_overview(self) -> Dict[str, Any]:
        """API 端点：刷新详情页概览（GET）。点击后详情页自动重渲染，读取最新订阅数据。"""
        try:
            subs = self._list_best_version_subscriptions()
            tv = sum(1 for s in subs if self._sub_is_tv(s))
            return self._api_response(True, f"当前共 {len(subs)} 个洗版订阅（剧集 {tv} / 电影 {len(subs) - tv}）",
                                      {"count": len(subs)})
        except Exception as e:
            logger.error(f"【未看洗版】读取订阅概览失败：{e}\n{traceback.format_exc()}")
            return self._api_response(False, str(e))

    @staticmethod
    def _sub_is_tv(sub: Any) -> bool:
        """判断订阅类型是否为剧集（v3 快照 type 字段取值随版本有差异，宽匹配）。"""
        t = str(getattr(sub, "type", "") or "")
        return t in (MediaType.TV.value, "电视剧", "TV", "Series", MediaType.TV.name)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面（v1.42）。
        订阅创建已完全交给 MP 原生（订阅开启洗版开关 + 两段式规则组），插件只做两件事：
        观看联动（自动后移 start_episode）与旧版清理（删低画质旧软链），
        配置项因此收敛为：基础设置 / 观看联动 / 旧版清理 / 试运行。
        """
        # 媒体服务器清单供「观看状态核对服务器」选择。只列名称，不主动连服务器，
        # 打不开也不该让配置页整个失败。
        try:
            server_names = self._get_server_names()
        except Exception as e:
            logger.error(f"【未看洗版】读取媒体服务器清单失败，核对服务器将为空：{e}")
            server_names = []
        server_items = [{'title': n, 'value': n} for n in server_names]

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
                            'class': 'text-subtitle-2',
                            'text': 'v1.48 起插件不再自己搜种下载，只把观看事实写回订阅：'
                            '① 按观看记录把逐集洗版订阅的「开始集数」推进到第一个未观看集；'
                            '② 已观看的集标记为「洗版已达标」，不再重复洗；'
                            '③ 用媒体库真实集号维护订阅总集数。'
                            '搜索、下载、画质比较与旧版去重全部交给 MoviePilot 官方路径。'
                            '另有「电影洗版」（库内低画质电影自动建洗版订阅）与'
                            '「旧版清理」（删除被洗版的低画质软链）。'
                        }
                    }),

                    # ---------- 1. 基础设置 ----------
                    section('基础设置'),
                    control('VSwitch', 'enabled', '启用插件', md=6),
                    control('VTextField', 'cron', '执行周期', md=6,
                            placeholder='留空则每 30 分钟运行一次',
                            hint='5 位 cron 表达式，留空按 30 分钟间隔；例：0 3 * * * 表示每天 03:00 运行',
                            **{'persistent-hint': True}),

                    # ---------- 2. 观看联动 ----------
                    section('观看联动（自动后移开始集数）'),
                    control('VSwitch', 'cancel_watched', '按观看进度同步洗版订阅', md=6,
                            hint='v1.47 起插件不再自己搜索下载，只把观看事实写回 MP 订阅并由官方路径洗版：'
                                 '① 把「开始集数」推进到第一个未观看集（只进不退）；'
                                 '② 把已观看的集标记为「洗版已达标」，不再重复洗；'
                                 '③ 用媒体库真实集号维护订阅总集数，避免范围失真。'
                                 '全部看完后订阅会由 MP 自然判定完成，插件不主动删除任何订阅',
                            **{'persistent-hint': True}),
                    control('VSelect', 'watched_check_servers', '观看状态核对服务器', md=6,
                            items=server_items, multiple=True, chips=True, clearable=True,
                            hint='留空表示所有已配置并连接的媒体服务器；可只选其中一个，避免多服务器观看进度不一致时误判',
                            **{'persistent-hint': True}),
                    control('VSwitch', 'cancel_removed', '资源出库自动取消未看洗版订阅', md=6,
                            hint='媒体库里已不存在的「未看洗版」存量订阅自动取消（需开启上方「按观看进度后移开始集数」才会逐轮核对）。'
                                 '仅代删本插件名下的存量订阅，你自己建的订阅只提示、不代删；'
                                 '媒体服务器连不上/抖动时不动，绝不误删',
                            **{'persistent-hint': True}),

                    # ---------- 3. 旧版清理 ----------
                    section('旧版清理（删低画质旧软链）'),
                    control('VSwitch', 'delete_washed_old', '删除被洗版的低画质旧条目', md=6,
                            hint='每轮核对订阅覆盖内容的媒体库版本：同一内容出现更高画质版本时删除旧的低画质条目。'
                                 '只调 Emby 删除接口（删软链），不碰下载器种子与原始文件；同一内容保留画质最高的一个',
                            **{'persistent-hint': True}),
                    control('VSwitch', 'tv_full_sweep', '剧集逐集核对', md=6,
                            hint='开启：对订阅覆盖季的全部集逐集核对（追更期旧版即时清理，推荐）；'
                                 '关闭：仅订阅完成事件指定的集核对（更保守）',
                            **{'persistent-hint': True}),
                    control('VTextField', 'delete_delay_minutes', '完成事件兜底删除延迟（分钟）', md=6,
                            placeholder='默认 10',
                            hint='订阅完成事件触发的兜底核对，延迟 N 分钟再执行，等待新版入库与媒体库刷新',
                            **{'persistent-hint': True}),

                    # ---------- 2.5 电影洗版 ----------
                    section('电影洗版（库内低画质 → 建洗版订阅）'),
                    control('VSwitch', 'movie_wash', '启用电影洗版', md=6,
                            hint='开启后每轮扫描媒体库电影：低于「目标画质」且尚无订阅的，自动建 '
                                 'best_version=1 洗版订阅（MP 原生洗版，下到高画质后替换旧版）。'
                                 '已有任何订阅的电影不重复建（如需洗版请手动开启 best_version）',
                            **{'persistent-hint': True}),
                    control('VSelect', 'wash_resolution', '洗版目标画质', md=6,
                            items=[{'title': x, 'value': x} for x in
                                   ['480P', '720P', '1080P', '4K', 'Remux']],
                            hint='低于该分辨率的电影才会触发洗版；库内多为 1080P 想冲 4K 则选 4K',
                            **{'persistent-hint': True}),

                    # ---------- 4. 试运行与手动触发 ----------
                    section('试运行与手动触发'),
                    control('VSwitch', 'dry_run', 'Dry-run 预览模式', md=6,
                            hint='只打印将做的动作（推进开始集数/删除旧版），不实际执行',
                            **{'persistent-hint': True}),
                    control('VSwitch', 'only_once', '保存后立即运行一次', md=6,
                            hint='保存配置后立刻执行一次（不受启用开关管控），执行后自动关闭',
                            **{'persistent-hint': True}),
                ]
            }]
        }], {
            "enabled": False,
            "cron": "",
            "only_once": False,
            "cancel_watched": True,
            "watched_check_servers": [],
            "cancel_removed": False,
            "movie_wash": False,
            "wash_resolution": "1080P",
            "delete_washed_old": False,
            "tv_full_sweep": True,
            "delete_delay_minutes": 10,
            "dry_run": False,
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

    def get_page(self) -> List[dict]:
        """
        详情页（数据查看，v1.42）：逐集洗版订阅概览。
        插件不再自建订阅、不再维护未观看清单与洗版历史，页面收敛为一张
        「洗版订阅概览」表：哪些订阅在洗版、季/开始集数推进到哪、归属是谁。
        """
        apikey = self._api_key()
        contents: List[dict] = []

        contents.append({
            'component': 'VAlert',
            'props': {
                'type': 'info', 'variant': 'tonal', 'class': 'text-subtitle-2 mb-3',
                'text': '插件每轮自动：① 按观看记录把逐集洗版订阅的「开始集数」后移到第一个未观看集；'
                        '② 核对媒体库多版本并清理低画质旧条目（需开启开关）。'
                        '订阅的创建与暂停请在 MoviePilot「订阅」页操作（开启洗版开关即可）。'
            }
        })

        try:
            subs = self._list_best_version_subscriptions()
        except Exception as e:
            subs = []
            contents.append({
                'component': 'VAlert',
                'props': {'type': 'error', 'variant': 'tonal', 'class': 'text-subtitle-2 mb-3',
                          'text': f'读取订阅失败：{e}'}
            })

        tv_subs = [s for s in subs if self._sub_is_tv(s)]
        movie_subs = [s for s in subs if not self._sub_is_tv(s)]

        # 分区标题 + 刷新按钮（点击调 API 后详情页自动重渲染）
        contents.append({
            'component': 'div',
            'props': {'class': 'd-flex align-center flex-wrap mb-2'},
            'content': [
                {'component': 'span', 'props': {'class': 'text-subtitle-1 font-weight-bold'},
                 'text': f'洗版订阅概览（共 {len(subs)} 个：剧集 {len(tv_subs)} / 电影 {len(movie_subs)}）'},
                {'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis ms-2'},
                 'text': '开始集数由插件按观看进度自动后移（只进不退）'},
                {'component': 'VBtn',
                 'props': {'color': 'primary', 'variant': 'tonal', 'size': 'small',
                           'prepend-icon': 'mdi-refresh', 'class': 'ml-auto'},
                 'text': '刷新',
                 'events': {'click': {'api': 'plugin/EmbyUnwatchedWash/overview',
                                      'method': 'get', 'params': {'apikey': apikey}}}},
            ]
        })

        if not subs:
            contents.append({
                'component': 'VAlert',
                'props': {'type': 'warning', 'variant': 'tonal', 'class': 'text-subtitle-2 mb-3',
                          'text': '当前没有开启洗版（best_version=1）的订阅。'
                                  '在 MoviePilot 订阅详情里打开「洗版」开关后，订阅会出现在这里。'}
            })
        else:
            cards = []
            for sub in subs:
                sid = getattr(sub, 'id', None)
                name = getattr(sub, 'name', '') or str(sid)
                year = getattr(sub, 'year', '') or ''
                season = getattr(sub, 'season', None)
                start_ep = getattr(sub, 'start_episode', None)
                username = str(getattr(sub, 'username', '') or '—')
                is_tv = self._sub_is_tv(sub)
                if is_tv:
                    scope = (f'第{season}季' if season is not None else '全剧') + \
                            (f' · 从第{start_ep}集' if start_ep is not None else '')
                else:
                    scope = '—'
                cards.append({
                    'component': 'div',
                    'props': {'class': 'd-flex align-center flex-wrap ga-2 py-1'},
                    'content': [
                        {'component': 'span', 'props': {'class': 'text-body-2 font-weight-medium'},
                         'text': f'{name}（{year}）' if year else name},
                        {'component': 'VChip',
                         'props': {'size': 'x-small', 'variant': 'tonal',
                                   'color': 'primary' if is_tv else 'success'},
                         'text': '剧集' if is_tv else '电影'},
                        {'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis'},
                         'text': scope},
                        {'component': 'VChip', 'props': {'size': 'x-small', 'variant': 'outlined'},
                         'text': username},
                    ]
                })
            contents.append(self._grid(cards))

        # 用 MoviePilot 官方 VContainer 包裹整页内容，使数据页宽度与设置页（get_form）
        # 共用官方默认容器宽度。
        return [{'component': 'VContainer', 'props': {'class': 'pa-0'}, 'content': contents}]

    # ==================================================================
    # v1.48：电影洗版 + 订阅字段同步（剧集搜索下载交给官方）
    # ==================================================================
    _RES_HEIGHT = {
        "480P": 480, "576P": 576, "720P": 720, "1080P": 1080, "1080I": 1080,
        "2K": 1440, "1440P": 1440, "4K": 2160, "2160P": 2160, "REMUX": 2160,
    }

    @staticmethod
    def _res_height(s: str) -> int:
        """把分辨率字符串映射到视频高度（像素），用于画质门槛比较。"""
        return EmbyUnwatchedWash._RES_HEIGHT.get((s or "").upper().strip(), 0)

    def _emby_get(self, inst, path: str, params: dict) -> Optional[Any]:
        """对媒体服务器发 GET，Emby(/emby 前缀) 失败自动回退 Jellyfin(根路径)。返回 json 或 None。"""
        info = self._server_http_base(inst)
        if not info:
            return None
        base, apikey = info
        import requests as _rq
        p = dict(params)
        p["api_key"] = apikey
        for b in (base, base[:-5]):
            try:
                r = _rq.get(f"{b}{path}", params=p, timeout=20)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                continue
        return None

    def _all_subs(self) -> List[Any]:
        """列出全部订阅（用于电影洗版查重，避免重复建订阅）。"""
        repo = getattr(self.subscribechain, "subscription_repository", None)
        if repo is None:
            return []
        try:
            return list(repo.list() or [])
        except Exception as e:
            logger.error(f"【未看洗版】读取全部订阅失败：{e}")
            return []

    def _list_library_movies(self, inst) -> List[dict]:
        """枚举媒体库全部电影（tmdbid + 画质），分页单请求取回。"""
        out: List[dict] = []
        start = 0
        limit = 500
        for _ in range(100):  # 上限 5 万，防死循环
            data = self._emby_get(inst, "/Items", {
                "Recursive": "true", "IncludeItemTypes": "Movie",
                "Fields": "ProviderIds,MediaSources,ProductionYear",
                "StartIndex": start, "Limit": limit,
            })
            if not data:
                break
            items = data.get("Items") or []
            for it in items:
                if not isinstance(it, dict):
                    continue
                pids = it.get("ProviderIds") or {}
                tmdb = pids.get("Tmdb") or pids.get("TmdbId")
                if not tmdb:
                    continue
                try:
                    tmdb = int(tmdb)
                except (TypeError, ValueError):
                    continue
                out.append({"tmdb_id": tmdb, "name": it.get("Name", ""),
                            "year": it.get("ProductionYear") or "", "item": it})
            if len(items) < limit:
                break
            start += limit
        return out

    def _list_library_series(self, inst) -> List[dict]:
        """枚举媒体库全部剧集（tmdbid + SeriesId）。"""
        out: List[dict] = []
        start = 0
        limit = 500
        for _ in range(100):
            data = self._emby_get(inst, "/Items", {
                "Recursive": "true", "IncludeItemTypes": "Series",
                "Fields": "ProviderIds",
                "StartIndex": start, "Limit": limit,
            })
            if not data:
                break
            items = data.get("Items") or []
            for it in items:
                if not isinstance(it, dict):
                    continue
                sid = it.get("Id")
                if not sid:
                    continue
                pids = it.get("ProviderIds") or {}
                tmdb = pids.get("Tmdb") or pids.get("TmdbId")
                if not tmdb:
                    continue
                try:
                    tmdb = int(tmdb)
                except (TypeError, ValueError):
                    continue
                out.append({"tmdb_id": tmdb, "name": it.get("Name", ""),
                            "year": it.get("ProductionYear") or "", "series_id": sid})
            if len(items) < limit:
                break
            start += limit
        return out

    # ---------- 电影洗版 ----------
    def _scan_movies_wash(self) -> None:
        """
        电影洗版（v1.46）：扫描媒体库电影，对低于目标画质且尚无订阅的，
        自动建 best_version=1 洗版订阅（username=未看洗版·电影）。MP 原生洗版会在下到
        高画质后替换旧版，本方法只负责「建订阅」这一步。

        安全边界：已有任何订阅的电影不重复建（尊重用户自建订阅，不翻不删）；
        已到目标画质的电影跳过；目标画质未配置则整轮跳过。
        """
        threshold = self._wash_resolution_height
        if threshold <= 0:
            logger.warning("【未看洗版】电影洗版：未配置有效目标画质，跳过")
            return
        servers = self._get_server_instances()
        if not servers:
            return
        bv_ids = {int(getattr(s, "media_id", None)) for s in self._list_best_version_subscriptions()
                  if getattr(s, "media_id", None)}
        all_ids = {int(getattr(s, "media_id", None)) for s in self._all_subs()
                   if getattr(s, "media_id", None)}
        created = 0
        for stype, sname, inst in servers:
            for m in self._list_library_movies(inst):
                tmdb = m["tmdb_id"]
                if tmdb in bv_ids:
                    continue
                h = self._quality_key(m["item"])[0]
                if h >= threshold:
                    continue
                if tmdb in all_ids:
                    logger.info(f"【未看洗版】电影 {m['name']} 已有订阅，跳过自动建洗版"
                                f"（如需洗版请手动开启 best_version）")
                    continue
                if self._dry_run:
                    logger.info(f"[Dry-run] 电影洗版将建订阅：{m['name']}（当前 {h}p < 目标 {threshold}p）")
                    continue
                # v1.47 起：MP v3.1.7 起订阅写入启用严格字段白名单
                #（application/subscription/contract.py 的 SubscriptionPatch.__post_init__），
                # 旧关键字 tmdbid 已不在 _SUBSCRIPTION_FIELDS 中，继续传它会直接抛
                # 「订阅写入包含未知字段: tmdbid」，导致电影洗版建订阅 100% 失败。
                # 改为显式传 media_source + media_id（二者必须成对出现）。
                sid, err = self.subscribechain.add(
                    title=m["name"], year=str(m["year"] or ""),
                    media_source="themoviedb", media_id=str(tmdb),
                    best_version=1, username=WASH_USERNAME_MOVIE, exist_ok=True)
                if sid:
                    created += 1
                    logger.info(f"【未看洗版】已为低画质电影建洗版订阅：{m['name']}（{h}p）")
                else:
                    logger.warning(f"【未看洗版】建电影洗版订阅失败：{m['name']} - {err}")
        logger.info(f"【未看洗版】电影洗版扫描完成，新建 {created} 个洗版订阅"
                    + ("（Dry-run 预览）" if self._dry_run else ""))

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
        v1.42 主流程：订阅创建完全交给 MP 原生（订阅 + 逐集洗版），本插件每轮做两件事：
          1. 观看联动：按观看记录自动后移 best_version=1 剧集订阅的 start_episode（只进不退）
          2. 旧版清理：核对订阅覆盖内容的媒体库多版本，删除被洗版的低画质旧条目
        Dry-run 模式下两者都只打印、不执行。
        """
        if not (self._cancel_watched or self._delete_washed_old or self._cancel_removed
                or self._movie_wash):
            logger.info("【未看洗版】观看联动、资源出库回收、旧版清理、电影洗版均未开启，本轮无事可做")
            return
        # 获取锁
        _is_lock: bool = lock.acquire(timeout=60)
        if not _is_lock:
            logger.warning("【未看洗版】获取任务锁超时，已有实例在运行，本次跳过")
            return
        try:
            logger.info("【未看洗版】========== 开始运行 ==========")
            # ---------- 旧版清理兜底：先处理 SubscribeComplete 事件排队的到期任务 ----------
            # 事件触发的一次性核对若因插件重启丢失，最迟这一步也会补上。
            try:
                self._process_delete_tasks()
            except Exception as _del_err:
                logger.error(f"【未看洗版】处理洗版后清理任务异常（不影响后续流程）：{_del_err}")
            results = {"deleted": 0, "updated": 0, "failed": 0, "details": []}
            # ---------- 功能1：观看联动（自动后移 start_episode） ----------
            try:
                self._apply_watched_policy(results)
            except Exception as e:
                logger.error(f"【未看洗版】观看联动异常：{e}\n{traceback.format_exc()}")
                results["failed"] = results.get("failed", 0) + 1
            # ---------- 功能2：旧版清理（订阅驱动的即时核对） ----------
            swept = {"deleted": 0, "checked": 0, "failed": 0}
            try:
                self._sweep_old_versions(swept)
            except Exception as e:
                logger.error(f"【未看洗版】旧版清理异常：{e}\n{traceback.format_exc()}")
                swept["failed"] = swept.get("failed", 0) + 1
            # ---------- 功能3：电影洗版（库内低画质→建洗版订阅） ----------
            if self._movie_wash:
                try:
                    self._scan_movies_wash()
                except Exception as e:
                    logger.error(f"【未看洗版】电影洗版异常：{e}\n{traceback.format_exc()}")
            # ---------- 汇总 ----------
            logger.info(f"【未看洗版】========== 运行完成 ========== | "
                        f"更新订阅 {results.get('updated', 0)} 个 | 取消订阅 {results.get('deleted', 0)} 个 | "
                        f"核对旧版 {swept.get('checked', 0)} 项 | 删除旧版 {swept.get('deleted', 0)} 个 | "
                        f"失败 {results.get('failed', 0) + swept.get('failed', 0)} 次"
                        + ("（Dry-run 预览，未实际执行）" if self._dry_run else ""))
            for idx, txt in enumerate((results.get('details') or [])[:30], 1):
                logger.info(f"  {idx}. {txt}")
        except Exception as e:
            # 兜底：任何未捕获异常都要打出来，否则任务会像"卡住"一样静默结束
            logger.error(f"【未看洗版】运行过程发生未捕获异常：{e}\n{traceback.format_exc()}")
        finally:
            lock.release()

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

    def _get_server_names(self) -> List[str]:
        """
        只取媒体服务器**名称**，不碰实例、不连服务器。

        供配置页的「观看状态核对服务器」下拉使用：配置页必须能打开，
        即使某台服务器当前连不上也不该让整个 get_form 失败。
        """
        names: List[str] = []
        try:
            services = MediaServerHelper().get_services()
        except Exception:
            # v3 的 get_services 依赖运行模块实例；未启动时拿不到，退化为读配置
            try:
                for stype, name, _ in self._get_server_instances():
                    if name and name not in names:
                        names.append(name)
            except Exception:
                pass
            return names
        for name, info in (services or {}).items():
            stype = (getattr(info, "type", "") or "").lower()
            if stype in ("emby", "jellyfin") and name and name not in names:
                names.append(name)
        if not names:
            try:
                for _stype, name, _inst in self._get_server_instances():
                    if name and name not in names:
                        names.append(name)
            except Exception:
                pass
        return names

    # ==================================================================
    # 已观看联动（v1.36）
    #
    # 需求：洗版订阅建好之后，如果这部影视被看了，就不该再继续洗版 ——
    #   · 电影变为已观看  -> 取消它的洗版订阅
    #   · 剧集某些集已观看 -> 把这些集从洗版范围里剔除（订阅「开始集数」推进到
    #                        第一个未观看的集）；整季都看完 -> 取消该季订阅
    #
    # 为什么用「推进开始集数」而不是「逐集排除」：MoviePilot 的订阅模型只支持
    # `start_episode`（从第几集开始搜）这一个范围字段，没有「排除某些集」的概念。
    # 而本插件建剧集订阅时本来就是「该季第一个未观看集 = start_episode」，
    # 所以「重算 start_episode」在语义上正好等价于「已看的集不再洗」。
    #
    # 判定数据源：直接问媒体服务器（Emby/Jellyfin），不用订阅表里的状态 ——
    # 订阅表只有「下载/洗版进度」，没有「用户看没看」。
    # ==================================================================

    def _watched_targets(self) -> List[Tuple[str, str, Any]]:
        """返回用于核对观看状态的服务器实例；配置了名单则只取名单内的。"""
        servers = self._get_server_instances()
        wanted = [s for s in (self._watched_check_servers or []) if s]
        if not wanted:
            return servers
        picked = [x for x in servers if x[1] in wanted]
        missing = [w for w in wanted if w not in [x[1] for x in servers]]
        if missing:
            logger.warning(f"【未看洗版】配置的观看状态核对服务器未连接/不存在，已跳过："
                           f"{'、'.join(missing)}")
        return picked

    @staticmethod
    def _episodes_of_series(inst, series_id: str, user_id: str) -> Optional[Dict[int, Dict[int, bool]]]:
        """
        查询某部剧在媒体服务器上的**每一集是否已观看**。

        返回 {季号: {集号: 是否已看}}；查不到返回 None（调用方必须把 None 当「未知」
        而不是「都没看」—— 否则服务器一抖就会误判成全部未观看，白洗一遍）。

        用 `/Shows/{id}/Episodes?UserId=...`：Emby/Jellyfin 只有带上 UserId 才会在
        每集上返回 UserData.Played。本机实测（势在必行 Id=21046）：不带 UserId 时
        Items 里没有 UserData 字段，带上后每集都有 Played。
        """
        try:
            host = getattr(inst, "host", "") or getattr(inst, "_host", "")
            apikey = getattr(inst, "apikey", "") or getattr(inst, "_apikey", "")
            if not host or not apikey:
                return None
            host = host.rstrip("/")
            # 统一走 requests，避免依赖某个版本的 SDK 方法签名。
            # Emby 挂在 /emby 前缀下、Jellyfin 直接挂在根上 —— 用 host 里有没有
            # /emby 判断前缀，拼出正确的 Episodes 地址。
            base = host if host.endswith("/emby") else (host + "/emby")
            url = f"{base}/Shows/{series_id}/Episodes"
            import requests
            r = requests.get(url, params={
                "api_key": apikey,
                "UserId": user_id,
                "Fields": "ProviderIds",
            }, timeout=15)
            if r.status_code != 200:
                # Jellyfin 无 /emby 前缀：换根路径再试一次
                r = requests.get(f"{host}/Shows/{series_id}/Episodes", params={
                    "api_key": apikey,
                    "UserId": user_id,
                    "Fields": "ProviderIds",
                }, timeout=15)
            if r.status_code != 200:
                logger.warning(f"【未看洗版】查询剧集集观看状态失败（HTTP {r.status_code}）："
                               f"series_id={series_id}")
                return None
            items = (r.json() or {}).get("Items") or []
            out: Dict[int, Dict[int, bool]] = {}
            for it in items:
                if not isinstance(it, dict):
                    continue
                season = it.get("ParentIndexNumber")
                ep = it.get("IndexNumber")
                if season is None or ep is None:
                    continue
                played = bool(((it.get("UserData") or {}).get("Played")))
                try:
                    out.setdefault(int(season), {})[int(ep)] = played
                except (TypeError, ValueError):
                    continue
            return out
        except Exception as e:
            logger.warning(f"【未看洗版】查询剧集集观看状态异常（series_id={series_id}）：{e}")
            return None

    @staticmethod
    def _movie_played(inst, tmdb_id: int, user_id: str) -> Optional[bool]:
        """
        查询某部电影是否已观看。返回 True/False；**查不到返回 None（未知）**。

        用 `AnyProviderIdEquals=Tmdb.{id}` 精确定位，避免按标题匹配到同名的另一部。
        本机实测（东极岛 tmdb=1305653）：返回 Items[0].UserData.Played。
        """
        try:
            host = getattr(inst, "host", "") or getattr(inst, "_host", "")
            apikey = getattr(inst, "apikey", "") or getattr(inst, "_apikey", "")
            if not host or not apikey:
                return None
            host = host.rstrip("/")
            import requests
            r = requests.get(f"{host}/emby/Items", params={
                "api_key": apikey,
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "AnyProviderIdEquals": f"Tmdb.{tmdb_id}",
                "UserId": user_id,
                "Fields": "ProviderIds",
            }, timeout=15)
            if r.status_code != 200:
                # Jellyfin 没有 /emby 前缀，退一步再试一次
                r = requests.get(f"{host}/Items", params={
                    "api_key": apikey,
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie",
                    "AnyProviderIdEquals": f"Tmdb.{tmdb_id}",
                    "UserId": user_id,
                    "Fields": "ProviderIds",
                }, timeout=15)
            if r.status_code != 200:
                return None
            items = (r.json() or {}).get("Items") or []
            if not items:
                # 条目在服务器上找不到（可能被删/挪库）—— 也是未知，不能当「没看过」
                return None
            return bool(((items[0].get("UserData") or {}).get("Played")))
        except Exception as e:
            logger.warning(f"【未看洗版】查询电影观看状态异常（tmdb={tmdb_id}）：{e}")
            return None

    @staticmethod
    def _series_id_of(inst, tmdb_id: int, name: str = "", year: str = "") -> Optional[str]:
        """
        在媒体服务器上按 tmdbid（优先）或标题定位剧集的 SeriesId。找不到返回 None。
        """
        try:
            host = getattr(inst, "host", "") or getattr(inst, "_host", "")
            apikey = getattr(inst, "apikey", "") or getattr(inst, "_apikey", "")
            if not host or not apikey:
                return None
            host = host.rstrip("/")
            import requests
            # 1) 按 tmdbid 精确找（最可靠）
            for prefix in ("/emby", ""):
                r = requests.get(f"{host}{prefix}/Items", params={
                    "api_key": apikey,
                    "Recursive": "true",
                    "IncludeItemTypes": "Series",
                    "AnyProviderIdEquals": f"Tmdb.{tmdb_id}",
                    "Fields": "ProviderIds",
                }, timeout=15)
                if r.status_code == 200:
                    items = (r.json() or {}).get("Items") or []
                    if items:
                        return items[0].get("Id")
                    break
            # 2) 退化为标题搜索
            if name:
                for prefix in ("/emby", ""):
                    r = requests.get(f"{host}{prefix}/Items", params={
                        "api_key": apikey,
                        "Recursive": "true",
                        "IncludeItemTypes": "Series",
                        "SearchTerm": name,
                    }, timeout=15)
                    if r.status_code == 200:
                        for it in ((r.json() or {}).get("Items") or []):
                            if it.get("Name") == name and (
                                    not year or str(it.get("ProductionYear")) == str(year)):
                                return it.get("Id")
                        break
            return None
        except Exception as e:
            logger.warning(f"【未看洗版】定位剧集失败（tmdb={tmdb_id}）：{e}")
            return None

    def _apply_watched_policy(self, results: Optional[dict] = None) -> None:
        """
        观看联动主流程（v1.48）：核对**所有** best_version=1 订阅的观看状态。

        剧集：把观看事实写回订阅字段（start_episode / total_episode / episode_priority），
              搜索与下载交给 MP 官方洗版路径；插件不主动取消订阅，
              全看完后由 MP 自然判定完成。
        电影：已观看则取消洗版订阅（电影没有「集」的概念）。

        服务范围：所有 best_version=1 的订阅（不止插件自己建的那条）。
        安全边界：只写订阅字段，不删用户的剧集订阅；每次写库走官方 mutation 入口。
        每个订阅独立 try 保护。
        """
        if results is None:
            results = {"deleted": 0, "updated": 0, "failed": 0, "details": []}
        if not self._cancel_watched and not self._cancel_removed:
            return
        servers = self._watched_targets()
        if not servers:
            logger.warning("【未看洗版】已开启观看联动，但没有可用的媒体服务器，本轮跳过观看状态核对")
            return
        stype, srv_name, inst = servers[0]
        user_id = self._server_user_id(inst)
        if not user_id:
            logger.warning(f"【未看洗版】无法从 {srv_name} 取到用户 ID，本轮跳过观看状态核对")
            return
        try:
            subs = self._list_best_version_subscriptions()
        except Exception as e:
            logger.error(f"【未看洗版】读取逐集洗版订阅失败，本轮跳过观看状态核对：{e}")
            return
        tv_subs = [s for s in subs if self._sub_is_tv(s)]
        movie_subs = [s for s in subs if not self._sub_is_tv(s)]
        logger.info(f"【未看洗版】观看联动：以 {srv_name}({stype}) 用户 {user_id[:8]}… 为准核对"
                    f"逐集洗版剧集订阅 {len(tv_subs)} 部、电影洗版订阅 {len(movie_subs)} 部")
        for sub in tv_subs:
            try:
                self._reconcile_one_subscription(sub, inst, user_id, results)
            except Exception as e:
                results["failed"] = results.get("failed", 0) + 1
                logger.error(f"【未看洗版】核对剧集订阅 id={getattr(sub, 'id', '?')} 失败：{e}"
                             f"\n{traceback.format_exc()}")
        for sub in movie_subs:
            try:
                self._reconcile_one_movie_subscription(sub, inst, user_id, results)
            except Exception as e:
                results["failed"] = results.get("failed", 0) + 1
                logger.error(f"【未看洗版】核对电影订阅 id={getattr(sub, 'id', '?')} 失败：{e}"
                             f"\n{traceback.format_exc()}")

    @staticmethod
    def _server_user_id(inst) -> Optional[str]:
        """
        取媒体服务器的用户 ID。

        优先级：实例自带 user 属性 → /Users 列表第一条。之所以需要它：
        Emby/Jellyfin 只有带上 UserId 才返回 UserData.Played。
        """
        try:
            uid = getattr(inst, "user", None)
            if uid:
                return str(uid)
            host = getattr(inst, "host", "") or getattr(inst, "_host", "")
            apikey = getattr(inst, "apikey", "") or getattr(inst, "_apikey", "")
            if not host or not apikey:
                return None
            host = host.rstrip("/")
            import requests
            for url in (f"{host}/Users", f"{host}/emby/Users"):
                try:
                    r = requests.get(url, params={"api_key": apikey}, timeout=15)
                    if r.status_code == 200:
                        users = r.json()
                        if isinstance(users, list) and users:
                            # 默认取第一个用户；本插件场景下就是管理员自己
                            return str(users[0].get("Id"))
                except Exception:
                    continue
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 洗版后清理（v1.41）：删除被洗版的低质量旧条目
    # ------------------------------------------------------------------
    # 背景：媒体库是 MoviePilot 整理后的软链接结构，洗版是「新增高画质文件」而非
    # 覆盖，完成后同一电影/同一集会新旧两个条目并存。本组逻辑在订阅完成事件后
    # 核对并删除低质量旧条目——只调 Emby 删除接口（删的是软链接），
    # 绝不触碰下载器种子与原始文件。

    @staticmethod
    def _server_http_base(inst) -> Optional[Tuple[str, str]]:
        """
        取媒体服务器的 (base_url, apikey)：Emby 挂 /emby 前缀、Jellyfin 挂根路径，
        返回以 /emby 结尾的 base（调用方对 Jellyfin 失败时自行回退根路径重试）。
        """
        host = getattr(inst, "host", "") or getattr(inst, "_host", "")
        apikey = getattr(inst, "apikey", "") or getattr(inst, "_apikey", "")
        if not host or not apikey:
            return None
        host = host.rstrip("/")
        base = host if host.endswith("/emby") else (host + "/emby")
        return base, apikey

    @staticmethod
    def _quality_key(item: dict) -> Tuple[int, int, int]:
        """
        条目画质排序键 (视频高度, 视频宽度, 总字节)。
        从 MediaSources.MediaStreams 里的视频流取最大宽高；取不到时全 0。
        全 0 表示识别信息不全——这类条目**永远不参与删除**（宁漏勿误删）。
        """
        w = h = 0
        size = 0
        for ms in (item.get("MediaSources") or []):
            if not isinstance(ms, dict):
                continue
            try:
                size += int(ms.get("Size") or 0)
            except (TypeError, ValueError):
                pass
            for st in (ms.get("MediaStreams") or []):
                if not isinstance(st, dict) or st.get("Type") != "Video":
                    continue
                try:
                    w = max(w, int(st.get("Width") or 0))
                    h = max(h, int(st.get("Height") or 0))
                except (TypeError, ValueError):
                    continue
        return (h, w, size)

    def _movie_versions(self, inst, tmdb_id: int) -> Optional[List[dict]]:
        """
        按 tmdbid 查媒体服务器上该电影的**全部**条目（多版本 = 多条目）。
        复用 _movie_played 验证过的 AnyProviderIdEquals 精确定位，Fields 追加
        Path/MediaSources/DateCreated 供画质对比与日志。失败返回 None（未知，不删）。
        """
        info = self._server_http_base(inst)
        if not info:
            return None
        base, apikey = info
        import requests as _rq
        params = {
            "api_key": apikey,
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "AnyProviderIdEquals": f"Tmdb.{tmdb_id}",
            "Fields": "Path,MediaSources,DateCreated",
        }
        try:
            r = _rq.get(f"{base}/Items", params=params, timeout=15)
            if r.status_code != 200:
                # Jellyfin 无 /emby 前缀：回退根路径重试
                r = _rq.get(f"{base[:-5]}/Items", params=params, timeout=15)
            if r.status_code != 200:
                logger.warning(f"【未看洗版】查询电影版本条目失败（HTTP {r.status_code}）：tmdb={tmdb_id}")
                return None
            items = [it for it in ((r.json() or {}).get("Items") or []) if isinstance(it, dict)]
            return items
        except Exception as e:
            logger.warning(f"【未看洗版】查询电影版本条目异常（tmdb={tmdb_id}）：{e}")
            return None

    def _episode_versions(self, inst, series_id: str, season: int, episode: int) -> Optional[List[dict]]:
        """
        查询某剧某季某集的**全部**条目（同集号多版本 = 多条目并存）。
        复用 _episodes_of_series 验证过的 /Shows/{id}/Episodes 端点。失败返回 None。
        """
        info = self._server_http_base(inst)
        if not info:
            return None
        base, apikey = info
        import requests as _rq
        params = {
            "api_key": apikey,
            "Fields": "Path,MediaSources,DateCreated",
        }
        try:
            r = _rq.get(f"{base}/Shows/{series_id}/Episodes", params=params, timeout=15)
            if r.status_code != 200:
                r = _rq.get(f"{base[:-5]}/Shows/{series_id}/Episodes", params=params, timeout=15)
            if r.status_code != 200:
                logger.warning(f"【未看洗版】查询剧集集条目失败（HTTP {r.status_code}）：series_id={series_id}")
                return None
            items = []
            for it in ((r.json() or {}).get("Items") or []):
                if not isinstance(it, dict):
                    continue
                try:
                    if int(it.get("ParentIndexNumber") or -1) == int(season) \
                            and int(it.get("IndexNumber") or -1) == int(episode):
                        items.append(it)
                except (TypeError, ValueError):
                    continue
            return items
        except Exception as e:
            logger.warning(f"【未看洗版】查询剧集集条目异常（series_id={series_id}）：{e}")
            return None

    def _delete_server_item(self, inst, item: dict) -> bool:
        """
        调用 Emby/Jellyfin 删除接口移除单个条目（DELETE /Items/{id}）。
        只删媒体服务器条目（软链接场景下即删软链），不触碰下载器与原始文件。
        """
        info = self._server_http_base(inst)
        if not info:
            return False
        base, apikey = info
        import requests as _rq
        item_id = str(item.get("Id") or "")
        if not item_id:
            return False
        params = {"api_key": apikey}
        try:
            r = _rq.delete(f"{base}/Items/{item_id}", params=params, timeout=15)
            if r.status_code not in (200, 204):
                # Jellyfin 无 /emby 前缀：回退根路径重试
                r = _rq.delete(f"{base[:-5]}/Items/{item_id}", params=params, timeout=15)
            if r.status_code in (200, 204):
                return True
            logger.error(f"【未看洗版】删除条目失败（HTTP {r.status_code}）："
                         f"{item.get('Name')} / {item.get('Path')} —— "
                         f"请检查媒体服务器是否允许 API 删除媒体")
            return False
        except Exception as e:
            logger.error(f"【未看洗版】删除条目异常：{item.get('Name')} - {e}")
            return False

    def _subinfo_get(self, info: dict, *names):
        """从订阅快照里按多个候选键名取值（v3 快照字段名随版本有差异）。"""
        for n in names:
            v = info.get(n)
            if v is not None:
                return v
        return None

    @eventmanager.register(EventType.SubscribeComplete)
    def subscribe_complete_handler(self, event):
        """
        订阅完成事件 → 排入「删除旧版本」待办任务（兜底链，v1.42）。

        v1.42 匹配规则（插件不再自建订阅、不再维护 history，改看事件快照）：
        订阅快照 best_version=1（开启了逐集洗版）或 username == '未看洗版'
        （存量自建订阅）才排任务，其余订阅的完成事件直接忽略。
        """
        try:
            if not self._enabled or not self._delete_washed_old:
                return
            ed = getattr(event, "event_data", None) or {}
            sid = ed.get("subscribe_id")
            sub_info = ed.get("subscribe_info") or {}
            if not isinstance(sub_info, dict) or not sub_info:
                return
            best = self._subinfo_get(sub_info, "best_version")
            username = str(self._subinfo_get(sub_info, "username", "user", "user_name") or "")
            if not best and username != WASH_USERNAME:
                return
            sub_tmdb = self._subinfo_get(sub_info, "tmdbid", "tmdb_id", "media_id")
            try:
                sub_tmdb = int(str(sub_tmdb))
            except (TypeError, ValueError):
                return
            hit = {
                "title": self._subinfo_get(sub_info, "name", "title"),
                "year": self._subinfo_get(sub_info, "year"),
                "tmdbid": sub_tmdb,
                "type": self._subinfo_get(sub_info, "type"),
                "season": self._subinfo_get(sub_info, "season"),
                "start_episode": self._subinfo_get(sub_info, "start_episode"),
            }
            self._queue_delete_task(hit, sid)
        except Exception as e:
            logger.error(f"【未看洗版】处理订阅完成事件异常：{e}\n{traceback.format_exc()}")

    def _queue_delete_task(self, hit: dict, subscribe_id) -> None:
        """
        把一条「洗版完成」记录排队为删除任务（持久化，跨重启不丢），
        并安排一次性核对（插件自有调度器不可用时靠每轮 sync 兜底）。
        """
        tasks = self.get_data('delete_tasks') or {}
        # 同一订阅只排一次（事件可能重复投递）
        sig = str(subscribe_id or f"{hit.get('tmdbid')}:{hit.get('season')}:{hit.get('start_episode')}")
        for t in tasks.values():
            if t.get("sig") == sig and t.get("status") == "pending":
                return
        now = datetime.now()
        exec_at = now + timedelta(minutes=self._delete_delay_minutes)
        task_id = now.strftime("%Y%m%d%H%M%S") + f"_{int(now.timestamp() * 1000) % 100000}"
        tasks[task_id] = {
            "sig": sig,
            "title": hit.get("title"),
            "year": hit.get("year"),
            "tmdbid": hit.get("tmdbid"),
            "type": hit.get("type"),
            "season": hit.get("season"),
            "start_episode": hit.get("start_episode"),
            "subscribe_id": subscribe_id,
            "status": "pending",
            "retry": 0,
            "created": now.strftime("%Y-%m-%d %H:%M:%S"),
            "execute_at": exec_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_data('delete_tasks', tasks)
        logger.info(f"【未看洗版】洗版完成，已排队删除旧版本（延迟 {self._delete_delay_minutes} 分钟核对）："
                    f"{hit.get('title')}（{hit.get('type')}，tmdb={hit.get('tmdbid')}，"
                    f"季={hit.get('season')}，开始集={hit.get('start_episode')}）")
        # 一次性调度：到期即刻核对（调度器不可用时由每轮 sync 兜底）
        try:
            if self._scheduler is None:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if not self._scheduler.running:
                self._scheduler.start()
            self._scheduler.add_job(
                self._process_delete_tasks, 'date',
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(minutes=self._delete_delay_minutes),
                id=f"wash_delete_{task_id}",
                name="未看洗版·删除旧版本核对",
                replace_existing=True,
            )
        except Exception as e:
            logger.warning(f"【未看洗版】一次性核对调度不可用（将由每轮扫描兜底处理）：{e}")

    def _process_delete_tasks(self) -> None:
        """
        核对并执行到期的删除任务。入口有三：订阅完成事件的一次性调度、
        每轮 sync 的兜底调用、本方法内失败重试。全部安全优先：
        Dry-run 只记录不删；画质参数不全的条目永不删除；异常最多重试 3 次。
        """
        if not self._delete_washed_old:
            return
        tasks = self.get_data('delete_tasks') or {}
        if not tasks:
            return
        now = datetime.now()
        changed = False
        for task_id, t in list(tasks.items()):
            if t.get("status") != "pending":
                continue
            try:
                exec_at = datetime.strptime(str(t.get("execute_at")), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                t["status"], t["error"] = "failed", "execute_at 无法解析"
                changed = True
                continue
            if exec_at > now:
                continue
            try:
                self._do_delete_old_versions(t)
                t["status"] = "done"
                t["finished"] = now.strftime("%Y-%m-%d %H:%M:%S")
            except Exception as e:
                t["retry"] = int(t.get("retry") or 0) + 1
                t["error"] = str(e)
                logger.warning(f"【未看洗版】删除旧版本核对失败（第 {t['retry']} 次）："
                               f"{t.get('title')} - {e}")
                if t["retry"] >= 3:
                    t["status"] = "failed"
                    logger.error(f"【未看洗版】删除旧版本任务失败放弃：{t.get('title')} - {e}")
            changed = True
        # 清理 7 天前已结束的任务，避免 plugindata 膨胀
        cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        for task_id in list(tasks.keys()):
            t = tasks[task_id]
            if t.get("status") in ("done", "failed") and str(t.get("finished") or t.get("created") or "") < cutoff:
                del tasks[task_id]
        if changed:
            self.save_data('delete_tasks', tasks)

    def _do_delete_old_versions(self, task: dict) -> None:
        """
        执行单个兜底删除任务（SubscribeComplete 事件队列）：
        在每台已连接媒体服务器上查该内容的全部版本条目，保留画质最高的一个，删除其余。
        v1.42：剧集任务缺集信息且开启「逐集核对」时，改为对整季（或全剧）逐集核对。
        查询失败抛异常交由 _process_delete_tasks 重试（≤3 次）。
        """
        tmdb = task.get("tmdbid")
        try:
            tmdb = int(tmdb)
        except (TypeError, ValueError):
            logger.warning(f"【未看洗版】删除任务 tmdbid 无效，跳过：{task.get('title')}")
            return
        is_tv = str(task.get("type")) == MediaType.TV.value
        servers = self._get_server_instances()
        if not servers:
            raise RuntimeError("无已连接的媒体服务器")
        for stype, srv_name, inst in servers:
            if is_tv:
                series_id = self._series_id_of(inst, tmdb,
                                               name=str(task.get("title") or ""),
                                               year=str(task.get("year") or ""))
                if not series_id:
                    logger.info(f"【未看洗版】[{srv_name}] 未找到剧集条目（可能尚未入库），跳过删除：{task.get('title')}")
                    continue
                season = task.get("season")
                episode = task.get("start_episode")
                if episode is not None and season is not None:
                    versions = self._episode_versions(inst, series_id, int(season), int(episode))
                    if versions is None:
                        raise RuntimeError(f"[{srv_name}] 查询版本条目失败")
                    self._prune_versions(inst, srv_name, str(task.get("title")),
                                         f"S{season}E{episode}", versions)
                elif self._tv_full_sweep:
                    # 整部洗版记录（缺集信息）：逐集核对整季/全剧
                    ep_versions = self._season_episode_versions(
                        inst, series_id, int(season) if season is not None else None)
                    if ep_versions is None:
                        raise RuntimeError(f"[{srv_name}] 查询整季版本条目失败")
                    for s in sorted(ep_versions):
                        for ep in sorted(ep_versions[s]):
                            self._prune_versions(inst, srv_name, str(task.get("title")),
                                                 f"S{s}E{ep}", ep_versions[s][ep],
                                                 quiet_single=True)
                else:
                    logger.info(f"【未看洗版】[{srv_name}] 删除任务缺季/集信息且未开启逐集核对，跳过：{task.get('title')}")
            else:
                versions = self._movie_versions(inst, tmdb)
                if versions is None:
                    raise RuntimeError(f"[{srv_name}] 查询版本条目失败")
                self._prune_versions(inst, srv_name, str(task.get("title")), "电影", versions)

    def _season_episode_versions(self, inst, series_id: str,
                                 season: Optional[int] = None
                                 ) -> Optional[Dict[int, Dict[int, List[dict]]]]:
        """
        一次查询某剧（整剧或指定季）每一集的**全部**版本条目（v1.42 新增）。
        返回 {季号: {集号: [版本条目, ...]}}；失败返回 None（未知，不删）。
        season=None 时查全剧所有季。与 _episodes_of_series 一致：单次请求不翻页
        （Emby/Jellyfin /Shows/{id}/Episodes 不带 Limit 时全量返回）。
        按季+集两级分组：整剧查询时不同季的集号会重复，绝不能合并成一个桶。
        """
        info = self._server_http_base(inst)
        if not info:
            return None
        base, apikey = info
        import requests as _rq
        params = {
            "api_key": apikey,
            "Fields": "Path,MediaSources,DateCreated",
        }
        if season is not None:
            params["Season"] = int(season)
        try:
            r = _rq.get(f"{base}/Shows/{series_id}/Episodes", params=params, timeout=20)
            if r.status_code != 200:
                # Jellyfin 无 /emby 前缀：回退根路径重试
                r = _rq.get(f"{base[:-5]}/Shows/{series_id}/Episodes", params=params, timeout=20)
            if r.status_code != 200:
                logger.warning(f"【未看洗版】查询集条目失败（HTTP {r.status_code}）：series_id={series_id}")
                return None
            out: Dict[int, Dict[int, List[dict]]] = {}
            for it in ((r.json() or {}).get("Items") or []):
                if not isinstance(it, dict):
                    continue
                s = it.get("ParentIndexNumber")
                e = it.get("IndexNumber")
                if s is None or e is None:
                    continue
                try:
                    out.setdefault(int(s), {}).setdefault(int(e), []).append(it)
                except (TypeError, ValueError):
                    continue
            return out
        except Exception as e:
            logger.warning(f"【未看洗版】查询集条目异常（series_id={series_id}）：{e}")
            return None

    def _prune_versions(self, inst, srv_name: str, title: str, label: str,
                        versions: List[dict], quiet_single: bool = False) -> int:
        """
        对同一内容的多个版本条目执行「保留画质最高者，删除其余」（v1.42 从
        _do_delete_old_versions 提取共享，供事件兜底链与每轮即时核对复用）。
        返回实际删除（或 Dry-run 标记）的条数。
        安全规则（v1.41 沿革）：
        · 仅剩 1 个版本（旧版可能已被覆盖/替换）→ 不动作（quiet_single=True 时静默）
        · 画质参数不全（宽高字节全 0）的条目永不删除
        · 只删画质**严格低于**保留者；并列/更高一律跳过
        · Dry-run 模式只打印将删清单，不真删
        """
        if len(versions) <= 1:
            if not quiet_single:
                logger.info(f"【未看洗版】[{srv_name}] {title} {label} 仅 {len(versions)} 个版本"
                            f"（旧版可能已被覆盖替换），无需删除")
            return 0
        versions.sort(key=self._quality_key, reverse=True)
        keep = versions[0]
        keep_key = self._quality_key(keep)
        logger.info(f"【未看洗版】[{srv_name}] {title} {label} 共 {len(versions)} 个版本，"
                    f"保留最高画质：{keep.get('Name')}（{keep_key[0]}x{keep_key[1]}，"
                    f"{keep_key[2] / 1024 / 1024:.0f}MB）{keep.get('Path') or ''}")
        removed = 0
        for old in versions[1:]:
            old_key = self._quality_key(old)
            if old_key[0] <= 0 or old_key[1] <= 0:
                # 宽高任一缺失即视为画质信息不全（v1.42 收紧：v1.41 只拦 (0,0,0)，
                # 「有字节大小但无视频流宽高」的条目会被误判成低画质删掉；
                # 宁漏勿误删——size 大不代表画质高）
                logger.warning(f"【未看洗版】[{srv_name}] 跳过删除（画质信息不全，宁漏勿误删）："
                               f"{old.get('Name')} / {old.get('Path')}")
                continue
            if old_key >= keep_key:
                logger.warning(f"【未看洗版】[{srv_name}] 跳过删除（画质不低于保留版本，疑似未识别的新版）："
                               f"{old.get('Name')}（{old_key[0]}x{old_key[1]}）")
                continue
            if self._dry_run:
                logger.info(f"【未看洗版】[{srv_name}] [Dry-run] 将删除旧版本："
                            f"{old.get('Name')}（{old_key[0]}x{old_key[1]}，"
                            f"{old_key[2] / 1024 / 1024:.0f}MB）{old.get('Path') or ''}")
                removed += 1
                continue
            if self._delete_server_item(inst, old):
                logger.info(f"【未看洗版】[{srv_name}] 已删除被洗版旧条目："
                            f"{old.get('Name')}（{old_key[0]}x{old_key[1]}）{old.get('Path') or ''}")
                removed += 1
            else:
                logger.error(f"【未看洗版】[{srv_name}] 删除旧条目失败：{old.get('Name')}")
        return removed

    def _list_best_version_subscriptions(self) -> List[Any]:
        """
        列出所有开启逐集洗版（best_version=1）的订阅（v1.42，不再限插件自建）。

        走 SubscribeChain 的 subscription_repository（v3 组合根注入的只读仓储），
        而不是旧版 SubscribeOper：后者在本容器里读会抛
        `RuntimeError: 同步事务执行器尚未配置`（插件进程没有启动期组合根）。
        """
        chain = self.subscribechain
        repo = getattr(chain, "subscription_repository", None)
        if repo is None:
            logger.warning("【未看洗版】订阅仓储不可用（v3 组合根未注入），无法读取洗版订阅")
            return []
        try:
            all_subs = repo.list() or []
        except Exception as e:
            logger.error(f"【未看洗版】列出订阅失败：{e}")
            return []
        return [s for s in all_subs if getattr(s, "best_version", 0)]

    def _reconcile_one_subscription(self, sub: Any, inst: Any, user_id: str,
                                    results: dict) -> None:
        """
        核对单个逐集洗版订阅（v1.48 重写版）。

        **插件只做一件事：把 Emby 的观看事实翻译成订阅字段，剩下的全交给 MP。**
        搜索、下载、画质比较、旧版去重、整理入库一律走官方路径。

        写回三样东西：
          · total_episode     —— 该季已入库的最大集号（只增不减）。
                                 必须 >= start_episode，否则 MP 会把 range(start, total+1)
                                 算成空集，判定「已全部下载」并自动取消订阅。
          · episode_priority  —— 已观看集写 100（官方语义：该集洗版已达标）。
                                 MP 的两条待洗判定（!=100 与 >0）都会跳过它。
          · start_episode     —— 推进到第一个未观看集（只进不退）。

        整季看完时不主动删订阅：所有集 priority=100 后 MP 自然判定完成并收尾。
        """
        sid = getattr(sub, "id", None)
        name = getattr(sub, "name", "") or str(sid)
        year = getattr(sub, "year", "") or ""
        media_id = getattr(sub, "media_id", None)
        try:
            tmdb_id = int(str(media_id)) if media_id else None
        except (TypeError, ValueError):
            tmdb_id = None
        if not tmdb_id:
            return
        season = getattr(sub, "season", None)
        series_id = self._series_id_of(inst, tmdb_id, name=name, year=year)
        if not series_id:
            # 媒体库找不到该剧集：可能是未入库 / 已出库 / 标题对不上。
            # 开启「资源出库回收」时，精确核对是否真的已从库里移除，是则取消本插件存量订阅。
            if self._cancel_removed:
                self._try_cancel_removed(sub, inst, tmdb_id, "Series", name, results)
            else:
                logger.info(f"【未看洗版】{name}：在 {getattr(inst, 'host', '媒体服务器')} 上找不到该剧集，"
                            f"本轮不动它的订阅（可能未入库/已被删除）")
            return
        # 观看联动未开启时，找到剧集即视为仍在库，直接跳过
        if not self._cancel_watched:
            return
        eps_map = self._episodes_of_series(inst, series_id, user_id)
        if eps_map is None:
            logger.info(f"【未看洗版】{name}：集观看状态未知，本轮不动它的订阅")
            return
        s_map = eps_map.get(int(season)) if season is not None else None
        if not s_map:
            if season is not None:
                logger.info(f"【未看洗版】{name}：媒体服务器上没有第{season}季的集，本轮不动它的订阅")
                return
            # 整剧订阅（season=None）：把所有季合并判断
            combined: Dict[int, bool] = {}
            for _s, m in eps_map.items():
                combined.update(m)
            s_map = combined
        if not s_map:
            return

        label = f"第{season}季" if season is not None else "整剧"
        all_eps = sorted(e for e in s_map if e and e > 0)
        if not all_eps:
            return
        unplayed = [e for e in all_eps if not s_map[e]]

        updates: Dict[str, Any] = {}

        # ---- 1) total_episode：真实已入库最大集号，只增不减 ----
        # 只增不减是为了避免 Emby 暂时缺集（清理中/刮削中）时把范围缩小，
        # 导致 range(start, total+1) 变空、订阅被误判完成。
        real_total = all_eps[-1]
        try:
            old_total = int(sub.total_episode) if sub.total_episode else 0
        except (TypeError, ValueError):
            old_total = 0
        if real_total > old_total:
            updates["total_episode"] = real_total
        # 防止 MP 用「季内集数（len）」覆盖我们用绝对集号维护的 total
        if not getattr(sub, "manual_total_episode", 0):
            updates["manual_total_episode"] = 1

        # ---- 2) episode_priority：已观看集写 100（不洗）----
        old_pri = getattr(sub, "episode_priority", None) or {}
        if not isinstance(old_pri, dict):
            old_pri = {}
        new_pri: Dict[str, int] = {}
        for e in all_eps:
            key = str(e)
            if s_map[e]:
                new_pri[key] = 100
            elif key in old_pri:
                # 未观看：保留 MP 自己维护的优先级，不擅自改动
                new_pri[key] = old_pri[key]
        if new_pri != old_pri:
            updates["episode_priority"] = new_pri
        watched_count = len(all_eps) - len(unplayed)

        # ---- 3) start_episode：推进到第一个未观看集，只进不退 ----
        try:
            old_start = int(sub.start_episode) if sub.start_episode else 1
        except (TypeError, ValueError):
            old_start = 1
        if unplayed:
            new_start = unplayed[0]
            if new_start > old_start:
                updates["start_episode"] = new_start

        if not updates:
            logger.debug(f"【未看洗版】{name} {label}：订阅字段已是期望值，无需更新"
                         f"（已看 {watched_count}/{len(all_eps)} 集）")
            return

        # Dry-run：只报告不写库
        if self._dry_run:
            parts = []
            if "start_episode" in updates:
                parts.append(f"开始集数 {old_start} → {updates['start_episode']}")
            if "total_episode" in updates:
                parts.append(f"总集数 {old_total} → {updates['total_episode']}")
            if "episode_priority" in updates:
                parts.append(f"已看 {watched_count} 集标记为不再洗版")
            logger.info(f"【未看洗版】[Dry-run] 将更新 {name} {label}：{'；'.join(parts)}")
            results.setdefault("details", []).append(
                f"[Dry-run] 「{name}」{label}：{'；'.join(parts)}")
            return

        if self._update_subscription_fields(sid, updates):
            results["updated"] = results.get("updated", 0) + 1
            parts = []
            if "start_episode" in updates:
                parts.append(f"开始集数 {old_start} → {updates['start_episode']}")
            if "total_episode" in updates:
                parts.append(f"总集数 {old_total} → {updates['total_episode']}")
            if "episode_priority" in updates:
                parts.append(f"{watched_count}/{len(all_eps)} 集已看")
            detail = f"「{name}」{label}：{'；'.join(parts)}"
            results.setdefault("details", []).append(detail)
            logger.info(f"【未看洗版】已同步订阅：{detail}")
            if not unplayed:
                logger.info(f"【未看洗版】{name} {label}：本季已入库的 {len(all_eps)} 集全部观看完毕，"
                            f"已全部标记为不再洗版，交由 MP 判定订阅完成")
        else:
            results["failed"] = results.get("failed", 0) + 1

    def _reconcile_one_movie_subscription(self, sub: Any, inst: Any, user_id: str,
                                          results: dict) -> None:
        """
        核对单个电影洗版订阅（v1.42 补回的需求，见 L612-615）：
        电影已观看 -> 取消它的洗版订阅。

        与剧集的权限边界一致：
          · username == 未看洗版 的存量自建订阅 -> 自动取消
          · 用户自建订阅 -> 只提示，绝不代删
        Dry-run 模式只打印将做的动作。
        观看状态未知（查不到/异常）一律跳过，绝不误取消——这是安全底线。
        """
        sid = getattr(sub, "id", None)
        name = getattr(sub, "name", "") or str(sid)
        media_id = getattr(sub, "media_id", None)
        username = str(getattr(sub, "username", "") or "")
        try:
            tmdb_id = int(str(media_id)) if media_id else None
        except (TypeError, ValueError):
            tmdb_id = None
        if not tmdb_id:
            return
        played = self._movie_played(inst, tmdb_id, user_id)
        if played is None:
            # 观看状态未知：可能是库里已无此电影（出库），也可能是媒体服务器异常。
            # 开启「资源出库回收」时精确核对，确认已出库则取消本插件存量订阅。
            if self._cancel_removed:
                self._try_cancel_removed(sub, inst, tmdb_id, "Movie", name, results)
            else:
                logger.info(f"【未看洗版】{name}：在媒体服务器上观看状态未知，本轮不动它的订阅")
            return
        if not self._cancel_watched:
            return
        # 电影洗版订阅（WASH_USERNAME_MOVIE）：洗到目标画质前不因「已观看」取消，
        # 只在电影出库时由「资源出库回收」取消。
        if username == WASH_USERNAME_MOVIE:
            return
        if not played:
            # 还没看，继续洗版，不做任何事
            return
        # 已观看 → 取消（v1.44 起不限账户；MP 电影订阅洗满会自取消，此处补残留 + 放开代删）
        if self._dry_run:
            logger.info(f"【未看洗版】[Dry-run] 将取消电影洗版订阅：{name}（已观看）")
            results.setdefault("details", []).append(
                f"[Dry-run] 「{name}」已观看，将取消电影洗版订阅")
            return
        if self._delete_subscription(sid, f"电影已观看：{name}"):
            results["deleted"] = results.get("deleted", 0) + 1
            results.setdefault("details", []).append(
                f"已取消「{name}」电影洗版订阅（已观看，不限账户）")
        else:
            results["failed"] = results.get("failed", 0) + 1

    @staticmethod
    def _media_item_missing(inst, tmdb_id: int, item_type: str) -> Optional[bool]:
        """
        确认某 tmdb 对应的条目是否**已从媒体库移除**（出库/被删）。

        三态返回：
          True  = 服务器连通且按 tmdbid 精确查询成功，但确实无此条目（确认出库）
          False = 查到了条目（仍在库）
          None  = 服务器不可达/查询异常（未知；调用方应跳过、绝不误取消）

        仅按 tmdbid 精确匹配，不退化标题搜索——避免同名不同 tmdb 的误判；
        与 _series_id_of 的 tmdbid 分支一致，但本方法不触发标题退化搜索，
        从而把「确定缺失」与「查询异常」区分开（这是出库回收能安全取消的前提）。
        """
        info = EmbyUnwatchedWash._server_http_base(inst)
        if not info:
            return None
        base, apikey = info
        import requests as _rq
        params = {
            "api_key": apikey,
            "Recursive": "true",
            "IncludeItemTypes": item_type,
            "AnyProviderIdEquals": f"Tmdb.{tmdb_id}",
        }
        try:
            r = _rq.get(f"{base}/Items", params=params, timeout=15)
            if r.status_code != 200:
                r = _rq.get(f"{base[:-5]}/Items", params=params, timeout=15)
            if r.status_code != 200:
                return None
            items = (r.json() or {}).get("Items") or []
            return len(items) == 0
        except Exception:
            return None

    def _try_cancel_removed(self, sub: Any, inst: Any, tmdb_id: int,
                            item_type: str, name: str, results: dict) -> None:
        """
        出库回收（v1.45 新增，默认关闭）：媒体库里已经没有的资源，取消其「未看洗版」
        存量洗版订阅，避免 MP 继续为空库资源搜刮下载。

        安全边界（与整季看完取消一致）：
          · 仅对 username==未看洗版 的存量自建订阅代删；
          · 用户自建订阅（best_version=1）只提示、绝不代删；
          · 媒体服务器状态未知/异常（_media_item_missing 返回 None）一律不动，绝不误删；
          · Dry-run 模式只打印。
        """
        sid = getattr(sub, "id", None)
        username = str(getattr(sub, "username", "") or "")
        label = "剧集" if item_type == "Series" else "电影"
        missing = self._media_item_missing(inst, tmdb_id, item_type)
        if missing is None:
            logger.info(f"【未看洗版】{name}：媒体服务器状态未知（可能未连接/抖动），"
                        f"本轮不取消它的订阅")
            return
        if not missing:
            # 查到了条目：理论上不会走到这（能查到 series_id 就不会进此分支），保险处理
            return
        # 确认已出库：区分归属
        if username not in (WASH_USERNAME, WASH_USERNAME_MOVIE):
            logger.warning(f"【未看洗版】{name}：媒体库已无此{label}（可能已出库），"
                           f"但属用户自建订阅，未代删（请到 MP 订阅页手动取消）")
            return
        reason = f"{label}已从媒体库移除（资源出库）：{name}"
        if self._dry_run:
            logger.info(f"【未看洗版】[Dry-run] 将取消洗版订阅：{name}（{reason}）")
            results.setdefault("details", []).append(f"[Dry-run] 「{name}」{reason}，将取消订阅")
            return
        if self._delete_subscription(sid, reason):
            results["deleted"] = results.get("deleted", 0) + 1
            results.setdefault("details", []).append(f"已取消「{name}」洗版订阅（{reason}）")
        else:
            results["failed"] = results.get("failed", 0) + 1

    def _delete_subscription(self, sid: Any, reason: str) -> bool:
        """
        取消一个洗版订阅。

        走 `chain.sync_subscription_delete_scope()`（v3 官方删除入口，负责
        单测事务、发布订阅删除事件并同步媒体服务器）。旧版 SubscribeOper.delete
        在本容器里会抛「同步事务执行器尚未配置」，不可用。
        """
        if sid is None:
            return False
        scope = getattr(self.subscribechain, "sync_subscription_delete_scope", None)
        if not callable(scope):
            logger.error("【未看洗版】订阅删除入口不可用（v3 组合根未注入），无法取消订阅")
            return False
        try:
            from app.application.subscription.delete import SubscribeDeletionActor
            actor = SubscribeDeletionActor(username=WASH_USERNAME, is_superuser=True)
        except Exception:
            actor = None
        try:
            with scope() as command:
                if actor is not None:
                    ok = command.execute(int(sid), actor)
                else:
                    ok = command.execute(int(sid))
            if ok:
                logger.info(f"【未看洗版】已取消洗版订阅 (ID={sid})：{reason}")
            else:
                logger.warning(f"【未看洗版】取消洗版订阅失败 (ID={sid})：{reason}")
            return bool(ok)
        except Exception as e:
            logger.error(f"【未看洗版】取消洗版订阅异常 (ID={sid})：{e}\n{traceback.format_exc()}")
            return False

    def _update_subscription_start(self, sid: Any, start_episode: int) -> bool:
        """
        把订阅的「开始集数」推进到指定集。

        走 `chain.sync_subscription_mutation_scope()` —— 与官方订阅助手插件
        BestVersionConverter 完全相同的写法（同一把锁 + 同一套事件发布），
        确保订阅变更会正常发出 SubscribeModified 事件、被 UI 和搜索队列看到。
        """
        return self._update_subscription_fields(sid, {"start_episode": int(start_episode)})

    def _update_subscription_fields(self, sid: Any, values: Dict[str, Any]) -> bool:
        """
        统一写回订阅字段（v1.47）。

        插件不再自己搜索/下载，只负责把「观看事实」翻译成 MP 订阅字段：
          · start_episode     —— 从第一个未观看集开始洗
          · total_episode     —— 真实已入库最大集号（只增不减）
          · episode_priority  —— 已观看集写 100，表示「这集不用洗」

        走 `sync_subscription_mutation_scope()`（与官方订阅助手插件同款入口），
        保证发出 SubscribeModified 事件、UI 与搜索队列都能看到变更。
        """
        if sid is None or not values:
            return False
        scope = getattr(self.subscribechain, "sync_subscription_mutation_scope", None)
        if not callable(scope):
            logger.error("【未看洗版】订阅变更入口不可用（v3 组合根未注入），无法更新订阅")
            return False
        try:
            from app.application.subscription.mutation import SubscriptionActor
            actor = SubscriptionActor(name=WASH_USERNAME, is_superuser=True)
        except Exception:
            actor = None
        payload = {k: v for k, v in values.items() if v is not None}
        if not payload:
            return False
        try:
            with scope() as mutation:
                if actor is not None:
                    change = mutation.update(int(sid), payload, actor, scene="unwatched_wash")
                else:
                    change = mutation.update(int(sid), payload)
            if change:
                return True
            logger.warning(f"【未看洗版】更新订阅失败 (ID={sid}, fields={sorted(payload)})："
                           f"宿主未返回更新结果")
            return False
        except Exception as e:
            logger.error(f"【未看洗版】更新订阅异常 (ID={sid})：{e}\n{traceback.format_exc()}")
            return False

    def _sweep_old_versions(self, results: Optional[dict] = None) -> None:
        """
        旧版清理主流程（v1.42 即时化）：每轮对所有 best_version=1 订阅覆盖的内容
        核对媒体库多版本，发现「同内容多版本且存在严格更高画质者」即删除低画质旧条目。

        与 SubscribeComplete 事件兜底链（_process_delete_tasks）的关系：
        事件链只在订阅完成时触发一次；追更期的逐集洗版订阅可能长期不「完成」，
        旧版软料会一直堆积——本流程补上这个缺口，事件链保留为兜底。
        幂等：删完只剩单版本，下一轮自动跳过，不会重复删。
        """
        if results is None:
            results = {"deleted": 0, "checked": 0, "failed": 0}
        if not self._delete_washed_old:
            return
        subs = self._list_best_version_subscriptions()
        if not subs:
            logger.info("【未看洗版】当前没有开启洗版（best_version=1）的订阅，旧版清理无事可做")
            return
        servers = self._get_server_instances()
        if not servers:
            logger.warning("【未看洗版】没有已连接的媒体服务器，本轮跳过旧版清理")
            return
        logger.info(f"【未看洗版】旧版清理：核对 {len(subs)} 个洗版订阅覆盖的内容"
                    f"（{len(servers)} 台服务器，逐集核对={'开' if self._tv_full_sweep else '关'}）")
        for sub in subs:
            sid = getattr(sub, "id", None)
            name = getattr(sub, "name", "") or str(sid)
            year = getattr(sub, "year", "") or ""
            media_id = getattr(sub, "media_id", None)
            try:
                tmdb_id = int(str(media_id)) if media_id else None
            except (TypeError, ValueError):
                tmdb_id = None
            if not tmdb_id:
                continue
            for stype, srv_name, inst in servers:
                try:
                    if self._sub_is_tv(sub):
                        season = getattr(sub, "season", None)
                        series_id = self._series_id_of(inst, tmdb_id, name=name, year=year)
                        if not series_id:
                            logger.info(f"【未看洗版】[{srv_name}] {name}：未入库，跳过旧版核对")
                            continue
                        if not self._tv_full_sweep:
                            # 未开逐集核对：仅核对订阅开始集那一集（保守模式）
                            if season is None:
                                continue
                            ep = getattr(sub, "start_episode", None) or 1
                            versions = self._episode_versions(inst, series_id, int(season), int(ep))
                            if versions is None:
                                results["failed"] = results.get("failed", 0) + 1
                                continue
                            results["checked"] = results.get("checked", 0) + 1
                            results["deleted"] = results.get("deleted", 0) + \
                                self._prune_versions(inst, srv_name, name, f"S{season}E{ep}", versions)
                        else:
                            ep_versions = self._season_episode_versions(
                                inst, series_id, int(season) if season is not None else None)
                            if ep_versions is None:
                                results["failed"] = results.get("failed", 0) + 1
                                continue
                            results["checked"] = results.get("checked", 0) + \
                                sum(len(v) for m in ep_versions.values() for v in m.values())
                            for s in sorted(ep_versions):
                                for ep in sorted(ep_versions[s]):
                                    results["deleted"] = results.get("deleted", 0) + \
                                        self._prune_versions(inst, srv_name, name,
                                                             f"S{s}E{ep}", ep_versions[s][ep],
                                                             quiet_single=True)
                    else:
                        versions = self._movie_versions(inst, tmdb_id)
                        if versions is None:
                            results["failed"] = results.get("failed", 0) + 1
                            continue
                        results["checked"] = results.get("checked", 0) + 1
                        results["deleted"] = results.get("deleted", 0) + \
                            self._prune_versions(inst, srv_name, name, "电影", versions)
                except Exception as e:
                    results["failed"] = results.get("failed", 0) + 1
                    logger.error(f"【未看洗版】[{srv_name}] {name} 旧版核对失败：{e}\n{traceback.format_exc()}")
