/**
 * 资金划转页（顶层 Tab）：合约账户转入/转出记录
 * - 数据来源：每小时 :10 结算巡检从币安 income TRANSFER 同步（GET /api/trades/transfers?scope=）
 * - 环境切换（正式网/测试网）与资金分析页一致：默认跟随当前交易环境
 * - 概览：初始资金 / 当前账户资金 / 累计转入 / 累计转出 / 净流入（服务端全量聚合）；明细表带符号金额着色（随全局涨跌配色）
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Empty, Spin, Table, Tag, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import { bj } from '../utils/dayjs'
import { schemeValueColor } from '../utils/scheme'
import { useScanStore } from '../stores/scanStore'
import StatCard from './StatCard'
import ScopeSelector, { defaultScopeOf } from './ScopeSelector'
import type { AccountTransfer, TransferStats } from '../types'

function fmtTime(s: string | null | undefined): string {
  if (!s) return '-'
  return bj(s).format('MM-DD HH:mm:ss')
}

export default function TransfersPanel() {
  // 默认 scope 跟随当前交易环境（多 live 账号跟随选中账号，docs/17）。
  // 面板被 Tabs 保活：环境切换（defaultScope 变化）后重新跟随，手动选择不被配置刷新打断
  const { aiConfig, fetchConfig } = useScanStore()
  const defaultScope = defaultScopeOf(aiConfig)
  const lastDefaultRef = useRef<string | null>(null)
  const [scope, setScope] = useState<string>(defaultScope)
  const [rows, setRows] = useState<AccountTransfer[]>([])
  const [statsSrv, setStats] = useState<TransferStats | null>(null)
  const [loading, setLoading] = useState(false)
  const colorScheme = useScanStore((s) => s.colorScheme)

  useEffect(() => {
    if (!aiConfig) {
      fetchConfig()
      return
    }
    if (lastDefaultRef.current !== defaultScope) {
      lastDefaultRef.current = defaultScope
      setScope(defaultScope)
    }
  }, [aiConfig, fetchConfig, defaultScope])

  // seq 竞态守卫：scope 切换并发两批请求时，仅渲染最新一批（旧响应丢弃）
  const seqRef = useRef(0)
  const fetchData = useCallback(async () => {
    const seq = ++seqRef.current
    setLoading(true)
    try {
      // 统计走服务端全量聚合（不受明细 limit 截断），含初始资金与实时钱包余额
      const [data, st] = await Promise.all([
        scanApi.trades.transfers(scope),
        scanApi.trades.transferStats(scope).catch(() => null),
      ])
      if (seq !== seqRef.current) return
      setRows(data)
      setStats(st)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取资金划转失败')
    } finally {
      setLoading(false)
    }
  }, [scope])

  // scope 初始化/切换时重新拉取
  useEffect(() => {
    fetchData()
  }, [fetchData])

  // 概览：服务端统计优先（全量聚合 + 初始资金 + 实时钱包余额）；
  // 统计接口失败时回落前端按当前明细行计算（仅前 200 条口径，作降级展示）
  const stats = useMemo(() => {
    if (statsSrv) return statsSrv
    const tin = rows.filter((t) => t.amount > 0).reduce((s, t) => s + t.amount, 0)
    const tout = rows.filter((t) => t.amount < 0).reduce((s, t) => s + Math.abs(t.amount), 0)
    return { tin, tout, net: tin - tout, initial_capital: 0, wallet_balance: 0, available: false }
  }, [statsSrv, rows])

  const columns: ColumnsType<AccountTransfer> = [
    {
      title: '时间',
      dataIndex: 'transfer_time',
      key: 'transfer_time',
      width: 170,
      render: fmtTime,
    },
    {
      title: '方向',
      dataIndex: 'amount',
      key: 'amount',
      width: 100,
      render: (v: number) =>
        v > 0 ? (
          <Tag color="blue" style={{ marginInlineEnd: 0 }}>转入</Tag>
        ) : (
          <Tag color="orange" style={{ marginInlineEnd: 0 }}>转出</Tag>
        ),
    },
    {
      title: '金额',
      dataIndex: 'amount',
      key: 'amount_val',
      width: 160,
      render: (v: number) => (
        <span style={{ color: schemeValueColor(v, colorScheme), fontWeight: 600 }}>
          {v > 0 ? '+' : ''}
          {v.toFixed(2)} USDT
        </span>
      ),
    },
    {
      title: '资产',
      dataIndex: 'asset',
      key: 'asset',
      width: 90,
      render: (v: string) => <Tag style={{ marginInlineEnd: 0 }}>{v}</Tag>,
    },
    {
      title: '交易所流水号',
      dataIndex: 'tran_id',
      key: 'tran_id',
      render: (v: string) => (
        <span style={{ fontSize: 12, color: '#999', fontFamily: 'monospace' }}>{v}</span>
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* 筛选条：账号范围（docs/17）+ 刷新（与资金分析页一致） */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
        <ScopeSelector value={scope} onChange={setScope} />
        <Button icon={<ReloadOutlined />} onClick={fetchData}>
          刷新
        </Button>
      </div>

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <StatCard
          title="初始资金"
          value={stats.initial_capital > 0 ? `${stats.initial_capital.toFixed(2)} USDT` : '--'}
          color="#722ed1"
        />
        <StatCard
          title="当前账户资金（钱包余额）"
          value={stats.available ? `${stats.wallet_balance.toFixed(2)} USDT` : '--'}
          color="#13c2c2"
        />
        <StatCard title="累计转入" value={`+${stats.tin.toFixed(2)} USDT`} color="#1677ff" />
        <StatCard title="累计转出" value={`-${stats.tout.toFixed(2)} USDT`} color="#fa8c16" />
        <StatCard
          title="净流入（转入−转出）"
          value={`${stats.net > 0 ? '+' : ''}${stats.net.toFixed(2)} USDT`}
          color={stats.net === 0 ? undefined : schemeValueColor(stats.net, colorScheme)}
        />
      </div>

      <Spin spinning={loading}>
        {rows.length === 0 && !loading ? (
          <Empty description="暂无资金划转记录（每小时巡检时从交易所同步）" style={{ marginTop: 60 }} />
        ) : (
          <Table<AccountTransfer>
            rowKey="id"
            columns={columns}
            dataSource={rows}
            size="small"
            scroll={{ y: 480 }}
            pagination={{
              pageSize: 20,
              showSizeChanger: true,
              pageSizeOptions: [20, 50, 100],
              showTotal: (t) => `共 ${t} 条`,
              position: ['bottomRight'],
            }}
          />
        )}
      </Spin>
    </div>
  )
}
