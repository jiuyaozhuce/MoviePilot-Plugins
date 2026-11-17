# 下载通知洗版标注

订阅**洗版（升级）**触发下载时，宿主发出的下载通知和首次订阅一模一样，看不出这次是在洗版。本插件在通知上补两处标记：

- **标题**前加前缀（默认 `【洗版】`），例如 `【洗版】谷雨 S01 E03-E26 开始下载`
- **正文**首行加一行说明：
  - 部分集洗版：`洗版：升级 E03-E26`
  - 全集洗版：`洗版：全集覆盖（E01-E26）`
  - 电影：`洗版：覆盖已有版本`

首次订阅的下载不受影响。

## 判定依据

宿主在提交订阅下载前会把「本次是首次订阅还是洗版、洗了哪些集」写进 `Context.subscription_download`（`app/application/subscription/priority.py::annotate_subscription_download`），本插件只在 `submission_type == "upgrade"` 时动手。

## 为什么是运行期补丁

下载通知由 `DownloadProcessingOwner._build_download_notification` 用 Jinja2 模板渲染，模板里能拿到的变量只有 `meta / mediainfo / torrentinfo / download_episodes` 等，**没有**「是不是洗版」的上下文，所以单改通知模板做不出条件标记。而它的调用方手里正好有 `context`，只是没往下传。

改容器里的宿主文件在容器重建或镜像升级后会丢失；本插件在 `init_plugin` 里给这个方法包一层，每次宿主启动、插件加载都会自动生效。补丁只加内容，不改任何判定、不写任何数据，异常只在 debug 日志留痕，不会阻断下载或通知。

## 配置

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| 启用洗版标注 | 关 | 关闭时补丁会被卸载 |
| 标题前缀 | `【洗版】` | 标题已经带该前缀时不重复添加 |
| 正文注明升级集数 | 开 | 关掉只加标题前缀 |

## 状态确认

插件详情页会显示补丁是否生效和一条标注示例；也可以用 `GET /api/v1/plugin/DownloadWashMark/status` 查看。
