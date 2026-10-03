import { useEffect } from 'react'
import { Tooltip, Tag } from 'antd'
import { useScanStore } from '../stores/scanStore'

/** 双网自动交易状态徽标（工具栏常驻，高亮醒目）：2026-10-03 删环境切换，
 *  改为双网开关状态——测试网开=橙色实心 / 正式网开=红色实心（volcano），
 *  关闭=灰色描边，一眼可辨两网自动成交是否在跑。
 *  挂载时拉取一次配置；系统配置弹窗保存后 store 更新即随之刷新。 */
export default function EnvironmentTag() {
  const { aiConfig, fetchConfig } = useScanStore()

  useEffect(() => {
    if (!aiConfig) fetchConfig()
  }, [aiConfig, fetchConfig])

  if (!aiConfig) return null
  const tnTrading = aiConfig.trading_enabled_testnet ?? false
  const liveTrading = aiConfig.trading_enabled_live ?? false

  const tagStyle = (on: boolean, rgb: string) => ({
    marginInlineEnd: 0,
    fontSize: 14,
    fontWeight: 600,
    lineHeight: '24px',
    padding: '2px 12px',
    borderRadius: 999,
    boxShadow: on ? `0 0 0 2px rgba(${rgb}, 0.15)` : undefined,
  })

  return (
    <Tooltip
      title={
        '自动成交开关（双网独立，可同时开启）：' +
        `测试网${tnTrading ? '已开启' : '已关闭'} / 正式网${liveTrading ? '已开启' : '已关闭'}` +
        `（仅控 AI 分析后自动开新单；手动开单/AI 分析不受限）。在「系统配置」弹窗中可切换。`
      }
    >
      <span>
        <Tag color={tnTrading ? 'orange' : 'default'} style={tagStyle(tnTrading, '250, 140, 22')}>
          测试网 {tnTrading ? '开' : '关'}
        </Tag>
        <Tag color={liveTrading ? 'volcano' : 'default'} style={tagStyle(liveTrading, '207, 19, 34')}>
          正式网 {liveTrading ? '开' : '关'}
        </Tag>
      </span>
    </Tooltip>
  )
}
