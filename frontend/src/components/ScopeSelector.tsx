/**
 * 账号范围选择器（docs/17 P0）：交易记录 / 资金分析 / 资金划转三页共享。
 * - 单 live 账号（现状）：Segmented 正式网/测试网，值 live/testnet，与旧版行为一致
 * - 多 live 账号（.env 配了 BINANCE_LIVE_2_* 等）：Select 下拉，可选正式网聚合
 *   或具体账号（显示 .env NAME）
 * - 值口径与后端 scope 参数一致：live/testnet 环境聚合或账号 key（live-N/testnet-1）
 */
import { Segmented, Select } from 'antd'
import { useScanStore } from '../stores/scanStore'
import type { SystemConfig } from '../types'

/** 页面默认 scope（2026-10-03 删环境切换）：无全局「当前环境」概念，
 *  恒返回 'all'（后端统计口径已改为双网合计）；保留参数兼容 4 个面板调用点 */
export function defaultScopeOf(_cfg?: SystemConfig | null): string {
  return 'all'
}

/** scope 值是否属于测试网侧（如清空测试网数据按钮的显示条件） */
export function scopeIsTestnet(scope: string): boolean {
  return scope === 'testnet' || scope.startsWith('testnet-')
}

interface Props {
  value: string
  onChange: (v: string) => void
}

export default function ScopeSelector({ value, onChange }: Props) {
  const accounts = useScanStore((s) => s.aiConfig?.accounts) ?? []
  const liveAccounts = accounts.filter((a) => !a.testnet && a.configured)

  // 单 live 账号：Segmented 三档（2026-10-03 加「全部」=默认双网合计口径；
  // value 若是账号 key 则按环境归一显示）
  if (liveAccounts.length <= 1) {
    return (
      <Segmented
        value={value === 'all' ? 'all' : scopeIsTestnet(value) ? 'testnet' : 'live'}
        onChange={(v) => onChange(v as string)}
        options={[
          { label: '全部', value: 'all' },
          { label: '正式网', value: 'live' },
          { label: '测试网', value: 'testnet' },
        ]}
      />
    )
  }
  return (
    <Select
      value={value}
      style={{ minWidth: 168 }}
      onChange={onChange}
      options={[
        { label: '全部', value: 'all' },
        { label: `正式网（全部 ${liveAccounts.length} 个账号）`, value: 'live' },
        ...liveAccounts.map((a) => ({ label: `${a.name}（${a.key}）`, value: a.key })),
        { label: '测试网', value: 'testnet' },
      ]}
    />
  )
}
