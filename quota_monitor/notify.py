"""放号通知：events.json -> 日期/防抖过滤 -> 聚合 -> QQ SMTP 邮件。

环境变量（CI secrets 注入；全部缺省时静默跳过，不让 CI 失败）：
- QQ_SMTP_USER / QQ_SMTP_PASS : 发件 QQ 邮箱与 SMTP 授权码
- ADMIN_EMAIL                 : 管理员收件邮箱（必收）
- SUBSCRIBER_KEY              : 订阅者文件 Fernet 解密钥（Phase 5）
- NOTIFY_COOLDOWN_MIN         : 单格冷却分钟数，默认 360
- DRY_RUN=1                   : 打印代替真实发送
"""

from __future__ import annotations

import copy
import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

from .util import mask_email

HKT = timezone(timedelta(hours=8))
DATA = Path("data")
STATE_PATH = DATA / "notify_state.json"

# 用官方预约系统显示的地区名（接口 district 字段），别用「港岛办事处」这类
# 内部称谓——用户在官网看到的是「湾仔」「长沙湾」，对不上会以为是两套数据
OFFICE_NAMES = {"RHK": "湾仔", "RKO": "长沙湾", "RTK": "将军澳",
                "FTO": "火炭", "TMO": "屯门", "YLO": "元朗"}
STATUS_TEXT = {"g": "充足", "y": "少量"}
# fork 自部署时链接自动指向自己的仓库（CI 注入 GITHUB_REPOSITORY）
REPO = os.environ.get("GITHUB_REPOSITORY", "chen1111-a/hkid-quota-monitor")
_OWNER, _NAME = REPO.split("/", 1)
# 主仓库的看板链接指 Cloudflare Pages：github.io 在内地移动网络时通时断，
# 通知里的按钮点不开等于白通知。fork 没有这个部署，仍走各自的 github.io；
# 也可用 DASHBOARD_URL 环境变量覆盖
DASHBOARD = (os.environ.get("DASHBOARD_URL")
             or ("https://hkid-quota-monitor.pages.dev/"
                 if REPO == "chen1111-a/hkid-quota-monitor"
                 else f"https://{_OWNER}.github.io/{_NAME}/"))
BOOKING = "https://www.gov.hk/tc/residents/immigration/idcard/hkic/bookregidcard.htm"


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def load_alert_cfg(path: str = "config.json") -> dict:
    """读分级提醒阈值。config 是用户网页直编的不可信输入：
    文件缺失/坏 JSON/非字符串/非 ISO 格式一律丢弃该键（回退为无分级），
    绝不让一次手滑编辑炸掉通知链路；两阈值填反时自动对调。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    cfg = {k: v for k, v in raw.items()
           if k in ("urgent_before", "notice_before", "monitor_from", "monitor_before")
           and isinstance(v, str) and _ISO_DATE.fullmatch(v)}
    u, n = cfg.get("urgent_before"), cfg.get("notice_before")
    if u and n and u > n:
        cfg["urgent_before"], cfg["notice_before"] = n, u
    return cfg


def in_monitor_window(date: str, cfg: dict) -> bool:
    """邮件窗口：monitor_from 包含当天，monitor_before 排除当天。
    两个边界独立且可选；不影响看板数据，也不使用分级阈值作上限。"""
    return ((not cfg.get("monitor_from") or date >= cfg["monitor_from"])
            and (not cfg.get("monitor_before") or date < cfg["monitor_before"]))


def tier_of(date: str, cfg: dict) -> str:
    """'urgent' / 'notice' / 'info'。ISO 日期字符串可直接比较；等于阈值日不算。"""
    if cfg.get("urgent_before") and date < cfg["urgent_before"]:
        return "urgent"
    if cfg.get("notice_before") and date < cfg["notice_before"]:
        return "notice"
    return "info"


def _now() -> datetime:
    return datetime.now(HKT)


def load_state() -> dict:
    """读通知冷却状态。这个文件会被并发轮次提交、也可能被人工编辑——
    坏了一律回退空状态，绝不能让它炸掉整条通知链路（回退代价只是
    重发一轮提醒，比连续几天静默不发轻得多）。"""
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict) and isinstance(
                state.get("cell_last_notified"), dict):
            return state
        print("WARN notify_state 结构异常，按空状态处理")
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"WARN notify_state 读取失败({e})，按空状态处理")
    return {"cell_last_notified": {}}


def prune_state(state: dict, today: str | None = None) -> None:
    """剪掉已过期日期的冷却键，防止 state 文件无界增长。
    顺带清掉键格式非法的脏数据（否则下游解析会炸）。"""
    today = today or _now().strftime("%Y-%m-%d")
    cells = state.get("cell_last_notified", {})
    for key in list(cells):
        parts = key.split("|")
        if len(parts) != 3 or parts[1] < today:
            del cells[key]


def filter_events(events: list[dict], state: dict,
                  cooldown_min: int, now: datetime | None = None) -> list[dict]:
    """只留通知级事件，且冷却期外；就地更新 state 的最近通知时间。"""
    now = now or _now()
    out = []
    cells = state.setdefault("cell_last_notified", {})
    for e in events:
        if e["type"] not in ("quota_open", "new_date"):
            continue
        key = f'{e["office"]}|{e["date"]}|{e["session"]}'
        last = cells.get(key)
        if last:
            try:
                elapsed = (now - datetime.fromisoformat(last)).total_seconds() / 60
            except (TypeError, ValueError):
                elapsed = float("inf")  # 时间戳坏了当作无冷却，宁可多提醒不可整轮崩
            if elapsed < cooldown_min:
                continue
        cells[key] = now.isoformat(timespec="seconds")
        out.append(e)
    return out


def office_name(off: str) -> str:
    """办事处代码转显示名。白名单外的一律只留中英数字：officeId 直接来自上游
    接口（fetch.normalize 不校验格式），原样落进飞书 lark_md 就能伪造
    <at id=all> 假 @所有人或假系统提示。邮件走 html.escape、看板走 esc()，
    卡片这条新链路是唯一漏网的 sink。"""
    if off in OFFICE_NAMES:
        return OFFICE_NAMES[off]
    return re.sub(r"[^0-9A-Za-z一-鿿]", "", off)[:16] or "未知办事处"


def summarize(events: list[dict], md: bool = False) -> list[str]:
    """按办事处聚合成人话行：湾仔：09/02、09/03（少量）。
    md=True 时办事处名加粗（飞书卡片用 lark_md，邮件走 compose 的 HTML）。"""
    by_office: dict[str, list[dict]] = {}
    for e in events:
        by_office.setdefault(e["office"], []).append(e)
    lines = []
    for off in sorted(by_office):
        evs = sorted(by_office[off], key=lambda x: x["date"])
        parts = []
        for e in evs:
            d = e["date"][5:].replace("-", "/")
            tag = STATUS_TEXT.get(e["to"], e["to"])
            sess = "延长时段" if e["session"] == "K" else ""
            parts.append(f"{d}{sess}({tag})")
        name = office_name(off)
        lines.append(f"**{name}**：{'、'.join(parts)}" if md
                     else f"{name}：{'、'.join(parts)}")
    return lines


def load_subscribers() -> list[dict]:
    """解密订阅者列表（含个性化偏好）。文件或密钥缺失返回空表。
    旧记录无 offices/before 字段 = 全量订阅（向后兼容）。"""
    key = os.environ.get("SUBSCRIBER_KEY", "")
    enc = DATA / "subscribers.json.enc"
    if not key or not enc.exists():
        return []
    try:
        from cryptography.fernet import Fernet
        raw = Fernet(key.encode()).decrypt(enc.read_bytes())
        subs = json.loads(raw.decode().rstrip())  # 去掉 save_roster 的定长填充
        # 白名单式取字段：漏了哪个键，那个偏好会静默失效（订阅者收到
        # 他明确说不要的推送）。subscribe.PREF_KEYS 变更时这里必须同步
        return [{"email": s["email"], "offices": s.get("offices"),
                 "before": s.get("before"), "on": s.get("on")}
                for s in subs if s.get("email") and s.get("active", True)]
    except Exception as e:  # noqa: BLE001 - 订阅表坏了不该阻塞管理员通知
        print(f"WARN subscribers decrypt failed: {e}")
        return []


def event_matches(sub: dict, e: dict) -> bool:
    """个性化过滤：订阅者未设的维度不过滤。"""
    if sub.get("offices") and e["office"] not in sub["offices"]:
        return False
    # on = 锁定到某几天，比 before 更严格，两者都设时以 on 为准：
    # 用户特意点了某一天，就是不想收那天以外的任何东西
    if sub.get("on"):
        return e["date"] in sub["on"]
    if sub.get("before") and e["date"] >= sub["before"]:
        return False
    return True


def compose(events: list[dict], cfg: dict) -> tuple[str, str]:
    """一组事件 -> (邮件主题, 邮件 HTML)。分级取组内最高档。"""
    lines = summarize(events)
    tiers = [tier_of(e["date"], cfg) for e in events]
    tier = "urgent" if "urgent" in tiers else "notice" if "notice" in tiers else "info"
    # n_top = 落在该档阈值内的个数；n_all = 本批全部。两个数字含义不同，
    # 凡是说「X 前有 N 个」的地方必须用 n_top，否则强提醒会报错数字
    n_top, n_all = tiers.count(tier), len(events)
    subject = {
        "urgent": f"🚨 紧急放号：{cfg.get('urgent_before', '近期')} 前有 {n_top} 个名额！",
        "notice": f"🔔 香港ID放号：{cfg.get('notice_before', '近期')} 前有 {n_top} 个名额",
        "info": f"🎫 香港ID预约放号：{n_all} 个名额",
    }[tier]
    return subject, build_email_html(lines, n_top, tier, cfg, n_all,
                                     stray=is_stray(events))


def build_email_html(lines: list[str], n_top: int, tier: str = "info",
                     cfg: dict | None = None, n_all: int | None = None,
                     stray: bool = False) -> str:
    """n_top=落在该提醒档内的名额数（标题用）；n_all=本批全部（副标题用）。"""
    items = "".join(f"<li style='margin:4px 0'>{html.escape(ln)}</li>" for ln in lines)
    stray_html = (f"<p style='background:#fff7e0;border-radius:8px;padding:8px 10px;"
                  f"margin:0 0 10px;color:#6b4a00;font-weight:600'>{STRAY_HINT}</p>"
                  if stray else "")
    cfg = cfg or {}
    n_all = n_top if n_all is None else n_all
    extra = f"（本批共检出 {n_all} 个）" if n_all > n_top else ""
    if tier == "urgent":
        head_color, head = "#d03b3b", (f"🚨 {cfg.get('urgent_before', '近期')} 前有 "
                                       f"{n_top} 个名额放出，手慢无{extra}")
    elif tier == "notice":
        head_color, head = "#b8860b", (f"🔔 {cfg.get('notice_before', '近期')} 前有 "
                                       f"{n_top} 个名额放出{extra}")
    else:
        head_color, head = "#0b57d0", f"🎫 检测到 {n_all} 个预约名额放出"
    return f"""<div style="font-family:system-ui,'PingFang SC','Microsoft YaHei';max-width:560px">
<h2 style="color:{head_color};margin:0 0 6px">{head}</h2>
<p style="color:#666;margin:0 0 12px">香港入境处智能身份证预约（检测时间 {_now().strftime('%m-%d %H:%M')} 港时）</p>
{stray_html}
<ul style="padding-left:18px">{items}</ul>
<p style="margin:16px 0">
<a href="{BOOKING}" style="background:#0b57d0;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">立即去官方预约/改期</a>
&nbsp;<a href="{DASHBOARD}" style="color:#0b57d0">查看实时看板</a></p>
<p style="color:#999;font-size:12px;line-height:1.6">名额变动很快，以官方预约页实际为准。<br>
第三方公益工具，非入境处官方服务。想停止提醒：给本邮箱另发一封主题为「退订」的新邮件即可。</p></div>"""


def send_emails(payloads: list[tuple[str, str, str]], dry: bool) -> None:
    """发送 (收件人, 主题, HTML) 列表——逐人独立内容（个性化）也逐人独立容错。"""
    user = os.environ.get("QQ_SMTP_USER", "")
    pwd = os.environ.get("QQ_SMTP_PASS", "")
    if not payloads:
        print("skip email: no recipients")
        return
    if dry:
        for rcpt, subject, _ in payloads:
            print(f"[DRY] email -> {mask_email(rcpt)}: {subject}")
        return
    # 缺凭据必须抛，不能跟 dry 走同一条静默返回：那会让本通道计为「成功」，
    # 于是「飞书 token 坏 + SMTP 没配」这种半配状态下一个人都没通知到，
    # 冷却却照烧 6 小时且不回滚 —— 那批名额彻底错过
    if not user or not pwd:
        raise RuntimeError("QQ_SMTP_USER/PASS 缺失，邮件通道未发送")
    sent = failed = 0
    with smtplib.SMTP("smtp.qq.com", 587, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, pwd)
        for rcpt, subject, html in payloads:
            msg = MIMEText(html, "html", "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = formataddr((str(Header("香港ID配额监控", "utf-8")), user))
            msg["To"] = rcpt
            try:
                s.sendmail(user, [rcpt], msg.as_string())
                sent += 1
            except smtplib.SMTPException as e:
                failed += 1
                print(f"WARN send to {mask_email(rcpt)} failed: {e}")
    print(f"email sent -> {sent} ok, {failed} failed")
    if failed and not sent:
        raise RuntimeError("所有邮件均发送失败")


class FeishuError(RuntimeError):
    """带结构化 code 的飞书失败。上层要按 code 分流（19001 不重发、其余退纯文本），
    从格式化后的消息里 `"19001" in str(e)` 反解会被返回体里的 log_id 等
    无关数字误命中，把「可恢复的卡片被拒」升级成整条通道失败。
    继承 RuntimeError：main 的双通道容错和既有调用点无需改。"""

    def __init__(self, code, message: str):
        super().__init__(message)
        self.code = code


def cn_date(iso: str | None) -> str:
    """2026-09-01 -> 9月1日。阈值来自网页可编的 config，格式不对就原样退回，
    宁可标题难看也不能让通知在字符串格式化上炸掉。"""
    m = re.match(r"^\d{4}-(\d{2})-(\d{2})$", iso or "")
    return f"{int(m.group(1))}月{int(m.group(2))}日" if m else (iso or "近期")


def earliest_line(events: list[dict]) -> str:
    """最早那个可约日期的一句话。抢名额时人只需要先看这一条，
    所以它单独占一行加粗，而不是埋在按办事处分组的列表里。
    同日 R/K 都放号时优先报一般时段——延长时段是少数人才要的，
    放在最显眼那行会误导多数人。"""
    if not events:
        return ""
    e = min(events, key=lambda x: (x["date"], x["session"] != "R", x["office"]))
    d = e["date"][5:].replace("-", "/")
    sess = "延长时段 " if e["session"] == "K" else ""
    return (f'{office_name(e["office"])} {d} '
            f'{sess}（{STATUS_TEXT.get(e["to"], e["to"])}）')


_TIER_CARD = {"urgent": ("red", "🚨"), "notice": ("orange", "🔔"),
              "info": ("blue", "🎫")}
STRAY_MAX = 2
STRAY_HINT = ("⚡ 散号回流：通常几分钟内消失，很可能是黄牛转关失手——"
              "抢到就是从他们手里截胡，手快专场")


def is_stray(events: list[dict]) -> bool:
    """散号 = 一轮只冒出一两格的孤立回流（改期/取消释放的典型形态），
    与「泄洪式」大批量放号相对。它们最值得扑：出现随机、几分钟内蒸发，
    正是黄牛转关的失手窗口——民间抢到一个，他们就赔一单「包的」。"""
    return 0 < len(events) <= STRAY_MAX
DISCLAIMER = "第三方公益工具，非入境处官方服务 · 只做监控提醒，不代抢代约"


def _count_note(n_all: int, n_top: int, tier: str, cfg: dict) -> str:
    deadline = cn_date(cfg.get("urgent_before" if tier == "urgent"
                               else "notice_before"))
    return f"本轮共 {n_all} 个，其中 {n_top} 个在{deadline}前"


def build_feishu_card(lines: list[str], n_top: int, tier: str, cfg: dict,
                      n_all: int, earliest: str = "", stray: bool = False) -> dict:
    """卡片而非纯文本：群消息流里纯文本会被划过去，带色头的卡片
    一眼能分出「9月1日前的红色急件」和「普通提醒」。

    参数顺序刻意与 build_email_html 对齐（n_top=档内数在前，n_all=总数在后）：
    两个同形状的姊妹构造函数若把语义相反的整数放在对调的槽位上，
    调用方写反不会报错，只会静默播出错误数字——这类回归本文件出过。"""
    color, icon = _TIER_CARD.get(tier, _TIER_CARD["info"])
    if tier == "urgent":
        title = f"{icon} {cn_date(cfg.get('urgent_before'))}前放出 {n_top} 个名额 · 速抢"
    elif tier == "notice":
        title = f"{icon} {cn_date(cfg.get('notice_before'))}前放出 {n_top} 个名额"
    else:
        title = f"{icon} 检测到 {n_all} 个香港ID预约名额"

    elements: list[dict] = []
    if tier == "urgent":
        # @所有人需群机器人开启「允许 @ 所有人」；未开启时飞书按普通文本展示，不报错
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
                         "<at id=all></at> 名额常在几分钟内被抢完，现在就点下面的按钮"}})
    if stray:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": f"**{STRAY_HINT}**"}})
    if earliest:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": f"**最早可约：{earliest}**"}})
    if lines:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": "\n".join(lines)}})
    # 标题只报本档数量，列表却是全部——不说明的话「说 2 个却列了 3 行」很像 bug。
    # info 档没有截止日可言（它的定义就是落在 notice_before 之外），说了反而自相矛盾
    if n_all != n_top and tier != "info":
        elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                         "content": _count_note(n_all, n_top, tier, cfg)}]})
    elements += [
        {"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "立即去官网预约"},
             "url": BOOKING, "type": "primary"},
            {"tag": "button", "text": {"tag": "plain_text", "content": "查看实时看板"},
             "url": DASHBOARD, "type": "default"},
        ]},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": DISCLAIMER}]},
    ]
    return {"config": {"wide_screen_mode": True},
            "header": {"template": color,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def _post_feishu(hook: str, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(hook, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read(65536).decode("utf-8", "replace")
        status = resp.status
    check_feishu_body(status, raw)


def feishu_hooks() -> list[str]:
    """FEISHU_WEBHOOK 支持多个群机器人：逗号/换行/空白分隔。
    群满 500 人只能开二群，同一条提醒要同时进所有群。非 https 的一律丢弃。"""
    raw = os.environ.get("FEISHU_WEBHOOK", "")
    # BOM/零宽字符：把多行 webhook 存进 GitHub Secret 时，各种编辑器和
    # PowerShell 管道都可能在开头塞一个 U+FEFF。它不属于 \s，切分后仍粘在
    # 第一条 hook 上，startswith 判定失败 → 整个群静默掉线且只在 CI 日志里
    # 留一行字（实测 1 群这样哑了一天）。这里直接抹掉再判。
    # 写成转义码而非字面量：这几个字符在编辑器里不可见，写成字面量哪天被
    # 格式化工具悄悄清掉，这道防线会无声消失
    ZW = "\ufeff\u200b\u200e\u200f\u00a0"   # BOM/零宽空格/LRM/RLM/不断行空格
    hooks = [h for h in (x.strip(ZW) for x in re.split(r"[\s,]+", raw)) if h]
    bad = [h for h in hooks if not h.startswith("https://")]
    for h in bad:
        print(f"skip feishu hook（must be https）: {h[:24]}...")
    return [h for h in hooks if h.startswith("https://")]


def send_feishu(lines: list[str], n: int, dry: bool, tier: str = "info",
                cfg: dict | None = None, n_top: int | None = None,
                events: list[dict] | None = None) -> None:
    hooks = feishu_hooks()
    if not hooks:
        print("skip feishu: no webhook")
        return
    cfg = cfg or {}
    n_top = n if n_top is None else n_top
    earliest = earliest_line(events or [])
    stray = is_stray(events or [])
    card = build_feishu_card(lines, n_top, tier, cfg, n, earliest, stray)
    # 纯文本兜底：卡片 schema 是飞书说了算的，哪天字段改了也不能让这条通道整个哑掉。
    # 兜底必须是「同样一条消息的降级版」而非精简版——触发它的场景（飞书改了卡片
    # 字段）会是持续性的，@所有人 一旦只活在卡片里，届时紧急提醒会全部静默沉底
    at_all = '<at user_id="all">所有人</at> ' if tier == "urgent" else ""
    note = (f"\n（{_count_note(n, n_top, tier, cfg)}）"
            if n != n_top and tier != "info" else "")
    head = card["header"]["title"]["content"]
    text = (at_all + (f"{STRAY_HINT}\n" if stray else "")
            + (f"{head}\n最早可约：{earliest}\n" if earliest else f"{head}\n")
            # 去掉 lark_md 的加粗记号——纯文本消息里它会原样显示成星号
            + "\n".join(ln.replace("**", "") for ln in lines) + note +
            f"\n\n官方预约：{BOOKING}\n实时看板：{DASHBOARD}\n{DISCLAIMER}")
    if dry:
        print(f"[DRY] feishu card ({len(hooks)} hooks):\n"
              f"{json.dumps(card, ensure_ascii=False, indent=1)}")
        print(f"[DRY] feishu text fallback:\n{text}")   # 兜底也要能被演练看见
        return
    # 逐群独立容错：二群的机器人挂了不能连累一群。全灭才算通道失败
    #（main 靠通道异常决定冷却回滚——只要有一个群收到，就不该重试轰炸它）
    ok = 0
    last_err: Exception | None = None
    for i, hook in enumerate(hooks, 1):
        tag = f"hook{i}/{len(hooks)}"
        try:
            _post_feishu(hook, {"msg_type": "interactive", "card": card})
            print(f"feishu sent: card ok ({tag})")
            ok += 1
            continue
        except FeishuError as e:
            last_err = e
            if e.code == 19001:
                # token/权限问题，退化重发一样被拒，白发一次还拖慢后面的群
                print(f"WARN feishu {tag} token 无效（19001），跳过该群")
                continue
            print(f"WARN 飞书卡片被拒（{tag}: {e}），退回纯文本重发")
        except Exception as e:  # noqa: BLE001 - 网络类异常也不许拦住下一个群
            last_err = e
            print(f"WARN feishu {tag} card failed: {e}")
        try:
            _post_feishu(hook, {"msg_type": "text", "content": {"text": text}})
            print(f"feishu sent: text fallback ok ({tag})")
            ok += 1
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"WARN feishu {tag} fallback failed: {e}")
    if ok == 0:
        raise RuntimeError(f"feishu: all {len(hooks)} hooks failed") from last_err


def check_feishu_body(status: int, raw: str) -> None:
    """飞书 webhook 失败也回 HTTP 200，错误只写在返回体的 code 里
    （换机器人没同步 secret -> code 19001 param invalid）。只看 HTTP 状态码
    等于把「一条都没送到」记成成功，而放号提醒漏一轮就是彻底错过，
    所以必须解析返回体并抛出——抛出后该通道计为失败，两条通道全失败时
    main 才回滚冷却让下一轮重试（单通道失败不回滚，避免另一通道重复轰炸）。
    返回体解析不出来时不抛：那是飞书改格式，不该拖垮真送达的通知。"""
    try:
        code = json.loads(raw).get("code")
    except (ValueError, AttributeError):
        return
    if code not in (0, None):
        raise FeishuError(code, f"feishu rejected (HTTP {status}, code={code}): "
                                f"{raw[:200]}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    dry = os.environ.get("DRY_RUN") == "1"
    ev_path = DATA / "events.json"
    if not ev_path.exists():
        print("skip: no events.json")
        return
    events = json.loads(ev_path.read_text(encoding="utf-8")).get("events", [])
    cooldown = int(os.environ.get("NOTIFY_COOLDOWN_MIN", "360"))

    cfg = load_alert_cfg()
    in_window = [e for e in events if in_monitor_window(e["date"], cfg)]
    n_out = len(events) - len(in_window)
    if n_out:
        print(f"filtered: {n_out} 个事件在监测窗口外"
              f"（起始日含当天={cfg.get('monitor_from', '不限')}，"
              f"上限不含当天={cfg.get('monitor_before', '不限')}），不推送")

    state = load_state()
    state_before = copy.deepcopy(state)
    fresh = filter_events(in_window, state, cooldown)
    if not fresh:
        print("skip: no notify-worthy events after window/cooldown filter")
        return

    n = len(fresh)
    # 逐人个性化：管理员收全量；订阅者只收自己偏好范围内的事件，无匹配不打扰
    payloads: list[tuple[str, str, str]] = []
    admin = os.environ.get("ADMIN_EMAIL", "").lower()
    if admin:
        subject, html = compose(fresh, cfg)
        payloads.append((admin, subject, html))
    skipped = 0
    for sub in load_subscribers():
        if sub["email"] == admin:
            continue  # 管理员已收全量
        sub_ev = [e for e in fresh if event_matches(sub, e)]
        if not sub_ev:
            skipped += 1
            continue
        subject, html = compose(sub_ev, cfg)
        payloads.append((sub["email"], subject, html))
    if skipped:
        print(f"personalized: {skipped} subscribers had no matching events")

    if not payloads:
        print("skip email: no recipients; cooldown unchanged")
        return
    # 邮件是唯一启用通道；发送前落盘保持原冷却机制，失败则恢复过滤前状态。
    prune_state(state)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    try:
        send_emails(payloads, dry)
    except Exception as e:  # noqa: BLE001 - 发送失败不能烧掉冷却
        STATE_PATH.write_text(json.dumps(state_before, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        print(f"WARN 邮件发送失败({e})，已回滚冷却状态待下轮重试（{n} cells）")
        return
    print(f"OK notified={n} cells, {len(payloads)} emails")


if __name__ == "__main__":
    main()
