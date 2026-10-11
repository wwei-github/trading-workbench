/**
 * 筛选漏斗（2026-10-11）：展示每轮扫描各币种卡在哪一关
 * - 数据源：GET /api/scans/{scanId}/funnel（ScanRecord.funnel_json，扫描完成时落库）
 * - 顶部漏斗概览：全市场 → 进管线 → 候选（进 AI），旁路待确认池/异常
 * - 主体按关卡分组展示被拒币种 Tag（预过滤 5 关 + 指标管线 7 关），关卡顺序即漏斗逻辑序
 * - 进待确认池的币不算被拒（见「待确认池」Tab）；旧版本扫描/异常轮次无数据（funnel=null）
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button, Empty, Spin, Tag, Tooltip, Typography, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { scanApi } from '../api/scan'
import { useScanStore } from '../stores/scanStore'
import type { FunnelData } from '../types'

const { Text } = Typography

// 关卡元数据（顺序即漏斗逻辑序；代码与 scanner.py reject_out 语义一一对应）
const FUNNEL_STAGES: { key: string; label: string; color: string; hint: string }[] = [
  { key: 'liquidity', label: '流动性', color: 'default', hint: '24h 成交额低于门槛，不进扫描' },
  { key: 'untradable', label: '不可交易', color: 'default', hint: '下单环境未上架该合约（开单会 -1121）' },
  { key: 'new_coin', label: '新币冷静期', color: 'default', hint: '上市未满 N 天（默认 14 天）不进扫描' },
  { key: 'risk_tier', label: '风险档位', color: 'default', hint: '交易所最大允许杠杆 <10×' },
  { key: 'blacklist', label: '黑名单', color: 'default', hint: '手动添加（永久）或连续止损自动加入（冷却期）' },
  { key: 'gate_4h', label: '4h趋势闸门', color: 'orange', hint: '4h 无明确趋势（EMA144 斜率不足/缠绕/ADX<20）一票否决' },
  { key: 'votes', label: '指标多数决', color: 'orange', hint: '票数不足（voted<5）或方向中性（|net|<3）' },
  { key: 'direction_flip', label: '方向冲突', color: 'orange', hint: '指标多数决方向与 4h 闸门方向相反' },
  { key: 'structure_1h', label: '1h结构', color: 'orange', hint: '1h 三线排列 + ATR 发散度不成立' },
  { key: 'range_score', label: '震荡评分', color: 'orange', hint: '震荡评分卡 ≥3（强震荡共识）' },
  { key: 'no_trigger', label: '无触发', color: 'purple', hint: '通过全部门但无触发信号，也未进待确认池' },
  { key: 'trigger_whitelist', label: '触发白名单外', color: 'purple', hint: '触发类型不在白名单（筛选配置）' },
]

export default function FunnelPanel() {
  const [data, setData] = useState<FunnelData | null>(null)
  const [loading, setLoading] = useState(false)
  const currentScanId = useScanStore((s) => s.currentScanId)
  const isScanning = !!useScanStore((s) => s.status?.is_scanning)

  const fetchData = useCallback(async (scanId: string | null) => {
    if (!scanId) return
    setLoading(true)
    try {
      setData((await scanApi.funnel(scanId)).funnel)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取筛选漏斗失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData(currentScanId)
  }, [currentScanId, fetchData])

  // 扫描完成（进行中→结束）自动刷新到最新一轮漏斗
  useEffect(() => {
    if (!isScanning && currentScanId) fetchData(currentScanId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isScanning])

  // 按关卡逻辑序分组（stages 里无数的关卡不占行）
  const groups = useMemo(
    () =>
      data
        ? FUNNEL_STAGES.map((st) => ({
            ...st,
            count: data.stages[st.key] ?? 0,
            symbols: Object.entries(data.rejects)
              .filter(([, v]) => v === st.key)
              .map(([k]) => k),
          })).filter((g) => g.count > 0)
        : [],
    [data],
  )

  if (!currentScanId) {
    return <Empty description="暂无扫描记录，先执行一次扫描" style={{ marginTop: 80 }} />
  }

  return (
    <div style={{ padding: '0 4px' }}>
      <div style={{ marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={() => fetchData(currentScanId)}
        >
          刷新
        </Button>
        {isScanning && <Text type="secondary">扫描进行中，完成后自动刷新…</Text>}
      </div>

      {loading && !data ? (
        <div style={{ textAlign: 'center', padding: 60 }}><Spin /></div>
      ) : !data ? (
        <Empty description="该轮无漏斗数据（旧版本扫描或异常轮次）" style={{ marginTop: 60 }} />
      ) : (
        <>
          {/* 漏斗概览 */}
          <div style={{ marginBottom: 16, display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
            <Tooltip title="全市场 USDT 永续合约数"><Tag color="blue">全市场 {data.universe}</Tag></Tooltip>
            <Text type="secondary">→</Text>
            <Tooltip title="预过滤（流动性/可交易/新币/风险档位/黑名单）后进指标管线数">
              <Tag color="blue">进管线 {data.scanned}</Tag>
            </Tooltip>
            <Text type="secondary">→</Text>
            <Tooltip title="通过全部筛选关卡、产生信号候选并进入 AI 分析">
              <Tag color="green">候选进AI {data.candidates}</Tag>
            </Tooltip>
            <Tooltip title="回踩触碰但当根无金K，转入待确认池等确认（不算被拒）">
              <Tag color="cyan">待确认池 {data.pending_pool}</Tag>
            </Tooltip>
            {data.errors > 0 && (
              <Tooltip title="拉K线/指标计算异常数"><Tag color="red">异常 {data.errors}</Tag></Tooltip>
            )}
          </div>

          {/* 各关卡被拒明细 */}
          {groups.length === 0 ? (
            <Empty description="本轮没有被拒币种" style={{ marginTop: 40 }} />
          ) : (
            groups.map((g) => (
              <div key={g.key} style={{ marginBottom: 12 }}>
                <div style={{ marginBottom: 4 }}>
                  <Tooltip title={g.hint}>
                    <Text strong>{g.label}</Text>
                  </Tooltip>
                  <Text type="secondary" style={{ marginLeft: 8 }}>· {g.count} 个</Text>
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                  {g.symbols.map((sym) => (
                    <Tag key={sym} color={g.color} style={{ marginInlineEnd: 0 }}>{sym}</Tag>
                  ))}
                </div>
              </div>
            ))
          )}
        </>
      )}
    </div>
  )
}
