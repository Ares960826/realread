"""User-facing messages in English and Chinese, picked from the system language.

Agent-facing data (statuses, Meta.message, MCP results) stays in English.
"""

from __future__ import annotations

import functools
import locale
import os
import subprocess
import sys

MESSAGES: dict[str, dict[str, str]] = {
    # init
    "init.intro": {
        "en": "RealRead needs the locations below. Press Enter to accept a default or type a new path.\n",
        "zh": "RealRead 需要以下位置，回车接受默认值，或输入新路径：\n",
    },
    "init.legacy": {
        "en": "Found a configuration from the earlier name 'paper-reader':\n  {path}\n"
              "Reuse its directories? Your existing logins are kept. [Y/n] ",
        "zh": "发现旧名称 paper-reader 的配置：\n  {path}\n沿用其中的目录吗？已有的登录状态会保留。[Y/n] ",
    },
    "init.profile": {
        "en": "1) Dedicated Chrome profile (keeps your publisher / library logins; readable only by you)",
        "zh": "1) 专用 Chrome 配置目录（保存你的出版商/图书馆登录状态，只有你本人可读）",
    },
    "init.library": {
        "en": "2) Library directory (extracted full texts, PDFs, quota state, audit log)",
        "zh": "2) 全文库目录（保存提取的全文、PDF、限额状态和审计日志）",
    },
    "init.chrome": {"en": "3) Chrome executable", "zh": "3) Chrome 可执行文件"},
    "init.access": {
        "en": "5) How do you usually open paywalled papers?\n"
              "   1 = \"Access through your institution\" on the publisher site, or directly on campus / VPN\n"
              "       (most common; nothing to fill in)\n"
              "   2 = Through your library's database links (the address contains ezproxy, proxy, ...)",
        "zh": "5) 你平时怎么打开付费论文？\n"
              "   1 = 在出版商网站点“通过机构登录 / Access through your institution”，或在校园网、VPN 下直接打开\n"
              "       （最常见，不需要填写任何地址）\n"
              "   2 = 从图书馆网站的数据库入口进入，论文地址里带有 ezproxy、proxy 之类的字样",
    },
    "init.choose": {"en": "   Choose", "zh": "   请选择"},
    "init.proxy_link": {
        "en": "   Paste one paper link opened that way (Enter to skip; `realread detect-proxy` can find it later):\n  ",
        "zh": "   粘贴一个这样打开的论文链接（直接回车则跳过，稍后用 realread detect-proxy 自动识别）：\n  ",
    },
    "init.proxy_unknown": {
        "en": "  No library proxy recognised in that link; using direct access. Run `realread detect-proxy` later.",
        "zh": "  没能从这个链接识别出图书馆代理，先按“直接访问”处理；之后可运行 realread detect-proxy。",
    },
    "init.summary": {"en": "\nRealRead will use:", "zh": "\n将使用："},
    "init.row.config": {"en": "  Config file     {v}", "zh": "  配置文件     {v}"},
    "init.row.profile": {"en": "  Chrome profile  {v}", "zh": "  Chrome 配置  {v}"},
    "init.row.library": {"en": "  Library         {v}", "zh": "  全文库       {v}"},
    "init.row.chrome": {"en": "  Chrome          {v}", "zh": "  Chrome       {v}"},
    "init.row.access": {"en": "  Access          {v}", "zh": "  访问方式     {v}"},
    "init.row.limits": {
        "en": "  Limits          >= {interval:g}s between papers per publisher; per publisher <= {hour}/hour, "
              "<= {day}/day;{total} <= {issue} per journal issue per day",
        "zh": "  限额         同一出版商每篇间隔 ≥{interval:g} 秒；每家每小时 ≤{hour} 篇、每天 ≤{day} 篇；"
              "{total}同一期刊同一期每天 ≤{issue} 篇",
    },
    "init.total": {"en": " <= {n}/day overall;", "zh": "合计每天 ≤{n} 篇；"},
    "init.confirm": {"en": "\nCreate these? [y/N] ", "zh": "\n确认创建？[y/N] "},
    "init.cancelled": {"en": "Cancelled; nothing was written.", "zh": "已取消，未写入任何文件。"},
    "init.no_chrome": {"en": "Warning: Chrome not found at {path}", "zh": "警告：找不到 Chrome：{path}"},
    "init.saved": {"en": "Saved {path}. Next: realread login", "zh": "已保存 {path}。下一步：realread login"},
    "access.prefix": {"en": "library proxy (prefix {v})", "zh": "图书馆代理（前缀 {v}）"},
    "access.suffix": {"en": "library proxy (host *.{v})", "zh": "图书馆代理（域名 *.{v}）"},
    "access.direct": {
        "en": "directly at the publisher (institutional sign-in / campus / VPN)",
        "zh": "直接访问出版商（机构登录 / 校园网 / VPN）",
    },
    "limits.ignored": {
        "en": "Note: looser-than-built-in limits in the config are ignored: {v}",
        "zh": "提示：配置中比内置限额更宽松的设置不会生效：{v}",
    },
    # login / proxy
    "close.not_running": {"en": "The RealRead Chrome is not running.", "zh": "RealRead 的 Chrome 没有在运行。"},
    "close.busy": {"en": "A fetch is using the RealRead Chrome (process {pids}). Let it finish, or use --force.",
                   "zh": "有抓取任务正在使用 RealRead 的 Chrome（进程 {pids}）。请等它结束，或者加 --force。"},
    "close.done": {"en": "The RealRead Chrome is closed. It starts again when a fetch needs it.",
                   "zh": "RealRead 的 Chrome 已关闭。下次抓取时会自动启动。"},
    "close.failed": {"en": "The RealRead Chrome is still running after several attempts.",
                     "zh": "试了几次，RealRead 的 Chrome 还在运行。"},
    "login.opened": {
        "en": "Opened the RealRead Chrome window. Sign in as you normally do:\n"
              "  - on the publisher site, \"Access through your institution\", pick your university and sign in; or\n"
              "  - through your library's database links (then run `realread detect-proxy`).\n"
              "Open the publishers you use and check that full text shows. Keep this Chrome window open.",
        "zh": "已在 RealRead 专用 Chrome 中打开页面。请像平时一样登录：\n"
              "  · 在出版商网站点“通过机构登录”，选择你的学校并完成学校登录；或\n"
              "  · 从图书馆网站的数据库入口进入（之后运行 realread detect-proxy，自动识别代理地址）。\n"
              "可以多打开几家常用出版商，确认能看到全文。完成后保持这个 Chrome 窗口开着即可。",
    },
    "chrome.not_running": {
        "en": "The RealRead Chrome is not running. Run `realread login` first.",
        "zh": "RealRead 专用 Chrome 没有运行。请先运行 realread login。",
    },
    "proxy.not_found": {
        "en": "No library proxy found in the open tabs.\n"
              "If you signed in on the publisher sites, you do not need one.\n"
              "If you use your library's links, open any paper through the library in the RealRead Chrome and rerun.",
        "zh": "在已打开的标签页里没找到图书馆代理地址。\n"
              "如果你是在出版商网站上通过机构登录的，就不需要代理，保持现状即可。\n"
              "如果你是从图书馆网站进入的，请在专用 Chrome 里经由图书馆打开任意一篇论文，再运行一次。",
    },
    "proxy.found": {"en": "Found library proxy: *.{v}", "zh": "识别到图书馆代理：*.{v}"},
    "proxy.save": {"en": "Save and use it from now on? [y/N] ", "zh": "保存并在之后的抓取中使用？[y/N] "},
    "saved": {"en": "Saved.", "zh": "已保存。"},
    # fetch events
    "ev.wait": {"en": "  ... waiting {secs}s for {host} ({reason})", "zh": "  ... 等待 {secs} 秒再访问 {host}（{reason}）"},
    "reason.interval": {"en": "polite interval", "zh": "礼貌间隔"},
    "reason.burst_pause": {"en": "short break after several papers", "zh": "连续阅读几篇后的休息"},
    "reason.hourly_cap": {"en": "hourly limit", "zh": "每小时上限"},
    "reason.cooldown": {"en": "cool-down after repeated checks", "zh": "多次触发校验后的冷却"},
    "reason.slowdown": {"en": "slowed down after a bot check", "zh": "触发过校验，已放慢"},
    "ev.blocked.bot": {
        "en": "!! {host} shows a bot check. Solve it in the RealRead Chrome window; the batch resumes "
              "automatically (waiting up to {mins} min).",
        "zh": "!! {host} 出现人机校验。请在 RealRead 专用 Chrome 窗口里手动完成，完成后自动继续"
              "（最多等待 {mins} 分钟）。",
    },
    "ev.blocked.login": {
        "en": "!! {host} needs you to sign in. In the RealRead Chrome window use \"Access through your "
              "institution\"; the batch resumes automatically (waiting up to {mins} min).",
        "zh": "!! {host} 需要登录。请在 RealRead 专用 Chrome 窗口里点“通过机构登录”完成登录，完成后自动继续"
              "（最多等待 {mins} 分钟）。",
    },
    "ev.unblocked": {"en": "  ok, continuing (this publisher is slowed down for a while)",
                     "zh": "  已通过，继续（这家出版商接下来会放慢一段时间）"},
    "ev.university_session_ended": {"en": "  your university sign-in ended on {when}; please sign in once yourself",
                                    "zh": "  学校的登录已在 {when} 到期，需要你本人登录一次"},
    "ev.auto_sign_in_again": {"en": "  {host}: that sign-in did not finish; going back and signing in once more",
                              "zh": "  {host}：这次登录没有完成，返回后再登录一次"},
    "ev.signed_in": {"en": "  signed in, continuing", "zh": "  已登录，继续"},
    "ev.still_blocked": {"en": "  still waiting for you on {host} after {mins} min; the tab shows: {title}",
                         "zh": "  {host} 已等你 {mins} 分钟；标签页当前显示：{title}"},
    "ev.stopped": {"en": "Stopped: {why}", "zh": "已停止：{why}"},
    "ev.quota": {"en": "Daily limit reached for {scope}; resumes after {when}.",
                 "zh": "{scope} 已达到每日上限，{when} 之后可以继续。"},
    "ev.cooldown": {"en": "{host} is cooling down until {when}; its remaining papers are left for later.",
                    "zh": "{host} 正在冷却（到 {when}），它剩下的论文留到之后再读。"},
    "ev.auto_sign_in": {"en": "  ... {host} session expired; signing in again through your institution",
                        "zh": "  ... {host} 的登录已过期，正在通过你的学校自动重新登录"},
    "ev.auto_sign_in_ok": {"en": "  signed in again, continuing", "zh": "  已重新登录，继续"},
    "ev.auto_sign_in_failed": {"en": "  your institution wants you to sign in yourself",
                               "zh": "  学校要求你本人登录（需要输入密码或确认）"},
    "ev.auto_sign_in_skipped": {"en": "  {host}: an automatic re-sign-in was tried too recently (next one after {when})",
                                "zh": "  {host}：刚尝试过自动重新登录（{when} 之后可再试）"},
    "ev.idp_learned": {"en": "  remembered your institution for automatic re-sign-in: {idp}",
                       "zh": "  已记住你的学校，之后登录过期会自动重新登录：{idp}"},
    "sso.none": {"en": "No institution known yet. Sign in once on a publisher with \"Access through your institution\" "
                       "in the RealRead Chrome, then run `realread sso --learn`.",
                 "zh": "还不知道你的学校。请先在 RealRead 专用 Chrome 里通过“机构登录”登录一家出版商，"
                       "再运行 realread sso --learn。"},
    "sso.show": {"en": "Institution (IdP): {idp}\nAutomatic re-sign-in: {on}",
                 "zh": "学校身份提供方（IdP）：{idp}\n自动重新登录：{on}"},
    "inst.ask": {
        "en": "4) Your institution's name, to look up its sign-in and library proxy in public directories\n"
              "   (SeamlessAccess, libproxy-db, OpenAthens; Enter to skip)",
        "zh": "4) 你所在机构（学校）的名称，用于在公开目录里查找它的统一登录和图书馆代理\n"
              "   （SeamlessAccess、libproxy-db、OpenAthens；直接回车则跳过）",
    },
    "inst.searching": {"en": "Looking up \"{q}\" ...", "zh": "正在查找“{q}”……"},
    "inst.none": {
        "en": "Nothing found for \"{q}\". Try the English name, or a shorter part of it.",
        "zh": "没有找到“{q}”。可以试试英文名称，或只输入名称中的一部分。",
    },
    "inst.offline": {"en": "Lookup failed ({v}).", "zh": "查找失败（{v}）。"},
    "inst.row": {"en": "  {n:>2}. {name}{where}", "zh": "  {n:>2}. {name}{where}"},
    "inst.row.sso": {"en": "      sign-in: {v}", "zh": "      统一登录：{v}"},
    "inst.row.proxy": {"en": "      library proxy: {v}", "zh": "      图书馆代理：{v}"},
    "inst.pick": {"en": "Which one is yours? Number (Enter = none)", "zh": "哪一个是你的机构？输入编号（回车 = 都不是）"},
    "inst.more": {"en": "  ... {n} more; type a more specific name to narrow down.",
                  "zh": "  ……还有 {n} 个，可以输入更完整的名称缩小范围。"},
    "inst.methods": {
        "en": "How should RealRead open papers for {name}?\n"
              "   1 = directly at the publisher (campus / VPN / \"Access through your institution\"); recommended\n"
              "       when that is how you sign in, and needed for automatic re-sign-in",
        "zh": "RealRead 应该怎样为 {name} 打开论文？\n"
              "   1 = 直接访问出版商（校园网 / VPN / “通过机构登录”）。如果你平时就是这样登录，推荐选这个；\n"
              "       自动重新登录也依赖这种方式",
    },
    "inst.method.proxy": {
        "en": "   2 = through the library proxy {v}\n"
              "       (sign in once at the library; works for publishers the library has set up)",
        "zh": "   2 = 经由图书馆代理 {v}\n"
              "       （在图书馆登录一次即可；只对图书馆已接入代理的出版商有效）",
    },
    "inst.method.openathens": {
        "en": "   3 = through the OpenAthens redirector for {v}",
        "zh": "   3 = 经由 OpenAthens 跳转入口（{v}）",
    },
    "inst.saved": {"en": "Institution: {name}\nAccess: {access}{sso}", "zh": "机构：{name}\n访问方式：{access}{sso}"},
    "inst.saved.sso": {"en": "\nAutomatic re-sign-in through: {v}", "zh": "\n自动重新登录将通过：{v}"},
    "inst.attribution": {
        "en": "Sources: SeamlessAccess discovery service; libproxy-db (CC BY-SA 4.0, github.com/tom5760/ezproxy-db).",
        "zh": "数据来源：SeamlessAccess 发现服务；libproxy-db（CC BY-SA 4.0，github.com/tom5760/ezproxy-db）。",
    },
    "access.template": {"en": "library link pattern {v}", "zh": "图书馆链接模板 {v}"},
    "access.browser_proxy": {"en": "; network proxy for the RealRead Chrome: {v}",
                             "zh": "；RealRead 专用 Chrome 的网络代理：{v}"},
    "access.restart": {
        "en": "Quit the RealRead Chrome window so the new network proxy takes effect at the next start.",
        "zh": "请退出 RealRead 专用 Chrome 窗口，下次启动时新的网络代理才会生效。",
    },
    "ev.proxy_skipped": {"en": "  ... the library proxy is not set up for {host}; opening it directly",
                         "zh": "  ... 图书馆代理没有接入 {host}，改为直接访问"},
    "notify.title": {"en": "RealRead needs you", "zh": "RealRead 需要你操作"},
    "notify.bot": {"en": "Bot check on {host}: solve it in the RealRead Chrome window.",
                   "zh": "{host} 出现人机校验，请在 RealRead 的 Chrome 窗口里完成。"},
    "notify.login": {"en": "{host} needs you to sign in in the RealRead Chrome window.",
                     "zh": "{host} 需要登录，请在 RealRead 的 Chrome 窗口里完成。"},
    "fetch.summary": {"en": "summary: {v}", "zh": "汇总：{v}"},
    "fetch.listing": {"en": "Refused (not a single article): {v}", "zh": "已拒绝（不是单篇论文地址）：{v}"},
}


@functools.cache
def _system_lang() -> str:
    for var in ("REALREAD_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        if v := os.environ.get(var):
            return "zh" if v.lower().startswith("zh") else "en"
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True,
                                 text=True, timeout=3).stdout
            first = next((ln.strip(' ",()') for ln in out.splitlines() if ln.strip(' ",()')), "")
            return "zh" if first.lower().startswith("zh") else "en"
        except Exception:
            pass
    loc = (locale.getlocale()[0] or "").lower()
    return "zh" if loc.startswith("zh") or loc.startswith("chinese") else "en"


_lang_override: str | None = None


def set_language(lang: str) -> None:
    global _lang_override
    _lang_override = lang if lang in ("en", "zh") else None


def lang() -> str:
    return _lang_override or _system_lang()


def t(key: str, **kw) -> str:
    entry = MESSAGES[key]
    return entry.get(lang(), entry["en"]).format(**kw)
