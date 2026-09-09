import { useEffect, useRef, useState } from 'react'
import { Spin } from 'antd'
import { scanApi } from '../api/scan'
import type { Kline } from '../types'

interface Props {
  symbol: string
  limit?: number
}

const W = 520
const H = 300
const padding = { top: 10, right: 50, bottom: 20, left: 8 }

export default function KlineChart({ symbol, limit = 100 }: Props) {
  const [klines, setKlines] = useState<Kline[]>([])
  const [loading, setLoading] = useState(false)
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    scanApi
      .klines(symbol, limit)
      .then((data) => {
        if (!cancelled) setKlines(data.klines)
      })
      .catch(() => {
        if (!cancelled) setKlines([])
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [symbol, limit])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || klines.length === 0) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const innerW = W - padding.left - padding.right
    const innerH = H - padding.top - padding.bottom

    // 清空
    ctx.clearRect(0, 0, W, H)

    // 计算价格范围
    let minPrice = Infinity
    let maxPrice = -Infinity
    let maxVol = 0
    for (const k of klines) {
      minPrice = Math.min(minPrice, k.low)
      maxPrice = Math.max(maxPrice, k.high)
      maxVol = Math.max(maxVol, k.volume)
    }
    if (minPrice === Infinity) return
    const priceRange = maxPrice - minPrice || 1
    // 留 5% 空隙
    minPrice -= priceRange * 0.05
    maxPrice += priceRange * 0.05

    const candleW = innerW / klines.length
    const candleBodyW = Math.max(2, candleW * 0.7)

    const yPrice = (p: number) =>
      padding.top + ((maxPrice - p) / (maxPrice - minPrice)) * innerH

    // 价格刻度（5 条）
    ctx.strokeStyle = '#e0e0e0'
    ctx.fillStyle = '#999'
    ctx.font = '10px monospace'
    ctx.lineWidth = 0.5
    for (let i = 0; i <= 5; i++) {
      const p = minPrice + (priceRange * i) / 5
      const y = yPrice(p)
      ctx.beginPath()
      ctx.moveTo(padding.left, y)
      ctx.lineTo(W - padding.right, y)
      ctx.stroke()
      const label =
        p < 1 ? p.toFixed(6) : p < 100 ? p.toFixed(4) : p.toFixed(2)
      ctx.fillText(label, W - padding.right + 4, y + 3)
    }

    // 画 K 线
    klines.forEach((k, i) => {
      const x = padding.left + i * candleW + candleW / 2
      const isUp = k.close >= k.open
      const color = isUp ? '#52c41a' : '#ff4d4f'

      // 影线
      ctx.strokeStyle = color
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(x, yPrice(k.high))
      ctx.lineTo(x, yPrice(k.low))
      ctx.stroke()

      // 实体
      const yOpen = yPrice(k.open)
      const yClose = yPrice(k.close)
      const bodyTop = Math.min(yOpen, yClose)
      const bodyH = Math.max(1, Math.abs(yClose - yOpen))
      ctx.fillStyle = color
      ctx.fillRect(x - candleBodyW / 2, bodyTop, candleBodyW, bodyH)
    })

    // 标注最后价格
    const lastK = klines[klines.length - 1]
    if (lastK) {
      const lastPrice = lastK.close
      const y = yPrice(lastPrice)
      ctx.strokeStyle = '#1890ff'
      ctx.setLineDash([4, 2])
      ctx.beginPath()
      ctx.moveTo(padding.left, y)
      ctx.lineTo(W - padding.right, y)
      ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = '#1890ff'
      const label =
        lastPrice < 1
          ? lastPrice.toFixed(6)
          : lastPrice < 100
          ? lastPrice.toFixed(4)
          : lastPrice.toFixed(2)
      ctx.fillText(label, W - padding.right + 4, y + 3)
    }
  }, [klines])

  return (
    <Spin spinning={loading}>
      <div style={{ textAlign: 'center' }}>
        <canvas
          ref={canvasRef}
          width={W}
          height={H}
          style={{ maxWidth: '100%' }}
        />
        {klines.length === 0 && !loading && (
          <span style={{ color: '#999' }}>暂无K线数据</span>
        )}
      </div>
    </Spin>
  )
}
