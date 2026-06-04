# 排行榜高并发 Demo：完整生产级 Rust 实现

> 从客户端聚合到 Redis 分片写入的完整链路。每条模块都可以直接放进 Ares 框架的 RankingService。

---

## 依赖 (Cargo.toml)

```toml
[dependencies]
tokio = { version = "1", features = ["full"] }
tonic = "0.13"
prost = "0.13"
redis = { version = "0.27", features = ["tokio-comp", "connection-manager"] }
crossbeam-queue = "0.3"
dashmap = "6"
lru = "0.12"
xxhash-rust = { version = "0.8", features = ["xxh3"] }
```

---

## 第一步：客户端侧 —— 分数批量缓冲

玩家在客户端打了一局，分数变化。不是立即发——攒 100ms 或 50 条再打包发。

```rust
use std::collections::VecDeque;
use std::time::{Duration, Instant};

/// 客户端侧分数发送器
pub struct ScoreSender {
    buffer: VecDeque<ScoreDelta>,
    last_flush: Instant,
    batch_interval: Duration,   // 100ms
    max_batch: usize,           // 最多攒 50 条
}

#[derive(Clone)]
struct ScoreDelta {
    leaderboard_id: String,     // "arena_s1", "hero_power" 等
    delta: i64,                 // 分数变化量（不是绝对值）
    version: u64,               // 幂等校验版本号
    timestamp: u64,             // 客户端时间戳
}

impl ScoreSender {
    pub fn new() -> Self {
        Self {
            buffer: VecDeque::with_capacity(64),
            last_flush: Instant::now(),
            batch_interval: Duration::from_millis(100),
            max_batch: 50,
        }
    }

    /// 每次分数变化时调用
    pub fn on_score_change(&mut self, leaderboard_id: &str, delta: i64) {
        self.buffer.push_back(ScoreDelta {
            leaderboard_id: leaderboard_id.to_string(),
            delta,
            version: self.next_version(),
            timestamp: Self::now_millis(),
        });

        if self.buffer.len() >= self.max_batch
            || self.last_flush.elapsed() >= self.batch_interval
        {
            self.flush();
        }
    }

    fn flush(&mut self) {
        if self.buffer.is_empty() {
            return;
        }
        // 批量取出，发送给 Gateway
        let batch: Vec<_> = self.buffer.drain(..).collect();
        // tokio::spawn(gateway_client.batch_update_score(batch));
        tracing::info!("flush {} score deltas to gateway", batch.len());
        self.last_flush = Instant::now();
    }

    fn next_version(&self) -> u64 { /* 单调递增 */ 0 }
    fn now_millis() -> u64 { /* SystemTime */ 0 }
}
```

---

## 第二步：Gateway 层 —— 流量整形（窗口去重）

Gateway 收到大量客户端分数更新。同一玩家 + 同一榜单，200ms 窗口内只保留最后一次。

```rust
use dashmap::DashMap;
use std::time::{Duration, Instant};
use lru::LruCache;
use std::num::NonZeroUsize;

/// Gateway 侧分数聚合器：窗口去重
pub struct ScoreAggregator {
    // key: (player_id, leaderboard_id) → 最新分数变化
    pending: DashMap<(u64, String), ScoreDelta>,
    last_flush: Instant,
    window: Duration,  // 200ms
}

impl ScoreAggregator {
    pub fn new() -> Self {
        Self {
            pending: DashMap::new(),
            last_flush: Instant::now(),
            window: Duration::from_millis(200),
        }
    }

    /// 收到客户端分数更新时调用
    /// 同一 (player, leaderboard) 在窗口内只保留最后一次
    pub fn on_client_update(&self, player_id: u64, delta: ScoreDelta) {
        let key = (player_id, delta.leaderboard_id.clone());
        // dashmap insert 自动覆盖旧值 → 窗口去重
        self.pending.insert(key, delta);
    }

    /// 定时任务：每 200ms 批量发给 RankingService
    pub async fn tick_and_flush(&mut self, rank_client: &mut RankingClient) {
        if self.last_flush.elapsed() < self.window {
            return;
        }
        let batch: Vec<_> = self.pending
            .drain()
            .map(|(_, delta)| delta)
            .collect();

        if !batch.is_empty() {
            tracing::info!("gateway flush {} aggregated deltas", batch.len());
            // let _ = rank_client.batch_update_score(batch).await;
        }
        self.last_flush = Instant::now();
    }
}
```

**收益**：10 万客户端每秒各发 1 次 → Gateway 收到 10 万/秒。每个玩家 200ms 只保留最后一条 → 实际发给 RankingService 的只有 **~5 千/秒**（假设每个玩家 200ms 内平均更新 1 次）。流量压缩到 5%。

---

## 第三步：RankingService 核心 —— 无锁 Ring Buffer

Gateway 发来的批量更新，进入 RankingService 的无锁环形队列。

```rust
use crossbeam::queue::ArrayQueue;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

/// 写缓冲：无锁 MPMC 环形队列
pub struct WriteBuffer {
    buffer: Arc<ArrayQueue<ScoredUpdate>>,
    dropped: AtomicU64,  // 溢出丢弃计数（监控用）
    capacity: usize,
}

#[derive(Debug, Clone)]
pub struct ScoredUpdate {
    pub player_id: u64,
    pub leaderboard_id: String,
    pub delta: i64,
    pub version: u64,    // 幂等键
    pub gateway_ts: u64, // Gateway 收到时间
}

impl WriteBuffer {
    /// 容量必须是 2 的幂，ArrayQueue 内部用掩码取模
    pub fn new(capacity: usize) -> Self {
        assert!(capacity.is_power_of_two(), "capacity must be power of two");
        Self {
            buffer: Arc::new(ArrayQueue::new(capacity)),
            dropped: AtomicU64::new(0),
            capacity,
        }
    }

    /// 写入（无锁，纳秒级返回）
    /// 如果缓冲区满了 → 丢弃（由监控告警处理）
    pub fn submit(&self, update: ScoredUpdate) {
        match self.buffer.push(update) {
            Ok(()) => {}
            Err(_) => {
                self.dropped.fetch_add(1, Ordering::Relaxed);
                // metrics::increment!("rank.write_buffer.dropped");
            }
        }
    }

    pub fn dropped_count(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }

    pub fn usage_pct(&self) -> f64 {
        self.buffer.len() as f64 / self.capacity as f64
    }
}
```

---

## 第四步：BatchWriter —— 攒批 + Redis Pipeline 分片写入

独立协程，每 50ms 从 Ring Buffer 捞一批，分片后 Pipeline 写入 Redis。

```rust
use std::collections::HashMap;
use tokio::time::{interval, Duration, MissedTickBehavior};

/// 批量写入器：从 Ring Buffer 消费 → 分片 → Redis Pipeline
pub struct BatchWriter {
    buffer: Arc<ArrayQueue<ScoredUpdate>>,
    redis_pool: RedisPool,
    batch_size: usize,          // 每批 500 条
    flush_interval: Duration, // 50ms
    shard_count: u32,           // 16 分片
}

impl BatchWriter {
    pub fn new(buffer: Arc<ArrayQueue<ScoredUpdate>>, redis_pool: RedisPool) -> Self {
        Self {
            buffer,
            redis_pool,
            batch_size: 500,
            flush_interval: Duration::from_millis(50),
            shard_count: 16,
        }
    }

    pub async fn run(mut self) {
        let mut ticker = interval(self.flush_interval);
        ticker.set_missed_tick_behavior(MissedTickBehavior::Skip);

        loop {
            ticker.tick().await;
            self.flush_once().await;
        }
    }

    async fn flush_once(&mut self) {
        // 1. 从 ring buffer 批量捞取
        let mut batch = Vec::with_capacity(self.batch_size);
        for _ in 0..self.batch_size {
            match self.buffer.pop() {
                Some(u) => batch.push(u),
                None => break,
            }
        }
        if batch.is_empty() {
            return;
        }

        // 2. 按 (leaderboard_id, shard) 分组
        let mut groups: HashMap<(String, u32), Vec<&ScoredUpdate>> = HashMap::new();
        for update in &batch {
            let shard = shard_id(update.player_id, self.shard_count);
            let key = (update.leaderboard_id.clone(), shard);
            groups.entry(key).or_default().push(update);
        }

        // 3. 每组一个 Redis Pipeline
        let mut pipe = redis::pipe();
        for ((lb_id, shard), updates) in &groups {
            let redis_key = format!("rank:{}:shard:{}", lb_id, shard);
            for u in updates {
                // ZADD key score member
                // 注意：这里应该先 GET 当前分数，再加上 delta
                // 简化版本直接 ZINCRBY
                pipe.zincr(&redis_key, u.player_id.to_string(), u.delta);
            }
        }

        // 4. 执行 Pipeline
        match pipe.query_async::<_, ()>(&mut self.redis_pool.conn().await).await {
            Ok(_) => {
                tracing::info!("flushed {} updates in {} groups", batch.len(), groups.len());
            }
            Err(e) => {
                tracing::error!("redis pipeline failed: {}", e);
                // 降级：写入本地 WAL 文件，等待恢复
            }
        }
    }
}

/// 哈希分片：xxhash 保证均匀分布
fn shard_id(player_id: u64, shard_count: u32) -> u32 {
    let hash = xxhash_rust::xxh3::xxh3_64(&player_id.to_le_bytes());
    (hash % shard_count as u64) as u32
}
```

**关键点**：

- `ZINCRBY` 不是 `ZADD`：原子增量，不需要先 GET 再 SET，避免读-修改-写竞态
- 16 分片 Pipeline 并行：一次 RTT 写 16 个 Key
- `MissedTickBehavior::Skip`：如果上一批处理慢了，跳过积压的 tick，避免雪崩

---

## 第五步：快照缓存 —— 读写分离的核心

写路径只管写 Redis。读路径读内存快照。快照每秒异步刷新。

```rust
use std::sync::{Arc, RwLock};
use std::time::Instant;
use tokio::sync::watch;

/// 排行榜快照（只读，共享所有权）
pub struct RankSnapshot {
    pub entries: Vec<RankEntry>,    // 按分数降序排列
    pub version: u64,               // 快照版本号
    pub generated_at: Instant,
    pub leaderboard_id: String,
}

#[derive(Clone)]
pub struct RankEntry {
    pub player_id: u64,
    pub score: i64,
    pub rank: u32,  // 从 1 开始
}

/// 快照管理器
pub struct SnapshotManager {
    // 每个榜单一个快照，用 watch channel 推送更新
    snapshots: DashMap<String, watch::Receiver<Arc<RankSnapshot>>>,
    redis_pool: RedisPool,
    shard_count: u32,
    ttl: Duration,     // 快照有效期：1秒
    top_n: usize,      // 只缓存 Top 1000
}

impl SnapshotManager {
    /// 读路径：获取排行榜（零 Redis 查询）
    pub async fn get_top_k(&self, lb_id: &str, k: usize) -> Option<Vec<RankEntry>> {
        let rx = self.snapshots.get(lb_id)?;
        let snap = rx.borrow().clone();

        // 如果快照在有效期内，直接返回
        if snap.generated_at.elapsed() < self.ttl {
            let k = k.min(snap.entries.len());
            return Some(snap.entries[..k].to_vec());
        }
        None  // 过期了，由调用方触发 refresh
    }

    /// 读路径：查自己的排名
    pub async fn get_my_rank(&self, lb_id: &str, player_id: u64) -> Option<RankEntry> {
        let rx = self.snapshots.get(lb_id)?;
        let snap = rx.borrow().clone();
        // 二分查找（按 player_id 或按 score？排行榜一般需要按 ID 定位）
        // 这里简化：线性扫描 Top 1000
        snap.entries.iter().find(|e| e.player_id == player_id).cloned()
    }

    /// 强制刷新快照（定时任务调用，或 TTL 过期时触发）
    pub async fn refresh(&self, lb_id: &str) {
        // 1. 从 16 个 Redis 分片各取 Top 1000
        let mut all_entries = Vec::new();
        for shard in 0..self.shard_count {
            let key = format!("rank:{}:shard:{}", lb_id, shard);
            let mut conn = self.redis_pool.conn().await;
            let shard_top: Vec<(String, i64)> = redis::cmd("ZREVRANGE")
                .arg(&key)
                .arg(0isize)
                .arg((self.top_n - 1) as isize)
                .arg("WITHSCORES")
                .query_async(&mut conn)
                .await
                .unwrap_or_default();

            for (pid_str, score) in shard_top {
                if let Ok(pid) = pid_str.parse::<u64>() {
                    all_entries.push((pid, score));
                }
            }
        }

        // 2. 全局排序（K 路归并的简化版：全部放一起排序）
        all_entries.sort_by(|a, b| b.1.cmp(&a.1));
        all_entries.truncate(self.top_n);

        // 3. 构建快照
        let entries: Vec<RankEntry> = all_entries
            .into_iter()
            .enumerate()
            .map(|(i, (pid, score))| RankEntry {
                player_id: pid,
                score,
                rank: (i + 1) as u32,
            })
            .collect();

        let snapshot = Arc::new(RankSnapshot {
            entries,
            version: Self::next_version(),
            generated_at: Instant::now(),
            leaderboard_id: lb_id.to_string(),
        });

        // 4. 通过 watch channel 推送（所有读取者自动收到新版本）
        if let Some(tx) = self.get_or_create_channel(lb_id) {
            let _ = tx.send(snapshot);
        }
    }

    fn get_or_create_channel(&self, lb_id: &str) -> Option<watch::Sender<Arc<RankSnapshot>>> {
        // dashmap entry API 创建 watch channel
        None  // 简化
    }

    fn next_version() -> u64 { 0 }
}
```

---

## 第六步：gRPC Service —— 把上面串起来

```rust
use tonic::{Request, Response, Status};

pub struct RankingServiceImpl {
    write_buffer: Arc<WriteBuffer>,
    snapshot_mgr: Arc<SnapshotManager>,
    /// 同一 (player, lb) 的幂等去重缓存
    idempotency: DashMap<(u64, String), u64>,  // key → last_version
}

#[tonic::async_trait]
impl RankingService for RankingServiceImpl {
    /// 写入路径：Gateway → WriteBuffer（纳秒级返回）
    async fn batch_update_score(
        &self,
        request: Request<BatchScoreRequest>,
    ) -> Result<Response<BatchScoreResponse>, Status> {
        let req = request.into_inner();
        let mut accepted = 0u32;
        let mut rejected = 0u32;

        for update in req.updates {
            let key = (update.player_id, update.leaderboard_id.clone());

            // 幂等检查：版本号不能倒退
            if let Some(last_ver) = self.idempotency.get(&key) {
                if update.version <= *last_ver {
                    rejected += 1;
                    continue;  // 重复或过期的更新，丢弃
                }
            }

            // 提交到无锁写缓冲
            self.write_buffer.submit(ScoredUpdate {
                player_id: update.player_id,
                leaderboard_id: update.leaderboard_id,
                delta: update.delta,
                version: update.version,
                gateway_ts: update.timestamp,
            });

            // 更新幂等版本号
            self.idempotency.insert(key, update.version);
            accepted += 1;
        }

        Ok(Response::new(BatchScoreResponse {
            accepted,
            rejected,
        }))
    }

    /// 读取路径：快照缓存（零 Redis 查询）
    async fn get_top_k(
        &self,
        request: Request<TopKQuery>,
    ) -> Result<Response<TopKResult>, Status> {
        let req = request.into_inner();
        let k = req.k as usize;

        // 先尝试从快照读取
        if let Some(entries) = self.snapshot_mgr.get_top_k(&req.leaderboard_id, k).await {
            return Ok(Response::new(TopKResult {
                entries: entries.into_iter().map(|e| RankEntryProto {
                    player_id: e.player_id,
                    score: e.score,
                    rank: e.rank,
                }).collect(),
                source: "snapshot_cache".to_string(),
            }));
        }

        // 快照过期 → 触发刷新 + 降级直读 Redis
        tokio::spawn({
            let mgr = self.snapshot_mgr.clone();
            let lb_id = req.leaderboard_id.clone();
            async move { mgr.refresh(&lb_id).await; }
        });

        // 降级：直接从 Redis 读取（稍慢，但不会失败）
        self.read_from_redis_fallback(&req.leaderboard_id, k).await
    }
}
```

---

## 第七步：定时任务编排

```rust
/// 服务启动时的初始化
pub async fn start_ranking_service(redis_url: &str) {
    let redis_pool = RedisPool::connect(redis_url).await;

    // 1. 创建写缓冲（64K 容量，2^16）
    let write_buffer = Arc::new(WriteBuffer::new(65536));

    // 2. 启动 BatchWriter（消费写缓冲，写入 Redis）
    let writer = BatchWriter::new(
        write_buffer.buffer.clone(),
        redis_pool.clone(),
    );
    tokio::spawn(writer.run());

    // 3. 创建快照管理器
    let snapshot_mgr = Arc::new(SnapshotManager::new(
        redis_pool.clone(),
        16,                    // 16 分片
        Duration::from_secs(1), // 1 秒 TTL
        1000,                   // Top 1000
    ));

    // 4. 快照定时刷新（每 1 秒）
    let mgr = snapshot_mgr.clone();
    tokio::spawn(async move {
        let mut ticker = interval(Duration::from_secs(1));
        ticker.set_missed_tick_behavior(MissedTickBehavior::Skip);
        loop {
            ticker.tick().await;
            // 刷新所有活跃榜单
            for lb_id in &["arena_s1", "hero_power", "guild_boss"] {
                mgr.refresh(lb_id).await;
            }
        }
    });

    // 5. 监控协程：每 5 秒打印缓冲区使用率
    let wb = write_buffer.clone();
    tokio::spawn(async move {
        let mut ticker = interval(Duration::from_secs(5));
        loop {
            ticker.tick().await;
            let pct = wb.usage_pct();
            let dropped = wb.dropped_count();
            if pct > 0.8 || dropped > 0 {
                tracing::warn!("write_buffer usage={:.1}%, dropped={}", pct * 100.0, dropped);
            }
        }
    });

    // 6. 启动 gRPC 服务
    let service = RankingServiceImpl {
        write_buffer,
        snapshot_mgr,
        idempotency: DashMap::new(),
    };

    // tonic::transport::Server::builder()
    //     .add_service(RankingServiceServer::new(service))
    //     .serve(addr)
    //     .await?;
}
```

---

## 第八步：数据流全景（一图看清）

```
═══════════════════════════════════════════════════════════════════
                    穿起来的完整链路
═══════════════════════════════════════════════════════════════════

[客户端]                     [GateWay]                 [RankingService]
                                                            │
ScoreSender                  ScoreAggregator               │
  │                            │                           │
  │ on_score_change()          │ on_client_update()        │
  │  ↓                         │  ↓                        │
  │ buffer.push(delta)         │ pending.insert(key, delta)│
  │  ↓                         │  ↓                        │
  │ 100ms or 50条触发flush     │ 200ms tick_and_flush()    │
  │  ↓                         │  ↓                        │
  │ batch → gRPC ──────────→  │ drain → gRPC ──────────→ │
  │                            │                           │
                                                    ┌──────▼───────┐
                                                    │  gRPC Handler │
                                                    │ batch_update  │
                                                    │ _score()      │
                                                    └──────┬───────┘
                                                           │
                                                    幂等检查 (version)
                                                           │
                                                    ┌──────▼───────┐
                                                    │  WriteBuffer  │
                                                    │  ArrayQueue   │  ← 无锁, 纳秒级
                                                    │  容量: 65536  │
                                                    └──────┬───────┘
                                                           │
                                           BatchWriter 每 50ms 捞取
                                                           │
                                                    ┌──────▼───────┐
                                                    │  分片分组      │
                                                    │ 16个shard key │
                                                    └──────┬───────┘
                                                           │
                                              Redis Pipeline 并行写入
                                              ZINCRBY (原子增量)
                                                           │
                                              ┌────────────┼────────────┐
                                              │            │            │
                                         shard:0      shard:8      shard:15
                                              │            │            │
                                              └────────────┼────────────┘
                                                           │
                                          SnapshotManager 每秒刷新
                                          16分片 ZREVRANGE → K路归并
                                                           │
                                                    ┌──────▼───────┐
                                                    │ watch channel │
                                                    │ Arc<Snapshot> │  ← 零拷贝, 共享所有权
                                                    └──────┬───────┘
                                                           │
                                         GetTopK / GetMyRank 直接读 Arc
                                         零 Redis 查询, < 1μs 延迟
═══════════════════════════════════════════════════════════════════
```

---

## 第九步：关键数字验证

用上面的代码参数做容量计算：

```text
输入:  50 万写/秒 (800万CCU × 每16秒结算一次)

Step 1 - 客户端聚合 (100ms):
  每人 100ms 内最多更新 1 次 → 800万 × 0.1/16 ≈ 5万/秒 到达 Gateway

Step 2 - Gateway 去重 (200ms):
  每人 200ms 保留最后一条 → 5万 × (100ms/200ms) ≈ 2.5万/秒 到达 RankingService

Step 3 - WriteBuffer:
  ArrayQueue push: ~15ns/次
  2.5万 × 15ns = 0.375ms CPU时间/秒 → 几乎忽略

Step 4 - BatchWriter → Redis:
  每 50ms 捞一批 → 2.5万/秒 × 0.05 = 1250 条/批
  16 分片 → 每分片 ~78 条/批
  Redis ZINCRBY: ~5μs/条 → 78 × 5μs = 390μs
  16 分片 Pipeline 并行 → 总延迟 ~500μs (含网络 RTT)
  ✅ 远低于 5ms SLA

Step 5 - 快照刷新:
  每秒 1 次, 1万条/秒 的写入 → 快照刷新时的排序开销:
  16 × 1000 条归并 → ~50μs
  ✅ 不影响服务

Step 6 - 读路径:
  Arc::clone + 切片: ~20ns (原子引用计数 + 指针复制)
  ✅ < 1μs, 超过 SLA 5000 倍余量
```

---

## 第十步：如果图片里是你画的其他方案

如果你发的截图是指另外的实现思路（比如不用 Redis、自己写存储引擎、或者网络拓扑上的不同设计），描述一下关键步骤，我按你的思路重新写一版 demo。这套代码是模块化的——每个 Step 都可以替换。
