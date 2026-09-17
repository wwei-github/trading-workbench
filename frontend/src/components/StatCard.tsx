/**
 * 概览小卡片：灰底 + 大数字，与 AI 价格卡同风格。
 * 交易记录页与资金划转页共用。
 */
export default function StatCard({ title, value, color }: { title: string; value: string; color?: string }) {
  return (
    <div style={{ flex: 1, minWidth: 120, padding: '8px 14px', borderRadius: 8, background: '#fafafa' }}>
      <div style={{ fontSize: 12, color: '#999', marginBottom: 2 }}>{title}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: color || 'rgba(0,0,0,0.88)', lineHeight: 1.3 }}>
        {value}
      </div>
    </div>
  )
}
