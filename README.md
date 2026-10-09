# 香港身份证预约配额看板与邮件提醒

本仓库是 [chen1111-a/hkid-quota-monitor](https://github.com/chen1111-a/hkid-quota-monitor) 的个人 Fork。
监控湾仔、长沙湾、将军澳、火炭、屯门、元朗六个人事登记办事处，全部地点均可给 `ADMIN_EMAIL` 发通知。
只读查询公开配额并提醒；预约或改期由用户在官方网页自行完成，不自动提交预约。

## 看板

GitHub Pages 启用后的地址：[xchen72010.github.io/hkid-quota-monitor](https://xchen72010.github.io/hkid-quota-monitor/)。
看板从 Pages 地址自动识别本 Fork，沿用原有全部日期、全部办事处展示、筛选与刷新逻辑。
邮件的起始日期不筛掉看板日期；看板的分级横幅仍按原有阈值显示。
本 Fork 不开放邮件订阅或飞书群入口；收件地址仅通过 Secrets 配置。
当前已配置 cron-job.org 每 2 分钟触发监控，并保留 GitHub 自带的 15 分钟兜底；实际执行仍可能排队或延迟。

## 邮件范围与分级

根目录 [config.json](config.json) 当前设置为：

| 配置 | 值 | 含义 |
|---|---|---|
| `monitor_from` | `2026-10-18` | 通知起始日，包含当天；排除 2026-10-17 及更早日期 |
| `monitor_before` | `2026-11-01` | 排他的上限，即最晚通知 2026-10-31（包含当天）；移除此键可恢复无结束日期 |
| `urgent_before` | `2026-10-26` | 早于该日为 🚨 大提醒 |
| `notice_before` | `2026-11-02` | 早于该日为 🔔 小提醒；其余为 🎫 常规提醒 |

邮件范围为 **2026-10-18 至 2026-10-31（包含两端）**。
分级阈值只决定提醒样式，不是通知截止日；移除通知上限后，更晚日期仍可发常规提醒，所有六个办事处均不按地点偏好过滤。
只对符合范围的 `new_date`（新增可约日期）和 `quota_open`（重新开放名额）发邮件。
保留原有每格 360 分钟冷却（可用 `NOTIFY_COOLDOWN_MIN` 调整）、旧节点防抖和分级能力。
无收件人时不消耗冷却；邮件发送失败恢复本轮之前的冷却状态。
不设起始日时兼容旧配置；两个分级阈值填反仍自动对调。

## 审核后启用

1. 审核代码与配置，再将改动合入自己的 `main`。不要对原作者仓库操作。
2. 在本 Fork 的 **Settings → Secrets and variables → Actions → New repository secret** 配置三项：

   | Secret | 内容 |
   |---|---|
   | `QQ_SMTP_USER` | 作为发件人的完整 QQ 邮箱地址 |
   | `QQ_SMTP_PASS` | QQ 邮箱 SMTP 授权码，直接粘贴到 GitHub Secret，不是 QQ 登录密码，也不要贴到聊天或提交到仓库 |
   | `ADMIN_EMAIL` | 管理员收件邮箱地址 |

   QQ SMTP 沿用 `smtp.qq.com:587` + STARTTLS。无需 `SUBSCRIBER_KEY` 或其他消息平台凭据。
3. **Actions** 页如显示 Fork 工作流停用，点击启用。保留工作流 `7,22,37,52 * * * *` 的 15 分钟兜底，错开整点调度高峰；GitHub 调度可能延迟或丢失，失败的一轮不会关闭后续计划。
4. **Settings → Pages → Build and deployment**：Source 选 **Deploy from a branch**，分支 **main**，目录 **/(root)**，保存后才会发布看板。
5. 仅需要两分钟级触发时，手动按 [docs/cron-setup.md](docs/cron-setup.md) 配置外部 cron；默认不创建外部账户或服务。

这些设置应在审核后完成，避免旧 `main` 配置被提前启用。代码改动本身不会启动监控或发布网页。

## 工作流程与最小冒烟

```
GitHub Actions（15 分钟兜底 / 可选外部 tick）
  → quota_monitor.run：抓取、校验、diff、保存全部地点/日期数据
  → quota_monitor.notify：通知日期窗口、每格冷却、QQ SMTP 邮件
  → data/ 提交与 GitHub Pages 静态看板
```

运行本次改动的离线冒烟：

```sh
python tests/test_monitor_email_smoke.py
```

仅验证日期边界、晚日期、六办事处与事件类型、分级、冷却、邮件 dry-run 和失败恢复。
使用临时数据目录及邮件桩，不查询入境处、不发送真实邮件、不修改仓库配额数据。
接口结构见 [docs/api-notes.md](docs/api-notes.md)。

第三方工具，非入境处官方服务；名额以[入境处官方预约网页](https://www.gov.hk/tc/residents/immigration/idcard/hkic/bookregidcard.htm)为准。
