# cron-job.org 每 2 分钟触发配置（可选增强，10 分钟搞定）

不配置时系统靠 GitHub Actions 自带 schedule 兜底（约 15 分钟一次，高峰期可能延迟）。
此 Fork 默认保留 15 分钟兜底。本说明仅供用户在审核并启用监控后手动配置；不会自动创建外部账户或定时服务。
外部服务按每 2 分钟请求触发，但 GitHub Actions 仍可能排队或延迟。

## 步骤

1. **生成 GitHub Token（PAT）**
   - 打开 https://github.com/settings/personal-access-tokens/new
   - Token name: `quota-monitor-tick`；Expiration: 1 year
   - Repository access: Only select repositories → `hkid-quota-monitor`
   - Permissions → Repository permissions → **Contents: Read and write**
   - Generate token，复制 `github_pat_...` 备用

2. **注册 cron-job.org**（免费，无需信用卡）
   - https://console.cron-job.org/signup

3. **建 Cron Job**
   - Create cronjob
   - URL: `https://api.github.com/repos/xchen72010/hkid-quota-monitor/dispatches`
   - Schedule: Every 2 minutes
   - 展开 Advanced：
     - Request method: **POST**
     - Headers 加三条：
       | Key | Value |
       |---|---|
       | Accept | application/vnd.github+json |
       | Authorization | Bearer github_pat_你的token |
       | User-Agent | cron-job.org |
     - Request body: `{"event_type":"tick"}`
   - 保存后点 TEST RUN，返回 204 即成功

4. **验证**：仓库 Actions 页应出现 `repository_dispatch` 触发的运行。

> 注意：cron-job.org 在内地可直接访问；PAT 只授予了这一个仓库的写权限，泄露影响面有限，但仍不要发给任何人。
