# 运动康复费用理赔账本

面向运动员治疗费用、保险责任和俱乐部福利报销的 Python 后端服务。

系统读取治疗处方、服务项目、保险责任、免赔额、预授权和收据，把一笔费用自动拆成
**可报 / 待补件 / 个人承担 / 已拒赔** 四部分。报销顺序（商业保险 → 俱乐部福利 →
个人账户）由系统强制执行，前台无法手工填错；同一张票据重复索赔会被拦截并返回原申请
及其当前状态。

## 运行

需要 Python 3.11 或更高版本（仅标准库，无第三方依赖）：

```bash
python3 src/index.py
```

服务默认监听 `8000` 端口，访问 `GET /health` 可确认进程状态。执行测试：

```bash
python3 -m unittest discover -s tests
```

也可以运行 `docker compose up --build` 启动容器。业务数据持久化在 `.runtime/ledger.json`
（临时文件 + 原子替换写入）。

## 约定

- 所有响应均为 JSON；金额字段一律以**分**表示（字段名带 `_cents` 后缀），输入金额
  接受元的字符串或数字（如 `"1200.00"`），数量最多 6 位小数。
- 每个改变账本的决策都会写入事件（`GET /events` 可查），事件携带决定时点 `at` /
  `decided_at`：预授权过期（`pre_authorization_expired`）、治疗方案变更
  （`plan_changed`）、跨年度保单切换（`policy_year_switched`）、审批（`approval`）、
  拒赔（`denial`）、付款（`payout`）、跟进标记（`follow_up_flagged`）等。
- 收据版本只增不改：供应商补传走 `POST /receipts/{id}/versions` 追加新版本，
  原始影像永远保留在 `versions[0]`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/players` | 登记球员 |
| POST | `/courses` | 开立疗程（`player_id`, `diagnosis`） |
| POST | `/policies` | 登记保单/福利计划（`payer_type`: `insurance`/`club`，免赔额、责任比例、年度额度、覆盖与排除类目、需预授权类目、补件期限） |
| POST | `/prescriptions` | 开立处方（治疗方案） |
| POST | `/prescriptions/{id}/revisions` | 治疗方案变更：作废旧版本并留下决定时点 |
| POST | `/pre-authorizations` | 登记预授权（有效期、核准金额、适用类目） |
| POST | `/receipts` | 登记收据原始影像（同供应商同票号重复登记返回 409） |
| POST | `/receipts/{id}/versions` | 供应商补传新版本，不覆盖原始影像 |
| GET | `/receipts/{id}` | 查看收据全部版本 |
| POST | `/claims` | 提交理赔：系统按 保险→俱乐部→个人 顺序拆分费用；重复索赔返回 409 及原申请当前状态 |
| GET | `/claims/{id}` | 申请详情与四类去向汇总 |
| POST | `/claims/{id}/supplements` | 补件（关联收据、补预授权）并自动重审待补件部分 |
| GET | `/players/{pid}/courses/{cid}/coverage` | 队医视角：按球员+疗程的累计免赔额、剩余额度与费用去向 |
| POST | `/payment-batches` | 按保单或支付方生成付款批次（已付费用不会重复付款） |
| GET | `/payment-batches/{id}` | 批次详情 |
| GET | `/payment-batches/{id}/trace` | 会计视角：从批次反查每张收据的责任依据（保单、比例、免赔、预授权、决定时点） |
| GET | `/follow-ups` | 补件期限到达的待补件案件，读取时自动标记需要跟进 |
| GET | `/events` | 审计事件，可按 `entity_type`/`entity_id` 过滤 |

## 费用拆分规则

每行费用依次经过：

1. 不在当前处方（治疗方案）内 → **已拒赔**（`not_in_prescription`）；
2. 缺少收据 → **待补件**（`receipt_missing`），进入补件期限倒计时；
3. 主商业保险要求预授权但缺失/过期 → **待补件**（`pre_authorization_missing` /
   `pre_authorization_expired`，过期会留下决定时点）；
4. 按支付方顺序结算：先扣免赔额（计入累计），再按责任比例与剩余额度赔付；
   免赔额与自付部分继续流向下一支付方，最终剩余为 **个人承担**；
5. 被所有支付方排除的服务类目 → **已拒赔**（`service_excluded`）。

免赔额与额度消耗以追加台账记录，补件重审只会追加新决定，不改写历史。
