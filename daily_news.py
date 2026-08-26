#!/usr/bin/env python3
"""
每日新闻简报自动化脚本
流程：RSS抓取 → 网页抓取内容 → 合并 → AI统一分类整理 → 生成HTML邮件 → 发送
"""

import os
import re
import sys
import logging
import feedparser
import requests
from bs4 import BeautifulSoup
import yaml
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

# ==================== 加载配置 ====================

SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

with open(SCRIPT_DIR / "config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

log = logging.getLogger("daily_news")
SCRAPE_FAIL_PLACEHOLDER = "[内容抓取失败，请点击原文链接查看]"
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
    provider = (llm_cfg.get("provider") or "agnes").strip().lower()
    providers = llm_cfg.get("providers") or {}
    if provider not in providers:
        available = ", ".join(providers.keys()) or "(无)"
        raise RuntimeError(f"未知 LLM 厂商: {provider}，可选: {available}")

    provider_cfg = providers[provider]
    for key in ("model", "base_url", "api_key_env"):
        if not provider_cfg.get(key):
            raise RuntimeError(f"llm.providers.{provider} 缺少字段: {key}")

    return {
        "name": provider,
        "model": provider_cfg["model"],
        "base_url": provider_cfg["base_url"],
        "api_key": require_env(provider_cfg["api_key_env"]),
    }


def create_llm_client(llm):
    """创建 OpenAI 兼容客户端。"""
    return OpenAI(api_key=llm["api_key"], base_url=llm["base_url"])


# ==================== 第1步：抓取RSS ====================

def _briefing_cfg():
    return config.get("briefing") or {}


def parse_published(entry, published_str):
    """把 RSS 时间转成北京时间。解析失败返回 None。"""
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc).astimezone(TZ_CN)
        except Exception:
            pass
    if published_str:
        try:
            dt = parsedate_to_datetime(published_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(TZ_CN)
        except Exception:
            pass
    return None


def freshness_label(published_dt, now):
    """给人看的时效：3小时前 / 2天前。"""
    if published_dt is None:
        return "时间未知"
    seconds = (now - published_dt).total_seconds()
    if seconds < 0:
        return "刚刚"
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)}小时前"
    return f"{int(seconds // 86400)}天前"


def score_article(article):
    """关键词 + 新鲜度打分，用于选出速览和精选。"""
    title = article.get("title") or ""
    score = 0
    for kw in _briefing_cfg().get("weight_keywords") or []:
        if kw and kw.lower() in title.lower():
            score += 2
    hours = article.get("hours_ago")
    if hours is not None:
        if hours <= 6:
            score += 3
        elif hours <= 12:
            score += 2
        elif hours <= 24:
            score += 1
        else:
            score -= 1
    if re.search(r"\d", title):
        score += 1
    return score


def enrich_articles(articles):
    """补上时效、权重，并按分数排序（高价值在前）。"""
    now = now_cn()
    fresh_hours = float(_briefing_cfg().get("fresh_hours") or 12)
    for article in articles:
        published_dt = article.get("published_dt")
        article["freshness"] = freshness_label(published_dt, now)
        if published_dt is None:
            article["hours_ago"] = None
            article["is_fresh"] = False
        else:
            hours = (now - published_dt).total_seconds() / 3600
            article["hours_ago"] = hours
            article["is_fresh"] = 0 <= hours <= fresh_hours
        article["score"] = score_article(article)

    articles.sort(key=lambda a: a.get("score", 0), reverse=True)
    fresh_count = sum(1 for a in articles if a.get("is_fresh"))
    min_fresh = int(_briefing_cfg().get("min_fresh_articles") or 5)
    low_activity = fresh_count < min_fresh
    log.info(
        f"📊 时效：过去 {int(fresh_hours)} 小时内新鲜 {fresh_count}/{len(articles)} 篇"
        f"{'（低活跃）' if low_activity else ''}"
    )
    return articles, low_activity, fresh_count


def fetch_rss():
    """抓取所有RSS源的文章元数据；缺标题或链接的条目直接跳过。"""
    articles = []
    for feed_url in config["rss_feeds"]:
        log.info(f"📡 抓取 RSS: {feed_url}")
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            log.warning(f"   RSS 请求失败，跳过该源: {feed_url} — {e}")
            continue

        if getattr(feed, "bozo", False) and feed.bozo_exception:
            log.warning(f"   RSS 解析异常，已尽量继续: {feed_url} — {feed.bozo_exception}")

        taken = 0
        for entry in feed.entries[: config["max_articles"]]:
            title = (entry.get("title") or "").strip() or "无标题"
            link = (entry.get("link") or "").strip()
            if title == "无标题" or not link:
                log.warning("   跳过无标题或无链接的条目")
                continue
            published_str = entry.get("published") or entry.get("updated") or ""
            articles.append({
                "title": title,
                "link": link,
                "published": published_str,
                "published_dt": parse_published(entry, published_str),
                "source": feed.feed.get("title", feed_url),
            })
            taken += 1
        log.info(f"   本源入选 {taken} 篇")
    log.info(f"✅ 共抓取 {len(articles)} 篇文章（仅记录标题与链接，不记录正文）")
    return articles


# ==================== 第2步：抓取网页内容 ====================

def scrape_article(url, timeout=15):
    """抓取单篇文章的文本内容（替代 FireCrawl）"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 移除无用元素
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # 取正文文本，限制长度
        text = soup.get_text(separator="\n", strip=True)
        # 清理多余空行
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return "\n".join(lines)[:3000]  # 每篇文章最多3000字
    except Exception as e:
        log.warning(f"⚠️ 抓取失败，使用摘要代替: {url} — {e}")
        return SCRAPE_FAIL_PLACEHOLDER


def scrape_all(articles):
    """批量抓取网页内容（正文只用于后续整理，不写入日志）"""
    content_list = []
    for i, article in enumerate(articles):
        log.info(f"🔍 [{i+1}/{len(articles)}] 抓取: {article['title'][:50]}...")
        content = scrape_article(article["link"])
        if content != SCRAPE_FAIL_PLACEHOLDER:
            log.info(f"   抓取成功（约 {len(content)} 字，正文不写入日志）")
        content_list.append(content)

    failed = sum(1 for c in content_list if c == SCRAPE_FAIL_PLACEHOLDER)
    log.info(f"✅ 正文抓取完成：成功 {len(content_list) - failed}，失败 {failed}")
    return content_list


# ==================== 第3步：AI 统一分类整理 ====================

SYSTEM_PROMPT = """你是一个专业的新闻编辑。用户会给你一批新闻（标题、链接、时效、权重分、正文）。

按「速览 → 精选 → 快讯」三层输出一份简报，让人可以 30 秒扫完，也可以往下细读。

## 分层规则
1. **📌 30秒速览**：只放 3 条「今日必知」。优先用用户给出的高权重、较新的条目。每条一行，带时效。
2. **📊 今日关键数据**：从正文里抽出最多 3 个变化最明显的数字，放在速览正下方（不要放到文末）。
3. **🔥 深度精选**：只展开 2～3 条最重要的，用表格写 2～3 句摘要。不要把所有新闻都写成详版。
4. **⚡ 快讯速览**：其余全部压成「一句话」。标题做成 Markdown 链接，不要在正文里堆裸 URL。
5. **🔗 参考来源**：文末只保留一句说明「点击标题可打开原文」，不要再列一长串链接。
6. **标签**：只放在全文最后一行，不要出现在标题下方。

时效必须使用用户提供的「3小时前 / 2天前」原文，禁止自己编时间或换算时区。
整理时间必须使用用户给出的北京时间。
标题必须做成 [标题](链接)，方便点进原文。
不要用分类（国际/国内/科技）当一级结构；分类如需出现，写在快讯那一行的末尾括号里即可。
语言简洁客观，不写评论。没有某类内容就省略，不要凑数。
若用户标明「低活跃」，在元信息里保留该提示，不要假装资讯很多。

## 输出格式（严格遵守）

```
# YYYY-MM-DD 热点简报

> 📅 整理时间：YYYY-MM-DD HH:MM
> 📡 来源：RSS 聚合公开报道

## 📌 30秒速览

- **[3小时前]** [小米折叠屏将首发新芯片](https://example.com)：9 月上市，主打轻薄。
- **[1小时前]** [美 30 年期国债收益率创阶段新高](https://example.com)：突破 **5.31%**。
- **[5小时前]** [加拿大宣布对美商品加征关税](https://example.com)：涉及约 **200 亿美元**。

## 📊 今日关键数据

| 指标 | 数值 |
|------|------|
| 美 30 年期国债 | **5.31%** ↑ |
| 布伦特原油 | **86.26 美元** ↓ |

## 🔥 深度精选

| 时效 | 焦点 | 摘要 |
|------|------|------|
| 3小时前 | **[事件标题](URL)** | 2-3句话，含关键数字/人名/地点。数字用 **粗体**。 |
| 1小时前 | **[事件标题](URL)** | …… |

## ⚡ 快讯速览

- **[2小时前]** [标题](URL)：一句话摘要
- **[1天前]** [标题](URL)：一句话摘要

## 🔗 参考来源

点击上方标题即可打开原文。

#热点 #每日资讯 #国际 #国内 #科技 #市场
```
"""


def ai_classify(articles, contents, low_activity=False, fresh_count=0):
    """把所有文章内容合并，一次性发给 AI 做分层整理。"""
    combined = ""
    for i, (article, content) in enumerate(zip(articles, contents)):
        combined += f"""
══════════════════════════════════════
【文章 {i+1}】
标题：{article['title']}
链接：{article['link']}
来源站点：{article.get('source') or ''}
发布时间原文：{article.get('published') or ''}
时效：{article.get('freshness') or '时间未知'}
权重分：{article.get('score', 0)}
是否12小时内：{'是' if article.get('is_fresh') else '否'}
正文：
{content}
══════════════════════════════════════
"""

    now = now_cn()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today_str = now.strftime("%Y-%m-%d")
    activity_line = (
        f"低活跃：是（过去12小时内仅 {fresh_count} 篇新鲜资讯，不要假装内容很多）"
        if low_activity
        else f"低活跃：否（过去12小时内新鲜 {fresh_count} 篇）"
    )

    llm = get_llm_config()
    log.info(
        f"🤖 正在调用 {llm['name']} ({llm['model']}) 进行分层整理..."
        f"（共 {len(articles)} 篇；提示词与正文不写入日志）"
    )

    client = create_llm_client(llm)
    response = client.chat.completions.create(
        model=llm["model"],
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"当前北京时间：{now_str}\n"
                    f"标题请写成「# {today_str} 热点简报」。\n"
                    f"整理时间必须原样写成：{now_str}\n"
                    f"{activity_line}\n"
                    f"每条新闻展示时必须带上给定的「时效」，不要改写。\n"
                    f"请对以下 {len(articles)} 篇新闻生成三层简报：\n\n"
                    f"{combined}"
                ),
            },
        ],
        temperature=0.7,
        max_tokens=8000,
    )

    result = response.choices[0].message.content or ""
    result = _force_briefing_time(result, today_str, now_str)
    result = _inject_word_count(result)
    result = _inject_freshness_note(result, low_activity, fresh_count, len(articles))
    log.info(f"✅ AI 整理完成（provider={llm['name']}，输出约 {len(result)} 字，内容不写入日志）")
    return result


def _force_briefing_time(md_text: str, today_str: str, now_str: str) -> str:
    """避免模型把整理时间写成 UTC 或随便填一个整点。"""
    md_text = re.sub(
        r"(整理时间[：:]\s*)([^\n]+)",
        rf"\g<1>{now_str}",
        md_text,
        count=1,
    )
    md_text = re.sub(
        r"^#\s+\d{4}-\d{2}-\d{2}\s+热点(资讯|简报)",
        f"# {today_str} 热点简报",
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


def _inject_freshness_note(md_text: str, low_activity: bool, fresh_count: int, total: int) -> str:
    """在篇幅下方标明新鲜资讯占比；过少时标低活跃。"""
    hours = int(_briefing_cfg().get("fresh_hours") or 12)
    if low_activity:
        line = (
            f"> ⚠️ 低活跃：过去 {hours} 小时内仅 {fresh_count}/{total} 篇，"
            "以下可能含较早报道"
        )
    else:
        line = f"> ⏱️ 时效：过去 {hours} 小时内新鲜 {fresh_count}/{total} 篇"
    if re.search(r"篇幅：", md_text):
        return re.sub(
            r"(篇幅：[^\n]+\n)",
            rf"\1{line}\n",
            md_text,
            count=1,
        )
    return line + "\n\n" + md_text


# ==================== 第4步：Markdown → HTML ====================

def markdown_to_html(md_text):
    """将 Markdown 转为适合邮件的 HTML"""
    import markdown

    html_body = markdown.markdown(
        md_text,
        extensions=["extra", "codehilite", "nl2br"],
    )

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


def send_email(html_content, subject_extra=""):
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
    subject = email_cfg["subject"]
    if subject_extra:
        subject = f"{subject} {subject_extra}"

    msg = MIMEMultipart("alternative")
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["Subject"] = f"{subject} — {now_str}"
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
        try:
            log.info(f"📧 发送邮件: {from_email} → {to_email}（{smtp_host}:{port} / {mode}）")
            _smtp_send(smtp_host, port, mode, from_email, password, to_email, raw_message)
            log.info("✅ 邮件发送成功！")
            return
        except Exception as e:
            last_error = e
            log.warning(f"   {smtp_host}:{port} 失败，尝试备用方式: {e}")

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


# ==================== 主流程 ====================

def run():
    """一次完整的抓取 → 整理 → 发信。"""
    articles = fetch_rss()
    if not articles:
        log.error("❌ 没有抓取到任何文章，退出")
        return

    articles, low_activity, fresh_count = enrich_articles(articles)
    contents = scrape_all(articles)
    markdown_result = ai_classify(
        articles, contents, low_activity=low_activity, fresh_count=fresh_count
    )

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

    extra = "[低活跃]" if low_activity else ""
    send_email(html_content, subject_extra=extra)
    log.info("=" * 60)
    log.info("🎉 完成！")


def main():
    log_path = setup_logging()
    log.info("=" * 60)
    log.info(f"📰 每日新闻简报 — {now_cn().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"📄 日志文件: {log_path}")
    log.info("=" * 60)
    try:
        if "--resend" in sys.argv:
            resend_latest()
        else:
            run()
    except Exception:
        log.exception("❌ 运行失败")
        raise


if __name__ == "__main__":
    main()
