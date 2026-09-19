# 运动康复费用理赔账本

面向运动员治疗费用、保险责任和俱乐部福利报销的 Python 后端服务（仅标准库，Python 3.11+）。

前台只需登记收据并提交理赔，**报销顺序由账本决定**（商业保险 → 俱乐部福利 → 个人账户），
避免手工填错顺序导致同一票据被两边重复申请。每笔费用被拆分为四类归属，金额守恒：

| 归属 | 含义 |
| --- | --- |
| `reimbursable` | 可报：某支付方核定承担 |
| `pending_documents` | 待补件：单据不齐，限期补传 |
| `personal` | 个人承担：所有支付方处理后的余额 |
| `denied` | 已拒赔：预授权过期/缺失、计划外项目等 |

## 运行

```bash
python3 src/index.py
```

服务默认监听 `8000` 端口，`GET /health` 确认进程状态。持久化写入 `.runtime/ledger.json`
（可用 `LEDGER_PATH` 覆盖），补件期限默认 30 天（`SUPPLEMENT_WINDOW_DAYS` 覆盖）。
执行测试：

```bash
python3 -m unittest discover -s tests
```

也可以运行 `docker compose up --build` 启动容器。

## 接口一览

所有接口均为 JSON。错误响应为 `{"error": {"code", "message", "details"}}`。

### 建模

- `POST /players` — 球员
- `POST /courses` — 疗程（伤病诊断）
- `POST /prescriptions` — 处方（治疗方案首版）
- `POST /prescriptions/{id}/revisions` — 治疗方案变更，记录 `decided_at` 决定时点
- `POST /policies` — 保单：支付方类型、免赔额、年度额度、责任比例、所需单据、是否需预授权
- `POST /pre-authorizations` — 预授权（有效期、额度、限定项目）

### 日常业务

- `POST /receipts` — 登记收据（服务时间、费用行、单据、影像）。同一球员+供应商+票号
  重复提交返回原收据（`duplicate: true`），补传影像只追加新版本，**原始影像不被覆盖**
- `POST /receipts/{id}/images` — 追加影像版本
- `POST /claims` — 提交理赔并拆分。同一收据重复索赔返回原申请及当前状态（`duplicate: true`）
- `POST /claims/{id}/supplements` — 补件后自动重算拆分
- `GET /claims/{id}` / `GET /claims?player_id=&course_id=&status=`

### 视图

- `GET /players/{pid}/courses/{cid}/summary` — 队医视图：按球员和疗程的累计免赔额、
  剩余额度、四类归属合计
- `POST /payment-batches` — 会计按保单结算可报部分
- `GET /payment-batches/{id}/trace` — 从付款批次反查每张收据的责任依据
  （比例、免赔额、预授权、方案版本、决定事件）
- `GET /follow-ups` — 补件期限到达、需要跟进的案件（读取时自动标记）
- `POST /maintenance/sweep` — 按指定时点主动触发跟进标记
- `GET /events?type=&entity_id=` — 审计事件（预授权过期、方案变更、跨年度切换等决定时点）

## 拆分规则

对收据每条费用行，按支付方顺序依次处理：

1. 先匹配服务日期生效的治疗方案版本，计划外项目整条拒赔（`outside_treatment_plan`）；
2. 每张保单：免赔额剩余部分先流出（计入免赔累计、由下一支付方承担），余额按责任比例
   拆出保单承担部分——缺单据归 `pending_documents`，预授权过期/缺失归 `denied`，
   否则归 `reimbursable`；自付与超年度限额/预授权额度的部分继续流向下一支付方；
3. 全部支付方处理完的余额归 `personal`。

免赔额与年度额度按球员 + 保单年度累计；补件重算时先回滚该理赔的累计影响再重算。
