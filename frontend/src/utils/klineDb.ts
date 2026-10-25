/**
 * K 线前端缓存（2026-10-25 关注页：BTC/ETH/BNB/OKB/SOL × 1h/4h/1d）
 *
 * 两层：
 * - L1 内存 Map：当前会话即时命中，避免重复读 IDB
 * - L2 IndexedDB：跨刷新/重开复用已加载历史
 *
 * 正确性约束：
 * - klines 复合主键 [symbol, interval, time(秒)]，put 幂等，天然去重/覆盖
 * - K 线写入与 meta 更新必须同一 readwrite 事务
 * - meta 单调合并：oldest 取 min、newest 取 max；尾部刷新只改 tailFetchedAt
 * - 未收盘的最后一根每次尾部刷新都会被 put 覆盖，不会留下旧快照
 * - 上限裁剪只删最老数据，绝不裁掉最新端
 * - dataVersion 不匹配时整库清空，避免旧口径数据混入
 * - IndexedDB 不可用/写失败时自动降级为仅内存，不影响看图
 */
import type { Kline } from '../types'

export type KlineInterval = '1h' | '4h' | '1d'

export const INTERVAL_SEC: Record<KlineInterval, number> = {
  '1h': 3600,
  '4h': 14400,
  '1d': 86400,
}

const DB_NAME = 'trading-workbench-klines'
const DB_VERSION = 1
const DATA_VERSION = 1
export const MAX_BARS = 8000
const MEM_MAX_KEYS = 24
const META_SCHEMA_KEY = '__schema__'

interface CacheEntry {
  bars: Kline[]
  lastUsed: number
}

interface CacheMeta {
  key: string
  oldest: number
  newest: number
  count: number
  noMore: boolean
  tailFetchedAt: number
  dataVersion: number
}

const mem = new Map<string, CacheEntry>()
const writeLocks = new Map<string, Promise<void>>()
let dbPromise: Promise<IDBDatabase | null> | null = null
let schemaPromise: Promise<void> | null = null

const keyOf = (symbol: string, interval: string) => `${symbol}:${interval}`

const req = <T>(r: IDBRequest<T>) =>
  new Promise<T>((resolve, reject) => {
    r.onsuccess = () => resolve(r.result)
    r.onerror = () => reject(r.error)
  })

const txDone = (tx: IDBTransaction) =>
  new Promise<void>((resolve, reject) => {
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error)
    tx.onabort = () => reject(tx.error)
  })

/** 同 key 写串行化（跨标签页由 IDB 事务本身保证串行） */
function withLock<T>(key: string, fn: () => Promise<T>): Promise<T> {
  const prev = writeLocks.get(key) ?? Promise.resolve()
  const run = prev.then(fn, fn)
  writeLocks.set(
    key,
    run.then(
      () => undefined,
      () => undefined,
    ),
  )
  return run
}

function openDb(): Promise<IDBDatabase | null> {
  if (dbPromise) return dbPromise
  if (typeof indexedDB === 'undefined') {
    dbPromise = Promise.resolve(null)
    return dbPromise
  }
  const p: Promise<IDBDatabase | null> = new Promise<IDBDatabase | null>(
    (resolve, reject) => {
      const r = indexedDB.open(DB_NAME, DB_VERSION)
      r.onupgradeneeded = () => {
        const db = r.result
        if (!db.objectStoreNames.contains('klines')) {
          db.createObjectStore('klines', { keyPath: ['symbol', 'interval', 'time'] })
        }
        if (!db.objectStoreNames.contains('meta')) {
          db.createObjectStore('meta', { keyPath: 'key' })
        }
      }
      r.onsuccess = () => {
        const db = r.result
        // 另一标签页升级版本时主动让路，下次重新打开
        db.onversionchange = () => {
          db.close()
          dbPromise = null
          schemaPromise = null
        }
        resolve(db)
      }
      r.onerror = () => reject(r.error)
      r.onblocked = () => reject(new Error('indexeddb blocked'))
    },
  ).catch((e) => {
    console.warn('[klineDb] open failed, fallback to memory', e)
    return null
  })
  dbPromise = p
  return p
}

/** 口径变更时清空：只比 dataVersion，和结构版本 DB_VERSION 解耦 */
async function ensureSchema(db: IDBDatabase): Promise<void> {
  if (schemaPromise) return schemaPromise
  schemaPromise = (async () => {
    const tx = db.transaction(['klines', 'meta'], 'readwrite')
    const ks = tx.objectStore('klines')
    const ms = tx.objectStore('meta')
    const cur = (await req(ms.get(META_SCHEMA_KEY))) as { dataVersion?: number } | undefined
    if (cur?.dataVersion !== DATA_VERSION) {
      ks.clear()
      ms.clear()
      ms.put({ key: META_SCHEMA_KEY, dataVersion: DATA_VERSION })
      mem.clear()
    }
    await txDone(tx)
  })().catch((e) => {
    console.warn('[klineDb] schema check failed', e)
    schemaPromise = null
  })
  return schemaPromise
}

function touch(symbol: string, interval: string, bars: Kline[]): void {
  const key = keyOf(symbol, interval)
  const sorted = dedupeSorted(bars).slice(-MAX_BARS)
  mem.set(key, { bars: sorted, lastUsed: Date.now() })
  if (mem.size > MEM_MAX_KEYS) {
    let oldestKey: string | null = null
    let oldestAt = Infinity
    for (const [k, v] of mem) {
      if (k !== key && v.lastUsed < oldestAt) {
        oldestAt = v.lastUsed
        oldestKey = k
      }
    }
    if (oldestKey) mem.delete(oldestKey)
  }
}

function dedupeSorted(bars: Kline[]): Kline[] {
  const map = new Map<number, Kline>()
  for (const b of bars) map.set(b.time, b)
  return [...map.values()].sort((a, b) => a.time - b.time)
}

/** 尾部权威合并：旧数据只保留严格早于新页第一根的部分，重叠段一律以新页为准 */
function mergeTailBars(prev: Kline[], fresh: Kline[]): Kline[] {
  if (!fresh.length) return prev
  const first = fresh[0].time
  return dedupeSorted([...prev.filter((b) => b.time < first), ...fresh])
}

/**
 * 缓存尾端与新页首根是否断层（间隔超过一个周期，典型是长时间未打开页面）。
 * 断层时旧段不能直接衔接：合并会丢弃旧段，缓存写入也会清掉旧段。
 */
export function tailHasGap(
  prev: Kline[],
  fresh: Kline[],
  interval: KlineInterval,
): boolean {
  if (!prev.length || !fresh.length) return false
  const lastPrev = prev[prev.length - 1].time
  return fresh[0].time > lastPrev + INTERVAL_SEC[interval] * 1000
}

/**
 * 尾部新页是否「倒退」：返回页最新一根早于已有最新一根。
 * 触发场景：后端所有交易所失败时降级返回旧缓存（allow_stale），前端不能把它
 * 当成新数据合并/落盘，否则图上最新 K 线会回退，旧数据还会被标成刚刷新。
 */
export function tailRegressed(prev: Kline[], fresh: Kline[]): boolean {
  if (!prev.length || !fresh.length) return false
  return fresh[fresh.length - 1].time < prev[prev.length - 1].time
}

/** 尾部刷新合并（带断层/倒退保护）：有断层时只保留新页，倒退页直接丢弃 */
export function mergeTailFresh(
  prev: Kline[],
  fresh: Kline[],
  interval: KlineInterval,
): Kline[] {
  if (!fresh.length) return prev
  if (tailRegressed(prev, fresh)) return prev
  if (tailHasGap(prev, fresh, interval)) return dedupeSorted(fresh)
  return mergeTailBars(prev, fresh)
}

export function mergeOlderBars(prev: Kline[], older: Kline[]): Kline[] {
  return dedupeSorted([...prev, ...older])
}

async function readAllFromDb(
  db: IDBDatabase,
  symbol: string,
  interval: string,
): Promise<Kline[]> {
  const tx = db.transaction('klines', 'readonly')
  const ks = tx.objectStore('klines')
  const range = IDBKeyRange.bound(
    [symbol, interval, 0],
    [symbol, interval, Number.MAX_SAFE_INTEGER],
  )
  const rows = (await req(ks.getAll(range))) as Array<Kline & { symbol: string; interval: string }>
  await txDone(tx)
  return rows.map(({ time, open, high, low, close, volume }) => ({
    time,
    open,
    high,
    low,
    close,
    volume,
  }))
}

/** 读最近 limit 根（缓存优先：内存 → IDB） */
export async function loadLatest(
  symbol: string,
  interval: KlineInterval,
  limit: number,
): Promise<Kline[]> {
  const key = keyOf(symbol, interval)
  const cached = mem.get(key)
  if (cached && cached.bars.length >= limit) {
    cached.lastUsed = Date.now()
    return cached.bars.slice(-limit)
  }
  const db = await openDb()
  if (!db) return cached?.bars.slice(-limit) ?? []
  await ensureSchema(db)
  try {
    const all = await readAllFromDb(db, symbol, interval)
    const merged = cached ? dedupeSorted([...cached.bars, ...all]) : all
    touch(symbol, interval, merged)
    return merged.slice(-limit)
  } catch (e) {
    console.warn('[klineDb] loadLatest failed, use memory', e)
    return cached?.bars.slice(-limit) ?? []
  }
}

/** 读严格早于 beforeTime 的最近 limit 根（左滑翻页） */
export async function loadOlder(
  symbol: string,
  interval: KlineInterval,
  beforeTime: number,
  limit: number,
): Promise<Kline[]> {
  const key = keyOf(symbol, interval)
  const cached = mem.get(key)
  const fromMem = cached?.bars.filter((b) => b.time < beforeTime) ?? []
  if (fromMem.length >= limit) {
    cached!.lastUsed = Date.now()
    return fromMem.slice(-limit)
  }
  const db = await openDb()
  if (!db) return fromMem.slice(-limit)
  await ensureSchema(db)
  try {
    const all = await readAllFromDb(db, symbol, interval)
    const merged = cached ? dedupeSorted([...cached.bars, ...all]) : all
    touch(symbol, interval, merged)
    return merged.filter((b) => b.time < beforeTime).slice(-limit)
  } catch (e) {
    console.warn('[klineDb] loadOlder failed, use memory', e)
    return fromMem.slice(-limit)
  }
}

/** 读 meta（预缓存判断新鲜度用） */
export async function getMeta(
  symbol: string,
  interval: KlineInterval,
): Promise<CacheMeta | null> {
  const db = await openDb()
  if (!db) return null
  await ensureSchema(db)
  try {
    const tx = db.transaction('meta', 'readonly')
    const meta = (await req(tx.objectStore('meta').get(keyOf(symbol, interval)))) as
      | CacheMeta
      | undefined
    await txDone(tx)
    return meta ?? null
  } catch {
    return null
  }
}

async function pruneInTx(
  ks: IDBObjectStore,
  ms: IDBObjectStore,
  meta: CacheMeta,
  maxBars = MAX_BARS,
): Promise<void> {
  if (meta.count <= maxBars) return
  const [symbol, interval] = meta.key.split(':')
  const range = IDBKeyRange.bound(
    [symbol, interval, 0],
    [symbol, interval, Number.MAX_SAFE_INTEGER],
  )
  const keys = (await req(ks.getAllKeys(range))) as Array<[string, string, number]>
  const drop = keys.length - maxBars
  if (drop <= 0) {
    meta.count = keys.length
    return
  }
  for (const k of keys.slice(0, drop)) ks.delete(k)
  meta.count = keys.length - drop
  meta.oldest = keys[drop][2]
}

async function writeMetaAndBars(
  symbol: string,
  interval: KlineInterval,
  bars: Kline[],
  mode: 'tail' | 'older',
  hasMore: boolean,
): Promise<void> {
  if (!bars.length) {
    if (mode === 'older') {
      const db = await openDb()
      if (!db) return
      await ensureSchema(db)
      const tx = db.transaction('meta', 'readwrite')
      const ms = tx.objectStore('meta')
      const prev = (await req(ms.get(keyOf(symbol, interval)))) as CacheMeta | undefined
      ms.put({
        ...(prev ?? {
          key: keyOf(symbol, interval),
          oldest: Number.MAX_SAFE_INTEGER,
          newest: 0,
          count: 0,
          tailFetchedAt: 0,
        }),
        noMore: true,
        dataVersion: DATA_VERSION,
      })
      await txDone(tx)
    }
    return
  }

  const db = await openDb()
  if (!db) return
  await ensureSchema(db)

  const key = keyOf(symbol, interval)
  const first = bars[0].time
  const last = bars[bars.length - 1].time
  const tx = db.transaction(['klines', 'meta'], 'readwrite')
  const ks = tx.objectStore('klines')
  const ms = tx.objectStore('meta')
  const range = IDBKeyRange.bound(
    [symbol, interval, 0],
    [symbol, interval, Number.MAX_SAFE_INTEGER],
  )

  const prev = (await req(ms.get(key))) as CacheMeta | undefined
  // 尾部新页最新一根倒退（后端降级返回旧缓存）：整页丢弃且不更新 tailFetchedAt，
  // 避免把旧数据标成「刚刷新」让前端在 TTL 内不再重试
  const regressed = mode === 'tail' && !!prev && prev.count > 0 && last < prev.newest
  if (regressed) return
  // 尾部刷新且与缓存尾端不连续：旧段整体丢弃（与 mergeTailFresh 同一口径），
  // 只保留本次新页；noMore 复位，允许左滑按连续页重新拉回历史
  const gap =
    mode === 'tail' &&
    !!prev &&
    prev.count > 0 &&
    first > prev.newest + INTERVAL_SEC[interval] * 1000
  if (gap) ks.delete(range)
  // 历史页与缓存头不连续（期间尾部断层重建过）：丢弃该页，避免缓存出现断层
  const orphanOlder =
    mode === 'older' &&
    !!prev &&
    prev.count > 0 &&
    last + INTERVAL_SEC[interval] * 1000 < prev.oldest
  if (orphanOlder) return

  for (const b of bars) ks.put({ symbol, interval, ...b })

  // prev.count=0 时其 oldest 是占位值（空页 meta 可能写成 MAX_SAFE_INTEGER），
  // 不能沿用，否则 orphanOlder 会把后续所有历史页误判为孤立而拒绝落库
  const prevOldest =
    prev && prev.count > 0 ? prev.oldest : Number.MAX_SAFE_INTEGER
  const meta: CacheMeta = {
    key,
    oldest: gap ? first : Math.min(prevOldest, first),
    newest: gap ? last : Math.max(prev?.newest ?? last, last),
    count: 0, // 下面按实际总数重算，避免重叠段把计数算重
    noMore: mode === 'older' ? !hasMore : gap ? false : (prev?.noMore ?? false),
    tailFetchedAt: mode === 'tail' ? Date.now() : (prev?.tailFetchedAt ?? 0),
    dataVersion: DATA_VERSION,
  }
  meta.count = await req(ks.count(range))
  await pruneInTx(ks, ms, meta)
  ms.put(meta)
  await txDone(tx)
}

/** 尾部刷新：最新一页写入，覆盖未收盘根；保留更早历史 */
export async function saveTail(
  symbol: string,
  interval: KlineInterval,
  bars: Kline[],
): Promise<void> {
  const key = keyOf(symbol, interval)
  if (!bars.length) return
  const prev = mem.get(key)
  touch(symbol, interval, prev ? mergeTailFresh(prev.bars, bars, interval) : bars)
  await withLock(key, async () => {
    try {
      await writeMetaAndBars(symbol, interval, bars, 'tail', true)
    } catch (e) {
      console.warn('[klineDb] saveTail failed (memory kept)', e)
    }
  })
}

/** 左滑历史页：hasMore=false 表示已到最早一根 */
export async function saveOlder(
  symbol: string,
  interval: KlineInterval,
  bars: Kline[],
  hasMore: boolean,
): Promise<void> {
  const key = keyOf(symbol, interval)
  const prev = mem.get(key)
  // 与 writeMetaAndBars 的 orphanOlder 同口径：与内存头不连续的历史页不并入内存
  const last = bars.length ? bars[bars.length - 1].time : 0
  const orphan =
    !!prev &&
    prev.bars.length > 0 &&
    bars.length > 0 &&
    last + INTERVAL_SEC[interval] * 1000 < prev.bars[0].time
  if (bars.length && !orphan) {
    touch(symbol, interval, prev ? mergeOlderBars(prev.bars, bars) : bars)
  }
  await withLock(key, async () => {
    try {
      await writeMetaAndBars(symbol, interval, bars, 'older', hasMore)
    } catch (e) {
      console.warn('[klineDb] saveOlder failed (memory kept)', e)
    }
  })
}

/** 预缓存新鲜度：1h/4h 60 秒，日K 10 分钟 */
export function isTailFresh(meta: CacheMeta | null, interval: KlineInterval, now = Date.now()): boolean {
  if (!meta) return false
  const ttl = interval === '1d' ? 10 * 60_000 : 60_000
  return now - meta.tailFetchedAt < ttl
}
