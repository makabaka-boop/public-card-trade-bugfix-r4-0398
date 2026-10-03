# 四人模拟卡牌轮抽（FastAPI + WebSocket + SQLite + React）

四人各坐一个座位，每轮收到一包 **5 张牌**，秘密选择一张。只有当 **四人都提交**
或 **可控时钟超时** 时，服务器才在同一个事务里同时公开本轮选择，并把每人剩下的
4 张牌传给下一位、各补 1 张新牌，进入下一轮。

## 目录

```
backend/
  app/
    config.py     配置（人数、每包张数、默认轮数/时限）
    cards.py      卡牌目录 + 可复现的洗牌
    engine.py     纯函数事件溯源状态机（发牌/轮转/超时自动选择/投影）
    db.py         SQLite：游戏、玩家（只存令牌哈希）、append-only 事件表
    security.py   随机令牌 / SHA-256 / 恒定时间比较
    hub.py        WebSocket 扇出（按玩家各自投影，逐玩家保序）
    service.py    房间、每局异步锁、时钟看门狗、并发仲裁、重启恢复
    main.py       HTTP + WebSocket 接口
  tests/          28 个测试（真实 uvicorn + 真实 WebSocket + 真重启）
frontend/         React + Vite + TypeScript
```

## 运行

后端：

```bash
cd backend
pip install -r requirements.txt
./run.sh                       # http://localhost:8000  (文档 /docs)
# 可选环境变量：DRAFT_DB_PATH DRAFT_ROUNDS DRAFT_TIMEOUT DRAFT_ALLOW_ADMIN
```

前端：

```bash
cd frontend
npm install
npm run dev                    # http://localhost:5173 （已配置 /ws、/games 代理）
```

用 4 个浏览器标签加入同一房间号，入座后任一人点「开始游戏」。

## 接口

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/games` | 创建房间 `{rounds?, timeout?, seed?}` |
| POST | `/games/{id}/join` | 入座，**令牌只返回这一次**，服务端只存哈希 |
| POST | `/games/{id}/start` | 四人到齐后开局 |
| GET  | `/games/{id}?token=` | 重连快照，**按调用者投影** |
| WS   | `/ws/{id}?token=` | 下发 `state` / `pick_ack` / `error`，上行 `submit_pick` / `ping` |
| POST | `/admin/games/{id}/timeout` | 测试钩子：令当前轮立刻按超时结算 |
| GET  | `/admin/games/{id}/state` | 测试钩子：含所有秘密手牌的完整状态 |

WebSocket 帧：

```jsonc
// 服务端 -> 客户端（每个座位内容不同）
{ "type": "state", "state": { "my_pack": [/* 只有自己的5张 */],
  "my_pick": {...}, "lock_state": {"<pid>":"pending|locked", ...},
  "reveals": [/* 仅已公开轮次 */], "collections": {...}, ... } }
{ "type": "pick_ack", "card_id": 39 }
{ "type": "error", "code": "already_picked" }   // 仅泛化代码，无他人牌信息

// 客户端 -> 服务端
{ "type": "submit_pick", "card_id": 39 }
```

## 关键设计

### 1. 信息边界（每个玩家一套投影）

- 内存中的完整状态（`DraftState.packs` 含全部座位手牌）**从不直接下发**；
  `engine.project(state, viewer_id)` 为每个座位单独生成视图，`my_pack` 只含
  自己当前的 5 张牌。
- 其他座位在当前轮只暴露协调信息 `lock_state`（pending/locked），**不暴露选了哪张**。
- 只有 `round_revealed` 之后的牌才出现在 `reveals` / `collections` 中。
- 错误帧只有 `{type, code}`，HTTP 错误体只有一个代码字符串；非法令牌在
  WebSocket 握手阶段就被关闭（4401），不会先收到任何牌面。
- 重连快照（`GET /games/{id}`）同样按令牌投影，看不到他人未选的牌。

### 2. 重复点击 / 断线重连 / 并发提交只计一次

- 每局一把 `asyncio.Lock` 串行化所有状态迁移；提交在等待锁前捕获当前轮的
  **epoch**，拿锁后若轮次已被超时推进，直接返回 `round_advanced`，迟到的牌
  绝不会落到新一轮。
- 同一玩家重复提交同一张牌 → `duplicate`，只回 `pick_ack`、不写事件；
  改选另一张 → `already_picked`。同一玩家的多个 WebSocket（重连未及时断开旧连
  接、多标签）并发提交时，先拿锁者生效，另一个得到错误，而不是两张都记。
- SQLite `events (game_id, seq)` 上有 UNIQUE 约束，整批事件在一个
  `BEGIN IMMEDIATE` 事务写入，是「同一轮只结算一次」的持久层兜底。

### 3. 超时与人工提交竞争

- 时钟由 `loop.call_later(deadline-now)` 驱动；看门狗、人工凑齐、admin 强制、
  重启恢复都走同一个 `resolve_round` 纯函数路径。
- 每次 `round_opened` 给该轮实例一个单调递增的 `epoch`。结算必须匹配它在排队
  前捕获的 epoch：两个针对同一轮的结算（例如超时回调与凑齐四人）中只有第一个
  生效，第二个即使看到「有轮次开着」也会因为 epoch 已变而成为 no-op，不会把刚
  打开的下一轮误结算。
- 超时者按**稳定规则**自动选择：当前包里最小的卡牌 id（座位顺序决定多张缺交时
  的落库顺序，结果可复现）。

### 4. 持久化与重启恢复

- `events` 为 append-only 日志：`game_created / round_opened / pick_submitted /
  round_revealed / game_completed`。发牌位置 `cursor`、轮次 `epoch`、deadline
  都写进事件，重放是纯函数。
- 启动时对所有 active 局 `replay`：deadline 未到则重新挂看门狗；deadline 已过
  则立即按超时自动结算并打开下一轮（同样落库）。
- 令牌以 SHA-256 哈希存储，拿到数据库文件也无法直接冒名出牌。

## 测试

```bash
cd backend
python3 -m pytest -q
```

覆盖：

- **引擎**：发牌、余牌轮转且每包恒为 5 张、幂等提交、改选拒绝、超时自动选最小、
  事件重放逐字段一致、投影不含他人当前手牌/已秘密选择。
- **并发**：快速连点只记一张；同一玩家双 WebSocket 并发不同选择只记一张；
  最后一人人工提交与超时并发时结局唯一且自洽；两个同时的强制超时对同一轮不
  重复结算；真实挂钟超时；轮次推进后旧牌无法再选。
- **隐藏边界**：每个玩家所有收到的帧都与 admin 真相逐张比对；他人已锁定的
  秘密选择不出现在任何字段甚至原始 JSON 文本里；重连快照按座位隔离；伪造令牌
  HTTP 401 / WebSocket 4401；错误消息不含卡牌数据；坏 JSON 不会踢掉连接。
- **持久化**：部分提交后重启，用同一数据库启动全新服务实例，待选状态原样恢复、
  超时结算与首次运行完全一致；已完成轮次的选牌结果可重放；重启时若 deadline
  已过自动结算；重连后重复提交仍是幂等 ack。

测试在独立线程内用**独立事件循环 + 独立 app/Settings/Repository** 启动真实
uvicorn，因此「重启」是真正从空内存回放 SQLite，而不是复用进程内对象。

## 公开收藏交换

选择已公开的卡牌，创建两至四人的交换单。参与者均须有送出和收到的卡牌，同一张卡不能重复列入一单。所有参与者确认当前版本后整单交换；更改内容后重新确认。提出者可以修改或撤销，其他人只能确认。未公开手牌不能交换。历史选牌署名和结果不改变，当前收藏包含已成交交换；重连和重放一致。不预留卡牌，两单竞争同卡时只允许仍拥有全部送出卡牌的单成交。

POST /games/{id}/trades?token=... 接收 action(create/edit/confirm/cancel)、id、revision（创建除外）、offers（创建和修改使用）。offers 为 from/to/card_id 数组，from/to 使用玩家编号。页面包含交换单操作和确认按钮。重复确认不会重复交换。
