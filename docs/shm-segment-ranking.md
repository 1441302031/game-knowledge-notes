# 共享内存分段锁排行榜：零 Redis 方案

> 基于对话中「自定义协议 + map + 分段锁」思路的完整工程方案。不用 Redis，走进程内共享内存，位运算 O(1) 定位分段，段内跳表 O(log N) 排序。

---

## 一、与 Redis 方案的对比：为什么要走这条路

| 维度 | Redis 方案（上一篇） | 共享内存分段锁（本篇） |
|------|---------------------|----------------------|
| 外部依赖 | Redis Cluster（10分片） | **零依赖**，纯进程内 |
| 网络开销 | 每次写入一次 gRPC + 一次 Redis RTT | **零网络开销**，内存操作 |
| 写延迟 | ~500μs（含网络 RTT） | **~3-5μs**（内存 + 锁） |
| 读延迟 | ~1μs（快照缓存命中） | **~0.2μs**（分段锁 + 跳表查找） |
| 数据持久化 | Redis AOF | mmap 文件自动回写 |
| 跨进程共享 | 通过 Redis 间接共享 | **mmap 直接共享**，多进程读写同一块内存 |
| 运维复杂度 | 需要 Redis 集群 + 监控 | **零运维**，随进程启停 |
| 适用场景 | 弹性扩容、跨机房 | **单机极致性能**、多进程共享 |

**核心取舍**：Redis 方案方便扩容，分段锁方案性能碾压。如果你的排行榜是单机多进程部署（比如 Ares 的 Battle Server 多进程共享排行数据），分段锁方案更优。

---

## 二、分段寻址：位运算 O(1) 定位

这是对方提到的核心思路——「固定 key 值高位进行分段，位运算实现 O(1) 查找」。

```rust
/// 分段配置
const SEGMENT_BITS: u32 = 10;                    // 2^10 = 1024 个分段
const SEGMENT_COUNT: usize = 1 << SEGMENT_BITS;  // 1024
const SEGMENT_MASK: u64 = (SEGMENT_COUNT - 1) as u64;

/// 取 hash 值的高 SEGMENT_BITS 位作为分段 ID
/// 为什么用高位？高位分布更均匀（hash 函数的 avalanche 效应在高位更明显）
fn segment_id(key: u64) -> usize {
    let hash = xxhash64(&key.to_le_bytes());
    // 右移 (64 - SEGMENT_BITS) 位，取最高的 SEGMENT_BITS 位
    (hash >> (64 - SEGMENT_BITS)) as usize
    // 1024 个分段 → 用 key 的高 10 位决定落在哪个分段
    // 位运算 → 单条 CPU 指令 → O(1)
}

/// 取 hash 的低位作为段内索引（用于段内跳表排序）
fn segment_local_key(key: u64) -> u64 {
    let hash = xxhash64(&key.to_le_bytes());
    // 低 54 位（64 - 10）作为段内排序键
    hash & ((1u64 << (64 - SEGMENT_BITS)) - 1)
}
```

**为什么用高位而不是取模？—— 三层递进分析**

有三种做法可以把一个 hash 值映射到 1024 个分段：

```
做法 A（取模）：  segment = hash % 1024
做法 B（低掩码）：segment = hash & 0x3FF      (取低 10 位)
做法 C（高位移）：segment = hash >> 54        (取高 10 位)
```

---

**第一层：为什么不用取模（做法 A）？—— 指令级差距**

`% 1024` 在 CPU 上是**除法指令 `div`**，一条 `div` 要 ~30 个 CPU cycle：

```asm
; 取模：慢——需要 div 指令
mov  rax, hash_value
mov  rcx, 1024
xor  rdx, rdx
div  rcx        ; ← 这条指令 ~30 cycles
; 结果在 rdx（余数）
```

而 `& 0x3FF` 和 `>> 54` 都是**单 cycle 指令**：

```asm
; 低掩码：快
and  rax, 0x3FF    ; 1 cycle

; 高位移：也快
shr  rax, 54       ; 1 cycle
```

**30 cycle vs 1 cycle → 30 倍差距。** 每秒 50 万次分段寻址，这就是 1500 万 cycle vs 50 万 cycle 的区别——实打实的 CPU 时间。

---

**第二层：高位和低位有区别吗？—— Avalanche 效应**

**从随机性角度，没有区别。** 这就是 xxhash 的 avalanche 效应。

Avalanche 效应指的是：**输入改变 1 个 bit，输出约一半的 bit 会翻转。** 这意味着 hash 值的每一位都是输入所有位的"混合产物"——高位和低位一样随机：

```
输入: player_id = 0x0000000000000001
hash:  0xA7F3_9B2C_81D4_E506
       ^^^^^^^^ ^^^^^^^^
       高位      低位
       
输入: player_id = 0x0000000000000002  
hash:  0x3C81_7F2E_D915_A640
       ← 高位和后一位完全不同，和低位一样均匀 →
```

所以从**分布均匀性**角度，取高位 `hash >> 54` 和取低位 `hash & 0x3FF` 效果一样——都不产生聚集。

---

**第三层：那为什么选高位？—— 两个工程红利**

做法 B（低掩码）和做法 C（高位移）都是 1 cycle，性能无差别。选高位有两个实际原因：

**红利 1：与段内排序键天然解耦**

```rust
// 高位做分段 ID
let segment_id = (hash >> 54) as usize;   // 用高 10 位 → 决定去哪个分段

// 低位做段内排序键
let local_key  = hash & 0x003F_FFFF_FFFF_FFFF;  // 用低 54 位 → 段内跳表排序
```

高位和低位各司其职、互不干扰。如果用低位做分段（`hash & 0x3FF`），那段内排序键就只能取高位，还要右移回去——多一次操作，虽然也是 1 cycle，但没必要。

**红利 2：顺次分布，Top-K 查询天然有序**

取高位的分段方式产生的是**顺次分布**：

```
取高位（>>）：hash 的数值范围被均匀切成 1024 段

  分段 0:   hash 范围 [0x0000_0000_0000_0000, 0x003F_FFFF_FFFF_FFFF]
  分段 1:   hash 范围 [0x0040_0000_0000_0000, 0x007F_FFFF_FFFF_FFFF]
  ...
  分段 1023: hash 范围 [0xFFC0_0000_0000_0000, 0xFFFF_FFFF_FFFF_FFFF]
  
  → 分段 ID 越大，hash 值越大 → 高分段天然 = 高分区间的玩家

取低位（&）：hash 值被交织分配到各分段

  分段 0: hash 以 ...0000000000 结尾的
  分段 1: hash 以 ...0000000001 结尾的
  ...
  → 所有分段覆盖整个数值范围，没有高低之分
```

这意味着全局 Top-K 查询时：

```rust
// 取高位：从最高分段往低分段遍历，天然按分数降序
// 不需要额外的全局归并排序！
for seg_id in (0..SEGMENT_COUNT).rev() {
    collect_from_segment(seg_id, remaining);
}

// 取低位：所有分段交织覆盖整个分数范围
// Top-K 时必须对所有 1024 个分段做 K 路归并 → 多一轮 O(K × log 1024) 的排序
```

---

**总结：一条指令背后的三层决策**

```
问题：怎么把 64-bit hash 映射到 1024 分段？

├── 用取模 hash % 1024？
│   └── ❌ div 指令 30 cycles，慢 30 倍
│
├── 用低掩码 hash & 0x3FF？
│   ├── ✅ and 指令 1 cycle
│   └── ❌ 分段 ID 和段内排序键争抢同几位，Top-K 需要归并
│
└── 用高位移 hash >> 54？
    ├── ✅ shr 指令 1 cycle
    ├── ✅ 高位分段、低位排序，各不冲突
    └── ✅ 顺次分布，Top-K 天然有序
```

**省掉除法是首要动机，高位顺次分布是白捡的红利。**

---

## 三、共享内存布局

```
mmap 文件内存布局（假设 1024 分段）
═══════════════════════════════════════════════════════════

偏移 0          偏移 64         偏移 4096
┌──────────────┬──────────────┬─────────────────────────┐
│    Header    │ Segment Table│    Segment Data Area    │
│   (64 bytes) │ (1024×16B)   │  (每个分段独立区域)      │
└──────────────┴──────────────┴─────────────────────────┘
                  │
                  │  SegmentTable[i] = { offset, size, lock_state }
                  │
     ┌────────────┼────────────┬────────────┬────────────┐
     │ Segment 0  │ Segment 1  │ Segment 2  │  ......    │
     │ 跳表根节点  │ 跳表根节点  │ 跳表根节点  │            │
     │ 节点数      │ 节点数      │ 节点数      │            │
     │ RwLock     │ RwLock     │ RwLock     │            │
     └────────────┴────────────┴────────────┴────────────┘
```

```rust
use std::sync::atomic::{AtomicU64, AtomicU32, Ordering};
use std::sync::RwLock;

/// 共享内存 Header（64 bytes，缓存行对齐）
#[repr(C, align(64))]
struct ShmHeader {
    magic: u32,              // 0x52414E4B ("RANK")
    version: u32,            // 格式版本
    segment_count: u32,      // 分段数
    segment_bits: u32,       // 分段位数
    total_entries: AtomicU64, // 全局总数（原子操作）
    created_at: u64,         // 创建时间戳
    _pad: [u8; 28],          // 填充到 64 bytes
}

/// 分段表项（16 bytes，方便 SIMD 加载）
#[repr(C)]
struct SegmentEntry {
    offset: AtomicU64,       // 分段数据区偏移
    size: AtomicU32,         // 当前使用大小
    node_count: AtomicU32,   // 节点数（用于全局排名计算）
    _pad: [u8; 4],           // 对齐
}

/// 段内跳表节点（32 bytes）
#[repr(C)]
struct SkipNode {
    key: u64,                // segment_local_key（用于段内排序）
    player_id: u64,          // 原始玩家 ID
    score: AtomicU64,        // 分数（原子更新，无需写锁）
    forward_offset: [u64; 4], // 4 层前向指针（文件内偏移量）
    span: [u32; 4],          // 跨度（用于计算排名）
}
```

**关键设计决策**：

1. `score` 用 `AtomicU64` 而不是普通 u64——分数更新只需要原子写，不需要锁
2. 段内跳表高度固定 4 层（80% 节点在 L0，18% 在 L1，1.8% 在 L2，0.2% 在 L3），1024 个节点的段内高度 4 层足够
3. 所有指针用文件内偏移量（u64）而不是裸指针——因为 mmap 在不同进程中映射到不同虚拟地址

---

## 四、段内跳表实现（带锁粒度控制）

```rust
use std::sync::RwLock;

/// 一个分段 = 一把 RwLock + 一个跳表
struct RankSegment {
    /// 读写锁：读多写少的排行榜场景，RwLock 允许并发读
    lock: RwLock<()>,
    /// 跳表头节点偏移（在 mmap 中的位置）
    header_offset: u64,
    /// 当前节点数
    node_count: u64,
}

impl RankSegment {
    /// 插入或更新分数（写锁）
    fn upsert(&mut self, mmap: &MmapRef, player_id: u64, score: u64) {
        // 写锁：同一分段内串行化写入
        let _guard = self.lock.write().unwrap();

        let local_key = segment_local_key(player_id);

        // 查找插入位置（段内跳表，O(log 段内节点数)）
        let mut prev: [u64; 4] = [self.header_offset; 4];
        let mut rank: [u32; 4] = [0; 4];
        let mut current = self.header_offset;

        // 从最高层开始搜索
        for level in (0..4).rev() {
            while let Some(node) = self.read_node(mmap, current) {
                let forward = node.forward_offset[level];
                if forward == 0 { break; }
                let next = self.read_node(mmap, forward).unwrap();
                if next.local_key < local_key {
                    rank[level] += node.span[level];
                    current = forward;
                } else {
                    break;
                }
            }
            // 记录本层的前驱节点
            prev[level] = current;
        }

        // 检查是否已存在（更新分数）
        if let Some(existing) = self.read_node(mmap, prev[0]) {
            let forward = existing.forward_offset[0];
            if forward != 0 {
                let target = self.read_node(mmap, forward).unwrap();
                if target.player_id == player_id {
                    // 原子更新分数——不需要修改跳表结构
                    target.score.store(score, Ordering::Release);
                    return;  // ✅ 分数更新零结构化开销
                }
            }
        }

        // 新节点：随机生成层数
        let new_level = random_level();
        let new_offset = self.alloc_node(mmap, player_id, local_key, score, new_level);

        // 插入到每一层（经典跳表插入算法）
        for level in 0..new_level {
            let prev_node = self.read_node(mmap, prev[level]).unwrap();
            let next_offset = prev_node.forward_offset[level];

            // 设置新节点的 forward 指针
            let new_node = self.read_node(mmap, new_offset).unwrap();
            new_node.forward_offset[level] = next_offset;

            // 更新前驱节点的 forward 指针
            let prev_node = self.read_node_mut(mmap, prev[level]).unwrap();
            prev_node.forward_offset[level] = new_offset;

            // 更新 span
            // ...（span 更新逻辑略，详见完整代码）
        }

        self.node_count += 1;
    }

    /// 查询玩家排名（读锁）
    fn get_rank(&self, mmap: &MmapRef, player_id: u64) -> Option<u64> {
        // 读锁：多个查询可以并发
        let _guard = self.lock.read().unwrap();

        let local_key = segment_local_key(player_id);
        let mut current = self.header_offset;
        let mut rank: u64 = 0;

        for level in (0..4).rev() {
            while let Some(node) = self.read_node(mmap, current) {
                let forward = node.forward_offset[level];
                if forward == 0 { break; }
                let next = self.read_node(mmap, forward).unwrap();
                if next.local_key <= local_key {
                    rank += node.span[level] as u64;
                    current = forward;
                    if next.player_id == player_id {
                        return Some(rank);
                    }
                } else {
                    break;
                }
            }
        }
        None
    }

    /// 获取段内 Top-K（读锁）
    fn get_top_k(&self, mmap: &MmapRef, k: usize) -> Vec<(u64, u64)> {
        let _guard = self.lock.read().unwrap();
        let mut result = Vec::with_capacity(k);
        let mut current = self.header_offset;

        while result.len() < k {
            let node = match self.read_node(mmap, current) {
                Some(n) => n,
                None => break,
            };
            let forward = node.forward_offset[0];
            if forward == 0 { break; }
            let next = self.read_node(mmap, forward).unwrap();
            result.push((next.player_id, next.score.load(Ordering::Acquire)));
            current = forward;
        }
        result
    }

    fn read_node(&self, mmap: &MmapRef, offset: u64) -> Option<&SkipNode> { /* mmap 偏移读取 */ None }
    fn read_node_mut(&self, mmap: &MmapRef, offset: u64) -> Option<&mut SkipNode> { None }
    fn alloc_node(&mut self, mmap: &MmapRef, player_id: u64, local_key: u64, score: u64, level: usize) -> u64 { 0 }
}

fn random_level() -> usize {
    // 概率：L0 100%, L1 25%, L2 6.25%, L3 1.56%
    let mut level = 1;
    while level < 4 && fast_random() % 4 == 0 {
        level += 1;
    }
    level
}

fn fast_random() -> u32 { 0 /* xorshift 快速随机数 */ }
```

---

## 五、全局排名聚合器：跨分段查询

```rust
use std::sync::Arc;

/// 全局排行榜管理器
pub struct ShmRankingEngine {
    mmap: Arc<MmapRef>,
    segments: Vec<RankSegment>,    // 1024 个分段
    header: &'static ShmHeader,    // 指向 mmap 的 Header
}

impl ShmRankingEngine {
    /// 写入分数（先定位分段，再操作段内跳表）
    pub fn update_score(&self, player_id: u64, score: u64) {
        let seg_id = segment_id(player_id);
        // 只锁这一个分段，其他 1023 个分段不受影响
        self.segments[seg_id].upsert(&self.mmap, player_id, score);
    }

    /// 查询自己的排名
    /// 排名 = 段内排名 + 所有比该分段 hash 更高的分段的节点总数
    pub fn get_my_rank(&self, player_id: u64) -> Option<u64> {
        let seg_id = segment_id(player_id);

        // 第1步：段内排名
        let local_rank = self.segments[seg_id].get_rank(&self.mmap, player_id)?;

        // 第2步：累加所有高位分段的节点数
        // 因为分段按 hash 高位，所以高位分段的所有节点分数都大于该分段的任意节点
        let mut higher_count: u64 = 0;
        for i in (seg_id + 1)..SEGMENT_COUNT {
            higher_count += self.segments[i].node_count;
        }

        Some(local_rank + higher_count)
    }

    /// 获取全局 Top-K
    /// 从最高分段开始收集，直到凑够 K 个
    pub fn get_top_k(&self, k: usize) -> Vec<(u64, u64, u64)> {
        let mut result = Vec::with_capacity(k);

        // 从最高分段（SEGMENT_COUNT - 1）往低分段遍历
        for seg_id in (0..SEGMENT_COUNT).rev() {
            if result.len() >= k { break; }
            let remaining = k - result.len();

            let seg_top = self.segments[seg_id].get_top_k(&self.mmap, remaining);
            for (pid, score) in seg_top {
                result.push((pid, score, 0)); // rank 后续统一计算
            }
        }

        // 统一标注排名
        for (i, entry) in result.iter_mut().enumerate() {
            entry.2 = (i + 1) as u64;
        }
        result
    }
}
```

---

## 六、锁粒度分析：为什么 1024 分段是甜点

这是对方问的「怎么分的粒度会合适」——用数据回答：

```text
假设：1000 万玩家, 1024 分段

每分段平均节点数：1000 万 / 1024 ≈ 9765 个

写操作分析：
  - 50 万写/秒 均匀哈希分布 → 每分段 ~488 写/秒
  - 每次写持有写锁 ~3μs（段内跳表插入）
  - 每分段 488 × 3μs = 1.46ms / 秒 持有写锁
  - 写锁利用率：0.15% → 几乎不阻塞

读操作分析：
  - 500 万读/秒 均匀哈希分布 → 每分段 ~4882 读/秒
  - 每次读持有读锁 ~0.2μs（段内跳表查找）
  - RwLock 允许多个读者并发 → 读操作几乎不互相阻塞
  - 读被写阻塞概率：写锁时间 / 总时间 = 0.15% → 几乎不阻塞

锁竞争率：~0.3%（读等待写的概率 + 写等待写的概率）
            → 远低于 1%，分段粒度合理
```

**粒度调优规则**：

| 分段数 | 每段节点数 | 读写竞争率 | 内存开销（段表） | 适用场景 |
|--------|-----------|-----------|-----------------|---------|
| 64 | 15.6万 | ~3% | 1 KB | 低并发(< 1万QPS) |
| 256 | 3.9万 | ~0.8% | 4 KB | 中并发(1-10万QPS) |
| **1024** | **9765** | **~0.3%** | **16 KB** | **高并发(10-100万QPS)** ← 推荐 |
| 4096 | 2441 | ~0.08% | 64 KB | 极高并发(> 100万QPS) |
| 16384 | 610 | ~0.02% | 256 KB | 极端场景 |

**甜点 1024 的理由**：
- 每段 ~1 万节点，段内跳表 O(log 1万) ≈ 13 次指针跳转
- 竞争率 0.3%，锁开销可忽略
- 16 KB 段表完全在 L1 缓存中
- 向后可扩到 4096（仅改一个常量），向前不浪费

---

## 七、完整初始化与 mmap 创建

```rust
use memmap2::{MmapMut, MmapOptions};
use std::fs::{File, OpenOptions};

/// 创建或打开共享内存排行榜
pub fn create_or_open(path: &str, segment_bits: u32) -> ShmRankingEngine {
    let segment_count = 1usize << segment_bits;

    // 计算总文件大小
    let header_size = 64;
    let table_size = segment_count * 16;  // 每分段 16 bytes
    let data_area_size = segment_count * 1024 * 1024; // 每分段 1MB 预分配
    let total_size = header_size + table_size + data_area_size;

    let file = OpenOptions::new()
        .read(true).write(true).create(true)
        .open(path)
        .unwrap();
    file.set_len(total_size as u64).unwrap();

    let mmap = unsafe { MmapOptions::new().map_mut(&file).unwrap() };

    // 初始化 Header
    let header = unsafe { &mut *(mmap.as_ptr() as *mut ShmHeader) };
    header.magic = 0x52414E4B;
    header.version = 1;
    header.segment_count = segment_count as u32;
    header.segment_bits = segment_bits;
    header.total_entries.store(0, Ordering::Release);
    header.created_at = now_timestamp();

    // 初始化分段表
    let table_base = unsafe { mmap.as_ptr().add(header_size) as *mut SegmentEntry };
    let data_base = header_size + table_size;
    for i in 0..segment_count {
        unsafe {
            (*table_base.add(i)).offset.store((data_base + i * 1024 * 1024) as u64, Ordering::Release);
            (*table_base.add(i)).size.store(0, Ordering::Release);
            (*table_base.add(i)).node_count.store(0, Ordering::Release);
        }
    }

    // 初始化每个分段的跳表头节点
    let segments: Vec<RankSegment> = (0..segment_count)
        .map(|i| RankSegment {
            lock: RwLock::new(()),
            header_offset: (data_base + i * 1024 * 1024) as u64,
            node_count: 0,
        })
        .collect();

    ShmRankingEngine {
        mmap: Arc::new(MmapRef(mmap)),
        segments,
        header: unsafe { &*(mmap.as_ptr() as *const ShmHeader) },
    }
}
```

---

## 八、与对话中的思路一一对应

| 对话原文 | 对应实现 |
|---------|---------|
| 「进程内存共享」 | `mmap` 文件，多进程共享同一块物理内存 |
| 「自定义协议 + map」 | 段内跳表就是 map 的增强版（带排序 + 排名能力） |
| 「分段减少 hash 损耗」 | 1024 分段，段内只处理 ~1 万节点，hash 碰撞概率极低 |
| 「固定 key 值高位进行分段」 | `hash >> (64 - 10)`，高位 10 位决定分段 |
| 「位运算实现 O(1) 查找」 | 右移指令，单 CPU cycle |
| 「至少得保证 log(n)」 | 段内跳表 O(log 9765) ≈ 13 次操作 |
| 「锁可以理解为强制在这段单线程」 | 每分段一把 `RwLock`，写锁独占、读锁共享 |
| 「粒度、吞吐量、并发量怎么权衡」 | 1024 分段 × 竞争率 0.3% = 零阻塞 |
| 「每个细节点都会影响最终方案」 | 位运算不用除法、AtomicU64 分数免锁、4 层跳表刚好 |

---

## 九、两种方案选型指南

```
                    你需要什么？
                        │
           ┌────────────┼────────────┐
           │            │            │
     弹性扩容？     单机极致？    最简单？
           │            │            │
           ▼            ▼            ▼
      Redis 方案    分段锁方案    直接用 Redis
      (上一篇)      (本篇)       ZSET
           │            │            │
     ✓ 水平扩展    ✓ 零延迟      ✓ 一行命令
     ✓ 跨机房      ✓ 零运维      ✗ 扩展性差
     ✗ 有网络开销  ✗ 单机上限    ✗ 单线程瓶颈
```

---

## 十、与 Redis 方案的融合：两全其美

这两个方案不是互斥的。最佳实践是**组合使用**：

```rust
/// 混合引擎：分段锁做热数据，Redis 做持久化 + 跨服
struct HybridEngine {
    /// 分段锁热数据（微秒级读写）
    shm: ShmRankingEngine,
    /// Redis 异步备份（秒级同步）
    redis: RedisPool,
}

impl HybridEngine {
    pub fn update_score(&self, player_id: u64, score: u64) {
        // 1. 立即写入分段锁（微秒级，玩家无感知）
        self.shm.update_score(player_id, score);

        // 2. 异步写入 Redis（毫秒级，不影响请求返回）
        let redis = self.redis.clone();
        tokio::spawn(async move {
            redis.zadd("rank:arena", score, player_id).await;
        });
    }

    pub fn get_top_k(&self, k: usize) -> Vec<RankEntry> {
        // 读路径：只走分段锁，不走 Redis
        self.shm.get_top_k(k)
    }
}
```

**写入路径**：分段锁立即返回 → Redis 异步备份
**读取路径**：永远走分段锁（零网络开销）
**崩溃恢复**：从 Redis 重建分段锁数据

这就是「每种方案在适合的场景里发挥」的最终答案。
