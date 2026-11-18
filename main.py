# -*- coding: utf-8 -*-
"""小说学习器 / novel learner

/学 <网址>  抓正文 → 交给当前模型提炼写作风格卡 → 之后每轮对话注入 system_prompt。

只用 AstrBot 自带的 requests + beautifulsoup4 + lxml，不额外装包。
抓取放在线程里跑，不阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

# 优先尝试这些容器，取纯文本最长的那个
CANDIDATE_SELECTORS = (
    "#chaptercontent",
    "#content",
    "#booktext",
    "#htmlContent",
    "#article-content",
    "#chapter-content",
    ".read-content",
    ".article-content",
    ".chapter-content",
    "article",
    "main",
    ".content",
    ".text",
)

DROP_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg", "button")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

STORE_DIR = "astrbot_plugin_novel_learner"
STORE_FILE = "library.json"


@register(
    "astrbot_plugin_novel_learner",
    "YG",
    "抓取网页小说正文，提炼写作风格并注入对话，让 bot 学着那个调子说话",
    "1.0.0",
)
class NovelLearnerPlugin(Star):
    """小说学习器"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self._store_path = ""
        self._state: dict[str, Any] = {"items": [], "active": 0, "next_id": 1, "pending": {}}

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            if self.config is None:
                return default
            value = self.config.get(key, default)
            return default if value is None else value
        except Exception:
            return default

    def _enabled(self) -> bool:
        return bool(self._cfg("enable", True))

    def _umo(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.unified_msg_origin or "")
        except Exception:
            return ""

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            fn = getattr(event, "is_admin", None)
            if callable(fn):
                return bool(fn())
        except Exception:
            pass
        try:
            return str(getattr(event, "role", "")) == "admin"
        except Exception:
            return False

    # ------------------------------------------------------------------ 待确认
    def _pending_get(self, umo: str) -> dict | None:
        self._load()
        pending = self._state.setdefault("pending", {})
        rec = pending.get(umo)
        if not rec:
            return None
        ttl = int(self._cfg("confirm_timeout_sec", 300) or 300)
        try:
            expired = (time.time() - float(rec.get("ts") or 0)) > ttl
        except Exception:
            expired = True
        if expired:
            pending.pop(umo, None)
            self._save()
            return None
        return rec

    def _pending_set(self, umo: str, item_id: int) -> None:
        self._load()
        self._state.setdefault("pending", {})[umo] = {"id": item_id, "ts": time.time()}
        self._save()

    def _pending_clear(self, umo: str) -> None:
        self._load()
        if self._state.setdefault("pending", {}).pop(umo, None) is not None:
            self._save()

    # ------------------------------------------------------------------ 存储
    def _path(self) -> str:
        if self._store_path:
            return self._store_path
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = os.path.join(get_astrbot_data_path(), "plugin_data", STORE_DIR)
        except Exception:
            base = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(base, exist_ok=True)
        self._store_path = os.path.join(base, STORE_FILE)
        return self._store_path

    def _load(self) -> dict[str, Any]:
        # 只在首次读取文件：插件单实例，内存就是真相。
        # 每次调用都重读会把刚改过的 active 覆盖回去（踩过）。
        if getattr(self, "_loaded", False):
            return self._state
        self._loaded = True
        p = self._path()
        if not os.path.exists(p):
            return self._state
        try:
            data = json.load(open(p, encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                self._state = {
                    "items": data.get("items") or [],
                    "active": int(data.get("active") or 0),
                    "next_id": int(data.get("next_id") or (len(data.get("items") or []) + 1)),
                    "pending": data.get("pending") or {},
                }
        except Exception as exc:
            logger.error(f"[小说学习] 读取库失败: {exc}")
        return self._state

    def _save(self) -> None:
        try:
            json.dump(self._state, open(self._path(), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error(f"[小说学习] 保存库失败: {exc}")

    def _active_item(self) -> dict[str, Any] | None:
        self._load()
        active = int(self._state.get("active") or 0)
        for it in self._state["items"]:
            if int(it.get("id") or 0) == active:
                return it
        return None

    # ------------------------------------------------------------------ 抓取
    def _fetch_sync(self, url: str) -> tuple[str, str]:
        if requests is None or BeautifulSoup is None:
            raise RuntimeError("缺少 requests 或 beautifulsoup4，无法抓取")
        resp = requests.get(
            url,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=25,
            allow_redirects=True,
        )
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
        html = resp.text
        return self._extract(html)

    def _extract(self, html: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(DROP_TAGS):
            tag.decompose()

        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        if not title:
            h1 = soup.find("h1")
            title = h1.get_text(strip=True) if h1 else ""

        max_chars = int(self._cfg("max_chars", 12000) or 12000)
        ads = [str(x) for x in (self._cfg("ad_keywords", []) or [])]

        best = ""
        for sel in CANDIDATE_SELECTORS:
            try:
                node = soup.select_one(sel)
            except Exception:
                continue
            if not node:
                continue
            text = self._clean(node.get_text("\n"), ads)
            if len(text) > len(best):
                best = text
            if len(best) >= max_chars:
                break

        if len(best) < 200:  # 候选都没命中就退回整个 body
            body = soup.body or soup
            best = self._clean(body.get_text("\n"), ads)

        return title[:120], best[:max_chars]

    @staticmethod
    def _clean(raw: str, ads: list[str]) -> str:
        lines = []
        for line in raw.replace("\r", "\n").split("\n"):
            # 连续空白压成一个空格：中文正文本来没空格，英文也保得住分词
            line = re.sub(r"[ \t\u3000]+", " ", line).strip()
            if len(line) < 12:
                continue
            if any(k and k in line for k in ads):
                continue
            lines.append(line)
        # 合并过碎的行
        out = []
        buf = ""
        for line in lines:
            if len(line) < 30 and buf and len(buf) < 60:
                buf += line
                continue
            if buf:
                out.append(buf)
                buf = ""
            out.append(line)
        if buf:
            out.append(buf)
        return "\n".join(out)

    # ------------------------------------------------------------------ 学习
    async def _analyze(self, title: str, text: str) -> str:
        provider = None
        for getter in ("get_using_provider_async", "get_using_provider"):
            fn = getattr(self.context, getter, None)
            if not fn:
                continue
            try:
                provider = await fn() if asyncio.iscoroutinefunction(fn) else fn()
            except Exception:
                provider = None
            if provider:
                break
        if provider is None:
            raise RuntimeError("没有可用的模型 provider")

        prompt_tpl = str(self._cfg("style_prompt", "") or "").strip()
        prompt = f"{prompt_tpl}\n\n=== 正文节选（节选自《{title or '未命名'}》）===\n{text}"
        resp = await provider.text_chat(
            prompt=prompt,
            system_prompt="你是一位写作课老师，擅长把别人的文字拆成可执行的写作规则。只输出风格卡。",
        )
        card = str(getattr(resp, "completion_text", "") or "").strip()
        if not card:
            raise RuntimeError("模型没有返回风格卡")
        limit = int(self._cfg("inject_max_chars", 800) or 800)
        return card[:limit]

    # ------------------------------------------------------------------ 注入
    @filter.on_llm_request(priority=-99998)
    async def inject_style(self, event: AstrMessageEvent, req: Any) -> None:
        try:
            if not self._enabled() or not bool(self._cfg("inject_enabled", True)):
                return
            item = self._active_item()
            if not item or not item.get("style"):
                return
            card = str(item["style"]).strip()
            if not card:
                return
            block = (
                f"【写作风格参考 ·《{item.get('title') or '素材'}》】\n"
                f"下面是你正在模仿的写法，用它来组织语言与句式；不要复述风格卡本身，也不要提它的来源。\n{card}"
            )
            original = str(getattr(req, "system_prompt", "") or "")
            if block in original:
                return
            setattr(req, "system_prompt", (original + "\n\n" + block).strip())
            if bool(self._cfg("debug_log", False)):
                logger.info(f"[小说学习] 已注入风格卡 {len(card)} 字（{item.get('title')}）")
        except Exception as exc:
            logger.error(f"[小说学习] 注入失败: {exc}")

    # ------------------------------------------------------------------ 指令
    @filter.command("学")
    async def learn_cmd(self, event: AstrMessageEvent):
        """用法：/学 <网址> | /学 列表 | /学 看 | /学 用 N | /学 关 | /学 删 N | /学 重学"""
        act = ""
        try:
            raw = str(getattr(event, "message_str", "") or "")
            m = re.search(r"学(.*)", raw, flags=re.S)
            if m:
                act = m.group(1).strip()
        except Exception:
            act = ""

        if not self._enabled():
            yield event.plain_result("小说学习器已关闭（配置 enable=false）。")
            return
        if bool(self._cfg("require_admin", True)) and not self._is_admin(event):
            yield event.plain_result("仅管理员可用。")
            return

        low = act.lower()
        self._load()

        # ---- 状态
        if low in ("", "status", "状态"):
            item = self._active_item()
            yield event.plain_result(
                "[小说学习器]\n"
                f"素材数量：{len(self._state['items'])}\n"
                f"当前激活：{('#' + str(item['id']) + ' ' + (item.get('title') or '')) if item else '（无）'}\n"
                f"注入对话：{bool(self._cfg('inject_enabled', True))}\n"
                f"用法：/学 <网址>  ·  /学 列表  ·  /学 看  ·  /学 用 N  ·  /学 关  ·  /学 删 N"
            )
            return

        # ---- 列表
        if low in ("列表", "list", "ls"):
            if not self._state["items"]:
                yield event.plain_result("还没有学过的素材，发 /学 <网址> 开始。")
                return
            lines = ["[已学素材]"]
            for it in self._state["items"]:
                mark = "★" if int(it.get("id") or 0) == int(self._state.get("active") or 0) else " "
                lines.append(
                    f"{mark} #{it.get('id')}  {(it.get('title') or '未命名')[:28]}  "
                    f"{it.get('chars')}字  {it.get('created', '')}"
                )
            lines.append("用 /学 用 N 切换，/学 看 看风格卡")
            yield event.plain_result("\n".join(lines))
            return

        # ---- 看风格卡
        if low in ("看", "show", "详情"):
            item = self._active_item()
            if not item:
                yield event.plain_result("当前没有激活的素材。")
                return
            yield event.plain_result(
                f"[风格卡 ·《{item.get('title') or '未命名'}》]\n来源：{item.get('url')}\n\n{item.get('style') or '（空）'}"
            )
            return

        # ---- 关闭注入
        if low in ("关", "off", "关闭"):
            self._state["active"] = 0
            self._save()
            yield event.plain_result("已取消激活，风格不再注入对话（素材仍保留）。")
            return

        # ---- 切换 / 删除
        m = re.fullmatch(r"(用|use|切换)\s*#?(\d+)", low)
        if m:
            target = int(m.group(2))
            hit = next((it for it in self._state["items"] if int(it.get("id") or 0) == target), None)
            if not hit:
                yield event.plain_result(f"没有 #{target} 这条素材，/学 列表 看看。")
                return
            self._state["active"] = target
            self._save()
            yield event.plain_result(f"已切换到 #{target}《{hit.get('title') or '未命名'}》，下一轮对话开始生效。")
            return

        m = re.fullmatch(r"(删|del|删除)\s*#?(\d+)", low)
        if m:
            target = int(m.group(2))
            before = len(self._state["items"])
            self._state["items"] = [it for it in self._state["items"] if int(it.get("id") or 0) != target]
            if int(self._state.get("active") or 0) == target:
                self._state["active"] = 0
            self._save()
            yield event.plain_result(f"已删除 #{target}（原有 {before} 条，现 {len(self._state['items'])} 条）。")
            return

        # ---- 抓取学习
        url = act
        if not re.match(r"^https?://", url, re.I):
            yield event.plain_result("给我一个完整的 http/https 网址，比如 /学 https://example.com/book/123.html")
            return
        host = urlparse(url).netloc
        yield event.plain_result(f"开始抓取 {host} …")

        try:
            title, text = await asyncio.to_thread(self._fetch_sync, url)
        except Exception as exc:
            yield event.plain_result(f"抓取失败：{exc}")
            return

        if len(text) < 200:
            yield event.plain_result(f"只抓到 {len(text)} 字，正文可能没解析出来（页面是 JS 渲染或需要登录）。")
            return

        yield event.plain_result(f"抓到《{title or '未命名'}》{len(text)} 字，正在提取风格 …")

        try:
            card = await self._analyze(title, text)
        except Exception as exc:
            yield event.plain_result(f"风格分析失败：{exc}")
            return

        item_id = int(self._state.get("next_id") or 1)
        self._state["items"].append(
            {
                "id": item_id,
                "url": url,
                "title": title or "未命名",
                "chars": len(text),
                "style": card,
                "created": time.strftime("%m-%d %H:%M"),
            }
        )
        self._state["next_id"] = item_id + 1
        self._save()
        # 不自动激活：先挂成待确认，等管理员回一句
        self._pending_set(self._umo(event), item_id)

        preview = card[:600] + ("…" if len(card) > 600 else "")
        yield event.plain_result(
            f"学完了，存为 #{item_id}《{title or '未命名'}》，**还没激活**。\n\n{preview}\n\n"
            f"要现在激活吗？回「是」就套用，回「否」先放着。"
            f"（也可以稍后 /学 用 {item_id} 再启用，/学 删 {item_id} 删掉）"
        )

    @filter.regex(
        r"^(是|好|要|对|确定|激活|嗯|可以|行|否|不|不用|算了|不要|不激活|不了|别)[。.!！~]*$",
        priority=100,
    )
    async def confirm_activation(self, event: AstrMessageEvent):
        """抓取完成后等管理员回「是/否」，只在这个窗口内生效，不干扰正常聊天。"""
        if not self._enabled():
            return
        umo = self._umo(event)
        rec = self._pending_get(umo)
        if not rec:
            return
        if bool(self._cfg("require_admin", True)) and not self._is_admin(event):
            return

        item_id = int(rec.get("id") or 0)
        self._load()
        item = next((it for it in self._state["items"] if int(it.get("id") or 0) == item_id), None)
        if item is None:
            self._pending_clear(umo)
            try:
                event.stop_event()
            except Exception:
                pass
            yield event.plain_result("那条素材已经不在了，跳过激活。")
            return

        raw = str(getattr(event, "message_str", "") or "").strip()
        yes = raw[:1] in "是好要对确定激活嗯可以行"
        try:
            event.stop_event()
        except Exception:
            pass

        if yes:
            self._state["active"] = item_id
            self._pending_clear(umo)
            self._save()
            yield event.plain_result(
                f"已激活 #{item_id}《{item.get('title') or '未命名'}》，下一轮对话开始套用这套写法。"
            )
        else:
            self._pending_clear(umo)
            yield event.plain_result(
                f"好，先不激活 #{item_id}（素材留着，/学 用 {item_id} 随时启用）。"
            )

    async def terminate(self):
        self._state = {"items": [], "active": 0, "next_id": 1}
