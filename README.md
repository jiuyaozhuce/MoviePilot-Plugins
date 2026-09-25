# EmbyUnwatchedWash（未看洗版）

扫描 Emby / Jellyfin 中**未观看（IsUnplayed）**的影视，自动创建「洗版」订阅，让 MoviePilot 下载更高画质版本替换旧版。

## 实现来源（重要）

本插件**直接 fork 自 MoviePilot 官方插件库 `bestfilmversion`（收藏洗版，by wlj）**，
几乎不写新逻辑，全部复用其已验证的函数：

- `app.modules.emby.Emby` / `app.modules.jellyfin.Jellyfin` 的 `get_data()` / `get_iteminfo()`
- `app.chain.subscribe.SubscribeChain().add(..., best_version=True)` —— 这就是 MP 原生的「洗版」动作
- `_PluginBase` 的 `get_service` / `get_form` / `get_page` / `get_data` / `save_data` 骨架

相对 `bestfilmversion` 的**最小改动**（仅 3 处语义变化 + 删掉不适用的部分）：

| 项 | bestfilmversion | 本插件 |
|----|----------------|--------|
| 筛选条件 | `Filters=IsFavorite`（收藏） | `Filters=IsUnplayed`（未观看） |
| 媒体类型 | 仅 `Movie` | `Movie` + `Series`（剧集可开关） |
| Webhook 触发 | 有（收藏/评分事件） | 已删除（未观看洗版是扫描型，非事件型） |
| 其余 | —— | 原样复用 |

## 仓库结构（符合 MoviePilot 官方插件仓库格式）

```
MoviePilot-Plugins/
├── package.json          # 插件清单（兜底，MP 可能读取此文件）
├── package.v3.json       # 插件清单（v3 实际读取的文件）
├── plugins.v3/           # v3 插件源码目录（MP 规定源码须放在此处）
│   └── embyunwatchedwash/
│       └── __init__.py   # 插件主类 EmbyUnwatchedWash
└── README.md
```

> 说明：MoviePilot 在「插件仓库」里添加仓库时，会去仓库根读 `package.v3.json`（v3）
> 来枚举插件，并从 `plugins.v3/<插件id>/` 取源码。早期版本把源码直接放仓库根、缺清单，
> 因此无法被添加进仓库列表。现在的结构与官方 `MoviePilot-Plugins` 及单插件仓库
> `moviepilot-v2-course-organizer` 一致。

## 安装

### 方式一：作为插件仓库添加（推荐，最简单）

1. MoviePilot → **设置 → 插件 → 插件仓库 → 新增**
2. 仓库地址填：`https://github.com/jiuyaozhuce/MoviePilot-Plugins`
3. 分支填：`main`
4. 保存后刷新仓库列表，即可看到「Emby未看洗版」，点击安装。

### 方式二：手动放置（开发/调试用）

把 `plugins.v3/embyunwatchedwash/` 整目录复制到 MoviePilot 的 `app/plugins/` 下，
重启 MoviePilot 或重载插件即可在「插件」页看到「未看洗版」。

## 配置

- **启用插件**：开
- **发送通知**：开（目前为预留开关，行为同上游）
- **立即运行一次**：开 → 保存后延迟 3 秒执行一次扫描
- **包含剧集**：开 → 对 `Series`（电视剧）也创建洗版订阅；关 → 仅电影
- **执行周期**：5 位 cron（如 `0 4 * * *`）；留空则每 30 分钟扫描一次

## 触发方式

1. 定时：按 `执行周期` / 默认 30 分钟（`get_service`）
2. 手动：配置页打开「立即运行一次」并保存（`only_once`）
3. 详情页：查看已洗版历史记录（`get_page`）

## 工作原理

1. `sync()` 读取 `settings.MEDIASERVER`，对每个 Emby/Jellyfin 调用 `emby_get_items()`，
   用 `Users/{user}/Items?Filters=IsUnplayed&Recursive=true` 拉取未观看条目。
2. 按名称去重 → 跳过缓存中已处理的 → `get_iteminfo()` 取 tmdbid → `recognize_media()` 识别。
3. `subscribechain.add(..., best_version=True, exist_ok=True)` 创建洗版订阅（**已存在则跳过**）。
4. 写入缓存与历史，避免重复订阅。

## 注意

- **「洗版」= 升级画质**：订阅创建后由 MoviePilot 负责搜索更高画质版本并整理替换，
  前提是站点/订阅规则允许，且整理模式支持覆盖旧版（与官方洗版要求一致）。
- **大库的 Limit**：`emby_get_items` 单次取 `Limit=500` 条（按加入时间倒序，最新未观看优先）。
  超大库若 500 条不够，可在源码把 `Limit=500` 调大；已处理条目有缓存，未覆盖到的会在后续运行逐步补全。
- 本插件**不会删除任何媒体**，只创建订阅。
- Plex 暂未在 `settings.MEDIASERVER` 中支持（上游 Plex 走 watchlist API，与「未观看」语义不同，故略去）。
