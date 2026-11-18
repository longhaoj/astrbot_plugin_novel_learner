# 小说学习器 · astrbot_plugin_novel_learner

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.9.0-3b82f6)](https://astrbot.app)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0-orange)](./metadata.yaml)

发一条 `/学 <网址>`，把网页小说的正文抓下来，交给当前模型提炼一份**可执行的写作风格卡**，之后每轮对话自动把这张卡追加进 `system_prompt`——bot 就会用那本书的调子说话。

> 不需要额外安装任何包：只用 AstrBot 自带的 `requests` + `beautifulsoup4` + `lxml`。

---

## 功能一览

| 能力 | 说明 |
|---|---|
| 正文提取 | 依次尝试 `#chaptercontent` / `#content` / `#booktext` / `article` / `main` 等常见容器，**取纯文本最长的那个**；不足 200 字自动回退整个 `body` |
| 文本清洗 | 去 `script/style/nav/header/footer/aside/iframe`；丢弃 <12 字的短行；按 `ad_keywords` 剔除广告/导航行；合并过碎行 |
| 风格提炼 | 一次完整 LLM 调用，产出条目式风格卡：人称视角 / 句式节奏 / 用词偏好 / 对白风格 / 描写密度 / 3–5 条可照做的句式模板 |
| 注入对话 | 挂在 `on_llm_request`，把当前激活的风格卡追加进 `system_prompt`（默认压缩到 800 字） |
| 多素材管理 | 列表、查看、切换、删除、关闭注入，全部走 `/学` 子命令 |
| 二次确认 | 抓完**只入库不生效**，回「是」才套用；超时或换会话都不认，避免日常聊天里的「好」被误当成确认 |

---

## 安装

### 方式一：插件市场（推荐）

在 AstrBot WebUI → **插件市场** 搜索「小说学习器」或 `astrbot_plugin_novel_learner`，点安装。

### 方式二：仓库安装

WebUI → 插件市场 → 安装插件 → 填仓库地址：

```
https://github.com/longhaoj/astrbot_plugin_novel_learner
```

### 方式三：手动

```bash
cd AstrBot/data/plugins
git clone https://github.com/longhaoj/astrbot_plugin_novel_learner
```

装完在 WebUI → 插件管理里**重载 / 启用**本插件即可。

---

## 命令

| 命令 | 作用 |
|---|---|
| `/学 <网址>` | 抓取并学习，**存起来但不激活**，随后问你要不要激活 |
| `/学` 或 `/学 状态` | 看素材数量、当前激活、注入开关 |
| `/学 列表` | 列出所有已学素材（`★` 标当前激活） |
| `/学 看` | 看当前激活素材的完整风格卡 |
| `/学 用 N` | 切换到第 N 条（直接激活） |
| `/学 关` | 取消激活（素材保留，不再注入） |
| `/学 删 N` | 删除第 N 条 |

### 激活确认

抓取成功后会问一句「要现在激活吗？」，在**同一条会话**里回：

- `是` / `好` / `要` / `对` / `确定` / `激活` / `嗯` / `可以` / `行` → 激活
- `否` / `不` / `不用` / `算了` / `不要` / `不激活` / `不了` / `别` → 保持不激活（素材留着）

确认只在 `confirm_timeout_sec`（默认 300 秒）窗口内、且同会话同一个人应答才生效。平时聊天里说「好」「是」不会误触——只有存在待确认项时插件才会接管这条消息。

指令名「学」注册在 AstrBot 指令系统里，可在 WebUI →「指令管理」中改名 / 禁用 / 调权限。

---

## 工作流程

```
/学 https://xxx/book/123.html
   ├─ 抓取        requests + UA + apparent_encoding 自动编码
   ├─ 正文提取    bs4/lxml，候选容器取最长正文，<200 字回退 body
   ├─ 清洗        去标签 / 短行 / 广告行 / 合并碎行
   ├─ 风格提炼    交当前模型分析，产出条目式风格卡
   ├─ 存库        data/plugin_data/astrbot_plugin_novel_learner/library.json（active 不变）
   └─ 询问        回「是」才激活；回「否」或超时则只保存不生效
```

下一轮对话，插件在 `on_llm_request`（priority `-99998`）把风格卡追加进 `system_prompt`，注入内容形如：

```
【写作风格参考 ·《雪落下来的时候》】
下面是你正在模仿的写法，用它来组织语言与句式；不要复述风格卡本身，也不要提它的来源。
1. 叙事人称……（风格卡正文）
```

---

## 配置

WebUI → 插件管理 → 小说学习器 → 配置：

| 项 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 总开关，关掉后指令与注入都失效 |
| `require_admin` | `true` | 仅管理员可用（判定口径同 AstrBot 内置 shell 工具） |
| `max_chars` | `12000` | 单次抓取正文上限 |
| `inject_enabled` | `true` | 是否把风格卡注入对话 |
| `confirm_timeout_sec` | `300` | 确认激活的有效期（秒） |
| `inject_max_chars` | `800` | 风格卡注入上限，超出截断 |
| `style_prompt` | 内置 | 风格分析提示词，可自行改得更偏你要的维度 |
| `ad_keywords` | 15 个 | 命中即整行丢弃的广告 / 导航关键词 |

---

## 数据存储

```
AstrBot/data/plugin_data/astrbot_plugin_novel_learner/library.json
```

结构：

```json
{
  "items": [{"id": 1, "url": "...", "title": "...", "chars": 8432, "style": "…风格卡…", "created": "11-18 19:40"}],
  "active": 1,
  "next_id": 2,
  "pending": {}
}
```

备份直接拷这个文件即可；整个删掉就是清空学习记录。风格卡**不进**长期记忆，只影响 `system_prompt`。

---

## 限制

- **JS 渲染的站点抓不到**：只处理服务端返回的 HTML。只抓到几十字说明正文在脚本里，需要换站点或另配无头浏览器。
- **有反爬的站点会被拒**：部分站点直接 `ConnectionReset`（远程重置）。换源站，或自行在请求头里补 cookie。
- **分析要 1–2 分钟**：风格提炼是一次完整 LLM 调用，期间会先回两条进度提示。
- **会占用 system 长度**：风格卡默认压到 800 字，多条素材只注入当前激活那一条。

---

## 更新日志

### 1.0.0

- 首个公开版本：`/学` 抓取 + 风格卡提炼 + 多素材管理 + `system_prompt` 注入 + 激活二次确认。

---

## License

[MIT](./LICENSE) © 2026 YG
