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

/** 页面默认 scope：跟随当前交易环境；正式网多账号时跟随选中账号
 *  （current_account_key 已从 .env 移除时回落 live 聚合，避免 stale scope 触发 400） */
export function defaultScopeOf(cfg: SystemConfig | null | undefined): string {
  if (!cfg || (cfg.trading_testnet ?? true)) return 'testnet'
  const liveAccounts = (cfg.accounts ?? []).filter((a) => !a.testnet && a.configured)
  if (liveAccounts.length > 1 && cfg.current_account_key
      && liveAccounts.some((a) => a.key === cfg.current_account_key)) {
    return cfg.current_account_key
  }
  return 'live'
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

  // 单 live 账号：保持旧版 Segmented 两档（value 若是账号 key 则按环境归一显示）
  if (liveAccounts.length <= 1) {
    return (
      <Segmented
        value={scopeIsTestnet(value) ? 'testnet' : 'live'}
        onChange={(v) => onChange(v as string)}
        options={[
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
        { label: `正式网（全部 ${liveAccounts.length} 个账号）`, value: 'live' },
        ...liveAccounts.map((a) => ({ label: `${a.name}（${a.key}）`, value: a.key })),
        { label: '测试网', value: 'testnet' },
      ]}
    />
  )
}
