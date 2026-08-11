# 个人主页数据刷新设计

## 目标

个人主页展示稳定的身份信息、真实公开的技术活动和远栈文章。动态内容由仓库内脚本统一生成，不读取或推断私有仓库，也不要求个人访问令牌。

## 行业参考

| 决策点 | 行业模式 | 参考实现 | 本仓库选择 | 原因 |
|---|---|---|---|---|
| 个人主页入口 | 与账号同名的 Profile README | [GitHub Profile README](https://docs.github.com/en/account-and-profile/how-tos/profile-customization/managing-your-profile-readme) | `1yhy/1yhy` 的 `README.md` | 使用 GitHub 原生展示入口 |
| 定时刷新 | 仓库内计划任务 | [GitHub Actions scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) | 每天检查并支持手动触发 | 无需常驻服务，数据无变化时不产生提交 |
| 写入权限 | 最小权限 | [GitHub Actions workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions) | 仅授予 `contents: write` | 只允许提交生成内容 |
| 文章来源 | RSS 作为发布订阅契约 | [RSS 2.0 Specification](https://www.rssboard.org/rss-specification) | 读取远栈公开 RSS，只接受 `/posts/` | 未发布内容和普通页面不会进入主页 |
| 动态制品 | 构建时生成并保存最后成功版本 | GitHub 个人主页常用的 generated assets 模式 | 生成单一品牌化 SVG | 远端短暂失败时主页仍可展示上一次成功结果 |

## 数据边界

- GitHub：调用公开贡献日历、公开仓库和仓库语言接口，不读取私有仓库名称或内容。
- 远栈：仅读取 `https://stackonward.com/index.xml` 中路径位于 `/posts/` 的条目。
- 项目指标：星标、Fork、npm 版本由公开徽章接口实时渲染。
- WakaTime：当前不接入。编辑器、操作系统和实际编码时长无法由 GitHub 数据可靠推导。

## 数据流

```text
GitHub GraphQL / REST ─┐
                       ├─> refresh_profile.py ─> README 文章区
远栈 RSS ──────────────┘                     └─> generated/programming-stats.svg

GitHub Actions ─> 单元测试 ─> 生成 ─> 有变更时提交
```

生成过程先在内存中完成全部抓取和渲染。任一数据源失败时进程返回非零，不覆盖仓库内最后一次成功制品。

## 扩展边界

数据源通过协议隔离。新增 WakaTime 或 npm 下载统计时，实现新的数据源适配器并扩展对应渲染区，不修改 GitHub 与 RSS 的解析逻辑。

## 验证

- RSS 只保留公开文章并按发布时间倒序排列。
- README 生成区必须唯一存在，缺失或重复时失败。
- 贡献日历按月份和星期聚合，不推断 GitHub 无法提供的实际编码时长。
- SVG 必须是可解析 XML，且所有数值来自同一次数据快照。
- 工作流只提交 `README.md` 与 `generated/` 下的生成制品。
