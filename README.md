# MoviePilot 插件仓库

[jiuyaozhuce](https://github.com/jiuyaozhuce) 维护的 MoviePilot 插件源，适用于 MoviePilot V3（`system_version >=3.0.0`）。

## 插件列表

| 插件 | 版本 | 说明 |
| --- | --- | --- |
| 播放进度洗版 (PlaybackBestVersion) | 3.9.0 | 按 Emby / Jellyfin 分集播放进度决定订阅是否继续洗版；洗版信息作为模板变量 `is_wash` 等注入下载通知与入库通知（也可自动加【洗版】标记）；拦截同一种子的重复下载与重复通知 |
| 憨憨登录保活提醒 (HHClubKeepAlive) | 1.2.1 | 读取憨憨「用户访问记录」，按 30 天（封存 400 天）保活线推送剩余天数提醒 |
| 豆瓣书影音档案 (DouBanWatching) | 1.9.20 | 将剧集在看、看完状态同步到豆瓣书影音档案，含仪表盘海报墙 |

## 索引与兼容性

- 本仓库只维护 V3 索引 `package.v3.json` 与 `plugins.v3/` 目录。原 V2 索引 `package.json` 与 `plugins/doubanwatching/` 旧目录已移除——DouBanWatching 在 V3 索引里本就有同名同版本条目，无需重复维护。若仍有 V2 客户端接入本仓库，它将看不到任何插件。
- ✅ DouBanWatching 已完成 V3 SDK 适配（v1.9.20，实测宿主 **v3.2.6**）：配置 / 事件 / 日志 / 媒体元数据 / 插件基类 / 网络请求统一改走 `app.sdk.*`；旧路径（`app.core.event`、`app.core.config`、`app.core.metainfo`、`app.core.meta`、`app.helper.cookiecloud`、`app.utils.http`、`app.db.plugindata_oper`、`app.log`）在 V3 中已无源码文件，只靠宿主兼容层以别名注入，现已不再依赖。新旧符号经容器逐项实测为同一对象，功能与配置无变化。
  - `app.chain.media`、`app.schemas`、`app.schemas.types`、`app.db.oper` 在 V3 中仍是真实模块，保持原路径；`app.schemas.MediaInfo` 与 `app.sdk.media.MediaInfo` 不是同一个类，类型标注仍取前者。
  - 唯一例外：CookieCloud 在 V3 SDK 中只有 `app.sdk._legacy.cookiecloud` 一处实现（无新替代模块），与旧 `app.helper.cookiecloud` 指向同一个类。

## 安装

MoviePilot → 设置 → 插件 → 插件仓库 → 新增：

- 仓库地址：`https://github.com/jiuyaozhuce/MoviePilot-Plugins`
- 分支：`main`

保存后刷新仓库列表，安装所需插件即可。注意「添加仓库」与「绑定插件源」是两件事，需绑定后才会提示版本更新。

> **历史说明**：原自研插件「EmbyUnwatchedWash（未看洗版）」已移除，同类功能改由本仓库维护的 PlaybackBestVersion 承担（基于上游 [xlmc/MoviePilot-Plugins](https://github.com/xlmc/MoviePilot-Plugins) v3.2.0，本仓库增加 Webhook 即时触发并接管维护）。若之前启用过旧插件，它写入订阅表的 `start_episode`、分集 `episode_priority=100` **不会随卸载回滚**，升级后请手动核对订阅状态。

## 洗版规则组（现网配置）

宿主 `BestVersionFilterRuleGroups` 指向下面两个规则组（`systemconfig.UserFilterRuleGroups`）。规则串内**必须用 `>` 分层、单个规则组内写完整个阶梯**；并列多个规则组是串行过滤，会把低档资源直接筛掉。首层 100 分，每降一层减 1；不命中任何层的资源被丢弃。

电视剧（`media_type=电视剧`，最高到 2160p HDR 收工，已看集由 PlaybackBestVersion 写 100）：

```
4K & HDR > 4K > 1080P & HDR > 1080P
```

电影（`media_type=电影`，洗到 4K REMUX + 高清音轨）：

```
4K & REMUX & HDAUDIO > 4K & REMUX > 4K & DOLBY & HDAUDIO > 4K & DOLBY > 4K & HDR & HDAUDIO > 4K & HDR > 4K & HDAUDIO > 4K > 1080P & REMUX > 1080P & BLURAY & HDAUDIO > 1080P & BLURAY > 1080P
```

画质主序为 REMUX > 杜比视界 > HDR > SDR，音质只在同画质内加分。`HDAUDIO` 是本仓库补的自定义过滤规则（`CustomFilterRules`，内置的 `LOSSLESS` 只认音乐格式）：

```
id=HDAUDIO  name=高清音轨  include=TrueHD|DTS[- .]?HD|DTS[- .]?X|LPCM|Atmos|Dolby[ .+]?Atmos
```

电影侧 `current_priority` 由宿主在每次下载后按本轮最高档写入，洗版只接受**严格更高**的档位（`>` 判定），到 100 即停；720P 及以下不在阶梯内，不会被洗版流程下载。

> 改完 `systemconfig` 必须让宿主重载配置：直接 psql 写入不会更新运行期缓存（会与规则 API 的乐观锁打架，报「规则组已被其他请求修改」），要么走 `/api/v1/rule/groups/{name}` PUT，要么重启容器。

---

# PlaybackBestVersion（播放进度洗版）

按媒体服务器（Emby / Jellyfin）的**分集播放进度**决定订阅是否继续洗版：进度达到阈值（默认 90%）、播过、或标记已看的集写成宿主口径的洗版完成（`episode_priority=100`），后续洗版不再为它搜更高版本；电影命中则整部停止洗版。支持多服务器、多用户合并、试运行与通知。

## 下载通知洗版标注（v3.6.0 并入 / v3.8.0 支持模板变量）

原独立插件「下载通知洗版标注 (DownloadWashMark)」已合并进本插件，配置页第三段「下载通知洗版标注」：

- **启用洗版标注**（默认开）／**标题前缀**（默认 `【洗版】`，已带则不重复加）／**正文注明升级集数**（默认开）／**标注方式**（自动 / 插件直接改写 / 只注入模板变量）
- 只作用于 `subscription_download.submission_type == "upgrade"` 的下载通知，首次订阅的下载完全不改；
- 正文首行按洗版形态写：分集洗版 → `洗版：升级 E03-E26`，全集洗版 → `洗版：全集覆盖`，电影 → `洗版：覆盖已有版本`；
- 详情页摘要条下方显示标注状态（已生效 / 已启用但补丁未装 / 未启用）与当前由谁排版，插件接口 `GET /api/v1/plugin/PlaybackBestVersion/washmark?apikey=…` 返回补丁状态、模式与示例。

**为什么不能只改通知模板**：宿主构建下载通知时（`DownloadProcessingOwner._build_download_notification`）只往模板送 `meta` / `mediainfo` / `torrentinfo` / `download_episodes` / `username` / `current_time`，源码注释明写「不额外拼接订阅下载来源」——模板拿不到「这次是不是洗版」，改模板做不出标注。

**v3.8.0 的做法**：再包一层宿主的 `MessageTemplateHelper.render`，把结论作为额外变量喂进渲染上下文（`TemplateContextBuilder.build` 会 `context.update(kwargs)`），模板里就能写 `{% if is_wash %}`：

| 变量 | 含义 |
| --- | --- |
| `is_wash` | 是否洗版；首次订阅不注入，模板取不到即假 |
| `wash_prefix` | 标题前缀（取自插件配置） |
| `wash_episodes` | 升级的集号，如 `E03-E06、E09` |
| `wash_full` | 是否全集洗版 |
| `wash_text` | 正文首行说明，如 `洗版：升级 E03-E06` |

模板里出现上述任一变量 → 视为模板接管排版，插件不再二次改写（避免双重标注）；没出现 → 自动回落到插件直接加前缀，保证不写模板也一定有标注。

两处补丁都只做运行期方法替换，不写库、不改宿主文件：`_build_download_notification` 用来从调用栈取 `context.subscription_download`，`MessageTemplateHelper.render` 用来补模板变量。插件停用或关掉标注会立刻还原宿主方法，不阻断下载。

## 入库通知洗版标注（v3.9.0 新增）

入库（整理完成）通知是另一条链路：`TransferChain` 结算后调 `TransferHistoryOwner.send_transfer_message(meta, mediainfo, transferinfo, …)`，那里只剩元数据与整理结果，**拿不到** `context.subscription_download`，所以下载通知的那套判定在这里用不了。

做法是改从宿主自己写下的事实反查：`downloadhistory.note` 里的 `subscription_download` 就带着每次订阅下载的结论（`submission_type` 是 `upgrade` 还是 `initial`、升级了哪些集）。插件在运行期包装 `send_transfer_message`，按下面的口径判断本次入库：

1. **媒体身份**：`media_source` + `media_id`（媒体信息优先、元数据兜底，与宿主 `resolve_media_identity` 同源）；
2. **季 / 集**：`begin_season` + `episode_list`（季缺失时回退解析 `S01` 文本）；
3. **时间窗**：默认 48 小时（可配 1~168），洗版下载到入库通常几十分钟；
4. **命中条件**：窗口期内存在同媒体的 `upgrade` 记录，且该记录升级过的集与本次入库集**有交集**；电影不判季集，命中即算。

命中后才注入 `is_wash` / `wash_prefix` / `wash_episodes` / `wash_full` / `wash_text`，与下载通知共用同一套模板变量；首次订阅的入库通知完全不动。

- 配置页「入库通知也标注」（默认开，跟随「启用洗版标注」总开关）与「入库关联窗口（小时）」（默认 48）；
- 下载与入库是**两个模板**（`downloadAdded` / `organizeSuccess`），各自独立判定是否接管排版：只改了下载模板时，入库通知仍由插件直接加标记；
- 详情页摘要下方多一行入库标注状态条，接口 `/washmark` 增加 `organize` 段；带 `probe_media`（TMDB ID）、`probe_season`、`probe_episode` 可现场跑一遍判定（不发通知、不写库）：

```bash
curl -s "http://<host>/api/v1/plugin/PlaybackBestVersion/washmark?probe_media=279155&probe_season=1&probe_episode=19&apikey=<token>"
# → organize_probe: {"download_records": 9, "is_wash": true, "match": {"upgrade_episodes": [19], …}}
```

判定失败、媒体身份缺失、类型是音乐、或没有窗口内的 `upgrade` 记录时一律返回「不是洗版」，通知保持宿主原生样子 —— **只可能少标，不会把首次订阅误标成洗版**。

## 重复下载去重（v3.7.0 新增）

宿主的洗版门槛只在**入库完成**时写回，从「开始下载」到「入库」之间再跑一轮洗版搜索时，同一批集会被重新命中同一个种子；而 Transmission / qBittorrent 对重复种子不报错、只返回已有任务，宿主于是又记一条下载历史、再发一条通知（下载器里其实只有一份）。

插件包住宿主结算入口 `DownloadHistoryOwner._settle_download_success`：同 `download_hash` 在窗口期（默认 6 小时，可配 1~72）内已记录过、且那次记录的集范围覆盖本次要下的集，就把结算参数改成宿主的 `already_exists=True`——**不重复通知、不重复记文件明细**，并触发宿主自带的重复资源退避。

- 真升级到更高档（不同种子、不同 hash）正常放行；
- 同一个整包换集补下（集数没被覆盖）正常放行；
- 窗口外的同一种子（手动删了重下）正常放行；
- 状态见详情页摘要下方状态条与 `GET /api/v1/plugin/PlaybackBestVersion/dedupe?apikey=…`。

## Webhook 即时触发（v3.3.0 新增）

默认开启。Emby「设置 → Webhook」新增一条：

- URL：`http://<MoviePilot地址>/api/v1/webhook/?token=<API_TOKEN>&source=<媒体服务器名>`
- 请求方式：POST
- 事件：勾选「播放停止」「标记为已播放」「标记为未播放」

插件监听宿主 `WebhookMessage`，只认这三类会改变观看状态的事件（播放开始 / 暂停 / 心跳一律忽略），兼容 Emby 带点事件名与 Jellyfin 无点写法；按「去抖延迟」（默认 20 秒，0 = 立即）合并窗口内多次事件只跑一轮检查，不必等下一轮定时。事件本身不携带、也不改写任何订阅数据，实际动作仍由 `run()` 按媒体服务器事实决定，重复投递无副作用。定时检测（默认 30 分钟）与手动运行保持原样。

注意插件「只写不回滚」：**标记为未播放**会触发一次检查，但不会撤销该集已写入的 100，需要恢复洗版时手动修改订阅的分集优先级。

若之前用过旧版自研「未看洗版」插件，其遗留的 `start_episode` 与分集 100 不会自动恢复，建议先开启「试运行」核对一轮订阅状态。

完整配置说明见 [plugins.v3/playbackbestversion/README.md](plugins.v3/playbackbestversion/README.md)。

## 来源与许可

- 上游：[xlmc/MoviePilot-Plugins](https://github.com/xlmc/MoviePilot-Plugins) 的 `PlaybackBestVersion` v3.2.0（再上游为 justzerock / jxxghp 官方插件仓库）
- 本仓库由 `jiuyaozhuce` 接管维护，在 v3.2.0 基础上增加 Webhook 即时触发（v3.3.0）；插件作者与项目地址已改指本仓库（v3.3.1 仅为了让已安装的实例刷新该作者信息而升版本，功能不变）；v3.3.2 修复了详情页表格只显示分页、不显示条目的问题；v3.3.3 把「播放进度」列改成区间压缩显示（E1-33 这种），剧集多时不再把表格撑出横向滚动；v3.3.4 在详情页加了逐集状态方块条（一格一集，四色区分未下载/已下载/已洗版/已观看），并把表格精简到 4 列；v3.3.6 让订阅列表按「最近活跃」排序（最近观看 → 订阅更新 → 订阅创建），并在订阅名下方显示活跃时间；「最近观看」优先读分集 `LastPlayedDate`，Emby 列表接口不返回该字段时改用服务器活动日志的播放事件回填；v3.4.0 详情页整体重排成「摘要条 + 订阅卡片 + 折叠清单」（原 4 列表格在手机竖屏下完全不可用），方块条加集号刻度，未匹配订阅折叠，颜色改用主题变量适配深浅色
- 许可证 **GPL-3.0**，副本见 [plugins.v3/playbackbestversion/LICENSE](plugins.v3/playbackbestversion/LICENSE)

---

# HHClubKeepAlive（憨憨登录保活提醒）

读取憨憨（HHanClub）「用户访问记录」里的最后登录时间，按站规保活期限计算剩余天数，临期推送提醒。

> 憨憨是 NexusPHP 站点，没有馒头那种只读 ApiKey 资料接口，只能用站点 Cookie 读页面；
> 插件只做读取，不调用签到接口、不模拟登录。

## 保活规则（取自憨憨 wiki《账号规则》）

- 未封存账号：连续 **30 天**不登录 → 封禁
- 已封存账号：连续 **400 天**不登录 → 封禁
- Ultimate User 及以上永久保留；Veteran User 及以上封存后不会被删除

## 前提

1. MoviePilot「站点管理」里已添加憨憨（域名含 `hhanclub`），并填好 **Cookie**
2. 憨憨现域名为 `hhanclub.net`（老域名 `hhanclub.top` 已停止解析），站点 URL 请按新域名填
3. 站点在 Cloudflare 后面，插件会带浏览器 UA 请求；网络需要能访问站点

## 配置

- **启用插件**：总开关
- **每日汇报**：关闭后仅在临期 / 超期 / 检查失败时通知
- **执行周期**：5 位 cron，默认每天 00:01
- **保活期限（天）**：默认 30；账号已封存时填 400
- **临期提醒（天）**：剩余天数不大于该值必发通知，默认 7
- **核实阈值（天）**：默认 7。剩余天数大于该值时只做本地推算、**不访问站点**；
  进入阈值内才去站点读一次真实登录记录。填 0 表示每次都核实（回到 v1.0 的行为）

## 行为说明

- 运行策略「临期才核实」：30 天从最后一次登录起算，用户不登录时本地倒推不会算错，
  所以平时**完全不访问站点**；只有剩余天数 ≤「核实阈值」、本地无基准、或手动点「立即检查」时才请求站点
- 核实时请求 `/index.php` 确认登录态并取 uid，再请求 `/userloginlogs.php?uid=…` 解析「用户访问记录」最新一条
- 取数口径说明（已实测）：读页面**不会**在「用户访问记录」里新增记录（连续多次请求后记录仍是上一次真实登录），
  所以这个读数是真实登录事件、不会被插件自己刷新；
  而用户页「最近动向」每次访问都会刷新成当前时刻，因此只作为登录记录为空时的兜底，并在页面/通知里标注
- Cookie 失效（首页跳登录页）会直接推送「保活检查失败」，不留静默失败
- 详情页：主状态卡（状态徽章 + 数据来源徽章 + 大号剩余天数 + 期限进度条 + 保活截止日）、
  检查概况网格（最后登录/来源/最近检查/下次检查/期限/核实阈值/账号/站点）、检查历史卡，
  可点「立即检查」强制核实一次、「打开憨憨」跳站点
