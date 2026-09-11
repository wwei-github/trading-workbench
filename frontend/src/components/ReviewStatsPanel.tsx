import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Empty,
  Radio,
  Row,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  message,
} from 'antd'
import { ReloadOutlined, QuestionCircleOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import type { ReviewGroupStat, ReviewStats } from '../types'
import {
  EMA_STATE_MAP,
  POSITION_LABEL_MAP,
  SIGNAL_TYPE_MAP,
} from '../constants/labels'

/**
 * 复盘统计页：AI 建议开单 24h 后逐K回放定论的汇总统计。
 * 胜率口径：expired（超时）不计入胜率分母。
 */
export default function ReviewStatsPanel() {
  // 统计窗口天数（默认 30 天）
  const [days, setDays] = useState<7 | 30 | 90>(30)
  const [stats, setStats] = useState<ReviewStats | null>(null)
  const [loading, setLoading] = useState(false)

  const fetchData = useCallback(async (d: number) => {
    setLoading(true)
    try {
      const data = await scanApi.reviewStats(d)
      setStats(data)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取复盘统计失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 初始加载 + 切换窗口时重新拉取
  useEffect(() => {
    fetchData(days)
  }, [days, fetchData])

  // 胜率百分比展示（>=50% 绿色，<50% 红色）
  const renderWinRate = (rate: number) => {
    const pct = (rate * 100).toFixed(1) + '%'
    const color = rate >= 0.5 ? '#52c41a' : '#ff4d4f'
    return <span style={{ color, fontWeight: 600 }}>{pct}</span>
  }

  // 根据非空维度字段确定维度名与分组值（groups 每个元素只有一个维度非空）
  const renderDim = (g: ReviewGroupStat): { dim: string; value: string } => {
    if (g.signal_type) {
      return {
        dim: '信号类型',
        value: SIGNAL_TYPE_MAP[g.signal_type]?.label || g.signal_type,
      }
    }
    if (g.position) {
      return { dim: '位置', value: POSITION_LABEL_MAP[g.position] || g.position }
    }
    if (g.ema_state) {
      return {
        dim: '均线',
        value: EMA_STATE_MAP[g.ema_state]?.label || g.ema_state,
      }
    }
    return { dim: '其他', value: '-' }
  }

  const columns: ColumnsType<ReviewGroupStat> = [
    {
      title: '维度',
      key: 'dim',
      width: 90,
      render: (_: unknown, g: ReviewGroupStat) => renderDim(g).dim,
    },
    {
      title: '分组',
      key: 'group',
      render: (_: unknown, g: ReviewGroupStat) => renderDim(g).value,
    },
    {
      title: '总数',
      dataIndex: 'total',
      key: 'total',
      width: 70,
      sorter: (a, b) => a.total - b.total,
    },
    {
      title: (
        <Tooltip title="胜率口径：expired（超时）不计入胜率分母">
          胜率
        </Tooltip>
      ),
      dataIndex: 'win_rate',
      key: 'win_rate',
      width: 90,
      sorter: (a, b) => a.win_rate - b.win_rate,
      render: (v: number) => renderWinRate(v),
    },
    {
      title: '胜·TP1',
      dataIndex: 'win_tp1',
      key: 'win_tp1',
      width: 80,
      render: (v: number) => <Tag color="green">{v}</Tag>,
    },
    {
      title: '胜·TP2',
      dataIndex: 'win_tp2',
      key: 'win_tp2',
      width: 80,
      render: (v: number) => <Tag color="green">{v}</Tag>,
    },
    {
      title: '负',
      dataIndex: 'loss',
      key: 'loss',
      width: 60,
      render: (v: number) => <Tag color="red">{v}</Tag>,
    },
    {
      title: '超时',
      dataIndex: 'expired',
      key: 'expired',
      width: 70,
      render: (v: number) => <Tag color="default">{v}</Tag>,
    },
  ]

  const hasData = !!stats && stats.total > 0

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* 顶部：统计窗口切换 + 刷新 */}
      <Row justify="space-between" align="middle">
        <Col>
          <Radio.Group
            optionType="button"
            buttonStyle="solid"
            value={days}
            onChange={(e) => setDays(e.target.value)}
            options={[
              { value: 7, label: '7天' },
              { value: 30, label: '30天' },
              { value: 90, label: '90天' },
            ]}
          />
        </Col>
        <Col>
          <Button icon={<ReloadOutlined />} size="small" onClick={() => fetchData(days)}>
            刷新
          </Button>
        </Col>
      </Row>

      <Spin spinning={loading}>
        {hasData ? (
          <>
            {/* 汇总统计卡片 */}
            <Row gutter={[12, 12]}>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic title="复盘总数" value={stats!.total} />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic
                    title="胜率"
                    value={(stats!.win_rate * 100).toFixed(1)}
                    suffix="%"
                    valueStyle={{
                      color: stats!.win_rate >= 0.5 ? '#52c41a' : '#ff4d4f',
                    }}
                  />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic title="胜·TP1" value={stats!.win_tp1} valueStyle={{ color: '#52c41a' }} />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic title="胜·TP2" value={stats!.win_tp2} valueStyle={{ color: '#52c41a' }} />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic title="负·止损" value={stats!.loss} valueStyle={{ color: '#ff4d4f' }} />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic title="超时" value={stats!.expired} />
                </Card>
              </Col>
            </Row>

            {/* 维度分组统计表 */}
            <Card
              size="small"
              title={
                <Tooltip title="胜率口径：expired（超时）不计入胜率分母">
                  <span>
                    分组统计 <QuestionCircleOutlined style={{ color: '#999', fontSize: 12 }} />
                  </span>
                </Tooltip>
              }
              styles={{ body: { padding: 0 } }}
            >
              <Table<ReviewGroupStat>
                rowKey={(g) =>
                  `${g.signal_type ?? ''}-${g.position ?? ''}-${g.ema_state ?? ''}`
                }
                columns={columns}
                dataSource={stats!.groups}
                size="small"
                pagination={false}
              />
            </Card>
          </>
        ) : (
          <Empty description="暂无复盘数据（AI 建议 24h 后自动复盘）" style={{ marginTop: 60 }} />
        )}
      </Spin>
    </div>
  )
}
