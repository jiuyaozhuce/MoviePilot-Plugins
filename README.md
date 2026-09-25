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

## 安装

把 `embyunwatchedwash/` 整目录放到 MoviePilot 的 `app/plugins/` 下（与 `bestfilmversion` 同级），
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
