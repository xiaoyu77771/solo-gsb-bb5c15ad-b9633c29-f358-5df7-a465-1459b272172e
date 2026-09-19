# 文件交接留痕服务（Handoff Chain）

基于 **FastAPI + SQLite** 的多人文件交接留痕服务。每次交接形成一条只可追加的
**SHA-256 哈希链**，可查询、可审计、可完整性校验；并发移交与随机数重放由服务端
原子拒绝，**链条不会分叉**。

## 核心规则

- **创建链条**：提交文件 `SHA-256` 摘要与首位持有人，服务端写入序号 `0` 的创世事件：
  ```json
  {"file_hash": "<文件摘要>", "holder": "<首位持有人>", "seq": 0}
  ```
  对其键名排序的**紧凑 JSON**（无空白）计算 SHA-256，即创世哈希（初始链尾）。
- **执行移交**：每次请求须携带
  - `tail_hash`：客户端认定的**当前链尾哈希**；
  - `receiver`：接收方；
  - `nonce`：一次性随机数（同一链内用过即作废，防重放）。

  交出方（`giver`）由服务端依据当前链尾持有人确定，客户端无法伪造。
  新事件哈希按以下紧凑 JSON（`sort_keys` 后）计算：
  ```json
  {"giver":"...","nonce":"...","prev_hash":"<前序哈希>","receiver":"...","seq":<序号>}
  ```
  服务端在**同一个 SQLite 立即事务**中完成「校验链尾 → 查重随机数 → 追加事件 →
  推进链尾」，全程串行。链尾不匹配（过期/竞争请求）或随机数重放一律返回
  **`409 Conflict`**，不写入任何数据，因而不可能产生分叉。
- **待接收移交（邀约确认）**：除直接移交外，持有人也可先发起邀约、由接收方确认：
  1. **发起邀约** `POST /offers`：携带当前 `tail_hash`、指定的 `receiver`、
     有效期 `expires_in_seconds`（秒，上限 7 天）与一次性确认码
     `confirmation_code`。服务端**只保存确认码的 SHA-256 摘要**（明文不落库），
     返回 `offer_id`；链尾不匹配返回 **409**。
  2. **确认邀约** `POST /offers/{offer_id}/confirm`：接收方凭 `offer_id` 与
     确认码确认。服务端在**同一个 SQLite 立即事务**中依次校验「确认码摘要匹配 →
     邀约未过期 → 邀约未使用 →（见证模式下）见证同意数达阈值且无拒绝 →
     链尾仍匹配」，全部通过后追加唯一移交事件
     （`nonce` 取 `offer:<offer_id>`，天然一次性）并将邀约置为 `accepted`，
     整体原子提交。**过期（`offer_expired`）、重复确认（`offer_already_accepted`）、
     见证拒绝（`offer_rejected`）、同意数不足（`witness_quorum_not_met`）、
     链尾已推进（`tail_mismatch`）均返回 409 且不写任何事件**；
     确认码错误返回 **403**（`code_mismatch`）。
  3. **查询邀约** `GET /offers/{offer_id}`：返回邀约状态
     （`pending` / `accepted` / `expired` / `rejected`，后两者为派生状态）
     与确认结果，见证模式下附带通过阈值与表态统计；
     不含确认码、令牌及其摘要。
- **见证确认模式（可选）**：发起邀约时可一并提交 `witnesses`（2~5 名见证人，
  各自携带独立的一次性令牌）与通过阈值 `witness_threshold`
  （两者必须同时提供或同时缺省；缺省即无见证邀约，行为与旧版完全一致）：
  1. 服务端**只保存令牌的 SHA-256 摘要**（明文不落库），发起方通过线下渠道
     把各令牌分发给对应见证人；
  2. **见证人表态** `POST /offers/{offer_id}/witnesses`：凭令牌 + `decision`
     （`approve` / `reject`）表态，**同一见证人只能表态一次**
     （重复表态返回 409 `witness_already_decided`；令牌错误返回 403
     `token_mismatch`）。任一见证人**拒绝即终止邀约**（派生状态 `rejected`），
     之后的表态与确认一律 409；邀约已接受、已过期或链尾已推进时表态同样 409，
     均不落库；
  3. **查询见证状态** `GET /offers/{offer_id}/witnesses`：返回通过阈值、
     各见证人表态（`pending` / `approve` / `reject`）与统计，
     **响应不含令牌及其摘要**；
  4. 接收方确认时，只有**同意数达到阈值且无任何拒绝**，才在同一个
     SQLite 立即事务内校验邀约、见证结果与链尾并追加移交事件。
- **完整性校验**：服务端从创世记录起逐条重算哈希、校验序号连续性与 `prev_hash`
  指针，并比对链尾缓存；校验结果给出 **首个断裂序号 `first_broken_seq`**。

## 一条命令启动（依赖安装 + 数据库初始化 + 启动）

需要已安装 Docker（含 Compose 插件）。在项目根目录执行：

```bash
docker compose up --build
```

该命令会：构建镜像（其中 `pip install -r requirements.txt` 完成依赖安装）→
容器启动时自动初始化 SQLite 数据库（自动建表，数据位于命名卷 `handoff-data`，
库文件 `/data/handoff.db`）→ 启动 API 服务。

### 访问地址与端口

| 用途 | 地址 |
| --- | --- |
| API 服务根地址 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| 交互式文档（Swagger UI） | <http://localhost:8000/docs> |
| OpenAPI 描述 | <http://localhost:8000/openapi.json> |

后台运行：`docker compose up --build -d`；停止：`docker compose down`
（数据保留在卷中；如需连数据一并清除：`docker compose down -v`）。

### 不用 Docker 的本地启动方式

```bash
pip install -r requirements.txt
DATA_DIR=./data python -m app.main        # 自动建库建表后启动
# 或显式初始化：
python -c "from app.database import init_db; init_db()"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/chains` | 创建交接链（文件摘要 + 首位持有人） |
| `POST` | `/handovers` | 执行一次移交（链尾哈希 + 接收方 + 一次性随机数） |
| `POST` | `/offers` | 发起待接收移交邀约（链尾 + 接收方 + 有效期 + 一次性确认码，只存摘要；可选 2~5 名见证人 + 通过阈值） |
| `POST` | `/offers/{offer_id}/confirm` | 接收方凭确认码确认邀约，原子追加移交事件（见证模式下须同意数达阈值且无拒绝） |
| `POST` | `/offers/{offer_id}/witnesses` | 见证人凭一次性令牌表态（approve / reject），一人一次 |
| `GET`  | `/offers/{offer_id}/witnesses` | 查询见证状态（阈值 + 各见证人表态，不含令牌摘要） |
| `GET`  | `/offers/{offer_id}` | 查询单个邀约状态（pending / accepted / expired / rejected） |
| `GET`  | `/chains` | 列出全部链条 |
| `GET`  | `/chains/{chain_id}` | 查询链条与全部事件 |
| `GET`  | `/chains/{chain_id}/verify` | 完整性校验，指出首个断裂序号 |
| `GET`  | `/health` | 健康检查 |

错误响应统一为：`{"error": "<code>", "detail": "<说明>"}`。状态码约定：
**409** —— `tail_mismatch`（链尾不匹配/已推进）、`nonce_replay`（随机数重放）、
`offer_expired`（邀约已过期）、`offer_already_accepted`（邀约重复确认）、
`offer_rejected`（已有见证人拒绝，邀约终止）、
`witness_quorum_not_met`（见证同意数未达阈值）、
`witness_already_decided`（同一见证人重复表态）；
**403** —— `code_mismatch`（确认码错误）、`token_mismatch`（见证令牌错误）；
**404** —— 链条或邀约不存在；**400** —— `invalid_witness_config`
（见证人列表与通过阈值缺失或不匹配）。

## 使用示例

```bash
# 1) 计算文件摘要并创建链条
FILE_HASH=$(sha256sum some-file.bin | awk '{print $1}')
curl -s -X POST http://localhost:8000/chains \
  -H 'Content-Type: application/json' \
  -d "{\"file_hash\": \"$FILE_HASH\", \"first_holder\": \"alice\"}"

# 响应中含 chain_id 与 tail_hash（创世哈希），例如：
# {"chain_id":"ab12...","tail_hash":"9f0c...","current_holder":"alice", ...}

# 2) alice 移交给 bob（必须带上当前 tail_hash 和全新 nonce）
curl -s -X POST http://localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"9f0c...","receiver":"bob","nonce":"n-0001"}'
# 返回新的 event_hash（即新链尾），下一次移交必须携带它

# 3) 用旧链尾再交一次 → 409 tail_mismatch（拒绝，不分叉）
curl -i -X POST http://localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"9f0c...","receiver":"carol","nonce":"n-0002"}'

# 4) 随机数重放 → 409 nonce_replay
curl -i -X POST http://localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<新链尾>","receiver":"carol","nonce":"n-0001"}'

# 5) 查询链条
curl -s http://localhost:8000/chains/ab12...

# 6) 完整性校验（正常：valid=true）
curl -s http://localhost:8000/chains/ab12.../verify
```

### 待接收移交（邀约确认）示例

```bash
# 1) alice 以当前链尾发起邀约：指定 bob、1 小时有效、确认码 "s3cret"
curl -s -X POST http://localhost:8000/offers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<当前链尾>","receiver":"bob",
       "expires_in_seconds":3600,"confirmation_code":"s3cret"}'
# 响应：{"offer_id":"7c4e...","status":"pending","expires_at":"...", ...}
# 服务端只保存确认码摘要；alice 通过线下渠道把 offer_id 与确认码告知 bob

# 2) bob 凭 offer_id + 确认码确认 → 原子追加移交事件，邀约置为 accepted
curl -s -X POST http://localhost:8000/offers/7c4e.../confirm \
  -H 'Content-Type: application/json' \
  -d '{"confirmation_code":"s3cret"}'
# 响应与移交结果一致：{"seq":2,"event_hash":"<新链尾>","giver":"alice","receiver":"bob", ...}

# 3) 再确认一次 → 409 offer_already_accepted；确认码错误 → 403 code_mismatch；
#    过期 → 409 offer_expired；期间链尾被其他移交推进 → 409 tail_mismatch；
#    以上均不写任何事件

# 4) 查询邀约状态（pending / accepted / expired）
curl -s http://localhost:8000/offers/7c4e...
```

### 见证确认模式示例

```bash
# 1) alice 发起带见证的邀约：3 名见证人（各配独立一次性令牌）、阈值 2
curl -s -X POST http://localhost:8000/offers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<当前链尾>","receiver":"bob",
       "expires_in_seconds":3600,"confirmation_code":"s3cret",
       "witness_threshold":2,
       "witnesses":[{"name":"carol","token":"w-tok-carol"},
                    {"name":"dave","token":"w-tok-dave"},
                    {"name":"erin","token":"w-tok-erin"}]}'
# 响应：{"offer_id":"9e8f...","witness_threshold":2,"witness_count":3,...}
# 服务端只保存令牌摘要；alice 通过线下渠道把各令牌分发给对应见证人

# 2) 见证人 carol、dave 分别凭各自令牌表态同意（同一令牌只能用一次）
curl -s -X POST http://localhost:8000/offers/9e8f.../witnesses \
  -H 'Content-Type: application/json' \
  -d '{"token":"w-tok-carol","decision":"approve"}'
curl -s -X POST http://localhost:8000/offers/9e8f.../witnesses \
  -H 'Content-Type: application/json' \
  -d '{"token":"w-tok-dave","decision":"approve"}'

# 3) 查询见证状态（不含令牌及其摘要）
curl -s http://localhost:8000/offers/9e8f.../witnesses
# {"witness_threshold":2,"approvals":2,"rejections":0,"pending_count":1,
#  "quorum_reached":true,"witnesses":[{"witness":"carol","decision":"approve",...},...]}

# 4) 同意数已达阈值且无拒绝 → bob 凭确认码确认，原子追加移交事件
curl -s -X POST http://localhost:8000/offers/9e8f.../confirm \
  -H 'Content-Type: application/json' \
  -d '{"confirmation_code":"s3cret"}'

# 5) 异常情形（均不写事件）：
#    同意数不足时确认        → 409 witness_quorum_not_met
#    任一见证人 reject 后    → 邀约终止，确认/再表态均 409 offer_rejected
#    同一见证人重复表态      → 409 witness_already_decided
#    令牌错误                → 403 token_mismatch
#    过期 / 链尾已推进       → 409 offer_expired / tail_mismatch
```

校验响应示例（正常）：

```json
{
  "chain_id": "ab12...",
  "valid": true,
  "length": 3,
  "expected_next_seq": 3,
  "first_broken_seq": null,
  "reason": null,
  "recomputed_tail_hash": "…",
  "stored_tail_hash": "…"
}
```

若中途某条事件被篡改，响应形如
`{"valid": false, "first_broken_seq": 2, "reason": "事件哈希不匹配：…"}`。

## 防分叉与并发保证

1. 每次追加都在 `BEGIN IMMEDIATE` 事务内完成，且进程内写操作由互斥锁串行化；
   「读链尾 → 比对 → 插入 → 推进」是一个原子步骤。邀约确认同样如此：
   「验码 → 未过期 → 未使用 →（见证模式）无拒绝且同意数达阈值 → 链尾匹配 →
   追加事件 → 置 accepted」全部在同一立即事务内，任一校验失败即整体回滚。
   见证人表态也在同一套串行写事务中完成，与确认互斥，不会读到中间状态。
2. 数据库层设有 `UNIQUE(chain_id, seq)` 与 `UNIQUE(chain_id, nonce)` 唯一约束
   作为兜底，任何竞争写入都无法产生重号事件或重放随机数；邀约确认使用
   派生随机数 `offer:<offer_id>`，同一邀约并发确认最多成功一次。
   见证侧由 `PRIMARY KEY(offer_id, witness)` 与 `UNIQUE(offer_id, token_digest)`
   兜底，同一见证人不可能登记两次或表态两次。
3. WAL 日志模式 + busy timeout，兼顾读写并发与单写入者语义。
4. 数据库初始化（`init_db`）全部使用 `CREATE ... IF NOT EXISTS`，可重复执行；
   对已有数据文件只做增量补表（含新增的 `offer_witnesses` 表）与增量补列
   （`offers.witness_threshold`），存量数据与无见证邀约行为不受影响。

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与启动
│   ├── service.py     # 哈希链核心逻辑（创建 / 追加 / 邀约确认 / 见证表态 / 查询 / 校验）
│   ├── database.py    # SQLite 连接、建表（chains / events / offers / offer_witnesses）与串行写事务
│   ├── hashing.py     # 紧凑 JSON 规范与 SHA-256
│   ├── schemas.py     # Pydantic 请求/响应模型
│   └── config.py      # 路径与端口配置
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh
├── requirements.txt
└── README.md
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `/data` | 数据目录 |
| `DB_PATH` | `$DATA_DIR/handoff.db` | SQLite 文件路径 |
| `PORT` | `8000` | 监听端口（容器内，Compose 映射同为 8000） |
| `HOST` | `0.0.0.0` | 监听地址 |
