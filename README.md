# 每日新闻简报

从公开 RSS 源抓取资讯，交给大语言模型统一分类排版，再以 HTML 邮件发到指定邮箱。

不必把程序长期挂在自己电脑上，也不必租一台云服务器。仓库已接好 **GitHub Actions**：推到 GitHub、填好密钥后，每天约在北京时间 **07:40** 和 **18:40** 开始抓取整理，邮件大约在 **08:00** / **19:00** 前后送到（可能略早，可接受）。电脑关机简报也会到。这是本项目面向日常使用的推荐方式。

适合个人或小团队做每日资讯推送。RSS 源、栏目、模型和发送时间都可以按自己的关注点改。

## 能做什么

- **用 GitHub Actions 定时发送**：一天两次自动跑，无需本机常开、无需自备服务器
- 从多个 RSS 源拉取最新报道，抓取正文后交给 LLM 做全局分类与摘要
- 生成带篇幅提示的 HTML 邮件（约 xx 字、大约阅读时间）
- 发件人和收件人可以是同一个邮箱（自己发给自己）
- 本机也可以随时跑一次；运行过程写入日志（不含文章正文）

默认栏目：国际、国内、科技 & AI、市场、政策 & 监管。没有内容的栏目会自动省略。

## 特色：GitHub Actions 托管运行

不可能要求每个人都把脚本部署在自己电脑上，更不可能让电脑全天开机等着发信。本项目把定时发信放到 GitHub Actions 上，用的就是你的代码仓库，不是另租一台机器。

到点后 GitHub 会临时拉起一个环境，克隆当前仓库、执行 `daily_news.py`、把邮件发出去，然后销毁这个环境。你的电脑开不开、在不在家，都不影响。

| 做法 | 要不要电脑一直开 | 要不要自己买服务器 | 适合谁 |
|------|------------------|--------------------|--------|
| **GitHub Actions（推荐）** | 不用 | 不用 | 日常自动收简报 |
| 本机运行 / Windows 任务计划 | 到点时电脑要开机 | 不用 | 先冒烟、调试、偶尔手跑 |

个人使用一般不用为 Actions 付费。GitHub Free 对私有仓库每月约有 2000 分钟额度；本脚本一天两次、每次几分钟，远低于上限。发信类用途建议用**私有仓库**，密钥不要写进代码。

仓库里已经包含 `.github/workflows/daily-news.yml`。推上去之后，GitHub 会在仓库的 **Actions** 页看到工作流 `Daily News Email`。之后每次 `git push`，**下一次**定时或手动运行都会用最新代码，不必重新配置 Secrets。

### 使用步骤

1. 把本仓库放到 GitHub（建议私有）。
2. 打开 **Settings → Secrets and variables → Actions**，**每个变量单独建一条 Secret**（不要把整个 `.env` 糊进一个 Secret）。Name 必须和下面完全一致：
   - 必填：`AGNES_API_KEY`（若 `config.yaml` 已改成 DeepSeek，则改为 `DEEPSEEK_API_KEY`）、`EMAIL_FROM`、`EMAIL_TO`、`EMAIL_PASSWORD`
   - 建议：`SMTP_HOST`（QQ 邮箱填 `smtp.qq.com`）、`SMTP_PORT`（推荐 `587`）
   - 选用 DeepSeek 时再加 `DEEPSEEK_API_KEY`
3. 打开 **Actions → Daily News Email → Run workflow**，先手动跑通。
4. 成功后即可依赖定时：工作流在北京时间约 **07:40**、**18:40** 启动（UTC `23:40` / `10:40`），把爬取和整理做完，邮件大约在 **08:00** / **19:00** 前后到达（可能略早）。也可随时再点 Run 补发一次。

云端日志在该次运行记录里查看，也会作为 Artifact 保留约 14 天。日志和生成的 HTML **不会写回仓库**，这是正常的。

说明：

- `.env` **不要**提交到 Git。云上靠 Secrets 注入同名环境变量，脚本读法与本地相同。
- 工作流按 UTC 写的 `23:40` / `10:40`（北京时间 07:40 / 18:40）提前开工，目标约 08:00 / 19:00 送达，允许略早。GitHub 的定时**可能推迟几分钟到数小时**，不是脚本把时区写错了；它也做不到大厂那种「卡点投递」。
- 改了代码或 `requirements.txt` 后必须 `git push`。Actions 每次都是重新克隆仓库再 `pip install -r requirements.txt`，本机新装的包不会自动出现在云上。
- Actions 运行机多在海外，部分国内 RSS 可能抓不到，正文失败时仍会带上标题和链接。
- 邮件标题和「整理时间」按**北京时间**显示。

## 工作流程

```
RSS 抓取 → 网页正文抓取 → LLM 分类整理 → 保存 Markdown/HTML → SMTP 发信
```

1. **抓 RSS**：读取 `config.yaml` 中的源列表，每个源最多取 `max_articles` 篇（默认 5 篇），记录标题、链接、发布时间、来源名。
2. **抓正文**：对每条链接用 `requests` + BeautifulSoup 提取文本（去掉导航、页脚等），每篇最多保留约 3000 字。抓取失败时用占位文案，不中断整批。
3. **LLM 整理**：把全部文章一次性发给当前启用的模型，按固定栏目输出 Markdown（板块标题、一句话要点、表格摘要、今日数据、参考链接）。脚本会补上北京时间和篇幅统计。
4. **落盘并转 HTML**：结果写入 `output/news_YYYY-MM-DD.md` 和 `.html`，再套一层适合邮件客户端的样式。
5. **发邮件**：通过 SMTP 发送 HTML 邮件。账号、密码、收发件人放在 `.env`（本地）或 Actions Secrets（云端），不进代码仓库。

本地运行时，过程会同时出现在终端和 `logs/run_时间戳.log`。日志只记步骤、标题、链接、成功/失败，**不记录文章正文、模型提示词和邮件 HTML**。

## 环境要求

- Python 3.10+（本机运行时建议使用虚拟环境；Actions 会自动安装）
- 可访问配置中的 RSS 源
- 一个 OpenAI 兼容协议的 LLM API Key（默认 Agnes，也可换 DeepSeek 或其他兼容服务）
- 一个可用的 SMTP 邮箱（示例配置面向 QQ 邮箱，其他服务商改主机和端口即可）

## 本地快速开始（可选）

适合先在自己电脑上冒烟，确认能发出邮件，再交给 Actions 定时跑。

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

Windows 也可双击 `run.bat`。脚本会自动切到自身所在目录；若存在 `venv`，会优先用虚拟环境里的 Python。

只重发最近一份已生成的简报、不再抓取、不再调用模型：

```bash
python daily_news.py --resend
```

## 配置说明

敏感信息放 `.env`（本地）或 Actions Secrets（云端），其余放 `config.yaml`。不要把 `.env` 提交到 Git。

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

切换模型时，改 `llm.provider` 即可，不必改代码。密钥仍从环境变量读取。

内置 RSS 覆盖国内科技、国际综合、国际科技、财经，可按需要增删。

## 个性化改造

这是给大家用的通用脚本，默认配置只是起点。按自己的关注点改下面几处即可。

### 新闻源

编辑 `config.yaml` 的 `rss_feeds`，换成你关心的站点。数量和领域都可以自己定；`max_articles` 控制每个源的条数，避免一次塞给模型太多内容。

### 大语言模型

- 在已有的 Agnes / DeepSeek 之间切换：改 `llm.provider`。
- 接入其他 OpenAI 兼容服务：在 `llm.providers` 下新增一项（`model`、`base_url`、`api_key_env`），并在 `.env` / Secrets 里补上对应 Key，再把 `provider` 指过去。
- 简报栏目、表格格式、语言风格：改 `daily_news.py` 中的 `SYSTEM_PROMPT`。例如改成只看某个行业、改成英文输出、或增加「公司动态」栏目。

### 邮件

- 收发件人、SMTP：改 `.env` 或 Actions Secrets。`EMAIL_FROM` 和 `EMAIL_TO` 可以相同（自己发给自己），也可以不同（发给同事或其他邮箱）。
- 主题文案：改 `config.yaml` 的 `email.subject`。
- 邮件外观：改 `daily_news.py` 里 `markdown_to_html()` 的 CSS。

### 本机定时（备选）

更推荐用上面的 GitHub Actions。如果电脑经常开着，也可以在本机定时。

Windows：

```bash
python setup_schedule.py
```

默认创建名为 `DailyNewsEmail` 的任务，每天 08:00 和 18:00 各跑一次。之后可在「任务计划程序」里改时间：`Win + R` → `taskschd.msc`。

其他系统可用 cron、systemd timer 或任意调度器，定时执行 `python daily_news.py`。

## 输出文件

本地运行会写入（这些目录默认不进 Git）：

- `output/news_YYYY-MM-DD.md`：模型整理后的 Markdown
- `output/news_YYYY-MM-DD.html`：用于发信的 HTML
- `logs/run_YYYY-MM-DD_HHMMSS.log`：本次运行过程

同一天多次运行会覆盖同名的简报文件；日志按时间戳各留一份。GitHub Actions 上的对应文件在该次运行的 Artifact 里下载，不会出现在仓库文件列表中。

## 项目结构

```
.
├── daily_news.py        # 主流程
├── setup_schedule.py    # Windows 本机定时（备选）
├── config.yaml          # RSS、LLM、邮件主题
├── .env.example         # 环境变量模板
├── .env                 # 本地密钥（自行创建，勿提交）
├── requirements.txt
├── run.bat              # Windows 手动运行（自动定位项目目录）
├── .github/workflows/   # GitHub Actions 定时任务（推荐）
├── logs/                # 运行日志（本地）
└── output/              # 生成的简报（本地）
```

## 依赖

见 `requirements.txt`：`feedparser`、`openai`、`requests`、`beautifulsoup4`、`pyyaml`、`markdown`、`python-dotenv`、`tzdata`（Windows 上 `ZoneInfo` 需要它）。

## 注意事项

- 部分站点可能反爬或限制海外访问，正文抓取失败时仍会把标题和链接交给模型，邮件里会提示查看原文。
- 简报由模型根据公开报道整理，仅供参考，不构成投资或决策建议。
- 请遵守各 RSS 源与目标网站的使用条款，控制抓取频率。
- 发送前确认收件人是你有权投递的地址。自己发给自己时，把 `EMAIL_FROM` 和 `EMAIL_TO` 都写成你的邮箱即可。
