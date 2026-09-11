// TradingView 免费嵌入 Widget（iframe 看盘模式）
// - 图内自带全量内置指标（顶部工具栏 fx 按钮可添加/改参数），TradingView 官方数据
// - 限制：数据不走本系统后端，且无法绘制本系统的关键位线 / AI 仓位标注（需要标注请切"标注图"）
// - 将来自托管 Charting Library 审批通过后，此组件替换为 Charting Library 实现，切换 UI 不变

interface Props {
  symbol: string // 如 BTCUSDT
}

export default function TvWidgetChart({ symbol }: Props) {
  // 本系统币种统一来自币安系 USDT 交易对，直接映射 BINANCE:XXXUSDT
  const url = new URL('https://s.tradingview.com/widgetembed/')
  url.searchParams.set('symbol', `BINANCE:${symbol}`)
  url.searchParams.set('interval', '60') // 1h，与后端 kline_interval 一致
  url.searchParams.set('theme', 'dark')
  url.searchParams.set('style', '1') // 蜡烛图
  url.searchParams.set('timezone', 'Asia/Shanghai')
  url.searchParams.set('locale', 'zh')
  url.searchParams.set('withdateranges', '1')

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', minHeight: 360 }}>
      <iframe
        src={url.toString()}
        title={`${symbol} TradingView`}
        style={{ width: '100%', height: '100%', border: 0, borderRadius: 4 }}
        allow="clipboard-write"
        allowFullScreen
      />
      <div
        style={{
          position: 'absolute',
          bottom: 6,
          left: 8,
          zIndex: 20,
          fontSize: 11,
          color: 'rgba(255, 255, 255, 0.55)',
          pointerEvents: 'none',
          textShadow: '0 1px 2px rgba(0,0,0,0.8)',
        }}>
        TradingView 行情（BINANCE:{symbol}）· 若空白请确认可访问 tradingview.com
      </div>
    </div>
  )
}
