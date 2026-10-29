import type { SystemConfig } from '../types'

/** 保存回读校验（docs/23 T14b，2026-10-29）。
 *
 *  背景：07:21 那次页面保存把 `tp_full_exit_enabled` 改回 false，页面只报「配置
 *  已保存」，事后只能靠日志与审计表反推。这里在 PUT 之后立即 GET 一次，逐键比对
 *  「提交值」与「库内实际值」，不一致就把差异键报给用户并保持弹窗打开。
 *
 *  比对口径：
 *  - `null` / `undefined` 视为「未设」（后端 NULL=回落 env 默认），二者等价；
 *  - 数值按容差比较（后端 Numeric 列回读是 Decimal→float，避免 0.33000000001 误报）；
 *  - 数组按元素、对象按键递归；
 *  - `scan_filters` 按键比对（后端做键级合并：未提交的键保留库内旧值，不应报差异）；
 *  - `ai_api_key` 永不回传明文，跳过比对（只回 ai_api_key_set 布尔）。
 */

const EPS = 1e-6

function isNil(v: unknown): boolean {
  return v === null || v === undefined
}

function same(a: unknown, b: unknown): boolean {
  if (isNil(a)) return isNil(b)
  if (isNil(b)) return false
  if (typeof a === 'boolean' || typeof b === 'boolean') return Boolean(a) === Boolean(b)
  if (typeof a === 'number' || typeof b === 'number') {
    const na = Number(a)
    const nb = Number(b)
    if (!Number.isFinite(na) || !Number.isFinite(nb)) return false
    return Math.abs(na - nb) <= Math.max(EPS, Math.abs(na) * EPS)
  }
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b)) return false
    return a.length === b.length && a.every((v, i) => same(v, b[i]))
  }
  if (typeof a === 'object' && typeof b === 'object') {
    const ka = Object.keys(a as Record<string, unknown>)
    const kb = Object.keys(b as Record<string, unknown>)
    return ka.length === kb.length
      && ka.every((k) => same(
        (a as Record<string, unknown>)[k],
        (b as Record<string, unknown>)[k],
      ))
  }
  return a === b
}

/** 返回「提交了但库里不是这个值」的键（空数组 = 回读一致） */
export function diffConfig(
  submitted: Partial<SystemConfig>,
  fresh: SystemConfig,
): string[] {
  const diffs: string[] = []
  for (const [key, want] of Object.entries(submitted)) {
    if (key === 'scan_filters' || key === 'ai_api_key') continue
    // undefined = 本次没提交（JSON 里会被丢掉，后端保留库内旧值），不能要求
    // 回读值「也是 undefined」——否则 `x != null ? ... : undefined` 这类写法
    // 只要用户清空过该项，保存成功也会被误报「保存未生效」
    if (want === undefined) continue
    const got = (fresh as unknown as Record<string, unknown>)[key]
    if (!same(want, got)) diffs.push(key)
  }

  // scan_filters：显式 null=清除全部定制（期望库内也为 null）；对象=按键合并后回读
  if ('scan_filters' in submitted) {
    const want = submitted.scan_filters
    if (want === null) {
      if (fresh.scan_filters != null) diffs.push('scan_filters')
    } else if (want) {
      for (const [field, expected] of Object.entries(want)) {
        const got = (fresh.scan_filters as Record<string, unknown> | null | undefined)?.[field]
        if (!same(expected, got)) diffs.push(`scan_filters.${field}`)
      }
    }
  }
  return diffs
}
