#!/usr/bin/env python3
"""
每日新闻简报自动化脚本
流程：RSS抓取 → 网页抓取内容 → 合并 → AI统一分类整理 → 生成HTML邮件 → 发送
"""

import os
import sys
import logging
import feedparser
import requests
from bs4 import BeautifulSoup
import yaml
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid
from datetime import datetime
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


def setup_logging() -> Path:
    """同时输出到终端和 logs/ 文件。只用本模块的 logger，避免第三方库把正文打进日志。"""
    log_dir = SCRIPT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
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

def fetch_rss():
    """抓取所有RSS源的文章元数据"""
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

        taken = feed.entries[: config["max_articles"]]
        log.info(f"   本源入选 {len(taken)} 篇")
        for entry in taken:
            articles.append({
                "title": entry.get("title", "无标题"),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "source": feed.feed.get("title", feed_url),
            })
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

    llm = get_llm_config()
    log.info(
        f"🤖 正在调用 {llm['name']} ({llm['model']}) 进行全局分类整理..."
        f"（共 {len(articles)} 篇文章；提示词与正文不写入日志）"
    )

    client = create_llm_client(llm)
    response = client.chat.completions.create(
        model=llm["model"],
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"请对以下 {len(articles)} 篇新闻进行统一分类整理，生成新闻简报：\n\n{combined}"},
        ],
        temperature=0.7,
        max_tokens=8000,
    )

    result = response.choices[0].message.content or ""
    log.info(f"✅ AI 整理完成（provider={llm['name']}，输出约 {len(result)} 字，内容不写入日志）")
    return result


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
  📬 本简报由 AI 自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M')} · 仅供参考不构成任何建议
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
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    msg = MIMEMultipart("alternative")
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["Subject"] = f'{email_cfg["subject"]} — {now_str}'
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

    contents = scrape_all(articles)
    markdown_result = ai_classify(articles, contents)

    today_str = datetime.now().strftime("%Y-%m-%d")
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


def main():
    log_path = setup_logging()
    log.info("=" * 60)
    log.info(f"📰 每日新闻简报 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
