import { Statistic, Row, Col, Tag } from 'antd'
import { bj } from '../utils/dayjs'
import { useScanStore } from '../stores/scanStore'

/** 顶部运行状态一览（2026-09-20 重构）：
 *  扫描状态 / 最近扫描时间 / 最近命中 / 自动交易开关 / 在跑单数（当前环境）。
 *  扫描配置细节移除（策略配置弹窗内可见，非日常盯盘信息）。 */
export default function ScanStatus() {
  const { status } = useScanStore()
  const last = status?.last_scan
  const trading = status?.trading_enabled ?? false
  const openN = status?.open_trades ?? 0
  const maxN = status?.max_open_trades ?? 5
  // max=0 表示不限单数（仅余额风控兜底）
  const unbounded = maxN <= 0
  const nearLimit = !unbounded && openN >= maxN

  return (
    <Row gutter={28}>
      <Col>
        <Statistic
          title="扫描状态"
          valueRender={() =>
            status?.is_scanning ? (
              <Tag color="processing" style={{ fontSize: 14, marginInlineEnd: 0 }}>
                扫描中
              </Tag>
            ) : (
              <Tag color="success" style={{ fontSize: 14, marginInlineEnd: 0 }}>
                空闲
              </Tag>
            )
          }
        />
      </Col>
      <Col>
        <Statistic
          title="最近扫描"
          valueRender={() => (
            <span style={{ fontSize: 15 }}>
              {last?.finished_at ? bj(last.finished_at).format('MM-DD HH:mm') : '-'}
              <span style={{ fontSize: 12, color: '#8c8c8c', marginLeft: 6 }}>
                命中 {last?.hit_count ?? 0}
              </span>
            </span>
          )}
        />
      </Col>
      <Col>
        <Statistic
          title="自动交易"
          valueRender={() =>
            trading ? (
              <Tag color="green" style={{ fontSize: 14, marginInlineEnd: 0 }}>
                运行中
              </Tag>
            ) : (
              <Tag color="default" style={{ fontSize: 14, marginInlineEnd: 0 }}>
                已关闭
              </Tag>
            )
          }
        />
      </Col>
      <Col>
        <Statistic
          title="在跑单（当前环境）"
          valueRender={() => (
            <span style={{ fontSize: 15 }}>
              <span style={{ color: nearLimit ? '#fa8c16' : undefined, fontWeight: nearLimit ? 600 : undefined }}>
                {openN}
              </span>
              <span style={{ fontSize: 12, color: '#8c8c8c' }}>
                {' '}/ {unbounded ? '不限' : maxN}
              </span>
            </span>
          )}
        />
      </Col>
    </Row>
  )
}
