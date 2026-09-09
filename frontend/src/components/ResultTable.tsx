import { Table, Tag, Tooltip, Empty, Button, Descriptions, Typography, message, Row, Col, Alert, Progress, Space } from 'antd'
import { RobotOutlined, CopyOutlined, LoadingOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { bj } from '../utils/dayjs'
import { useScanStore } from '../stores/scanStore'
import { useState, useMemo } from 'react'
import KlineChart from './KlineChart'
import type { AIAnalysis, ScanResult } from '../types'

const { Text, Paragraph } = Typography

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return '-'
  if (v < 1) return v.toFixed(6)
  if (v < 100) return v.toFixed(4)
  return v.toFixed(2)
}

export default function ResultTable() {
  const {
    results,
    total,
    loading,
    resultsPage,
    resultsPageSize,
    currentScanId,
    aiAnalyses,
    aiLoading,
    aiPolling,
    aiPollingTimeout,
    triggerAiAnalysis,
    fetchResults,
    aiConfig,
  } = useScanStore()

  const aiEnabled = !!aiConfig?.ai_analysis_enabled
  // AI 分析进度：已分析数 / 总命中数（用后端 total，非当前页数）
  const aiProgress = useMemo(() => {
    if (total === 0) return 0
    return Math.round((aiAnalyses.length / total) * 100)
  }, [aiAnalyses, total])

  // 跟踪展开的行
  const [expandedRowKeys, setExpandedRowKeys] = useState<string[]>([])

  // 按 scan_result_id 索引 AI 分析
  const aiMap: Record<string, AIAnalysis> = useMemo(() => {
    const m: Record<string, AIAnalysis> = {}
    for (const a of aiAnalyses) {
      m[a.scan_result_id] = a
    }
    return m
  }, [aiAnalyses])

  const handleReAnalyze = async (record: ScanResult) => {
    if (!currentScanId) {
      message.warning('无当前扫描记录')
      return
    }
    try {
      await triggerAiAnalysis(currentScanId, record.id)
      message.success('AI 分析已提交，请稍候...')
    } catch (e: any) {
      message.error(e?.message || 'AI 分析失败')
    }
  }

  const handleCopySymbol = (e: React.MouseEvent, symbol: string) => {
    e.stopPropagation()
    const text = symbol + '.P'
    navigator.clipboard.writeText(text).then(() => {
      message.success(`已复制: ${text}`)
    }).catch(() => {
      message.error('复制失败')
    })
  }

  const columns: ColumnsType<ScanResult> = [
    {
      title: '币种',
      dataIndex: 'symbol',
      key: 'symbol',
      width: 150,
      render: (v: string) => (
        <span>
          <strong>{v.replace('USDT', '')}/USDT</strong>
          <Button
            type="text"
            size="small"
            icon={<CopyOutlined />}
            onClick={(e) => handleCopySymbol(e, v)}
            style={{ marginLeft: 4, padding: '0 4px' }}
          />
        </span>
      ),
    },
    {
      title: '信号类型',
      dataIndex: 'signal_type',
      key: 'signal_type',
      width: 110,
      filters: [
        { text: '下跌突破', value: 'downtrend_breakout' },
        { text: '区间震荡', value: 'range_bound' },
        { text: '上涨回调', value: 'uptrend_pullback' },
      ],
      onFilter: (value, record) => record.signal_type === value,
      render: (v: string) => {
        const map: Record<string, { label: string; color: string }> = {
          downtrend_breakout: { label: '下跌突破', color: 'red' },
          range_bound: { label: '区间震荡', color: 'orange' },
          uptrend_pullback: { label: '上涨回调', color: 'green' },
        }
        const cfg = map[v] || { label: v, color: 'default' }
        return <Tag color={cfg.color}>{cfg.label}</Tag>
      },
    },
    {
      title: 'K线形态',
      dataIndex: 'pattern',
      key: 'pattern',
      width: 120,
      render: (v: string | null, record: ScanResult) => {
        if (!v) return <span style={{ color: '#999' }}>-</span>
        const map: Record<string, string> = {
          hammer: '锤形线',
          inverted_hammer: '倒锤形线',
          bullish_engulfing: '看涨吞没',
          bearish_engulfing: '看跌吞没',
          morning_star: '启明星',
          piercing_line: '刺透线',
          close_above_prev_high: '收盘破前高',
        }
        const label = map[v] || v
        return (
          <Tooltip title={record.signal_reason || undefined}>
            <Tag color="blue">{label}</Tag>
          </Tooltip>
        )
      },
    },
    {
      title: '当前价格',
      dataIndex: 'current_price',
      key: 'current_price',
      width: 140,
      render: (v: number) =>
        v < 1 ? v.toFixed(6) : v < 100 ? v.toFixed(4) : v.toFixed(2),
    },
    {
      title: '24h成交额',
      dataIndex: 'volume_24h',
      key: 'volume_24h',
      width: 130,
      sorter: (a, b) => a.volume_24h - b.volume_24h,
      defaultSortOrder: 'descend',
      render: (v: number) => {
        if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B'
        if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M'
        if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K'
        return v.toFixed(0)
      },
    },
    {
      title: 'K线量能',
      key: 'volume_type',
      width: 120,
      sorter: (a, b) => a.volume - b.volume,
      render: (_: unknown, r: ScanResult) => {
        const colorMap: Record<string, string> = {
          倍量: 'red',
          放量: 'orange',
          平量: 'default',
          缩量: 'blue',
          地量: 'gray',
        }
        const volStr =
          r.volume >= 1e6
            ? (r.volume / 1e6).toFixed(2) + 'M'
            : r.volume >= 1e3
            ? (r.volume / 1e3).toFixed(1) + 'K'
            : r.volume.toFixed(0)
        return (
          <Tooltip title={`最新收盘量: ${volStr}`}>
            <Tag color={colorMap[r.volume_type] || 'default'}>
              {r.volume_type}
            </Tag>
          </Tooltip>
        )
      },
    },
    {
      title: '突破幅度',
      dataIndex: 'breakout_pct',
      key: 'breakout_pct',
      width: 120,
      sorter: (a, b) => a.breakout_pct - b.breakout_pct,
      render: (v: number) => (
        <span style={{ color: '#52c41a', fontWeight: 600 }}>
          +{v.toFixed(2)}%
        </span>
      ),
    },
    {
      title: '状态',
      dataIndex: 'is_repeat',
      key: 'is_repeat',
      width: 100,
      render: (v: boolean) =>
        v ? (
          <Tag color="default">重复命中</Tag>
        ) : (
          <Tag color="green">新命中</Tag>
        ),
    },
    {
      title: '命中时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (v: string) => bj(v).format('YYYY-MM-DD HH:mm:ss'),
    },
  ]

  return (
    <Table
      rowKey="id"
      className="result-table"
      columns={columns}
      dataSource={results}
      loading={loading}
      pagination={{
        current: resultsPage,
        pageSize: resultsPageSize,
        total,
        showTotal: (t) => `共 ${t} 条`,
        size: 'small',
        onChange: (page, pageSize) => fetchResults(undefined, page, pageSize),
      }}
      locale={{ emptyText: <Empty description="暂无命中币种" /> }}
      scroll={{ x: 1300, y: 'calc(100vh - 400px)' }}
      expandable={{
        expandedRowKeys,
        onExpand: (expanded, record) => {
          setExpandedRowKeys(expanded ? [record.id] : [])
        },
        expandedRowRender: (record: ScanResult) => {
          const ai = aiMap[record.id]
          return (
            <Row gutter={16}>
              {/* 左侧：AI 分析 */}
              <Col span={10}>
                {!aiEnabled ? (
                  <div style={{ textAlign: 'center', padding: '20px 0' }}>
                    <Text type="secondary">AI 分析未开启</Text>
                  </div>
                ) : !ai ? (
                  <div style={{ textAlign: 'center', padding: '20px 0' }}>
                    {aiPolling ? (
                      <>
                        <Text type="secondary">AI 正在批量分析中，请稍候...</Text>
                        <div style={{ marginTop: 8, padding: '0 20px' }}>
                          <Progress percent={aiProgress} size="small" status="active" />
                        </div>
                        <div style={{ marginTop: 4 }}>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            已完成 {aiProgress}%
                          </Text>
                        </div>
                      </>
                    ) : aiPollingTimeout ? (
                      <Alert
                        type="warning"
                        message="AI 分析超时"
                        description="90 秒内未完成分析，可能是 AI 服务响应慢或分析任务较多"
                        showIcon
                        style={{ marginBottom: 12 }}
                      />
                    ) : (
                      <Text type="secondary">暂无 AI 分析结果</Text>
                    )}
                    <div style={{ marginTop: 12, textAlign: 'center' }}>
                      <Button
                        type="primary"
                        ghost
                        icon={<RobotOutlined />}
                        loading={aiLoading}
                        onClick={() => handleReAnalyze(record)}
                      >
                        AI 分析
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div>
                    {aiPolling && (
                      <Alert
                        type="info"
                        message={
                          <Space>
                            <LoadingOutlined />
                            <span>AI 重新分析中... {aiProgress}%</span>
                          </Space>
                        }
                        style={{ marginBottom: 8 }}
                      />
                    )}
                    <Descriptions bordered size="small" column={2}>
                      <Descriptions.Item label="入场价">
                        {fmtPrice(ai.entry_price)}
                      </Descriptions.Item>
                      <Descriptions.Item label="止损价">
                        <span style={{ color: '#ff4d4f' }}>{fmtPrice(ai.stop_loss)}</span>
                      </Descriptions.Item>
                      <Descriptions.Item label="止盈1">
                        <span style={{ color: '#52c41a' }}>{fmtPrice(ai.take_profit_1)}</span>
                      </Descriptions.Item>
                      <Descriptions.Item label="止盈2">
                        <span style={{ color: '#52c41a' }}>{fmtPrice(ai.take_profit_2)}</span>
                      </Descriptions.Item>
                      <Descriptions.Item label="盈亏比">
                        {ai.risk_reward_ratio != null ? `${ai.risk_reward_ratio.toFixed(2)}` : '-'}
                      </Descriptions.Item>
                      <Descriptions.Item label="仓位建议">
                        {ai.position_pct != null ? `${ai.position_pct}%` : '-'}
                      </Descriptions.Item>
                      <Descriptions.Item label="分析时间" span={2}>
                        {bj(ai.created_at).format('YYYY-MM-DD HH:mm:ss')}
                      </Descriptions.Item>
                      <Descriptions.Item label="AI 推理" span={2}>
                        <Paragraph style={{ margin: 0 }}>{ai.analysis || '-'}</Paragraph>
                      </Descriptions.Item>
                    </Descriptions>
                    <div style={{ marginTop: 8, textAlign: 'center' }}>
                      <Button
                        type="primary"
                        ghost
                        icon={<RobotOutlined />}
                        loading={aiLoading}
                        onClick={() => handleReAnalyze(record)}
                      >
                        重新分析
                      </Button>
                    </div>
                  </div>
                )}
              </Col>
              {/* 右侧：K 线图 */}
              <Col span={14}>
                <div style={{ textAlign: 'center', fontWeight: 600, marginBottom: 4 }}>
                  {record.symbol} 最近100根K线
                </div>
                <KlineChart symbol={record.symbol} limit={100} />
              </Col>
            </Row>
          )
        },
        rowExpandable: () => true,
      }}
      onRow={(record) => ({
        onClick: () => {
          setExpandedRowKeys((prev) =>
            prev.includes(record.id)
              ? prev.filter((k) => k !== record.id)
              : [...prev, record.id]
          )
        },
        style: { cursor: 'pointer' },
      })}
    />
  )
}
