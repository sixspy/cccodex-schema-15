# cc-schema-lab

CC_Codex 二期（26.11）**数据工程**产品线：**契约换版台**（aiohttp + 原生 JS SPA + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**表契约版本登记 → 破坏性检查 → 换版应用 → 齐契入站闸口 → 目录对账**

与现有九线正交（不是采集、ETL、同步、解析、编排、摄取、留存、清单、水印）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（表契约 / 换版闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、aiohttp、SQLite |
| 前端 | 原生 JS SPA（全中文），暖杏纸面 + 墨绿强调；顶栏胶囊导航 |
| 测试 | pytest + pytest-aiohttp（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 表契约版本（表名 / 版本号 / 现行或草稿）
2. 对候选字段做破坏性检查（删列 / 改类型）
3. 换版应用（草稿升现行，旧现行停用）
4. 契约与目录表对账

## 齐契闸口（本期）

入站报文先过闸口，再谈入库。四处联查：**入站列表 · 门槛详情 · 拒收卡片 · 对账进度**。

### 工程凭证只择一

本仓选**旁路齐契凭证表**（`gate_credentials`），不启用唯一索引方案；选择理由落在
`gate_policy.rationale`，页上「门槛详情」可见。

- 凭证按 `table_name` 一行，携带 `contract_id / version_no / field_spec / fingerprint`。
- 换版应用时，契约状态、目录、凭证**同一事务原子切换**；中途失败整笔回滚。
- 入站校验、放行判断、页面核闸口都只读这一份凭证，所以**齐契与可入库同源**。
- 唯一索引方案没选：它只能约束「一表一现行」，挡不住未知列混进入库。

### 入站 → 放行 → 复验

| 环节 | 规则 |
|------|------|
| 入站 | 凭证未命中 → 缺契，整笔拒收；出现凭证外字段 → 未知列，整笔拒收；类型不符 → 拒收。**拒收不落库**，HTTP 422 + `ok=false`，不静默成功 |
| 放行 | 值班点放行时按**当前**凭证再核一遍，通过才写 `ingest_rows`；写失败整笔回滚，吐白话 |
| 回滚 | 已入库票可整笔回滚，行删除、状态置「已回滚」 |
| 并发 | 放行/回滚走 `BEGIN IMMEDIATE` + 状态守卫，左右并发点放行只写一次 |
| 对账复验 | 缺契已入库行整笔清掉并记台账；换版前按旧凭证入库的行只亮「换版前入库」，不擅自动数 |

### 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/gate` | 闸口凭证与选择理由 |
| POST | `/api/gate/check` | 页上核闸口（可带样例行，结论与入站一致） |
| POST | `/api/ingest` | 送一票入站报文 |
| GET | `/api/ingest-batches` | 入站列表（含命运签） |
| GET | `/api/ingest-batches/{id}` | 单票详情（落库行 + 台账） |
| POST | `/api/ingest-batches/{id}/release` | 值班放行 |
| POST | `/api/ingest-batches/{id}/rollback` | 整笔回滚 |
| GET | `/api/gate-events` | 编排台账 |

命运签：`齐契入库 / 待放行 / 缺契拦截 / 未知列拦截 / 类型拦截 / 整笔回滚`。

## 本地

```powershell
cd claude-workspace\cc-schema-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7100/login · **admin / 123456**

## Docker

端口 **7100**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
