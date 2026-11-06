"""
憨憨（HHanClub）登录保活提醒插件

憨憨是 NexusPHP 架构站点，没有馒头那种「只读」的 ApiKey 资料接口，只能用站点
Cookie 读取页面。取数分两级（均为实测确定）：

1. 主口径 /userloginlogs.php?uid=…「用户访问记录」表里的「创建时间」——
   这是真实登录事件，实测本插件的读取请求不会新增记录，因此读数不会被自己刷新；
2. 兜底口径 用户页「最近动向」——实测每次访问都会刷新成当前时刻，
   仅在没有登录记录时使用，并在页面/通知里明确标注。

运行策略为「临期才核实」：30 天是从最后一次登录起算的，只要用户不登录，
本地按这个时间倒推就不会算错，所以平时完全不访问站点；只有剩余天数进入
「核实阈值」、本地没有可信基准，或用户手动点「立即检查」时才真正请求站点。
这样既避免无谓的自动化访问（站方明确不支持自动化工具签到），也保证临期数据是准的。

插件只做页面读取，不调用签到接口、不模拟登录。

保活期限取自憨憨 wiki《账号规则 / 账号封禁规则》：
  1. 未封存账号：连续 30 天不登录 → 封禁；
  2. 已封存账号：连续 400 天不登录 → 封禁；
  3. Ultimate User 及以上永久保留；Veteran User 及以上封存后不会被删除。
站点同时注明「本站不支持自动化工具签到，请确保正常登录并手动访问站点」，
因此本插件只负责提醒，不尝试自动保活。
"""

import html as html_lib
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType
from app.utils.http import RequestUtils


class HHClubKeepAlive(_PluginBase):
    """憨憨登录保活提醒插件：读取「用户访问记录」的最后登录时间，按站规期限推送剩余天数提醒。"""

    # 插件名称
    plugin_name = "憨憨登录保活提醒"
    # 插件描述
    plugin_desc = "读取憨憨「用户访问记录」里的最后登录时间，按 30 天（封存 400 天）保活线推送剩余天数提醒，不签到、不模拟登录。"
    # 插件图标
    plugin_icon = "https://hhanclub.net/favicon.ico"
    # 插件版本
    plugin_version = "1.2.0"
    # 插件标签
    plugin_label = "站点"
    # 插件作者
    plugin_author = "jiuyaozhuce"
    # 作者主页
    author_url = "https://github.com/jiuyaozhuce"
    # 插件配置项ID前缀
    plugin_config_prefix = "hhclubkeepalive_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    # 配置属性
    _enabled: bool = False
    _cron: str = "1 0 * * *"
    _onlyonce: bool = False
    _limit_days: int = 30
    _warn_days: int = 7
    _verify_days: int = 7
    _daily_report: bool = True

    # 最近一次检查结果（插件侧视图，仅普通字典）
    _last_result: Dict[str, Any] = {}

    # 站点标识：域名/名称命中任一即认为是憨憨（老域名 hhanclub.top 已停用，现为 hhanclub.net）
    _domain_keywords = ("hhanclub", "hhclub")
    _name_keywords = ("憨憨", "hhanclub")

    # 页面里可能出现的「最后活跃时间」字段名，按优先级排列
    _action_labels = (
        "最近动向", "最近動向",
        "上次访问", "上次訪問",
        "最后访问", "最後訪問",
        "上次登录", "上次登錄",
        "Last Action", "Last access",
    )

    # 默认 UA：站点在 Cloudflare 后面，空 UA 或 python-requests 默认 UA 容易被拦
    _default_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # ---------- 详情页样式：只用静态类与内联样式，颜色一律交给组件属性 ----------
    # 主状态卡：去掉默认图标，让大号天数在卡里真正居中
    _hero_style = "padding:22px 16px;text-align:center"
    _hero_row_style = (
        "display:flex;align-items:baseline;justify-content:center;"
        "gap:8px;flex-wrap:wrap"
    )
    _hero_num_style = "font-size:clamp(2.4rem,9vw,3.4rem);font-weight:700;line-height:1"
    _hero_word_style = "font-size:1.05rem;font-weight:500;opacity:.85"
    _notice_title_style = "font-size:1.35rem;font-weight:600;line-height:1.3"
    # 副标题样式：另起一行并退回常规字号（否则会继承大号字体）
    _sub_text_style = (
        "font-size:.95rem;font-weight:400;line-height:1.5;"
        "margin-top:10px;text-align:center"
    )
    # 脚注、分区标题与网格标签/取值样式
    _foot_style = (
        "font-size:.8rem;line-height:1.6;margin-top:14px;"
        "opacity:.85;text-align:center"
    )
    _card_title_style = "font-size:1.02rem;font-weight:600;line-height:1.4"
    _label_style = "font-size:.72rem;line-height:1.4;letter-spacing:.03em"
    _value_style = "font-size:.95rem;line-height:1.45;margin-top:3px"
    _hint_style = "font-size:.75rem;line-height:1.4;margin-top:2px"
    # 概况网格：靠 flex 自适应列数，窄屏不会散成一列一个大格
    _grid_style = "display:flex;flex-wrap:wrap;gap:14px 18px"
    _cell_style = "flex:1 1 calc(33.333% - 12px);min-width:160px"
    # 列表标题：覆盖 Vuetify 的单行截断，长文本按容器宽度自动换行
    _list_title_style = (
        "display:flex;align-items:center;gap:6px;flex-wrap:wrap;"
        "overflow:visible;text-overflow:clip;-webkit-line-clamp:unset"
    )
    _wrap_style = (
        "display:block;white-space:normal;overflow:visible;"
        "text-overflow:clip;-webkit-line-clamp:unset"
    )
    # 详情页需要醒目标记的站点关键字与样式
    _keyword = "憨憨"
    _keyword_style = (
        "font-weight:700;text-decoration:underline;"
        "text-underline-offset:3px;color:inherit"
    )
    # 副标题文案：正常 / 临期 / 已超期
    _subtitle_normal = "站点活跃度正常，无需操作"
    _subtitle_warn = "活跃度已临期，请尽快手动登录憨憨站点保活"
    _subtitle_over = "已超期未登录，账号可能已被封禁，请立即到憨憨站点确认"
    # 详情页最多展示的检查次数
    _page_history = 10
    # 数据来源标签：本地推算 / 站点登录记录 / 用户页最近动向
    _source_meta = {
        "local": {"label": "本地推算", "color": "grey"},
        "login_log": {"label": "站点登录记录", "color": "primary"},
        "recent_action": {"label": "用户页最近动向", "color": "warning"},
    }

    _dt_re = re.compile(
        r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})日?[\sT]+(\d{1,2}):(\d{2})(?::(\d{2}))?"
    )
    _uid_re = re.compile(r"userdetails\.php\?id=(\d+)", re.I)

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._cron = str(config.get("cron") or "1 0 * * *")
            self._onlyonce = bool(config.get("onlyonce", False))
            self._limit_days = int(config.get("limit_days") or 30)
            self._warn_days = int(config.get("warn_days") or 7)
            self._verify_days = int(config.get("verify_days")
                                    if config.get("verify_days") is not None else 7)
            self._daily_report = bool(config.get("daily_report", True))
            self._last_result = dict(config.get("last_result") or {})

        # 立即运行一次
        if self._enabled and self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("憨憨登录保活提醒：立即运行一次")
            self._scheduler.add_job(
                func=self.run_check,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="憨憨登录保活检查",
            )
            self._onlyonce = False
            self._save_config()
            if self._scheduler.get_jobs():
                self._scheduler.start()

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    def _save_config(self) -> None:
        """保存插件配置（含最近一次检查结果）。"""
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "limit_days": self._limit_days,
            "warn_days": self._warn_days,
            "verify_days": self._verify_days,
            "daily_report": self._daily_report,
            "last_result": self._last_result,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return [
            {
                "path": "/check_now",
                "endpoint": self.api_check_now,
                "methods": ["GET"],
                "summary": "立即检查一次",
                "description": "在后台排入一次保活检查（约 3 秒后执行），接口本身立即返回。",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。"""
        if self._enabled and str(self._cron).strip().count(" ") == 4:
            try:
                return [{
                    "id": "HHClubKeepAlive",
                    "name": "憨憨登录保活提醒服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.run_check,
                    "kwargs": {},
                }]
            except Exception as err:
                logger.error(f"憨憨保活提醒定时任务配置错误：{str(err)}")
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "props": {"class": "ma-0"},
                "content": [
                    {
                        "component": "VRow",
                        "props": {"class": "ma-0"},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "class": "pa-2"},
                                "content": [
                                    {"component": "VDivider", "props": {"class": "mb-3"}},
                                    {"component": "h3", "props": {"class": "text-subtitle-1"},
                                     "text": "基础设置"},
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3, "class": "pa-2"},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件",
                                              "hide-details": True},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3, "class": "pa-2"},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "daily_report", "label": "每日汇报",
                                              "hide-details": True,
                                              "hint": "关闭后仅在临期/超期/检查失败时通知",
                                              "persistent-hint": True},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3, "class": "pa-2"},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次",
                                              "hide-details": True},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3, "class": "pa-2"},
                                "content": [{
                                    "component": "VCronField",
                                    "props": {
                                        "model": "cron",
                                        "label": "执行周期",
                                        "placeholder": "5 位 cron，默认每天 00:01",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "class": "pa-2"},
                                "content": [
                                    {"component": "VDivider", "props": {"class": "mb-3"}},
                                    {"component": "h3", "props": {"class": "text-subtitle-1"},
                                     "text": "保活期限"},
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6, "class": "pa-2"},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "limit_days",
                                        "type": "number",
                                        "label": "保活期限（天）",
                                        "hint": "憨憨站规：未封存账号 30 天不登录封禁；已封存账号为 400 天",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6, "class": "pa-2"},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "warn_days",
                                        "type": "number",
                                        "label": "临期提醒（天）",
                                        "hint": "剩余天数不大于该值时必发通知，建议 7",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6, "class": "pa-2"},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "verify_days",
                                        "type": "number",
                                        "label": "核实阈值（天）",
                                        "hint": "剩余天数大于该值时只做本地推算、不访问站点；"
                                                "进入阈值内才去站点读一次真实登录记录。0 = 每次都核实",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "class": "pa-2"},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "success",
                                        "variant": "tonal",
                                        "class": "text-subtitle-2",
                                        "text": "默认「临期才核实」：憨憨的 30 天从最后一次登录起算，只要你不登录，"
                                                "本地按这个时间倒推不会算错，所以平时不必访问站点——"
                                                "只有剩余天数进入核实阈值内，才去读一次登录记录（顺带确认 Cookie 是否还有效）。",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "class": "pa-2"},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "class": "text-subtitle-2",
                                        "text": "核实时会请求站点首页与「用户访问记录」，只读取、不调用签到接口、不模拟登录。"
                                                "憨憨官方明确「不支持自动化工具签到」，保活仍需手动登录一次站点。",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "class": "pa-2"},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "warning",
                                        "variant": "tonal",
                                        "class": "text-subtitle-2",
                                        "text": "前提：站点管理里已添加憨憨（域名含 hhanclub）并填好 Cookie；"
                                                "Cookie 失效时插件会推送检查失败提醒。",
                                    },
                                }],
                            },
                        ],
                    }
                ],
            }
        ], {
            "enabled": False,
            "daily_report": True,
            "onlyonce": False,
            "cron": "1 0 * * *",
            "limit_days": 30,
            "warn_days": 7,
            "verify_days": 7,
        }

    # ------------------------------------------------------------------ 解析工具

    @staticmethod
    def _clean_cell(cell: str) -> str:
        """清洗单元格：去标签、实体解码、压缩空白。"""
        cell = re.sub(r"(?is)<br\s*/?>", " ", cell)
        cell = re.sub(r"(?is)<[^>]+>", "", cell)
        cell = html_lib.unescape(cell)
        return re.sub(r"\s+", " ", cell).strip()

    def _parse_datetime(self, text: str, now: datetime) -> Optional[datetime]:
        """解析时间文本，兼容『今日 HH:MM』、『昨天 HH:MM』、『YYYY-MM-DD HH:MM』等格式。"""
        text = text.strip()
        m = self._dt_re.search(text)
        if m:
            y, mo, d, h, mi, s = (int(x) if x else 0 for x in m.groups())
            try:
                return datetime(y, mo, d, h, mi, s or 0)
            except ValueError:
                return None
        # 相对时间：今天/今日/昨天
        if re.search(r"(今天|今日)\s*(\d{1,2})[:：](\d{2})", text):
            m = re.search(r"(今天|今日)\s*(\d{1,2})[:：](\d{2})", text)
            if m:
                h, mi = int(m.group(2)), int(m.group(3))
                return datetime(now.year, now.month, now.day, h, mi)
        if re.search(r"昨天\s*(\d{1,2})[:：](\d{2})", text):
            m = re.search(r"昨天\s*(\d{1,2})[:：](\d{2})", text)
            if m:
                h, mi = int(m.group(2)), int(m.group(3))
                return datetime(now.year, now.month, now.day - 1, h, mi)
        return None

    def _extract_login_log(self, page: str, now: datetime) -> Optional[Dict[str, str]]:
        """解析「用户访问记录」表，取最近一条登录的时间/IP/地区/方式。

        憨憨该表列顺序为：序号 / 登陆ip / 登陆地区 / 登陆方式 / 创建时间。
        这里不写死列号，而是按「能解析出时间的那一格」反推左侧三格，兼容列顺序变化。
        """
        if not page:
            return None
        best: Optional[Tuple[datetime, Dict[str, str]]] = None
        for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", page):
            cells = [self._clean_cell(c)
                     for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
            if not cells:
                continue
            time_index = None
            value = None
            for index, cell in enumerate(cells):
                parsed = self._parse_datetime(cell, now)
                if parsed:
                    time_index, value = index, parsed
                    break
            if time_index is None:
                continue
            # 反推：时间列左侧依次为 方式、地区、IP
            method = cells[time_index - 3] if time_index >= 3 else ""
            region = cells[time_index - 2] if time_index >= 2 else ""
            ip = cells[time_index - 1] if time_index >= 1 else ""
            if best is None or value > best[0]:
                best = (value, {
                    "last_login": value.strftime("%Y-%m-%d %H:%M:%S"),
                    "ip": ip,
                    "region": region,
                    "method": method,
                })
        if best:
            return best[1]
        return None

    def _extract_action(self, page: str, now: datetime) -> Optional[Tuple[datetime, str, bool]]:
        """解析用户页「最近动向」或类似字段，返回 (时间, 标签, 是否自刷新)。"""
        if not page:
            return None
        for label in self._action_labels:
            pattern = re.compile(
                rf"(?is){re.escape(label)}[^<\d]*"
                r"(?:<[^>]*>)*\s*"
                r"(\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}日?[\sT]+\d{1,2}[:：]\d{2}(?::\d{2})?)"
            )
            m = pattern.search(page)
            if m:
                parsed = self._parse_datetime(m.group(1), now)
                if parsed:
                    # 自刷新判断：本次访问若在 10 分钟内，视为自刷新
                    self_refresh = (now - parsed) <= timedelta(minutes=10)
                    return parsed, label, self_refresh
            # 备选：相对时间「今天 HH:MM / 昨天 HH:MM」
            rel_pattern = re.compile(
                rf"(?is){re.escape(label)}[^<\d]*"
                r"(?:<[^>]*>)*\s*"
                r"((?:今天|今日|昨天)\s*\d{1,2}[:：]\d{2})"
            )
            m = rel_pattern.search(page)
            if m:
                parsed = self._parse_datetime(m.group(1), now)
                if parsed:
                    self_refresh = (now - parsed) <= timedelta(minutes=10)
                    return parsed, label, self_refresh
        return None

    def _extract_username(self, index_page: str, detail_page: str) -> str:
        """从首页或用户页提取用户名。"""
        # 优先从用户页详细信息中提取
        if detail_page:
            m = re.search(r"(?is)用户名[^<\d]*<[^>]*>\s*([^<\s]+)", detail_page)
            if m:
                return m.group(1)
            m = re.search(r"(?is)username[^<\d]*<[^>]*>\s*([^<\s]+)", detail_page)
            if m:
                return m.group(1)
        # 兜底从首页提取
        m = re.search(r"(?is)userdetails\.php\?id=\d+[^>]*>\s*([^<\s]+)", index_page)
        if m:
            return m.group(1)
        return ""

    def _get_site(self):
        """根据域名/名称关键字在站点管理中查找憨憨站点。"""
        from app.db.site_oper import SiteOper
        sites = SiteOper.get_sites()
        for site in sites:
            domain = (site.domain or "").lower()
            name = (site.name or "").lower()
            if any(kw in domain for kw in self._domain_keywords) \
                    or any(kw in name for kw in self._name_keywords):
                return site
        return None

    def _request(self, site, path: str):
        """带站点 Cookie 请求站点页面，失败返回 None。"""
        base = str(site.url or site.domain or "").strip()
        if not base:
            return None
        if "//" not in base:
            base = f"https://{base}"
        url = f"{base.rstrip('/')}/{str(path).lstrip('/')}"
        headers = {
            "User-Agent": site.ua or self._default_ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{base.rstrip('/')}/index.php",
        }
        try:
            res = RequestUtils(
                cookies=site.cookie,
                headers=headers,
                proxies=settings.PROXY if site.proxy else None,
                timeout=30,
            ).get_res(url)
        except Exception as err:
            import traceback
            logger.error(
                f"憨憨保活检查：请求 {path} 网络异常({type(err).__name__}): {str(err)}\n{traceback.format_exc()}"
            )
            return None
        if not res:
            logger.warning(f"憨憨保活检查：请求 {path} 超时或无响应 (IP被墙/Cookie失效?)")
            return None
        if res.status_code != 200:
            logger.warning(
                f"憨憨保活检查：请求 {path} 返回非200状态码: {res.status_code} (可能被Cloudflare拦截/需验证码)"
            )
            return None
        return res.text or ""

    @staticmethod
    def _is_logged_in(page: str) -> bool:
        """判断返回的页面是不是已登录的用户页。"""
        if not page:
            return False
        if re.search(r"userdetails\.php\?id=\d+", page, re.I):
            return True
        # 落到登录页的特征
        if re.search(r"(takelogin\.php|login\.php\?returnto|name=[\"']username[\"]')", page, re.I):
            return False
        return True

    # ------------------------------------------------------------------ 核心检查逻辑

    def run_check(self, force: bool = False) -> None:
        """执行一次保活检查，任何异常都转成错误通知，避免静默失败。"""
        try:
            self._do_check(force=force)
        except Exception as err:
            import traceback
            logger.error(
                f"憨憨保活检查执行异常({type(err).__name__}): {str(err)}\n{traceback.format_exc()}"
            )
            site_name = self._keyword
            try:
                site = self._get_site()
                if site:
                    site_name = site.name
            except Exception:
                pass
            # 增加错误信息截断，防止过长导致前端异常
            error_msg = f"插件执行异常：{str(err)[:150]}"
            self._notify(site_name, None, error=error_msg)

    def _record(self, now: datetime, result: Dict[str, Any]) -> None:
        """写入最近一次结果与历史。"""
        result["checked_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        result["limit_days"] = self._limit_days
        self._last_result = dict(result)
        self._save_config()

        history = self.get_data("check_history") or []
        history.insert(0, dict(result))
        self.save_data("check_history", history[:30])

    def _do_check(self, force: bool = False) -> None:
        """执行保活检查。

        默认走「临期才核实」：平时只按本地记录的登录时间倒计时、不访问站点；
        只有当剩余天数不大于「核实阈值」、本地没有可信基准，或用户手动点「立即检查」时，
        才真正请求站点读一次登录记录。
        """
        now = datetime.now(tz=pytz.timezone(settings.TZ)).replace(tzinfo=None)
        baseline = dict(self._last_result or {})

        # 本地基准可用：登录记录口径，或上一次本地推算
        base_time = None
        if baseline.get("last_action") and baseline.get("source") in ("login_log", "local"):
            try:
                base_time = datetime.strptime(str(baseline["last_action"]), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                base_time = None

        if base_time and not force and self._verify_days > 0:
            days_since = max((now.date() - base_time.date()).days, 0)
            remaining = self._limit_days - days_since
            if remaining > self._verify_days:
                logger.info(
                    f"憨憨保活检查：本地推算 {base_time.strftime('%Y-%m-%d %H:%M:%S')} 最后登录"
                    f"（{days_since} 天前），还剩 {remaining} 天，未达核实阈值 {self._verify_days} 天，"
                    f"本次不访问站点"
                )
                result = dict(baseline)
                result.update({
                    "days_since": days_since,
                    "remaining": remaining,
                    "source": "local",
                    "verified": False,
                })
                self._record(now, result)
                if self._daily_report or remaining <= self._warn_days:
                    self._notify(baseline.get("site") or self._keyword, self._last_result)
                return

        logger.info("开始执行憨憨登录保活检查（需要访问站点）...")
        site = self._get_site()
        if not site:
            logger.warning("憨憨保活提醒：未找到憨憨站点，请先在站点管理里添加（域名含 hhanclub）")
            self._notify(self._keyword, None, error="未找到憨憨站点，请先在站点管理里添加并配置 Cookie")
            return
        if not site.cookie:
            logger.warning("憨憨保活提醒：站点未配置 Cookie，无法读取用户页")
            self._notify(site.name, None, error="站点未配置 Cookie，无法读取用户页，请在站点管理里补全")
            return

        # 1. 首页：确认登录态并取 uid
        index_page = self._request(site, "index.php")
        if index_page is None:
            # 增强日志：区分网络问题 vs Cookie 失效
            if site.cookie:
                logger.warning(f"憨憨保活检查：Cookie可能失效（首页返回非预期内容）")
                self._notify(site.name, None, error="Cookie已失效，请重新获取憨憨Cookie并更新站点管理")
            else:
                logger.warning(f"憨憨保活检查：无法连接站点，请检查代理或网络 (域名: {site.url})")
                self._notify(site.name, None, error="连接站点失败，请检查代理设置或站点域名")
            return
        if not self._is_logged_in(index_page):
            logger.warning(f"憨憨保活检查：Cookie已失效或站点返回登录页")
            self._notify(site.name, None, error="Cookie已失效（首页返回登录页），请重新获取憨憨 Cookie 并更新站点管理")
            return

        # 2. 解析 uid - 增强异常捕获
        try:
            uid_match = self._uid_re.search(index_page)
            uid = uid_match.group(1) if uid_match else ""

            # 确保有 uid 才继续解析
            if not uid:
                logger.error(f"憨憨保活检查：无法从首页提取 uid，页面结构可能变了")
                self._notify(site.name, None, error="无法解析站点数据，页面结构可能已更新，请联插件作者")
                return

        except Exception as parse_err:
            logger.error(f"憨憨保活检查：解析首页时异常: {str(parse_err)}")
            self._notify(site.name, None, error=f"解析站点数据异常: {str(parse_err)[:100]}")
            return

        # 3. 主口径：用户访问记录表（真实登录事件，实测不会被本插件的读取刷新）
        login = None
        if uid:
            logs_page = self._request(site, f"userloginlogs.php?uid={uid}") or ""
            if logs_page:
                login = self._extract_login_log(logs_page, now)

        # 4. 兜底：用户页「最近动向」（会被本次访问刷新，仅在无登录记录时使用）
        action = None
        detail_page = ""
        if login is None:
            detail_page = self._request(site, f"userdetails.php?id={uid}") if uid else ""
            if detail_page:
                action = self._extract_action(detail_page, now)

        username = self._extract_username(index_page, detail_page)

        if login is None and action is None:
            logger.warning(f"憨憨保活提醒：未解析出登录时间（uid={uid or '未取到'}）")
            self._notify(site.name, None,
                         error="未能解析出登录时间（登录记录为空且用户页无「最近动向」），"
                               "站点页面结构可能变了，请检查 Cookie 是否有效")
            return

        if login:
            active_at = datetime.strptime(login["last_login"], "%Y-%m-%d %H:%M:%S")
            label = "最后登录"
            self_refresh = False
        else:
            active_at, label, _ = action
            self_refresh = (now - active_at) <= timedelta(minutes=10)

        days_since = max((now.date() - active_at.date()).days, 0)
        remaining = self._limit_days - days_since

        result = {
            "username": username or baseline.get("username", ""),
            "uid": uid or baseline.get("uid", ""),
            "site": site.name,
            "field": label,
            "last_action": active_at.strftime("%Y-%m-%d %H:%M:%S"),
            "ip": (login or {}).get("ip", ""),
            "region": (login or {}).get("region", ""),
            "method": (login or {}).get("method", ""),
            "days_since": days_since,
            "remaining": remaining,
            "self_refresh": self_refresh,
            "source": "login_log" if login else "recent_action",
            "verified": True,
            "verify_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._record(now, result)

        logger.info(
            f"憨憨保活检查完成：{label} {active_at.strftime('%Y-%m-%d %H:%M:%S')}"
            f"（{days_since} 天前，来源={'登录记录' if login else '最近动向'}），"
            f"距 {self._limit_days} 天期限还剩 {remaining} 天"
        )

        self._notify(site.name, result)

    # ------------------------------------------------------------------ 通知推送

    def _notify(self, site_name: str, result: Optional[Dict[str, Any]], error: Optional[str] = None) -> None:
        """推送保活提醒；result 为空表示检查失败，仅发送错误说明。"""
        if error:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=f"【{site_name}】保活检查失败",
                text=f"憨憨登录保活提醒插件执行失败：{error}",
            )
            return

        remaining = int(result.get("remaining", 0))
        urgent = remaining <= 0
        warn = remaining <= self._warn_days
        if urgent:
            title = f"🔴【{site_name}】保活已超期"
        elif warn:
            title = f"⚠️【{site_name}】登录保活提醒"
        else:
            title = f"【{site_name}】登录保活提醒"

        days_since = int(result.get("days_since", 0))
        days_text = "今天" if days_since <= 0 else f"{days_since} 天前"
        place = "·".join(x for x in (result.get("region"), result.get("method")) if x)
        login_line = (f"{result.get('field') or '最后登录'}：{result.get('last_action') or '—'}"
                      f"（{days_text}）" + (f" {place}" if place else ""))
        lines = [
            f"账号：{result.get('username') or '—'}（UID {result.get('uid') or '—'}）",
            login_line,
            f"保活期限：连续 {result.get('limit_days') or self._limit_days} 天不登录将被封禁"
            f"（已封存账号为 400 天）",
            f"剩余天数：{remaining} 天",
        ]
        if result.get("source") == "recent_action":
            lines.append("⚠️ 数据来源：用户页「最近动向」（本次访问可能已刷新该时间），建议手动登录站点核实")
        elif result.get("source") == "login_log":
            lines.append("✅ 数据来源：站点「用户访问记录」（真实登录事件）")
        elif result.get("source") == "local":
            lines.append("💡 数据来源：本地推算（未访问站点），如需核实请点「立即检查」")

        source_meta = self._source_meta.get(result.get("source"), {})
        source_label = source_meta.get("label", result.get("source", "未知"))
        source_color = source_meta.get("color", "grey")
        lines.append(f"验证状态：{'已核实' if result.get('verified') else '未核实'}（{source_label}）")

        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=title,
            text="\n".join(lines),
        )

    def _api_check_now(self):
        """API: 立即检查一次。"""
        from app.plugins import _PluginBase
        # 触发后台检查
        self.run_check(force=True)
        return {"success": True, "message": "已排入后台执行，约 3 秒后完成"}