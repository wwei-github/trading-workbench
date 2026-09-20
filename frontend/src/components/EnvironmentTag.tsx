import { useEffect } from 'react'
import { Tooltip, Tag } from 'antd'
import { useScanStore } from '../stores/scanStore'

/** 当前交易环境徽标（工具栏常驻）：显示环境与自动交易状态。
 *  挂载时拉取一次配置；系统配置弹窗保存后 store 更新即随之刷新。 */
export default function EnvironmentTag() {
  const { aiConfig, fetchConfig } = useScanStore()

  useEffect(() => {
    if (!aiConfig) fetchConfig()
  }, [aiConfig, fetchConfig])

  if (!aiConfig) return null
  const testnet = aiConfig.trading_testnet ?? true
  const trading = aiConfig.trading_enabled ?? false

  return (
    <Tooltip
      title={
        `当前交易环境：${testnet ? '测试网' : '正式网'}；自动交易${trading ? '已开启' : '已关闭'}` +
        `（关=定时扫描的 AI 分析与开新单停止，结算/复盘/日报照常）。在「系统配置」弹窗中可切换。`
      }
    >
      <Tag color={testnet ? 'orange' : 'red'} style={{ marginInlineEnd: 0 }}>
        {testnet ? '测试网' : '正式网'}
      </Tag>
    </Tooltip>
  )
}
