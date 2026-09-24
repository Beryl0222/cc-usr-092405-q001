# 文化推荐策略治理

管理文化内容分发策略、实验分段、反馈口径和历史决定复现——不训练新模型，先管住推荐策略。

## 解决的问题

- 完播率单一指标让数小时知识讲解、非遗慢直播、经典课文影像失去曝光。
- 加入收藏、回访、讨论质量、多样性信号后，策略变更要说得清、可审计、可回滚。
- 反馈迟到、撤回、重复不得污染指标；调权不能伪装成一次连续实验。
- 创作者要知道内容为何进出独立通道；用户关闭画像/重置兴趣后历史偏好失效。
- 任何角色不能反推出个人观看记录。

## 模块（`gov/`）

| 文件 | 职责 |
| --- | --- |
| `gov/metrics.py` | 冻结的多信号指标口径目录（v2.0）：完播按时长归一化、有效观看占比保护长内容，短期互动与长期回访分开，权重只能引用登记指标 |
| `gov/policies.py` | 策略提案状态机：目标/权重/人群/有效期四要素，内容+风险双批准才生效，小流量硬上限 10%，拒绝自创指标 |
| `gov/experiments.py` | 不可变实验分段：中途调权冻结旧段另开新段；HMAC 确定性分桶，决策日志可复现"某日每次分流"；紧急回滚闭合分段回落基线 |
| `gov/events.py` | 去标识化事件管道：原始标识拒收；同编号重投逐字段比对——相同为幂等（只计一次），事件类型/匿名主体/内容/时间/实验归属/业务数据有差异即判冲突、拒绝覆盖并留可查询证据；撤回乱序安全（先到挂起）；定稿窗口冻结后迟到数据进隔离区+修订台账；可选存储日志，重启回放后判断不变 |
| `gov/privacy.py` | 假名纪元轮换：重置兴趣销毁旧盐（旧假名不可反推、不可链接）；关闭画像立即清偏好且强制基线，重开也是全新画像 |
| `gov/channels.py` | 创作者独立通道进出原因链可查；对外聚合 k=5 匿名，低人数格子抑制 |
| `gov/platform.py` | 编排门面：分流盖戳、人群判定、分段隔离的短期/长期同口径对比 |

## 主要 HTTP 接口

启动：`python3 service.py --port 8000`（健康检查 `GET /health` 不变）。

- `POST /api/policies/submit`、`POST /api/policies/{id}/approve|reject` — 提案与双批准
- `POST /api/experiments/open`、`.../{id}/adjust`（调权另开分段）、`.../{id}/rollback`
- `POST /api/route` — 一次分流（内部假名化，返回策略、桶位、原因）
- `POST /api/events` — 曝光/收藏/评论/回访入库（可带 `revoke:true`、去标识化校验）。
  同 `event_id` 重投：载荷完全一致返回 `200 + duplicate`（幂等命中，只计一次）；
  载荷有差异返回 `409 + conflict`（拒绝覆盖首条记录，响应体带双方内容与差异字段）
- `GET  /api/events/conflicts?event_id=...&day=...` — 冲突台账查询，圈定需要复核的事件与实验窗口
- `GET  /api/reports/short?day=...`、`GET /api/reports/long?day=...`
- `GET  /api/experiments/{id}/compare?day=...` — 分段独立、口径一致的实验/对照/基线比较
- `GET  /api/reproduce?day=...` — 离线复算当日每次分流的策略与桶位
- `POST /api/privacy/disable|reset`、`GET /api/privacy/status`
- `POST /api/channels/enter|exit`、`GET /api/channels/explain?content_id=...`
- `POST /api/reports/kanon` — k 匿名聚合
- `POST /api/clock` — 推进时钟（联调/试运行）；`GET /api/metrics/catalog`、`GET /api/audit`

重启恢复：默认状态在单进程内存中；设置环境变量 `GOV_EVENT_STORE=<路径>` 后，
事件入库、窗口定稿与分流决策会追加写入该日志，进程重启时自动回放——
首条记录、冲突台账、定稿状态、挂起撤回与决策序号跨重启保持，同编号的
幂等/冲突判定不受影响。

## 验证

```bash
python3 service.py --check   # 基础配置检查
npm test                     # 36 个测试：服务契约 + 领域规则 + HTTP 端到端
python3 -m compileall -q .   # 编译检查全部 Python 模块
python3 trial_run.py         # 端到端试运行，退出码 0 即通过
```

`trial_run.py` 覆盖：长短内容同场曝光、策略双批准、中途调权另开分段、
紧急回滚、重复/撤回乱序/定稿后迟到的处理、同编号冲突重投的拒绝与证据、
重启后幂等/冲突/定稿判断恢复、当日分流复现、d7/d30 回访成熟前后的同口径
比较、关闭画像与兴趣重置、创作者通道解释、k 匿名抑制与原始标识拒收。

`fixtures/domain.json` 保存领域名词和状态样例，便于接口联调时保持一致语义。
