import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { LineChart, BarChart, PieChart, HeatmapChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, MarkLineComponent,
  CalendarComponent, VisualMapComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsCoreOption } from 'echarts/core'

echarts.use([
  LineChart, BarChart, PieChart, HeatmapChart,
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, MarkLineComponent,
  CalendarComponent, VisualMapComponent, CanvasRenderer,
])

/** ECharts 薄封装：按需注册 + ResizeObserver 自适应容器宽度 */
export default function EChart({ option, height = 300 }: {
  option: EChartsCoreOption
  height?: number
}) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!ref.current) return
    const chart = echarts.init(ref.current)
    chartRef.current = chart
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(ref.current)
    // StrictMode 双挂载安全：init 与 dispose 在同一 effect 的清理中配对
    return () => { ro.disconnect(); chart.dispose() }
  }, [])

  useEffect(() => {
    chartRef.current?.setOption(option, true)
  }, [option])

  return <div ref={ref} style={{ width: '100%', height }} />
}
