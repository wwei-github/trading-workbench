import { Statistic, Row, Col, Tag, Tooltip } from 'antd'
import { bj } from '../utils/dayjs'
import { useScanStore } from '../stores/scanStore'

/** 顶部运行状态一览：
 *  自动成交 + 熔断状态 + 最近扫描（时间+命中）+ 在跑单水位（资金数字全部下沉到各页面概览）。
 *  自动成交（2026-10-03 删环境切换）：双网独立开关状态 Tag + 整体 Tooltip
 *  说明（原按钮右侧 EnvironmentTag 徽标已并入此处，页面仅此一处展示）。
 *  熔断状态（2026-10-04）：各网当日亏损熔断展示（展示口径，钱包 5min 缓存；
 *  开仓拦截以后端 open gate 实时判定为准）——熔断中=红 / 正常=绿 / 关闭未知=灰。
 *  在跑单口径（2026-09-25 v3）：主数字 = 占上限名额的笔数（OPENED），
 *  已进止盈止损（TP1_HIT/TP2_HIT，止损已移保本）用绿色 Tag 单独标注、不占名额；
 *  满仓红色 / 差一满橙色分级预警。 */
export default function ScanStatus() {
  const { status } = useScanStore()
  const last = status?.last_scan
  const tnTrading = status?.trading_enabled_testnet ?? false
  const liveTrading = status?.trading_enabled_live ?? false
  const lb = status?.loss_breaker

  // 熔断状态 tag（net=testnet/live，prefix=测/正）：active 三态着色 + tooltip 明细
  const breakerTag = (net: 'testnet' | 'live', prefix: string, lastMargin: boolean) => {
    const s = lb?.[net]
    const active = s?.active
    const color = active === true ? 'red' : active === false ? 'green' : 'default'
    const text = active === true ? '熔断中' : active === false ? '正常' : s?.enabled ? '未知' : '关'
    const tip = active === true
      ? `${prefix === '测' ? '测试网' : '正式网'}当日亏损熔断已触发：UTC 当日已平仓净亏 ${s!.day_pnl} 达阈值 -${s!.cap}，UTC 0 点自动恢复（停开新仓，AI 分析/巡检不受限）`
      : active === false
        ? `${prefix === '测' ? '测试网' : '正式网'}未熔断：UTC 当日已平仓净亏 ${s!.day_pnl} / 阈值 -${s!.cap}（风险基数 × 熔断阈值%，系统配置可调）`
        : s?.enabled
          ? `${prefix === '测' ? '测试网' : '正式网'}熔断状态未知：钱包余额查询失败，无法判定（不影响实际开仓拦截）`
          : `${prefix === '测' ? '测试网' : '正式网'}熔断未启用（开关关闭 / 阈值为 0 / 该网未开自动交易）`
    return (
      <Tooltip key={net} title={tip}>
        <Tag color={color} style={{ fontSize: 14, marginInlineEnd: lastMargin ? 6 : 0 }}>
          {prefix} {text}
        </Tag>
      </Tooltip>
    )
  }

  const openN = status?.open_trades ?? 0
  const tpHitN = status?.open_tp_hit ?? 0
  const maxN = status?.max_open_trades ?? 5
  const unbounded = maxN <= 0
  // 上限名额仅统计未触发止盈止损的在跑单（TP1_HIT/TP2_HIT 止损已移保本，不占名额）
  const slotUsed = Math.max(0, openN - tpHitN)
  const full = !unbounded && slotUsed >= maxN
  const nearFull = !unbounded && !full && maxN >= 2 && slotUsed === maxN - 1
  const numColor = full ? '#cf1322' : nearFull ? '#fa8c16' : undefined

  const tip = unbounded
    ? `在跑 ${openN} 笔（未设上限）${tpHitN > 0 ? `；已保本 ${tpHitN} 笔` : ''}`
    : `在跑 ${openN} 笔 = 占名额 ${slotUsed} / ${maxN} 笔${tpHitN > 0 ? ` + 已保本 ${tpHitN} 笔` : ''}；已保本指止盈止损已触发、止损移至成本以上，不占上限名额`

  return (
    <Row gutter={28}>
      <Col>
        <Statistic
          title="自动成交"
          valueRender={() => (
            <Tooltip
              title={
                `自动成交开关（双网独立，可同时开启）：测试网${tnTrading ? '已开启' : '已关闭'}` +
                ` / 正式网${liveTrading ? '已开启' : '已关闭'}（AI 分析后自动开单，正式网为实盘）。` +
                `仅控自动开新单；手动开单、AI 分析、结算巡检均不受限。在「系统配置」弹窗中可切换。`
              }
            >
              <span>
                <Tag color={tnTrading ? 'orange' : 'default'} style={{ fontSize: 14, marginInlineEnd: 6 }}>
                  测 {tnTrading ? '运行中' : '关'}
                </Tag>
                <Tag color={liveTrading ? 'volcano' : 'default'} style={{ fontSize: 14, marginInlineEnd: 0 }}>
                  正 {liveTrading ? '运行中' : '关'}
                </Tag>
              </span>
            </Tooltip>
          )}
        />
      </Col>
      <Col>
        <Statistic
          title="熔断状态"
          valueRender={() => (
            <span>
              {breakerTag('testnet', '测', true)}
              {breakerTag('live', '正', false)}
            </span>
          )}
        />
      </Col>
      <Col>
        <Statistic
          title="最近扫描"
          valueRender={() => (
            <span style={{ fontSize: 15 }}>
              {last?.finished_at ? bj(last.finished_at).format('MM-DD HH:mm') : '-'}
              <span style={{ fontSize: 12, color: '#8c8c8c', marginLeft: 6 }}>命中 {last?.hit_count ?? 0}</span>
            </span>
          )}
        />
      </Col>
      <Col>
        <Statistic
          title="在跑单"
          valueRender={() => (
            <Tooltip title={tip}>
              <span style={{ fontSize: 15 }}>
                {unbounded ? (
                  <span>{openN}</span>
                ) : (
                  <>
                    <span style={{ color: numColor, fontWeight: full || nearFull ? 600 : undefined }}>
                      {slotUsed}
                    </span>
                    <span style={{ fontSize: 12, color: '#8c8c8c' }}> / {maxN}</span>
                  </>
                )}
                {tpHitN > 0 && (
                  <Tag color="green" style={{ fontSize: 12, marginLeft: 8, marginInlineEnd: 0, lineHeight: '20px' }}>
                    已保本 {tpHitN}
                  </Tag>
                )}
              </span>
            </Tooltip>
          )}
        />
      </Col>
    </Row>
  )
}
