/** 品牌 Logo：三根 K 线蜡烛内联 SVG（暗色 Header 底上醒目，零外部依赖） */
export default function Logo() {
  return (
    <svg width="30" height="30" viewBox="0 0 30 30" fill="none" aria-hidden="true" style={{ flexShrink: 0 }}>
      {/* 底板：微亮圆角矩形，与深色 Header 区分 */}
      <rect x="0" y="0" width="30" height="30" rx="7" fill="rgba(255,255,255,0.08)" />
      {/* 蜡烛1（左，矮）：影线 + 实体 */}
      <line x1="7" y1="6" x2="7" y2="24" stroke="#26a69a" strokeWidth="1.4" />
      <rect x="4.2" y="11" width="5.6" height="8" rx="1" fill="#26a69a" />
      {/* 蜡烛2（中，高——突破主升） */}
      <line x1="15" y1="4" x2="15" y2="26" stroke="#e6c86e" strokeWidth="1.4" />
      <rect x="12.2" y="7" width="5.6" height="12" rx="1" fill="#e6c86e" />
      {/* 蜡烛3（右，中） */}
      <line x1="23" y1="7" x2="23" y2="23" stroke="#ef5350" strokeWidth="1.4" />
      <rect x="20.2" y="13" width="5.6" height="7" rx="1" fill="#ef5350" />
    </svg>
  )
}
