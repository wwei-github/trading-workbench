import { Statistic, Row, Col, Tag } from 'antd'
import { bj } from '../utils/dayjs'
import { useScanStore } from '../stores/scanStore'

export default function ScanStatus() {
  const { status } = useScanStore()
  const last = status?.last_scan

  return (
    <Row gutter={24}>
      <Col>
        <Statistic
          title="扫描状态"
          valueRender={() =>
            status?.is_scanning ? (
              <Tag color="processing" style={{ fontSize: 14 }}>
                扫描中
              </Tag>
            ) : (
              <Tag color="success" style={{ fontSize: 14 }}>
                空闲
              </Tag>
            )
          }
        />
      </Col>
      <Col>
        <Statistic title="最近扫描命中" value={last?.hit_count ?? 0} suffix="个" />
      </Col>
      <Col>
        <Statistic
          title="最近扫描时间"
          valueRender={() =>
            last?.finished_at
              ? bj(last.finished_at).format('MM-DD HH:mm')
              : '-'
          }
        />
      </Col>
      <Col>
        <Statistic
          title="扫描配置"
          valueRender={() => (
            <span style={{ fontSize: 13 }}>
              {status?.config.kline_interval} · 窗口{status?.config.window} · 突破
              {((status?.config.breakout_threshold ?? 0) * 100).toFixed(0)}%
            </span>
          )}
        />
      </Col>
    </Row>
  )
}
