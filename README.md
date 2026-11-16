# MoviePilot 插件仓库

[jiuyaozhuce](https://github.com/jiuyaozhuce) 维护的 MoviePilot 插件源，适用于 MoviePilot V3（`system_version >=3.0.0`）。

## 插件列表

| 插件 | 版本 | 说明 |
| --- | --- | --- |
| 播放进度洗版 (PlaybackBestVersion) | 3.3.0 | 按 Emby / Jellyfin 分集播放进度决定订阅是否继续洗版，支持 Webhook 即时触发 |
| 憨憨登录保活提醒 (HHClubKeepAlive) | 1.2.1 | 读取憨憨「用户访问记录」，按 30 天（封存 400 天）保活线推送剩余天数提醒 |
| 豆瓣书影音档案 (DouBanWatching) | 1.9.19 | 将剧集在看、看完状态同步到豆瓣书影音档案，含仪表盘海报墙 |

## 安装

MoviePilot → 设置 → 插件 → 插件仓库 → 新增：

- 仓库地址：`https://github.com/jiuyaozhuce/MoviePilot-Plugins`
- 分支：`main`

保存后刷新仓库列表，安装所需插件即可。注意「添加仓库」与「绑定插件源」是两件事，需绑定后才会提示版本更新。

> **历史说明**：原自研插件「EmbyUnwatchedWash（未看洗版）」已移除，同类功能改由上游 [xlmc/MoviePilot-Plugins](https://github.com/xlmc/MoviePilot-Plugins) 的 PlaybackBestVersion 承担（本仓库分发版增加 Webhook 即时触发）。若之前启用过旧插件，它写入订阅表的 `start_episode`、分集 `episode_priority=100` **不会随卸载回滚**，升级后请手动核对订阅状态。

---

# PlaybackBestVersion（播放进度洗版）

按媒体服务器（Emby / Jellyfin）的**分集播放进度**决定订阅是否继续洗版：进度达到阈值（默认 90%）、播过、或标记已看的集写成宿主口径的洗版完成（`episode_priority=100`），后续洗版不再为它搜更高版本；电影命中则整部停止洗版。支持多服务器、多用户合并、试运行与通知。

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
- 本仓库分发版由 `jiuyaozhuce` 在 v3.2.0 基础上增加 Webhook 即时触发（v3.3.0）
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
