import { useEffect } from 'react'
import { Tooltip, Tag } from 'antd'
import { GlobalOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'

/** 当前交易环境徽标（工具栏常驻，高亮醒目）：实心色块 + 环境图标，
 *  测试网=橙色 / 正式网=红色（volcano），一眼可辨。
 *  挂载时拉取一次配置；系统配置弹窗保存后 store 更新即随之刷新。 */
export default function EnvironmentTag() {
  const { aiConfig, fetchConfig } = useScanStore()

  useEffect(() => {
    if (!aiConfig) fetchConfig()
  }, [aiConfig, fetchConfig])

  if (!aiConfig) return null
  const testnet = aiConfig.trading_testnet ?? true
  const trading = aiConfig.trading_enabled ?? false
  // 正式网模式显示当前选中账号名（docs/17：多账号时区分 live-1/live-2…）
  const accName = !testnet
    ? aiConfig.accounts?.find((a) => a.key === aiConfig.current_account_key)?.name
    : null
  const label = testnet ? '测试网' : accName || '正式网'

  return (
    <Tooltip
      title={
        `当前交易账号：${label}；自动成交${trading ? '已开启' : '已关闭'}` +
        `（仅控 AI 分析后自动开新单；手动开单/AI 分析不受限）。在「系统配置」弹窗中可切换。`
      }
    >
      <Tag
        icon={<GlobalOutlined />}
        color={testnet ? 'orange' : 'volcano'}
        style={{
          marginInlineEnd: 0,
          fontSize: 14,
          fontWeight: 600,
          lineHeight: '24px',
          padding: '2px 12px',
          borderRadius: 999,
          boxShadow: testnet
            ? '0 0 0 2px rgba(250, 140, 22, 0.15)'
            : '0 0 0 2px rgba(207, 19, 34, 0.15)',
        }}
      >
        {label}
      </Tag>
    </Tooltip>
  )
}
