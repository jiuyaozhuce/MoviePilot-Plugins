# EmbyUnwatchedWash（未看洗版 / Unwatched Wash）

扫描 Emby / Jellyfin 中**未观看（IsUnplayed）**的影视，自动创建「洗版」订阅，让 MoviePilot 下载更高画质版本替换旧版。
支持**手动指定只洗版部分影视**：在插件「数据查看」页从未观看清单中勾选即可；不选则默认对全部未观看影视洗版。

Scan Emby / Jellyfin for **unplayed** movies/series and auto-create "best-version" (wash) subscriptions so MoviePilot upgrades them to higher-quality releases.
You can also **manually pick** which items to wash by ticking them in the plugin's *Data* page; if nothing is selected, all unwatched items are washed by default.

## 实现来源（重要 / Source）

本插件**直接 fork 自 MoviePilot 官方插件库 `bestfilmversion`（收藏洗版，by wlj）**，
几乎不写新逻辑，全部复用其已验证的函数：

- `app.modules.emby.Emby` / `app.modules.jellyfin.Jellyfin` 的 `get_data()` / `get_iteminfo()`
- `app.chain.subscribe.SubscribeChain().add(..., best_version=True)` —— 这就是 MP 原生的「洗版」动作
- `_PluginBase` 的 `get_service` / `get_form` / `get_page` / `get_data` / `save_data` 骨架

Forked from the official `bestfilmversion` plugin. Only the filter changed from *IsFavorite* to *IsUnplayed*; all heavy lifting reuses the official, battle-tested functions.

相对 `bestfilmversion` 的**改动 / Differences vs `bestfilmversion`**：

| 项 / Item | bestfilmversion | 本插件 / This plugin |
|----|----------------|--------|
| 筛选条件 / Filter | `Filters=IsFavorite`（收藏） | `Filters=IsUnplayed`（未观看） |
| 媒体类型 / Types | 仅 `Movie` | `Movie` + `Series`（剧集可开关） |
| Webhook 触发 | 有（收藏/评分事件） | 已删除（未观看洗版是扫描型，非事件型） |
| 手动选择 / Manual pick | 无 | **新增**：配置页可勾选指定影视洗版 |
| 大库分页 / Pagination | 单页 `Limit=500` | **新增**：按 `StartIndex` 分页拉全 |
| 失败告警 / Failure alert | 无 | **新增**：订阅创建失败会通知并提示原因 |

## 仓库结构（符合 MoviePilot 官方插件仓库格式 / Repo layout）

```
MoviePilot-Plugins/
├── package.json          # 插件清单（兜底）
├── package.v3.json       # 插件清单（v3 实际读取）
├── plugins.v3/           # v3 插件源码目录
│   └── embyunwatchedwash/
│       └── __init__.py   # 插件主类 EmbyUnwatchedWash
├── icon.svg
└── README.md
```

> MoviePilot 在「插件仓库」里添加仓库时，会去仓库根读 `package.v3.json`（v3）来枚举插件，
> 并从 `plugins.v3/<插件id>/` 取源码。

## 安装 / Install

### 方式一：作为插件仓库添加（推荐 / Recommended）

1. MoviePilot → **设置 → 插件 → 插件仓库 → 新增**
2. 仓库地址 / Repo：`https://github.com/jiuyaozhuce/MoviePilot-Plugins`
3. 分支 / Branch：`main`
4. 保存后刷新仓库列表，即可看到「Emby未看洗版」，点击安装。

### 方式二：手动放置（开发/调试用 / Manual）

把 `plugins.v3/embyunwatchedwash/` 整目录复制到 MoviePilot 的 `app/plugins/` 下，
重启 MoviePilot 或重载插件即可在「插件」页看到「未看洗版」。

## 配置 / Configuration

- **启用插件 / Enable**：开 / on
- **发送通知 / Notify**：开 → 每次扫描完成后推送汇总通知（成功 N / 失败 M）
- **立即运行一次 / Run once**：开 → 保存配置后延迟 3 秒执行一次（这是 MoviePilot 官方标准的一次性触发方式，与「豆瓣想看」等官方插件一致；也可发送远程命令 `/emby_wash` 触发）
- **包含剧集 / Include series**：开 → 对 `Series`（电视剧）也创建洗版订阅；关 → 仅电影
- **剧集按未观看集洗版 / Episode level**（默认开）：剧集**不再整部洗版**——按季建订阅，并把订阅的「开始集数」设为**该季第一个未观看的集**，已看过的集不会被洗版；关 → 退回整部剧洗版
- **单次最多处理数量 / Limit**：0 = 不限。**强烈建议大库先设 5~20 试跑**（你的库有上千部未观看，一次全量会瞬间创建上千订阅）
- **排除媒体库 / Exclude libraries**：命中的媒体库整库跳过（不建订阅），且在「数据查看」页的未观看清单里**也不再出现**
- **排除关键字 / Exclude keywords**：按媒体库名做子串匹配（例如填 `少儿` 可避开「少儿动画」这类库）
- **指定洗版影视 / Selected items**：在「数据查看」页的未观看清单里**手动勾选**（勾选即保存）。留空 = 对全部未观看影视洗版；勾选 = 只洗版勾选的影视（剧集同样会按未观看集定位，需媒体服务器在线；否则按整部洗版）
- **执行周期 / Cron**：5 位 cron（如 `0 4 * * *`）；留空则每 30 分钟扫描一次

## 触发方式 / How to trigger

1. 定时 / Scheduled：按 `执行周期` / 默认 30 分钟（`get_service`）
2. 手动开关 / Toggle：配置页打开「立即运行一次」并保存（`only_once`，官方标准机制）
3. 远程命令 / Command：向 MoviePilot 发送 `/emby_wash`，立即执行一次扫描
4. 详情页 / Detail page：查看已洗版历史与媒体库未观看列表；另提供调试 API：
   - `GET /api/v1/plugin/EmbyUnwatchedWash/history` —— 历史 JSON
   - `GET /api/v1/plugin/EmbyUnwatchedWash/medias` —— 媒体库未观看影视可选项 JSON

## 工作原理 / How it works

1. `sync()` 优先读取 `selected_items`（手动选择）。若非空，直接对每个 tmdbid 调用 `recognize_media` 后创建洗版订阅，**不依赖媒体服务器在线**。
2. 若未选择，则通过 `MediaServerHelper().get_services()` 获取**已配置且已连接**的 Emby/Jellyfin 客户端实例
   （MoviePilot v3 走 `app.sdk.services`，v2 走 `app.helper.mediaserver`），对每台服务器调用
   `emby_get_items()` / `jellyfin_get_items()`，用 `Users/{user}/Items?Filters=IsUnplayed&Recursive=true` **分页拉全**未观看条目。
3. 条目按类型归组生成洗版任务：
   - `Movie` → 整部洗版（电影未看即整部未看）
   - `Series` + `Episode` → **按季**拆任务，取该季未观看集的最小集号作为 `start_episode`（订阅的「开始集数」），
     MoviePilot 从该集开始搜索/下载，**已观看的集不会被洗版**；若拿不到集明细（或关闭该开关）则整剧洗版
   - 电影按 tmdbid 去重、剧集按 `tmdbid+季` 去重（缓存键，避免同一季重复订阅）
4. 直接从列表条目的 `ProviderIds.Tmdb` 取 tmdbid（不再逐条查详情）→ `recognize_media()` 识别 →
   `subscribechain.add(..., season=季, start_episode=开始集数, best_version=True, exist_ok=True)` 创建洗版订阅（**已存在则跳过**）。
5. 写入缓存与历史；若 `发送通知` 开启，推送汇总（含失败数与失败原因提示）。

## 日志说明 / Logging

本插件使用 MoviePilot 内置 `logger`。

> 日志落点（2026-09-26 实测修正）：本插件会被 MoviePilot 记为**独立插件日志**，
> 输出在 `<MoviePilot 数据目录>/config/logs/plugins/embyunwatchedwash.log`
> （不是 `moviepilot.log`）。排查时直接看这个文件：

```bash
grep "未看洗版" /volume1/docker/moviepilot2/config/logs/plugins/embyunwatchedwash.log | tail -50
```

开启 MoviePilot 的 `DEBUG` 模式（`settings.DEBUG`）可看到更细的「跳过原因」等调试日志；默认（INFO）下即可看到完整运行叙事：

- `【未看洗版】========== 开始扫描任务 ==========` —— 每次运行开始
- `【未看洗版】运行模式：全量扫描 | 包含剧集=… | 媒体服务器=…` 或 `手动选择（指定 N 个 tmdbid 洗版）`
- `【未看洗版】读取到 N 个媒体库：…` —— 库归属是按库拉取的前提（见「注意」）
- `【未看洗版】Emby/Jellyfin 媒体库「<库名>」未观看条目 N 条` —— 每个库拉取到的数量
- `【未看洗版】未观看清单已按排除规则隐藏 N 条（媒体库：…）` —— 排除规则实际生效的条数
- `【未看洗版】<server> 去重后待处理 N 部影视`
- `【未看洗版】正在处理：<标题> (tmdbid=…)` / `已创建洗版订阅：<标题> (年份) [类型]` —— 每部影视的处理与成功
- `【未看洗版】创建洗版订阅失败：<标题> - <原因>` / `媒体识别失败` / `获取详情失败` —— 失败原因
- `【未看洗版】========== 扫描完成 ========== | 新建订阅 N 个 | 失败 M 个 | 跳过 K 个` —— 运行总结

搜索 `【未看洗版】` 即可在日志中快速过滤本插件全部输出。

## 注意 / Notes

- **「洗版」= 升级画质**：订阅创建后由 MoviePilot 负责搜索更高画质版本并整理替换，前提是站点/订阅规则允许，且整理模式支持覆盖旧版（与官方洗版要求一致）。
- **排除规则怎么判定「哪个库」**：Emby/Jellyfin 的 Items 接口**不返回 `LibraryName`**（写进 `Fields` 也会被忽略），所以插件改为**按媒体库分别拉取**（`Library/VirtualFolders` 取库根 `ItemId` → `ParentId` 逐库查询），入库时给每条打上库名。这也是排除规则能生效的前提；若库清单读取失败，会自动退化为全局拉取并在日志告警（此时排除规则不会命中）。
- **未观看清单会隐去被排除的条目**：命中「排除媒体库 / 排除关键字」的影视不会出现在「数据查看」页的清单里（它们运行时本来就不会被洗版），清单抬头会显示「已按排除规则隐藏 N 条」，便于核对数量。
- **大库分页**：已按 `StartIndex` 分页拉全，不再受 `Limit=500` 限制。
- 本插件**不会删除任何媒体**，只创建订阅。
- Plex 暂不支持（上游 Plex 走 watchlist API，与「未观看」语义不同，故略去）。

## 洗版不生效 · 排查清单 / Troubleshooting: wash not working

若创建了洗版订阅但实际没有更高画质版本入库，按顺序排查：

1. **确认「允许洗版 / 最佳版本」已开启**：MoviePilot 订阅创建 `best_version=True` 后，是否实际生效取决于系统是否允许洗版。若插件通知提示「部分失败 / 未开启允许洗版」，请到 MoviePilot 订阅/设置中确认洗版相关开关已打开。
2. **确认下载器与订阅配置正常**：洗版最终依赖下载器下载更高画质资源。请确认已配置可用下载器、订阅站点，且手动订阅能正常下载。
3. **确认媒体服务器已接入**：MoviePilot『设置 → 媒体 → 媒体服务器』中已添加 Emby/Jellyfin 且连接正常（插件通过 `MediaServerHelper` 自动枚举已配置的服务器，若日志提示「未连接」请检查 Host 与 API Key）。全量模式需要媒体服务器在线；手动选择模式可离线。
4. **确认 TMDB API Key 可用**：`recognize_media` 需要 TMDB 识别媒体信息；TMDB Key 失效会导致识别失败（日志会打印 `未识别到媒体信息`）。
5. **确认已有更高画质资源**：洗版是「有更好版本才替换」。如果站点上当前最高画质就是你已经有的版本，订阅会一直等待，不会重复下载。
6. **查看日志与通知**：插件会记录每条 `创建洗版订阅失败` 的原因（如 `未开启允许洗版`、`缺少下载器` 等），并（开启通知时）汇总到消息中。

---

### English

**Troubleshooting — wash not taking effect:**
1. Make sure "allow best version / wash" is enabled in MoviePilot; the plugin reports failures when it is off.
2. Ensure a working downloader and subscription sites are configured (wash still needs a higher-quality release to download).
3. For full-library mode, an Emby/Jellyfin server must be configured and connected in MoviePilot (the plugin enumerates servers via `MediaServerHelper`); manual-pick mode works offline.
4. A valid TMDB API Key is required for media recognition.
5. Wash only replaces when a *better* quality release exists on your sites.
6. Check MoviePilot logs/notifications — the plugin logs the exact failure reason per item.
