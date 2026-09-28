/**
 * 扫描结果 K 线图（扫描表/搜索/关注列表共用）
 * 图表能力（指标/图例/工具栏/关键位/AI 仓位标注）全部在 ChartCore，
 * 本组件只负责按 symbol 拉取 K 线数据与「更新」按钮的强制刷新。
 */
import { useCallback, useEffect, useState } from 'react'
import { Alert } from 'antd'
import { scanApi } from '../api/scan'
import type { AIAnalysis, KlineData } from '../types'
import ChartCore from './ChartCore'

interface Props {
  symbol: string
  limit?: number
  ai?: AIAnalysis
  // 变化时强制重新拉取 K 线（关注列表手动刷新用）
  refreshKey?: number
}

const CHART_HEIGHT = 560 // 容器无高度时的兜底值

export default function KlineChart({ symbol, limit = 500, ai, refreshKey = 0 }: Props) {
  const [data, setData] = useState<KlineData | null>(null)
  const [error, setError] = useState<string | null>(null)
  // 「更新」按钮进行中状态
  const [updating, setUpdating] = useState(false)
  // 周期切换（默认 1h，可切 4h；切换即重拉数据）
  const [interval, setInterval] = useState<'1h' | '4h'>('1h')

  // 拉取 K 线（symbol/interval/refreshKey 变化重建数据；先清空避免旧数据串图）
  useEffect(() => {
    setData(null)
    let cancelled = false
    scanApi
      .klines(symbol, limit, false, interval)
      .then((d) => {
        if (!cancelled) {
          setData(d)
          setError(null)
        }
      })
      .catch((e) => {
        // 展示后端 503/4xx 的 detail（如交易所封禁时长）
        if (!cancelled) setError(e?.response?.data?.detail || 'K线数据加载失败')
      })
    return () => {
      cancelled = true
    }
  }, [symbol, limit, refreshKey, interval])

  // 「更新」按钮：跳过缓存直连交易所拉最新 K 线，交易所返回后回写重置缓存
  const handleUpdate = useCallback(async () => {
    setUpdating(true)
    try {
      setData(await scanApi.klines(symbol, limit, true, interval))
      setError(null)
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'K线更新失败')
    } finally {
      setUpdating(false)
    }
  }, [symbol, limit, interval])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', minHeight: 360 }}>
      {error && (
        <Alert
          type="warning"
          showIcon
          message="K线数据获取失败"
          description={error}
          style={{ marginBottom: 8 }}
        />
      )}
      <ChartCore
        chartKey={symbol}
        watermark={symbol}
        label={symbol}
        data={data}
        ai={ai}
        onUpdate={handleUpdate}
        updating={updating}
        interval={interval}
        onIntervalChange={setInterval}
        height={CHART_HEIGHT}
      />
    </div>
  )
}
