# 每日新闻简报

从公开 RSS 源抓取资讯，交给大语言模型统一分类排版，再以 HTML 邮件发送到指定邮箱。适合个人或小团队做每日资讯推送，也可按自己的关注领域改源、改栏目、改发送时间。

## 能做什么

- 从多个 RSS 源拉取最新报道
- 抓取文章正文，交给 LLM 做全局分类与摘要
- 输出 Markdown / HTML 简报，并发送到邮箱
- 把每次运行过程写入日志（不含文章正文）
- 支持 Windows 定时任务；也可选用 GitHub Actions 定时跑（无需自备云服务器）

默认栏目包括：国际、国内、科技 & AI、市场、政策 & 监管。没有内容的栏目会自动省略。

## 工作流程

```
RSS 抓取 → 网页正文抓取 → LLM 分类整理 → 保存 Markdown/HTML → SMTP 发信
```

1. **抓 RSS**：读取 `config.yaml` 中的源列表，每个源最多取 `max_articles` 篇（默认 5 篇），记录标题、链接、发布时间、来源名。
2. **抓正文**：对每条链接用 `requests` + BeautifulSoup 提取文本（去掉导航、页脚等），每篇最多保留约 3000 字。抓取失败时用占位文案，不中断整批。
3. **LLM 整理**：把全部文章一次性发给当前启用的模型，按固定栏目输出 Markdown（板块标题、一句话要点、表格摘要、今日数据、参考链接）。
4. **落盘并转 HTML**：结果写入 `output/news_YYYY-MM-DD.md` 和 `.html`，再套一层适合邮件客户端的样式。
5. **发邮件**：通过 SMTP 发送 HTML 邮件。账号、密码、收发件人放在 `.env`，不进代码仓库。

运行过程会同时出现在终端和 `logs/run_时间戳.log`。日志只记步骤、标题、链接、成功/失败，**不记录文章正文、模型提示词和邮件 HTML**。

## 环境要求

- Python 3.10+（建议使用虚拟环境）
- 可访问配置中的 RSS 源
- 一个 OpenAI 兼容协议的 LLM API Key（默认 Agnes，也可换 DeepSeek 或其他兼容服务）
- 一个可用的 SMTP 邮箱（示例配置面向 QQ 邮箱，其他服务商改主机和端口即可）

## 快速开始

```bash
# 1. 进入项目目录，创建并激活虚拟环境
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
# source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 复制环境变量模板并填入真实值
# Windows
copy .env.example .env

# macOS / Linux
# cp .env.example .env

# 4. 运行一次
python daily_news.py
```

Windows 也可双击 `run.bat`。脚本会自动切到自身所在目录，项目放在哪都能用；若存在 `venv`，会优先用虚拟环境里的 Python。

## 配置说明

敏感信息放 `.env`，其余放 `config.yaml`。不要把 `.env` 提交到 Git。

### `.env`（密钥与邮箱）

| 变量 | 说明 |
|------|------|
| `AGNES_API_KEY` | Agnes API Key（当前默认厂商） |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（备用） |
| `SMTP_HOST` | SMTP 主机，默认 `smtp.qq.com` |
| `SMTP_PORT` | SMTP 端口。QQ 邮箱推荐 `587`（STARTTLS）。`465` 在部分网络会 SSL 握手超时，脚本会自动改试另一个端口 |
| `EMAIL_FROM` | 发件邮箱 |
| `EMAIL_TO` | 收件邮箱。可以和 `EMAIL_FROM` 填成同一个地址 |
| `EMAIL_PASSWORD` | SMTP 密码。QQ 邮箱请填 **授权码**，不是登录密码 |

**发件人和收件人可以是同一个人。** `EMAIL_FROM` 与 `EMAIL_TO` 填同一个邮箱完全合法，脚本会自己发给自己。不必准备两个账号，也不必「从别人发到我」或「我发给别人」。个人自用时，最常见的就是用自己的邮箱把简报投递到自己的收件箱。

QQ 邮箱授权码：登录网页版 QQ 邮箱 → 设置 → 账户 → 开启 SMTP 服务 → 生成授权码。

其他邮箱（Gmail、Outlook、企业邮等）只需改 `SMTP_HOST`、`SMTP_PORT`，并按该服务商要求填写密码或应用专用密码。

### `config.yaml`（源、模型、主题）

| 项 | 说明 |
|----|------|
| `rss_feeds` | RSS 源列表 |
| `max_articles` | 每个源最多抓取的条目数 |
| `llm.provider` | 当前厂商，内置 `agnes` / `deepseek` |
| `llm.providers` | 各厂商的 `model`、`base_url`、对应的环境变量名 |
| `email.subject` | 邮件主题前缀，实际发送时会附上日期时间 |

切换模型时，改 `llm.provider` 即可，不必改代码。密钥仍从 `.env` 读取。

内置 RSS 覆盖国内科技、国际综合、国际科技、财经，可按需要增删。

## 个性化改造

这是给大家用的通用脚本，默认配置只是起点。按自己的关注点改下面几处即可。

### 新闻源

编辑 `config.yaml` 的 `rss_feeds`，换成你关心的站点。数量和领域都可以自己定；`max_articles` 控制每个源的条数，避免一次塞给模型太多内容。

### 大语言模型

- 在已有的 Agnes / DeepSeek 之间切换：改 `llm.provider`。
- 接入其他 OpenAI 兼容服务：在 `llm.providers` 下新增一项（`model`、`base_url`、`api_key_env`），并在 `.env` 里补上对应 Key，再把 `provider` 指过去。
- 简报栏目、表格格式、语言风格：改 `daily_news.py` 中的 `SYSTEM_PROMPT`。例如改成只看某个行业、改成英文输出、或增加「公司动态」栏目。

### 邮件

- 收发件人、SMTP：改 `.env`。`EMAIL_FROM` 和 `EMAIL_TO` 可以相同（自己发给自己），也可以不同（发给同事或其他邮箱）。
- 主题文案：改 `config.yaml` 的 `email.subject`。
- 邮件外观：改 `daily_news.py` 里 `markdown_to_html()` 的 CSS。

### 定时发送

Windows 可用项目自带的任务注册脚本：

```bash
python setup_schedule.py
```

默认会创建名为 `DailyNewsEmail` 的任务，每天 08:00 和 18:00 各跑一次（使用 `venv\Scripts\pythonw.exe`，无控制台窗口）。脚本会尝试立刻跑一次作为测试。

之后可在「任务计划程序」里改时间、禁用或删除：`Win + R` → `taskschd.msc` → 任务计划程序库 → `DailyNewsEmail`。

其他系统可用 cron、systemd timer 或任意调度器，定时执行：

```bash
python daily_news.py
```

没有云服务器时，也可以用仓库里的 GitHub Actions 工作流定时跑，见下文。

## 用 GitHub Actions 定时发送（可选）

GitHub Actions **不是**你租的那台一直开机的云服务器。它是 GitHub 提供的「临时跑任务」：到点拉起一台短命的 Linux 环境，执行 `daily_news.py`，跑完就销毁。电脑关机、没有 VPS，简报也能发出去。

**费用：** 个人使用一般不用花钱。GitHub Free 对 **私有仓库** 每月有约 2000 分钟的 Actions 额度；本脚本一天跑两次、每次几分钟，远低于这个上限。**公开仓库** 的额度更宽松，但密钥和运行记录更暴露，个人发信建议用私有仓库。额度用完才会开始计费，这个项目正常用到计费的概率很低。

仓库已包含 `.github/workflows/daily-news.yml`，默认北京时间每天 08:00、18:00（工作流里写的是 UTC `00:00` / `10:00`），也可以在 GitHub 上手动点一次「Run workflow」做测试。

使用步骤：

1. 把本仓库推到 GitHub（建议私有仓库）。
2. 打开仓库 **Settings → Secrets and variables → Actions**，添加与 `.env` 相同的项：
   - 必填：`EMAIL_FROM`、`EMAIL_TO`、`EMAIL_PASSWORD`，以及当前 `config.yaml` 所用厂商的 Key（默认是 `AGNES_API_KEY`）
   - 选用 DeepSeek 时再加 `DEEPSEEK_API_KEY`
   - 非 QQ 邮箱时再加 `SMTP_HOST`、`SMTP_PORT`；不填则脚本仍使用 QQ 邮箱默认值
3. 打开 **Actions → Daily News Email → Run workflow**，先手动跑通。
4. 通过后即可依赖定时触发。定时任务偶尔会晚几分钟；公开仓库若长期无提交，GitHub 可能暂停定时工作流，私有仓库一般更稳定。

云端运行日志可在该次 Actions 的日志里看，也会作为 Artifact 保存约 14 天。注意：GitHub 的运行机多在海外，部分国内 RSS 可能抓不到，这是环境差异，不是脚本坏了。电脑常开的话，继续用本机 Windows 任务计划即可，不必上 Actions。

## 输出文件

每次运行会写入（这些目录默认不进 Git）：

- `output/news_YYYY-MM-DD.md`：模型整理后的 Markdown
- `output/news_YYYY-MM-DD.html`：用于发信的 HTML
- `logs/run_YYYY-MM-DD_HHMMSS.log`：本次运行过程

同一天多次运行会覆盖同名的简报文件；日志按时间戳各留一份。

## 项目结构

```
.
├── daily_news.py        # 主流程
├── setup_schedule.py    # Windows 定时任务
├── config.yaml          # RSS、LLM、邮件主题
├── .env.example         # 环境变量模板
├── .env                 # 本地密钥（自行创建，勿提交）
├── requirements.txt
├── run.bat              # Windows 手动运行（自动定位项目目录）
├── .github/workflows/   # GitHub Actions 定时任务（可选）
├── logs/                # 运行日志
└── output/              # 生成的简报
```

## 依赖

见 `requirements.txt`：`feedparser`、`openai`、`requests`、`beautifulsoup4`、`pyyaml`、`markdown`、`python-dotenv`。

## 注意事项

- 部分站点可能反爬或限制海外访问，正文抓取失败时仍会把标题和链接交给模型，邮件里会提示查看原文。
- 简报由模型根据公开报道整理，仅供参考，不构成投资或决策建议。
- 请遵守各 RSS 源与目标网站的使用条款，控制抓取频率。
- 发送前确认收件人是你有权投递的地址。自己发给自己时，把 `EMAIL_FROM` 和 `EMAIL_TO` 都写成你的邮箱即可。
