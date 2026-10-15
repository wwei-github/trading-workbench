import type { ReactNode } from 'react'

/** 配置弹窗分区卡片（2026-10-15 统一视觉）：
 *  浅底描边容器 + 主色竖条标题 + 灰色说明文字。
 *  策略配置 / 交易配置 / 系统配置三个弹窗共用；danger=true 切红色主题（危险操作区）。
 *  容器底部不留 padding，由最后一项自带的下边距（16px 或 Card 内既有间距）收尾。 */
export default function ConfigSection({ title, hint, first, danger, children }: {
  title: string
  hint?: string
  first?: boolean
  danger?: boolean
  children: ReactNode
}) {
  const accent = danger ? '#cf1322' : '#1677ff'
  return (
    <div
      style={{
        border: `1px solid ${danger ? '#ffccc7' : '#f0f0f0'}`,
        borderRadius: 8,
        background: danger ? '#fff2f0' : '#fafafa',
        padding: '12px 16px 0',
        marginTop: first ? 0 : 16,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <span style={{ width: 3, height: 13, borderRadius: 2, background: accent, flexShrink: 0 }} />
        <span style={{ fontSize: 13, fontWeight: 600, color: danger ? '#cf1322' : undefined }}>{title}</span>
        {hint && <span style={{ fontSize: 12, color: '#999' }}>{hint}</span>}
      </div>
      {children}
    </div>
  )
}
