# RealRead

**让 AI Agent 用你自己已有的访问权限读论文全文：走你自己已登录的浏览器，并且有节制地读。**

[English](README.md)

Agent 能检索、阅读开放获取论文，但大部分文献在订阅墙后面，而你（通过学校或图书馆）其实已经有权限。RealRead 让 Agent 在做文献调研时，**用你已有的权限**读这些全文：

1. 在一个专用的 Chrome 窗口里打开论文。你只需要在里面登录一次。
2. 提取文章正文，网页里有就用网页，没有就用 PDF。
3. 存进本地全文库，Agent 通过 MCP 阅读和检索。

**红线：** 只读你的账户本来就能打开的内容。不破解验证码，不绕过付费墙或任何访问控制，设计上也不是爬虫。

## 工作方式

1. 通过 doi.org 把 DOI 解析成出版商页面地址。这一步不访问出版商。
2. 在**专用 Chrome 配置**里新开一个标签页打开论文，你在出版商或学校的登录就会生效（机构 SSO、校园网、VPN、图书馆代理都可以）。cookie 由浏览器自己保存和续期，RealRead 不读取、也不导出它的内容，只查看学校登录的截止日期（见“自动重新登录”）。

   任何人都能打开的论文（arXiv、CVF、直链 PDF，或 OpenAlex 列出的出版商免费版本，只向它发送 DOI）用普通 HTTP 读取，不开浏览器。这类论文仍要确认拿到的是全文，下面的限制照样适用，失败就转到浏览器。
3. 像人阅读一样在页面上停留几秒并滚动（这样也能加载按需显示的正文），然后提取正文。网页里没有正文时，用同一个会话取 PDF 再转成文字。
4. 结果存成 Markdown，每篇标一个状态：

   | 状态 | 含义 |
   |---|---|
   | `ok` | 已存全文 |
   | `partial` | 可能不是全文：文字偏短，又没有 PDF 可对照。可能是篇短文章，也可能只有摘要 |
   | `no_access` | 你的账户打不开这篇 |
   | `not_extracted` | 没有找到正文，页面也没有要求登录，可以换其他渠道读 |
   | `not_found` | doi.org 查不到这个 DOI，或者编号既不是 DOI 也不是网址。没有打开任何页面 |
   | `needs_login` / `needs_user` | 等你登录或完成人机校验，但你没有在时限内完成 |
   | `deferred` | 留到之后再读（到了每日上限、正在冷却、这家出版商在等你处理、自动重新登录次数已用完、页面加载失败，或者任务已取消） |
   | `refused` | 不是单篇论文的地址 |

已用真实账户测过的出版商：IEEE Xplore、ScienceDirect、Springer、Nature、Wiley、ACS、ACM、Taylor & Francis、Science。其他网站走通用提取。

## 内置节制机制：是阅读，不是爬取

下面这些限额写死在代码里。配置文件只能把它们调得**更严，不能更松**。

| 机制 | 内置值 |
|---|---|
| 同一出版商同时只开一篇，相邻两篇间隔 | ≥ 20 秒（再乘 1–1.5 的随机系数） |
| 同一出版商每读 8 篇，休息一段时间 | 2–5 分钟 |
| 每家出版商 | 每小时 ≤ 20 篇，24 小时内 ≤ 80 篇 |
| 所有出版商合计 | 不设内置上限（可在配置文件里用 `max_total_day` 自行设定） |
| 同一期刊的同一期 | 每天 ≤ 10 篇（不整期下载） |
| 人机校验自己通过 | 不处罚，只记录 |
| 你完成了人机校验 | 这家出版商接下来 2 小时减速一半；1 小时内满 3 次，暂停 30 分钟 |
| 人机校验没人处理 | 减速一半；24 小时内第 2 次起冷却 30 分钟，每次翻倍，最长 4 小时 |
| 收到 HTTP 429 | 冷却 30 分钟起，每次翻倍，最长 24 小时；网站的 `Retry-After` 更长时按它的来 |
| 检索页、列表页、期刊目录页 | 拒绝读取，也从不跟踪页面上的链接 |
| 审计日志 | 每打开一篇都记录（`realread log`） |

各家出版商轮流排队。几百篇的调研会分散在几个小时到一两天里跑完，单个网站不会遇到集中访问。限额和冷却状态保存在磁盘上，重启工具也照样生效。

**出现人机校验或登录页时，这家出版商暂停**，其他出版商在后台标签页继续读，各自的限制不变：
- 你会收到一条系统通知，对应的标签页会切到最前面。
- RealRead 会一直等你在浏览器里手动处理完（默认最多 30 分钟），然后自动继续，并对这家出版商放慢速度。
- 如果你最后停在别的页面（比如出版商首页），RealRead 会重新打开这篇论文。
- 如果一直没人处理，这家出版商的论文标为 `deferred`，其他出版商继续读。

这些机制是为了保护你本人、你学校的订阅协议和出版商，避免无意中读得过量。和所有开源工具一样，别人可以改代码去掉这些限制；但为了批量下载而这样改，正是本项目反对的用法，也很可能违反你学校的订阅协议。

## 安装

需要 Python ≥ 3.11、[uv](https://docs.astral.sh/uv/) 和 Google Chrome。

```bash
git clone https://github.com/Ares960826/realread && cd realread && uv sync
uv run realread init     # 逐项列出要用的目录，你确认后才写入
uv run realread login    # 打开专用 Chrome，像平时一样登录，之后保持窗口开着
uv run realread doctor   # 检查安装和配置
```

`init` 会依次询问：

1. 专用 Chrome 配置目录（以 700 权限创建，只有你本人可读）；
2. 全文库目录（存放全文、PDF、限额状态和审计日志）；
3. Chrome 可执行文件的位置；
4. 你所在机构（学校）的名称，可以不填。RealRead 会到公开目录里查找，把查到的结果列出来：学校的统一登录（用于自动重新登录），以及图书馆代理（如果目录里有）。然后由你选择用哪种方式打开论文，见下文"校外访问"一节。
5. 如果第 4 步跳过了，会问你平时怎么打开付费论文：
   - **选 1**（大多数人）：在出版商网站点"通过机构登录"，或在校园网、VPN 下直接打开。什么都不用填。
   - **选 2**：从图书馆网站的数据库入口进入（地址里带 `ezproxy`）。粘贴一个这样打开的论文链接即可；也可以以后运行 `realread detect-proxy`，从已打开的标签页里自动识别。

配置文件在系统的配置目录下，例如 macOS 上是 `~/Library/Application Support/realread/config.toml`。想用别的位置，设置环境变量 `REALREAD_CONFIG`。

每家出版商只需登录一次：在那家打开任意一篇论文，点"Access through your institution"，选择你的学校并完成登录。登录状态保存在专用配置里，关掉 Chrome 再打开也还在：RealRead 启动 Chrome 时带了 `--restore-last-session`，会保留那些"关浏览器就失效"的登录 cookie。

不过出版商自己会让会话在一段时间后过期，比如 IEEE 几个小时后就会过期。到时 RealRead 会先尝试自己重新登录（见下文），不行才暂停，提醒你重新登录。

**自动重新登录。** 学校自己的统一登录通常比出版商保持得久得多，比如微软账户勾选"保持登录"后大约 90 天有效。

RealRead 会记住你用哪所学校登录。具体记的是学校的 SAML 标识，从你自己的登录过程里自动学到，也可以用 `realread sso` 查看或设置。出版商的登录过期时，它会打开这家出版商的"机构登录直达链接"：
- 学校那边的登录还有效：直接放行，你什么都不用做。
- 学校要求输入密码或点确认：RealRead 暂停，提醒你来操作。

RealRead 不经手任何密码或令牌。每家出版商两次尝试至少间隔 10 分钟；每天 48 次的上限只是防止登录一直失败时反复尝试。

如果没有直达链接，或者链接停在需要点一下的页面，RealRead 会像你一样点过去：“Access through your institution”、列表里的学校、唯一的账号、“保持登录”。它不会输入密码或验证码，不碰人机验证，也不替你同意授权页。使用 Microsoft 登录时，`realread status` 会显示这次登录最晚到哪天；过了这一天，需要你本人登录一次。

目前支持：IEEE、ScienceDirect、Springer、Nature、IOP、SPIE，以及 Atypon 平台上的出版商（Taylor & Francis、Science、Wiley、ACS、ACM、RSNA）。成功重新登录后的 1 小时内，仍然打不开的论文直接算作没有授权，不再尝试登录。

**关于 PDF。** RealRead 的职责是借你的权限拿到全文。正文只在 PDF 里时，它会把原件存为该论文库目录下的 `source.pdf`，旁边是它自己抽出的纯文字。需要精读时，用你自己的 PDF 工具处理这个文件即可。

## 校外访问：适用于任何机构

RealRead 没有内置任何学校的名单。它按你输入的名称去公开目录里查找，列出能用的访问方式，你确认之后才会保存。

```bash
uv run realread institution "University of Exampleton"   # 列出匹配的机构，选出你的，再选访问方式
uv run realread access                                    # 查看当前的访问方式
```

| 访问方式 | 适用情况 | 从哪里得到 |
|---|---|---|
| 直接访问（默认） | 校园网、VPN，或在出版商网站上点"通过机构登录" | 不需要额外信息 |
| 图书馆代理 | 图书馆给的链接形如 `…/login?url=…`（EZproxy 等） | [libproxy-db](https://github.com/tom5760/ezproxy-db)（约 1500 所机构）、你粘贴的链接，或 `realread detect-proxy` 自动识别 |
| OpenAthens 跳转入口 | 你的机构使用 OpenAthens | 实时向 `go.openathens.net/redirector/<机构域名>` 确认 |
| 自定义链接模板 | 其他代理或跳转服务 | `realread access --template 'https://proxy.example.edu/login?url=$@'`（`$@` 代表论文地址原样，`$%` 代表编码后的地址） |
| 专用 Chrome 的网络代理 | 学校公布了代理服务器或 PAC 文件 | `realread access --browser-proxy http://proxy.example.edu:8080`，或 `--pac https://example.edu/proxy.pac` |

学校统一登录的标识（SAML entity ID）来自 [SeamlessAccess](https://seamlessaccess.org) 的公开检索服务，那里收录了 eduGAIN 各联盟和 OpenAthens 的身份提供方。这个标识用于上文的自动重新登录。

图书馆代理只覆盖图书馆接入过的出版商。代理提示"这个网站没有配置"时，RealRead 在这一批剩下的论文里改为直接访问这家出版商。

网络代理只作用于 RealRead 专用 Chrome，不影响你平时用的浏览器。修改之后要退出专用 Chrome 窗口，下次启动才生效。

libproxy-db 采用 CC BY-SA 4.0 许可。RealRead 在运行时下载，在本地缓存一周，不随软件分发。

## 使用

命令行：

```bash
uv run realread fetch -f dois.txt          # 每行一个 DOI 或文章地址；批量较大时建议放后台跑
uv run realread search "contrastive loss"  # 在全文库里按正则检索
uv run realread read 10.1109/CVPR.2016.90  # 打印已保存的全文
uv run realread open 10.1109/CVPR.2016.90  # 在专用 Chrome 里打开这篇给你自己看
uv run realread status                     # 全文库状态、限额、各出版商用量和冷却状态
uv run realread log                        # 审计日志
uv run realread close                      # 关闭 RealRead 的 Chrome，下次抓取时会自动启动
uv run realread sso --learn                # 记住你的学校，供自动重新登录使用
uv run realread institution "<名称>"       # 在公开目录里查找你的机构的统一登录和图书馆代理
```

接入 Claude Code（MCP）：

```bash
claude mcp add --scope user realread -- uv run --directory /path/to/realread realread serve
```

MCP 提供的工具：

| 工具 | 作用 |
|---|---|
| `fetch_fulltext(ids)` | 启动后台抓取任务，一次最多 300 篇；等待 `wait_s` 秒后返回当前进度 |
| `job_status(job_id)` | 查看任务进度；需要你登录或完成校验时，返回 `state: waiting_for_user` 和 `action_required` |
| `cancel_job(job_id)` | 读完当前这篇后停止你自己的任务 |
| `read_fulltext(id, offset)` | 分段阅读已保存的全文 |
| `search_fulltext(pattern)` | 在全文库里按正则检索，返回上下文片段 |
| `library_status()` | 已保存的论文、出问题的论文及原因、今天剩余的限额 |

MCP 只通过 stdio 在本机运行。RealRead 是个人本地工具，不作为共享服务提供。更新 RealRead 后，要重启 MCP 客户端才会用上新代码。

## 安全提示

- 专用 Chrome 会开一个调试端口（默认 9333），只监听 127.0.0.1。它运行期间，本机任何程序都能控制这个配置，所以只用它登录与文献相关的账户。
- 配置目录里保存着你的登录会话，创建时设为只有你本人可读（700）。RealRead 不会把 cookie 复制到浏览器之外。
- 全文只保存在你本机。分享给别人时，请遵守你学校的订阅协议。

## 已知限制

- 出版商会改版，正文选择器写在 `src/realread/sites.py` 里。适配新出版商或修复失效的方法见 CONTRIBUTING.md。
- 国内高校的 WebVPN 会把网址加密改写，目前不支持。请改用 VPN 客户端，或在出版商网站上走机构登录。
- 从 PDF 提取的是纯文本，不还原版式。

## 许可证

MIT
