# 排行榜系统设计：从数据结构到分布式高并发优化

> 腾讯面试真题深度拆解：排行服用什么结构存分数数据？秒级数据刷新如何优化？在 Ares 分布式框架中的落地方案。

---

## 一、问题拆解：这道题到底在考什么？

面试官抛出的不是一个问题，是三层递进考察：

| 层次 | 考察点 | 隐含问题 |
|------|--------|---------|
| **L1 数据结构** | 用什么存排行数据？ | 为什么选它而不是别的？ |
| **L2 写入优化** | 每秒都有数据刷新怎么办？ | 写放大、锁竞争、持久化 |
| **L3 分布式** | 多服场景怎么做？ | 一致性、分区、聚合 |

如果一个候选人只答"用 Redis Sorted Set"，面试官心里已经降级了。他要听的是**从数据结构原理到分布式工程实践的完整推演**。

---

## 二、数据结构选型：不是只有 Redis ZSet

### 2.1 候选结构对比

| 数据结构 | 插入复杂度 | 查询排名 | 查询 Top-K | 内存占用 | 适合场景 |
|----------|-----------|---------|-----------|---------|---------|
| **跳表 (Skip List)** | O(log N) | O(log N) | O(log N + K) | 中（指针开销） | 通用排行榜首选 |
| **红黑树 + 子树大小** | O(log N) | O(log N) | O(log N + K) | 低 | 需要范围查询 |
| **B+树** | O(log N) | O(log N) | O(log N + K) | 低（磁盘友好） | 亿级磁盘存储 |
| **分段桶 (Bucket)** | O(1) | O(1) 近似 | O(K) | 极低 | 近似排行即可 |
| **Fenwick 树（树状数组）** | O(log N) | O(log N) 前缀和 | — | 极低 | 分数是整数且有界 |
| **堆 (Top-K Heap)** | O(log K) | — | O(K log K) | 极低 | 只看 Top 100 |

**Redis Sorted Set 的底层就是跳表 + 哈希表的组合**。它不是唯一答案，而是最方便的答案。

### 2.2 为什么跳表是最佳通用解？

```
跳表 = 多层有序链表，每层以概率 p 向上提升

Level 3:  1 ──────────────── 9
Level 2:  1 ──────── 5 ────── 9
Level 1:  1 ── 3 ── 5 ── 7 ── 9
Level 0:  1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9

查找 7：从 Level 3 开始 → 1→9 跳过 2-8 → 降到 L2 → 1→5→9 → 降到 L1 → 5→7 命中
```

- **插入/删除/查找全部 O(log N)**，且常数因子极小
- **天然有序**，不需要像哈希表那样额外排序
- **无锁并发友好**：跳表可以做到 lock-free（Java ConcurrentSkipListMap 就是典范）
- Redis ZSet 在这个基础上加了哈希表做 O(1) 按 member 查找

### 2.3 什么时候不用跳表？

```
分段桶策略（适合：分数是离散值且有上限，如排位分 0-9999）：

Bucket[0-99]:   玩家A(95), 玩家B(87)
Bucket[100-199]: 玩家C(150), 玩家D(120)
...

排名查询：目标所在桶的累计偏移 + 桶内排序
时间复杂度：O(1) 定位 + O(桶大小) 桶内排序
```

- **桶大小控制在 100 以内** → 近似 O(1)
- 腾讯早期的王者荣耀排位就是用类似的分段策略（段位 + 星数天然分段）

---

## 三、秒级写入压力：写路径优化金字塔

```
                 ┌──────────────┐
                 │  架构层优化   │  ← 分区/聚合/降级
                 ├──────────────┤
                 │  内存层优化   │  ← 写缓冲/批量/MQ
                 ├──────────────┤
                 │  结构层优化   │  ← 无锁/分片/Delta
                 ├──────────────┤
                 │  网络层优化   │  ← 二进制/UDP/批量包
                 └──────────────┘
```

### 3.1 网络层：减少"写操作抵达服务端"的开销

**问题**：100 万在线玩家，每人每秒更新一次分数 → 100 万次网络 IO。

```
❌ 错误的做法：
  每个分数变化 → 立即发 TCP 包 → 服务端 → 立即写 Redis
  结果：100 万 TCP 连接/秒 + 100 万 Redis 写/秒 → 直接打爆

✅ 优化后的路径：
  客户端批量缓冲            服务端批量聚合
  ┌──────────┐            ┌──────────────┐
  │ 分数变化1 │            │ gRPC stream  │
  │ 分数变化2 │── 100ms ──→│  批量接收     │
  │ 分数变化3 │  批量发送   │  ↓           │
  │  ...     │            │ Write Buffer  │── 1s flush ──→ Redis
  └──────────┘            │  (RingBuffer) │
                          └──────────────┘
```

**具体手段：**

#### a) 客户端聚合发送

```rust
// 客户端侧：不是每次分数变化就发包
struct ScoreSender {
    buffer: VecDeque<ScoreUpdate>,
    last_flush: Instant,
    batch_interval: Duration,  // 100ms
    max_batch: usize,           // 最多攒 50 条
}

impl ScoreSender {
    fn push(&mut self, update: ScoreUpdate) {
        self.buffer.push_back(update);
        if self.buffer.len() >= self.max_batch
           || self.last_flush.elapsed() >= self.batch_interval {
            self.flush();  // 批量发送给 Gateway
        }
    }
}
```

#### b) Gateway 层流量整形

Gateway 收到客户端分数更新后，不直接转发给排行服，而是做一层整形：

```rust
// Gateway 侧：同玩家同类型分数，窗口内只保留最新值
struct ScoreAggregator {
    // player_id → (score_type, latest_value, timestamp)
    pending: LruCache<u64, HashMap<ScoreType, ScoreValue>>,
    window: Duration, // 200ms
}
```

**收益**：如果一个玩家在 200ms 内连续更新 5 次分数，只有最后一次会到达排行服。网络流量直接降到 1/5。

#### c) 二进制协议 + 连接复用

```
Protobuf 二进制：一条分数更新 ≈ 30 bytes  vs  JSON ≈ 120 bytes
gRPC HTTP/2 多路复用：1 条连接承载 1000 路并发请求
UDP 可选：对允许丢数据的场景（如实时投票排行），用 UDP 无连接发送
```

### 3.2 内存层：写缓冲与无锁结构

#### a) Ring Buffer 写缓冲（核心！）

这是抗住秒级写入最关键的一层：

```rust
use crossbeam::queue::ArrayQueue;
use std::sync::Arc;

struct WriteBuffer {
    // 无锁环形队列，容量 65536
    buffer: Arc<ArrayQueue<ScoreUpdate>>,
}

impl WriteBuffer {
    // 业务线程：无锁 push，不需要等 Redis
    fn submit(&self, update: ScoreUpdate) {
        // 满了就丢弃（方案选择：丢弃 vs 阻塞 vs 扩容）
        let _ = self.buffer.push(update);
    }
}

struct BatchWriter {
    buffer: Arc<ArrayQueue<ScoreUpdate>>,
    batch_size: usize,        // 每批 500 条
    flush_interval: Duration,  // 每 50ms 检查一次
}

impl BatchWriter {
    async fn run(&self) {
        let mut ticker = tokio::time::interval(self.flush_interval);
        loop {
            ticker.tick().await;
            let mut batch = Vec::with_capacity(self.batch_size);
            // 一次性捞出最多 batch_size 条
            for _ in 0..self.batch_size {
                match self.buffer.pop() {
                    Some(u) => batch.push(u),
                    None => break,
                }
            }
            if !batch.is_empty() {
                // Pipeline 批量写 Redis
                self.flush_to_redis(&batch).await;
            }
        }
    }
}
```

**关键参数调优：**

| 参数 | 值 | 权衡 |
|------|-----|------|
| buffer 容量 | 65536（2^16） | 大了浪费内存且延迟高，小了丢数据 |
| batch_size | 500 条/批 | 太小没批量化效果，太大单次 Redis 阻塞长 |
| flush_interval | 50ms | 排行榜的"实时性"阈值，50ms 人眼无感知 |

#### b) 分片写锁 — 消除热点 Key 竞争

如果所有玩家写同一个 Redis ZSet → 单 Key 热点 → Redis 单线程瓶颈：

```rust
// 方案：按分片 Key 分散写压力
fn score_shard_key(game_mode: &str, shard_id: u32) -> String {
    format!("rank:{}:shard:{}", game_mode, shard_id)
}

fn player_shard(player_id: u64, total_shards: u32) -> u32 {
    (player_id % total_shards as u64) as u32
}
```

```
单 Key 方案：     rank:arena → 所有玩家 → 单 Redis Key 热点
分片方案：        rank:arena:shard:0  → 玩家 0, 16, 32...
                rank:arena:shard:1  → 玩家 1, 17, 33...
                ...
                rank:arena:shard:15 → 16 个 Key 并行写入
```

**查询 Top-K 时**：从 16 个分片各自取 Top-K，然后内存中做 K 路归并：

```rust
async fn get_top_k(client: &RedisClient, game_mode: &str, k: usize) -> Vec<RankEntry> {
    let mut tops = Vec::new();
    for shard in 0..TOTAL_SHARDS {
        let key = score_shard_key(game_mode, shard);
        let shard_top = client.zrevrange_withscores(&key, 0, k as isize - 1).await?;
        tops.push(shard_top);
    }
    // K 路归并：16 个分片 × 各 K 条 → 归并取全局 Top K
    k_way_merge(tops, k)
}
```

#### c) Delta 写入 — 只传变化量

```
不是每次都传"玩家当前总分 10500"
而是传   "玩家分数 +300" (delta)

服务端: current = GET player:123 → 10200
        new_score = 10200 + 300 = 10500
        ZADD rank:arena 10500 player:123
```

**收益**：delta 是定长小整数（1-2 bytes），不是变长大整数。对需要校验的场景，附带一个版本号：

```rust
struct DeltaScoreUpdate {
    player_id: u64,
    delta: i32,         // 分数变化量，用 varint 编码 ≈ 1-2 字节
    version: u32,       // 客户端本地版本号，服务端校验幂等
}
```

### 3.3 架构层：读路径与写路径分离

每秒大量写入的真正痛点不是写操作本身，而是**写操作引发的连锁反应**。

#### 问题：写入导致大量读请求缓存失效

```
玩家 A 分数更新 → Redis ZSet 写
                → 所有在看排行榜的玩家缓存失效
                → 10000 个客户端同时拉取排行榜
                → 打爆服务器
```

#### 方案：快照读 + 增量写

```rust
struct RankSnapshot {
    data: Arc<Vec<RankEntry>>,   // 一份只读快照
    version: AtomicU64,
    generated_at: Instant,
}

struct RankingService {
    current_snapshot: RwLock<Arc<RankSnapshot>>,
    redis: RedisClient,
}

impl RankingService {
    // 写路径：只写 Redis，不立即更新快照
    async fn update_score(&self, player_id: u64, score: u64) {
        self.redis.zadd("rank:arena", score, player_id).await;
        // 不在此处更新快照——避免写放大
    }

    // 读路径：读内存快照，不回源 Redis
    async fn get_top_k(&self, k: usize) -> Arc<Vec<RankEntry>> {
        let snap = self.current_snapshot.read().unwrap();
        // 如果快照还在有效期内（如 1 秒），直接返回
        if snap.generated_at.elapsed() < Duration::from_secs(1) {
            return snap.data[..k.min(snap.data.len())].to_vec().into();
        }
        // 否则触发一次快照更新
        drop(snap);
        self.refresh_snapshot().await
    }

    // 快照更新：独立定时任务（如每 1 秒一次）
    async fn refresh_snapshot(&self) {
        let data = self.redis.zrevrange_withscores("rank:arena", 0, 1000).await?;
        let snapshot = Arc::new(RankSnapshot {
            data,
            version: AtomicU64::new(self.snapshot_version() + 1),
            generated_at: Instant::now(),
        });
        *self.current_snapshot.write().unwrap() = snapshot;
    }
}
```

**关键设计**：写路径只管写 Redis，读路径读内存快照。快照 1 秒刷新一次。写操作不触发读——这就是读写分离。

---

## 四、Ares 框架中的排行服务设计

### 4.1 服务定位

在 Ares 的五层架构中，排行服务是**独立微服务**，与 Game Server 平行部署：

```
Gateway (连接管理 + 流量整形)
    ├── Game Server (匹配、DB读写、分数变化事件)
    │       │
    │       └─── gRPC ───→ Ranking Service (排行存储 + 查询)
    │                           │
    │                           ├── Redis (主存储)
    │                           ├── 内存快照 (读缓存)
    │                           └── PostgreSQL (冷数据归档)
    │
    ├── Battle Server (战斗结果 → 分数变化事件)
    │
    └── Chat Server
```

### 4.2 Proto 定义

```protobuf
syntax = "proto3";
package ranking;

service RankingService {
    // 批量更新分数（Game/Battle Server 调用）
    rpc BatchUpdateScore(BatchScoreRequest) returns (BatchScoreResponse);

    // 查询排名（客户端通过 Gateway 透传）
    rpc GetMyRank(RankQuery) returns (RankResult);

    // 查询 Top-K（客户端通过 Gateway 透传）
    rpc GetTopK(TopKQuery) returns (TopKResult);
}

message ScoreUpdate {
    uint64 player_id = 1;
    string leaderboard_id = 2;  // "arena_s1", "guild_boss" 等
    int64 delta = 3;            // 增量
    uint32 version = 4;         // 客户端版本号（幂等校验）
}

message BatchScoreRequest {
    repeated ScoreUpdate updates = 1;
    string source = 2;          // "battle_server_01" 来源标识
}

message BatchScoreResponse {
    uint32 accepted = 1;        // 成功写入数
    uint32 rejected = 2;        // 拒绝数（版本冲突等）
}
```

### 4.3 服务内部架构

```
RankingService 进程内部
═══════════════════════════════════════════════

┌─────────────────────────────────────────────┐
│              gRPC Server (tonic)             │
│  BatchUpdateScore  │  GetMyRank  │  GetTopK │
└─────────┬──────────┴──────┬──────┴────┬─────┘
          │                 │           │
          ▼                 │           ▼
┌──────────────────┐       │    ┌─────────────────┐
│   WriteBuffer    │       │    │  SnapshotCache  │
│  (ArrayQueue)    │       │    │   Arc<Vec<...>> │
│  容量: 65536     │       │    │  TTL: 1 second  │
└────────┬─────────┘       │    └────────┬────────┘
         │                 │             │
         ▼                 │             │ (miss)
┌──────────────────┐       │             ▼
│   BatchWriter    │       │    ┌─────────────────┐
│  flush / 50ms    │       │    │  Redis Client   │
│  batch / 500条   │       │    │  ZREVRANGE      │
└────────┬─────────┘       │    │  ZRANK          │
         │                 │    └────────┬────────┘
         ▼                 │             │
┌──────────────────┐       │             │
│  Redis Pipeline  │       │             │
│  分片写入 16 key │       │             │
│  ZADD batch N条  │       │             │
└──────────────────┘       │             │
                           │             │
                   ┌───────┴─────────────┘
                   │   SnapshotRefresher
                   │   interval: 1 second
                   │   refresh() → 更新 Arc
                   └────────────────────
```

### 4.4 完整数据流

```
【写入链路】
Battle Server 结算
  → 计算分数变化 (delta)
  → gRPC BatchUpdateScore (攒 100ms 批量发)
    → Gateway (可选的二次整形)
      → Ranking Service
        → WriteBuffer.push()  [无锁, 纳秒级返回]
          → BatchWriter 每 50ms 捞一批
            → Redis Pipeline: 16分片并行 ZADD
              → 写入完成

【读取链路】
客户端请求排行榜
  → Gateway 转发 GetTopK
    → Ranking Service
      → 检查 SnapshotCache 是否在 TTL 内
        → 在 TTL 内：直接返回 Arc 快照 → 零 Redis 查询
        → TTL 过期：Redis ZREVRANGE → 更新 SnapshotCache → 返回

【快照刷新】(独立协程, 1s 定时)
  Redis ZREVRANGE 16分片 → K 路归并 → 更新 Arc<Snapshot>
```

---

## 五、网络层深度优化

### 5.1 Gateway 侧流量整形

```rust
// Gateway 维护一个 ScoreAggregator
// 同玩家 + 同榜单的多次更新，窗口内合并为最后一次

struct ScoreAggregator {
    // key: (player_id, leaderboard_id) → latest delta (累积)
    pending: DashMap<(u64, String), ScoreUpdate>,
    ticker: tokio::time::Interval,  // 200ms
}

impl ScoreAggregator {
    async fn run(&self, ranking_client: RankingServiceClient) {
        loop {
            self.ticker.tick().await;
            // 取出所有 pending 更新
            let batch: Vec<ScoreUpdate> = self.pending.drain()
                .map(|(_, v)| v)
                .collect();
            if !batch.is_empty() {
                // 批量发送给排行服
                let _ = ranking_client.batch_update_score(batch).await;
            }
        }
    }
}
```

**收益分析**：

```
假设：10万玩家在线，每人每2秒更新1次分数（战斗结算）

无优化：100K / 2s = 50K 次/秒  gRPC 调用 → Ranking Service
有整形：每个玩家 200ms 窗口只保留最新值
       → 有效写入 ≈ min(玩家数波动, 并发战斗结束数) ≈ 500/秒
       → 流量降到 1%
```

### 5.2 客户端推送代替轮询

```
传统模式（轮询）：
  客户端每 3 秒 GET /rank/top100 → 即使排行榜没变化也在请求

推送模式：
  服务端检测排行变化 → 推送 Delta 给在线客户端
  客户端本地更新排名显示
```

```rust
// 快照更新后，计算 diff，只推送变化部分
fn compute_rank_changes(
    old: &[RankEntry],
    new: &[RankEntry],
) -> Vec<RankChange> {
    let mut changes = Vec::new();
    for entry in new {
        let old_rank = old.iter().position(|e| e.player_id == entry.player_id)
            .map(|i| i + 1);
        if old_rank != Some(entry.rank) {
            changes.push(RankChange {
                player_id: entry.player_id,
                new_rank: entry.rank,
                old_rank,
                score_delta: entry.score - old.iter()
                    .find(|e| e.player_id == entry.player_id)
                    .map(|e| e.score).unwrap_or(0),
            });
        }
    }
    changes
}
```

**带宽对比**：Top 100 完整数据 ≈ 2KB vs Diff 变化推送（通常只有 3-5 个玩家变化）≈ 150 bytes。

### 5.3 UDP 可选路径（非必须）

对于允许丢数据的场景（如娱乐赛实时投票排行）：

```rust
// 用 UDP 发送分数更新
// 优点：无连接、无握手、无重传 → 延迟 < 1ms
// 代价：可能丢包，需应用层容忍
socket.send_to(&score_update.encode_to_vec(), ranking_addr);
```

**适用条件**：
- 分数更新是高频低价值（如实时互动投票，丢了下一秒会覆盖）
- 有降级策略（客户端本地展示预测分数，服务端异步校准）

---

## 六、内存优化深挖

### 6.1 紧凑数据结构

```rust
// ❌ 浪费：Vec<RankEntry> 每个条目 ~48 bytes + 堆分配
struct RankEntry {
    player_id: u64,    // 8 bytes
    player_name: String, // 24 bytes + 堆分配（名字）
    score: u64,        // 8 bytes
    rank: u32,         // 4 bytes
    // padding: 4 bytes
}
// 总计：≈ 64 bytes/entry

// ✅ 紧凑：用定长数组 + 名字存 ID
struct CompactRankEntry {
    player_id: u64,    // 8 bytes
    score: u64,        // 8 bytes
    // rank 不存——由数组索引推算
}
// 总计：16 bytes/entry → 节省 75%！

// 名字通过独立的 HashMap<player_id, PlayerDisplayInfo> 按需查询
```

对于 100 万玩家的排行，内存占用：
- 优化前：64 MB（仅排行数据）
- 优化后：16 MB（排行数据）+ 按需加载的展示信息

### 6.2 Arena Allocator 消除碎片

排行榜快照是高频重建的（每 1 秒一次），频繁分配/释放大量小对象会导致内存碎片：

```rust
use bumpalo::Bump;

struct SnapshotArena {
    arena: Bump,
}

impl SnapshotArena {
    fn build_snapshot(&self, raw: &[RedisEntry]) -> &[CompactRankEntry] {
        // 一次性在 arena 中分配所有条目
        self.arena.alloc_slice_fill_iter(
            raw.iter().enumerate().map(|(i, e)| CompactRankEntry {
                player_id: e.player_id,
                score: e.score,
            })
        )
    }

    fn rotate(&mut self) {
        // 新快照 → 新 arena，旧 arena 整体释放
        self.arena.reset();
    }
}
```

**Arena 优势**：所有条目在连续内存中，无碎片，reset 是 O(1)。

### 6.3 CPU 缓存行对齐

热点数据的 false sharing 问题：

```rust
// ❌ 错误：version 和 data 在同一缓存行
// 写线程更新 version → 读线程的 data 缓存行被 invalidate
struct SnapshotBad {
    version: AtomicU64,  // 热写
    data: Vec<RankEntry>,  // 热读 → false sharing!
}

// ✅ 正确：padding 到不同缓存行
#[repr(align(64))]
struct SnapshotGood {
    version: AtomicU64,
    _pad: [u8; 56],  // 填充到 64 bytes
    data: Vec<RankEntry>,  // 自己的缓存行
}
```

---

## 七、极端场景下的降级策略

### 7.1 写缓冲区满了怎么办？

```rust
enum OverflowPolicy {
    Drop,              // 丢弃最旧 → 适合"分数变化频繁，丢一两个无所谓"
    DropNewest,        // 拒绝新写入 → 适合"顺序重要"的场景
    Block(Duration),   // 阻塞等待（最多 N ms）→ 适合强一致性
    Expand,            // 动态扩容 → 需要内存预算
}
```

**推荐**：业务层用 `Drop` + 客户端重试。因为分数更新的特点是"后续会覆盖"，丢一两个不影响最终排名。

### 7.2 Redis 挂了怎么办？

```
降级路径：
  Redis 不可用
    → 写请求：全部写入本地 WriteBuffer + 持久化日志
    → 读请求：返回最后一个有效快照（带"数据延迟"标识）
    → Redis 恢复后：回放日志 → 更新快照 → 清除降级标识
```

### 7.3 排行榜精确度 vs 性能权衡

| 场景 | 允许延迟 | 方案 |
|------|---------|------|
| 赛季排行榜 | 5-10 秒 | 快照 + 定时刷新 |
| 实时对战积分 | 1 秒 | 快照 + 增量推送 |
| 电竞比赛直播 | < 500ms | 直读 Redis（牺牲吞吐） |
| 活动实时投票 | 可丢数据 | UDP + 客户端预测 |

---

## 八、面试话术：如何回答这道题

> 面试官："排行服要用什么结构存储分数数据？如果每秒钟都有数据刷新的话怎么优化？"

**30 秒回答**（先给结论）：

> 底层用**跳表**，也就是 Redis Sorted Set 背后的数据结构，O(log N) 的插入和排名查询。但面对秒级写入压力，核心优化不在数据结构层面，而在**写缓冲 + 读写分离**：用无锁 Ring Buffer 做写入缓冲，批量 Pipeline 写入 Redis 分片 Key，读路径用秒级快照缓存，写不触发读。

**3 分钟展开**（展示深度）：

> 第一层，数据结构选跳表而不是堆或红黑树，因为跳表支持范围查询和按排名查询，而且可以做 lock-free 实现。实际工程中用 Redis ZSet 起步，但如果需要更高性能，可以在应用层用 crossbeam 的无锁跳表自建。
>
> 第二层，秒级写入优化的核心是**减少实际落到存储层的写入次数**。客户端侧攒 100ms 批量发送，Gateway 侧做窗口去重（同一玩家 200ms 内多次更新只保留最后一次），服务端用无锁 Ring Buffer → 50ms 一批 → Redis Pipeline 分片并行写入。这样把 10 万/秒的写入请求收缩到几百次/秒的实际存储操作。
>
> 第三层，读写分离。排行榜的读请求量通常是写请求的 100 倍以上。用秒级快照做读缓存，写操作只写 Redis 不重建快照，快照由独立协程每秒异步刷新。客户端读的是内存快照 Arc 指针，零拷贝零锁竞争。
>
> 第四层，分布式分片。按玩家 ID 哈希分成 16 个 Redis Key，写入并行，查询时 K 路归并。单个 Redis 不会被热点 Key 打爆。

---

## 九、方案总结

```
                    排行榜高并发优化全景
═══════════════════════════════════════════════════════════

  客户端层          网络层             服务层           存储层
 ┌─────────┐    ┌──────────┐    ┌──────────────┐    ┌─────────┐
 │ 批量缓冲 │ →  │ 流量整形  │ →  │ Ring Buffer  │ →  │ 分片Key │
 │ 100ms   │    │ 窗口去重  │    │ 50ms批量     │    │ 16分片  │
 │ delta发送│    │ gRPC流   │    │ Pipeline写   │    │ Pipeline│
 └─────────┘    └──────────┘    └──────────────┘    └─────────┘
                                      │
                                      │ 写路径不触发读
                                      ▼
                               ┌──────────────┐
                               │ 快照缓存      │
                               │ 1秒刷新       │
                               │ Arc<Vec<..>> │
                               └──────────────┘
                                      │
                                      │ 零拷贝读取
                                      ▼
                               ┌──────────────┐
                               │ 客户端拉取    │
                               │ 或 Diff 推送  │
                               └──────────────┘

═══════════════════════════════════════════════════════════
  核心原则：
  1. 写走缓冲，读走快照 —— 读写彻底分离
  2. 客户端聚合 + 服务端去重 —— 收缩写入量
  3. 分片并行 + Pipeline 批量化 —— 榨干 Redis 吞吐
  4. 降级路径必须在设计阶段预留 —— 不是出了问题再打补丁
```

---

## 十、与数据同步一致性的关系

本文与上一篇文章《游戏数据同步一致性体系》形成互补：

| | 数据同步一致性 | 排行榜系统 |
|------|--------------|-----------|
| 关注点 | 多副本状态一致 | 高并发写入性能 |
| 一致性级别 | 强一致 / 最终一致 | 最终一致（1-5 秒延迟可接受） |
| 核心冲突 | 并发写入冲突 | 写入热点 + 读放大 |
| 解决思路 | CRDT / Saga / 版本向量 | 缓冲批量化 + 读写分离 + 分片 |

**交集**：当排行数据需要跨服一致时（如全局全服排行榜），本文的写缓冲 + 分片策略需与上一篇文章的版本向量 + Saga 结合使用——写缓冲保证性能，版本向量保证一致性。
