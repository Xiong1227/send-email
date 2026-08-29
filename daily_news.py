#!/usr/bin/env python3
"""
每日新闻简报自动化脚本
流程：RSS抓取 → 网页抓取内容 → 合并 → AI统一分类整理 → 生成HTML邮件 → 发送
"""

import argparse
import os
import re
import sys
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Semaphore
from urllib.parse import urlparse
import feedparser
import httpx
import markdown
import requests
from bs4 import BeautifulSoup
import yaml
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import format_datetime, make_msgid
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

# ==================== 加载配置 ====================

SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

with open(SCRIPT_DIR / "config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

log = logging.getLogger("daily_news")
SCRAPE_FAIL_PLACEHOLDER = "[内容抓取失败，请点击原文链接查看]"
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
RSS_HEADERS = {
    **HTTP_HEADERS,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}
SKIP_SCRAPE_HOSTS = (
    "nytimes.com",
    "www.nytimes.com",
)
TZ_CN = ZoneInfo("Asia/Shanghai")


def now_cn() -> datetime:
    """统一用北京时间，避免 GitHub Actions 运行机（UTC）把标题写成差 8 小时。"""
    return datetime.now(TZ_CN)


def setup_logging() -> Path:
    """同时输出到终端和 logs/ 文件。只用本模块的 logger，避免第三方库把正文打进日志。"""
    log_dir = SCRIPT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    stamp = now_cn().strftime("%Y-%m-%d_%H%M%S")
    log_path = log_dir / f"run_{stamp}.log"

    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.propagate = False

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    log.addHandler(file_handler)
    log.addHandler(console_handler)
    return log_path


def require_env(name: str) -> str:
    """读取必需的环境变量，缺失时给出明确错误。"""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}，请在项目根目录的 .env 中配置（可参考 .env.example）")
    return value


def get_llm_config():
    """读取当前启用的 LLM 厂商配置（OpenAI 兼容协议）。"""
    llm_cfg = config.get("llm") or {}
    name = (llm_cfg.get("provider") or "agnes").strip().lower()
    providers = llm_cfg.get("providers") or {}
    if name not in providers:
        available = ", ".join(providers.keys()) or "(无)"
        raise RuntimeError(f"未知 LLM 厂商: {name}，可选: {available}")

    provider_cfg = providers[name]
    for key in ("model", "base_url", "api_key_env"):
        if not provider_cfg.get(key):
            raise RuntimeError(f"llm.providers.{name} 缺少字段: {key}")

    return {
        "name": name,
        "model": provider_cfg["model"],
        "base_url": provider_cfg["base_url"],
        "api_key": require_env(provider_cfg["api_key_env"]),
    }


def create_llm_client(llm):
    """创建 OpenAI 兼容客户端。加长连接超时：走代理时 SSL 握手经常超过默认 5 秒。"""
    return OpenAI(
        api_key=llm["api_key"],
        base_url=llm["base_url"],
        timeout=httpx.Timeout(180.0, connect=45.0, read=180.0, write=45.0, pool=45.0),
        max_retries=0,
    )


# ==================== 第1步：抓取RSS ====================

def _entry_summary(entry) -> str:
    """从 RSS 条目里抽出摘要/正文片段，网页抓取失败时仍能给模型材料。"""
    chunks = []
    summary = (entry.get("summary") or entry.get("description") or "").strip()
    if summary:
        chunks.append(summary)
    for item in entry.get("content") or []:
        value = (item.get("value") or "").strip()
        if value:
            chunks.append(value)
    if not chunks:
        return ""
    soup = BeautifulSoup("\n".join(chunks), "html.parser")
    lines = [line.strip() for line in soup.get_text(separator="\n", strip=True).split("\n") if line.strip()]
    return "\n".join(lines)[:3000]


def _download_feed(feed_url: str, retries: int = 2):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(feed_url, headers=RSS_HEADERS, timeout=15)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if parsed.entries:
                return parsed
            last_error = getattr(parsed, "bozo_exception", None) or "未解析到条目"
        except Exception as e:
            last_error = e
        log.warning(f"   第 {attempt}/{retries} 次未拿到有效条目，将重试: {last_error}")
    raise RuntimeError(last_error)


def _concurrency() -> dict:
    cfg = config.get("concurrency") or {}
    return {
        "rss_feeds": max(1, int(cfg.get("rss_feeds") or 4)),
        "articles": max(1, int(cfg.get("articles") or 6)),
        "per_host": max(1, int(cfg.get("per_host") or 2)),
    }


_host_sema_lock = Lock()
_host_semas: dict[str, Semaphore] = {}


def _host_sema(host: str) -> Semaphore:
    limit = _concurrency()["per_host"]
    key = host or "_"
    with _host_sema_lock:
        if key not in _host_semas:
            _host_semas[key] = Semaphore(limit)
        return _host_semas[key]


def _fetch_one_feed(feed_url: str) -> list:
    """拉一个 RSS 源，供线程池调用。"""
    log.info(f"📡 抓取 RSS: {feed_url}")
    feed = _download_feed(feed_url)
    if getattr(feed, "bozo", False) and feed.bozo_exception:
        log.warning(f"   RSS 解析有告警，已尽量继续: {feed_url} — {feed.bozo_exception}")
    taken = feed.entries[: config["max_articles"]]
    log.info(f"   本源入选 {len(taken)} 篇: {feed_url}")
    articles = []
    for entry in taken:
        articles.append({
            "title": entry.get("title", "无标题"),
            "link": entry.get("link", ""),
            "published": entry.get("published", ""),
            "source": feed.feed.get("title", feed_url),
            "summary": _entry_summary(entry),
        })
    return articles


def fetch_rss():
    """并行抓取各 RSS 源的文章元数据。"""
    urls = list(config["rss_feeds"])
    workers = min(_concurrency()["rss_feeds"], max(1, len(urls)))
    log.info(f"📡 并行拉取 RSS：{len(urls)} 个源，最多 {workers} 路同时进行")
    collected: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one_feed, url): url for url in urls}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                collected[url] = fut.result()
            except Exception as e:
                log.warning(f"   RSS 请求失败，跳过该源: {url} — {e}")
                collected[url] = []

    articles = []
    for url in urls:
        articles.extend(collected.get(url) or [])
    log.info(f"✅ 共抓取 {len(articles)} 篇文章（仅记录标题、链接与 RSS 摘要，不记录网页正文）")
    return articles


# ==================== 第2步：抓取网页内容 ====================

def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def scrape_article(url, timeout=15, retries: int = 2):
    """抓取单篇文章的文本内容。超时或被拒时重试，仍失败则返回空串由上层用 RSS 摘要兜底。"""
    last_error = None
    html_headers = {**HTTP_HEADERS, "Accept": "text/html,application/xhtml+xml;q=0.9"}
    host = _host_of(url)
    sema = _host_sema(host)
    with sema:
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(url, headers=html_headers, timeout=timeout)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    tag.decompose()
                lines = [line.strip() for line in soup.get_text(separator="\n", strip=True).split("\n") if line.strip()]
                return "\n".join(lines)[:3000]
            except Exception as e:
                last_error = e
                if attempt < retries:
                    log.warning(f"   网页抓取第 {attempt} 次失败，重试: {e}")
    log.warning(f"⚠️ 网页抓取失败: {url} — {last_error}")
    return ""


def _resolve_article_content(index: int, total: int, article: dict) -> str:
    """处理单篇：网页正文优先，否则 RSS 摘要。供线程池调用。"""
    title = (article.get("title") or "")[:50]
    log.info(f"🔍 [{index}/{total}] 抓取: {title}...")
    summary = (article.get("summary") or "").strip()
    host = _host_of(article.get("link") or "")
    skip_page = any(host == h or host.endswith("." + h) for h in SKIP_SCRAPE_HOSTS)

    content = ""
    if skip_page:
        log.info(f"   [{index}] 该站点常拦截正文抓取，改用 RSS 摘要")
    else:
        content = scrape_article(article.get("link") or "")

    if content:
        log.info(f"   [{index}] 网页正文成功（约 {len(content)} 字，内容不写入日志）")
        return content
    if summary:
        log.info(f"   [{index}] 使用 RSS 摘要（约 {len(summary)} 字，内容不写入日志）")
        return f"[以下为 RSS 摘要，非全文]\n{summary}"
    log.warning(f"   [{index}] 网页与 RSS 摘要都没有正文")
    return SCRAPE_FAIL_PLACEHOLDER


def scrape_all(articles):
    """并行抓取网页正文；失败或已知反爬站点改用 RSS 摘要。"""
    total = len(articles)
    if total == 0:
        return []

    workers = min(_concurrency()["articles"], total)
    per_host = _concurrency()["per_host"]
    log.info(f"🔍 并行抓取正文：{total} 篇，最多 {workers} 路同时进行（同站最多 {per_host} 路）")

    content_list = [""] * total
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_resolve_article_content, i + 1, total, article): i
            for i, article in enumerate(articles)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                content_list[i] = fut.result()
            except Exception as e:
                log.warning(f"   [{i + 1}] 并行任务异常: {e}")
                content_list[i] = SCRAPE_FAIL_PLACEHOLDER

    page_ok = rss_ok = empty = 0
    for content in content_list:
        if content.startswith("[以下为 RSS 摘要"):
            rss_ok += 1
        elif content == SCRAPE_FAIL_PLACEHOLDER:
            empty += 1
        else:
            page_ok += 1
    log.info(f"✅ 正文准备完成：网页 {page_ok}，RSS 摘要 {rss_ok}，空缺 {empty}")
    return content_list


# ==================== 第3步：AI 统一分类整理 ====================

SYSTEM_PROMPT = """你是一个专业的新闻编辑。用户会给你一批新闻文章的列表（标题 + 正文内容）。

你的任务是对这些新闻进行**全局统一分类**，并生成一份排版精美的新闻简报。

## 分类规则
先把所有新闻通读一遍，再按以下类别归类：
- 🌍 国际
- 🇨🇳 国内
- 🤖 科技 & AI
- 📈 市场
- ⚖️ 政策 & 监管

如果某条新闻不属于以上类别但值得关注，归入「💡 其他」。

## 输出格式要求（严格遵守）

### 板块标题格式
每个板块用 `## 🌍 国际 · 关键词` 格式，竖线后面是该板块的1-3个核心关键词。

### 板块摘要
每个 H2 板块标题下方紧跟一个 `> **一句话要点**：` 引用块，用一句话概括本板块的核心看点。

### 内容表格
板块摘要下方用表格呈现每条新闻：

| 焦点 | 摘要 |
|------|------|
| **事件标题** | 2-3句话概述，包含关键数字/人名/地名/时间节点。数字用 **粗体** 突出。 |

### 今日数据
所有板块之后加 `## 📌 今日数据`，用表格列出本期关键数字：

| 指标 | 数值 |
|------|------|
| 关键数字1 | 数值及含义 |

### 参考链接
最后加 `## 🔗 参考链接`，列出所有引用的来源链接：

- [来源名：文章标题](URL)

### 标签行
文末固定一行：`#热点 #每日资讯 #国际 #国内 #科技 #市场`

## 格式示例

```
# YYYY-MM-DD 热点资讯

> 📅 整理时间：YYYY-MM-DD HH:MM
> 📡 来源：RSS 聚合公开报道
> 🏷️ 标签：#热点 #每日资讯

## 🌍 国际 · 美伊谈判 + 油价

> **一句话要点**：美伊技术谈判将于近期恢复，国际油价大幅回落。

| 焦点 | 摘要 |
|------|------|
| **美伊技术谈判将恢复** | 美国国务卿鲁比奥表示，美伊技术团队将于6月30日在瑞士继续会谈，由核能、解除制裁等领域专家组成的多个工作组将展开磋商。 |
| **国际油价跌破战前水平** | 布伦特原油跌破每桶 **76美元** 关口，回落至伊朗战争爆发前水平；WTI原油收于 **70.34美元/桶**，跌3.92%。 |

## 📌 今日数据

| 指标 | 数值 |
|------|------|
| 布伦特原油 | **73.74美元/桶**（-4.33%） |
| 关键数字2 | 数值及含义 |

## 🔗 参考链接

- [来源名：文章标题](https://...)
- [来源名：文章标题](https://...)

#热点 #每日资讯 #国际 #国内 #科技 #市场
```

## 重要提醒
- 同一类别的新闻必须合并到同一个 ## 标题下，用表格呈现
- 不要用 ### 三级标题逐条列出新闻
- 表格中「焦点」列放加粗的事件标题，「摘要」列放2-3句概述
- 关键数字用 **粗体** 突出（金额、百分比、时间、伤亡等）
- 如果某个类别没有新闻，就不要写那个类别
- 每篇文章的原文链接必须出现在「参考链接」板块
- 信息量不足的类别可以省略，不要强行凑数
- 语言简洁客观，不写评论性语句
- 「整理时间」必须使用用户消息里给出的北京时间，不要自己编造或换算时区
- 直接输出 Markdown 正文，不要用 ```markdown 代码块包住全文
"""


def ai_classify(articles, contents):
    """把所有文章内容合并，一次性发给 AI 做全局分类"""
    # 拼接所有文章
    combined = ""
    for i, (article, content) in enumerate(zip(articles, contents)):
        combined += f"""
══════════════════════════════════════
【文章 {i+1}】
标题：{article['title']}
来源：{article['link']}
发布时间：{article['published']}
正文：
{content}
══════════════════════════════════════
"""

    now = now_cn()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today_str = now.strftime("%Y-%m-%d")
    user_content = (
        f"当前北京时间：{now_str}\n"
        f"标题请写成「# {today_str} 热点资讯」。\n"
        f"整理时间必须原样写成：{now_str}\n\n"
        f"请对以下 {len(articles)} 篇新闻进行统一分类整理，生成新闻简报：\n\n"
        f"{combined}"
    )

    llm = get_llm_config()
    client = create_llm_client(llm)
    last_error = None
    for attempt in range(1, 4):
        log.info(
            f"🤖 正在调用 {llm['name']} ({llm['model']}) 进行全局分类整理..."
            f"（共 {len(articles)} 篇，第 {attempt}/3 次；提示词与正文不写入日志）"
        )
        try:
            response = client.chat.completions.create(
                model=llm["model"],
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.7,
                max_tokens=8000,
            )
            result = response.choices[0].message.content or ""
            result = _force_briefing_time(result, today_str, now_str)
            result = _inject_word_count(result)
            log.info(
                f"✅ AI 整理完成（provider={llm['name']}，输出约 {len(result)} 字，内容不写入日志）"
            )
            return result
        except (APITimeoutError, APIConnectionError, APIStatusError) as e:
            last_error = e
            log.warning(f"   {llm['name']} 第 {attempt} 次失败: {e}")
            time.sleep(3 * attempt)

    raise RuntimeError(f"调用 {llm['name']} 失败: {last_error}") from last_error


def _force_briefing_time(md_text: str, today_str: str, now_str: str) -> str:
    """避免模型把整理时间写成 UTC 或随便填一个整点。"""
    md_text = re.sub(
        r"(整理时间[：:]\s*)([^\n]+)",
        rf"\g<1>{now_str}",
        md_text,
        count=1,
    )
    md_text = re.sub(
        r"^#\s+\d{4}-\d{2}-\d{2}\s+热点资讯",
        f"# {today_str} 热点资讯",
        md_text,
        count=1,
        flags=re.M,
    )
    return md_text


def _plain_char_count(md_text: str) -> int:
    """统计阅读用字数：去掉链接地址和标记符号，按可见文字计数。"""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md_text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[#>*`|_\-\[\]()]+", "", text)
    text = re.sub(r"\s+", "", text)
    return len(text)


def _inject_word_count(md_text: str) -> str:
    """在整理时间下方插入篇幅，方便打开邮件前感知阅读量。"""
    chars = _plain_char_count(md_text)
    minutes = max(1, round(chars / 400))
    line = f"> 📝 篇幅：约 {chars} 字 · 阅读约 {minutes} 分钟"
    if re.search(r"整理时间", md_text):
        return re.sub(
            r"(整理时间[：:][^\n]+\n)",
            rf"\1{line}\n",
            md_text,
            count=1,
        )
    return line + "\n\n" + md_text


# ==================== 第4步：Markdown → HTML ====================

def _unwrap_markdown_fence(md_text: str) -> str:
    """模型偶尔用 ``` 包住全文，转 HTML 后会变成代码块，QQ 里就像没排版。"""
    text = (md_text or "").strip()
    if not text.startswith("```"):
        return text
    text = re.sub(r"^```(?:markdown|md)?\s*\n", "", text, count=1, flags=re.I)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def _inline_email_styles(html_body: str) -> str:
    """QQ 等客户端常丢掉 <head> 里的 CSS，给表格和标题补上内联样式。"""
    soup = BeautifulSoup(html_body, "html.parser")
    styles = {
        "h1": "font-size:22px;color:#1a1a1a;margin:0 0 12px;",
        "h2": "font-size:20px;color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:8px;margin-top:28px;",
        "blockquote": "border-left:3px solid #3498db;padding-left:16px;margin:12px 0;color:#555;",
        "table": "width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;",
        "th": "background:#2c3e50;color:#ffffff;padding:10px 12px;text-align:left;",
        "td": "padding:10px 12px;border-bottom:1px solid #eeeeee;vertical-align:top;",
        "a": "color:#2980b9;text-decoration:none;",
    }
    for tag, style in styles.items():
        for el in soup.find_all(tag):
            prev = el.get("style") or ""
            el["style"] = f"{prev}{style}".strip()
    return str(soup)


def markdown_to_html(md_text):
    """将 Markdown 转为适合邮件的 HTML"""
    md_text = _unwrap_markdown_fence(md_text)
    html_body = markdown.markdown(
        md_text,
        extensions=["extra", "tables", "nl2br", "sane_lists"],
    )
    html_body = _inline_email_styles(html_body)

    # 嵌入美观的邮件样式
    html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{
    max-width: 680px;
    margin: 0 auto;
    padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    font-size: 15px;
    line-height: 1.7;
    color: #1a1a1a;
    background: #fafafa;
  }}
  .container {{
    background: #fff;
    border-radius: 8px;
    padding: 32px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  }}
  h2 {{
    font-size: 20px;
    color: #2c3e50;
    border-bottom: 2px solid #3498db;
    padding-bottom: 8px;
    margin-top: 32px;
  }}
  h3 {{
    font-size: 16px;
    color: #34495e;
    margin-top: 20px;
  }}
  ul {{
    padding-left: 20px;
  }}
  li {{
    margin-bottom: 4px;
  }}
  a {{
    color: #2980b9;
    text-decoration: none;
  }}
  a:hover {{
    text-decoration: underline;
  }}
  .footer {{
    margin-top: 40px;
    padding-top: 16px;
    border-top: 1px solid #eee;
    color: #999;
    font-size: 12px;
    text-align: center;
  }}
  blockquote {{
    border-left: 3px solid #3498db;
    padding-left: 16px;
    margin: 12px 0;
    color: #555;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
    font-size: 14px;
  }}
  th {{
    background: #2c3e50;
    color: #fff;
    padding: 10px 12px;
    text-align: left;
  }}
  td {{
    padding: 10px 12px;
    border-bottom: 1px solid #eee;
    vertical-align: top;
  }}
  tr:hover td {{
    background: #f8f9fa;
  }}
  code {{
    background: #f0f0f0;
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 13px;
  }}
</style>
</head>
<body>
<div class="container">
{html_body}
</div>
<div class="footer">
  📬 本简报由 AI 自动生成 · {now_cn().strftime('%Y-%m-%d %H:%M')} · 仅供参考不构成任何建议
</div>
</body>
</html>
"""
    return html


# ==================== 第5步：发送邮件 ====================

SMTP_TIMEOUT_SEC = 20


def _smtp_send(host, port, mode, from_email, password, to_email, raw_message):
    """mode: ssl（465）或 starttls（587）。"""
    if mode == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SEC) as server:
            server.login(from_email, password)
            server.sendmail(from_email, to_email, raw_message)
        return
    with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SEC) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(from_email, password)
        server.sendmail(from_email, to_email, raw_message)


def send_email(html_content):
    """通过 SMTP 发送 HTML 邮件（账号密码从 .env 读取）。

    部分网络上 QQ 的 465/SSL 会握手超时，这时自动改走 587/STARTTLS。
    """
    email_cfg = config["email"]
    from_email = require_env("EMAIL_FROM")
    to_email = require_env("EMAIL_TO")
    password = require_env("EMAIL_PASSWORD")
    smtp_host = os.getenv("SMTP_HOST", "").strip() or "smtp.qq.com"
    smtp_port = int(os.getenv("SMTP_PORT", "").strip() or "587")
    now_str = now_cn().strftime("%Y-%m-%d %H:%M")

    msg = MIMEMultipart("alternative")
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Date"] = format_datetime(now_cn())
    msg["Message-ID"] = make_msgid()
    msg["Subject"] = f'{email_cfg["subject"]} — {now_str}'
    msg.attach(MIMEText("请使用支持 HTML 的邮箱查看每日新闻简报。", "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    raw_message = msg.as_string()

    if smtp_port == 465:
        attempts = [(465, "ssl"), (587, "starttls")]
    elif smtp_port == 587:
        attempts = [(587, "starttls"), (465, "ssl")]
    else:
        attempts = [(smtp_port, "starttls")]

    last_error = None
    for port, mode in attempts:
        for try_n in range(1, 3):
            try:
                log.info(f"📧 发送邮件: {from_email} → {to_email}（{smtp_host}:{port} / {mode}，第 {try_n} 次）")
                _smtp_send(smtp_host, port, mode, from_email, password, to_email, raw_message)
                log.info("✅ 邮件发送成功！")
                return
            except Exception as e:
                last_error = e
                log.warning(f"   {smtp_host}:{port} 第 {try_n} 次失败: {e}")

    log.error(f"❌ 邮件发送失败: {last_error}")
    raise last_error


def resend_latest():
    """只重发最近一份已生成的 HTML，不再抓取、不再调用模型。"""
    output_dir = SCRIPT_DIR / "output"
    htmls = sorted(output_dir.glob("news_*.html"))
    if not htmls:
        raise RuntimeError("output/ 下没有可重发的 HTML，请先完整跑一次")
    latest = htmls[-1]
    log.info(f"📤 仅重发已有简报: {latest}")
    send_email(latest.read_text(encoding="utf-8"))


def check_setup() -> None:
    """不抓取、不调模型、不发信：只确认依赖导入与关键环境变量是否齐全。"""
    log.info("🔎 自检：依赖与配置（不会发信）")
    log.info(f"   Python: {sys.version.split()[0]} @ {sys.executable}")
    log.info(f"   httpx: {httpx.__version__}")
    log.info(f"   openai SDK 已导入；RSS 源数量: {len(config.get('rss_feeds') or [])}")

    llm = get_llm_config()
    log.info(f"   LLM: provider={llm['name']} model={llm['model']}")
    for name in ("EMAIL_FROM", "EMAIL_TO", "EMAIL_PASSWORD"):
        require_env(name)
    smtp_host = os.getenv("SMTP_HOST", "").strip() or "smtp.qq.com"
    smtp_port = os.getenv("SMTP_PORT", "").strip() or "587"
    log.info(f"   SMTP: {smtp_host}:{smtp_port}（账号已配置，不打印密钥）")
    log.info("✅ 自检通过。完整跑一次请去掉 --check；仅重发用 --resend。")


# ==================== 主流程 ====================

def run():
    """一次完整的抓取 → 整理 → 发信。"""
    articles = fetch_rss()
    if not articles:
        log.error("❌ 没有抓取到任何文章，退出")
        return

    contents = scrape_all(articles)
    markdown_result = ai_classify(articles, contents)

    today_str = now_cn().strftime("%Y-%m-%d")
    output_dir = SCRIPT_DIR / "output"
    output_dir.mkdir(exist_ok=True)

    md_path = output_dir / f"news_{today_str}.md"
    md_path.write_text(markdown_result, encoding="utf-8")
    log.info(f"💾 Markdown 已保存: {md_path}")

    html_content = markdown_to_html(markdown_result)
    html_path = output_dir / f"news_{today_str}.html"
    html_path.write_text(html_content, encoding="utf-8")
    log.info(f"💾 HTML 已保存: {html_path}")

    send_email(html_content)
    log.info("=" * 60)
    log.info("🎉 完成！")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每日新闻简报：RSS → LLM → 邮件")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="只检查依赖与环境变量，不抓取、不调模型、不发信",
    )
    mode.add_argument(
        "--resend",
        action="store_true",
        help="只重发 output/ 里最近一份 HTML，不再抓取、不再调用模型",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None):
    args = parse_args(argv)
    log_path = setup_logging()
    log.info("=" * 60)
    log.info(f"📰 每日新闻简报 — {now_cn().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）")
    log.info(f"📄 日志文件: {log_path}")
    log.info("=" * 60)
    try:
        if args.check:
            check_setup()
        elif args.resend:
            resend_latest()
        else:
            run()
    except Exception:
        log.exception("❌ 运行失败")
        raise


if __name__ == "__main__":
    main()
