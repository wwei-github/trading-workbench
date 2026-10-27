import { useState } from 'react'
import { Button, Col, Popconfirm, Row, Statistic, Tag, Tooltip, message } from 'antd'
import { bj } from '../utils/dayjs'
import { scanApi } from '../api/scan'
import { useScanStore } from '../stores/scanStore'

/** 顶部运行状态一览：
 *  自动成交 + 熔断状态 + 最近扫描（时间+命中）+ 在跑单水位（资金数字全部下沉到各页面概览）。
 *  自动成交（2026-10-03 删环境切换）：双网独立开关状态 Tag + 整体 Tooltip
 *  说明（原按钮右侧 EnvironmentTag 徽标已并入此处，页面仅此一处展示）。
 *  熔断状态（2026-10-04）：各网当日亏损熔断展示（展示口径，钱包 5min 缓存；
 *  开仓拦截以后端 open gate 实时判定为准）——熔断中=红 / 正常=绿 / 关闭未知=灰。
 *  账号口径（2026-10-13）：后端按 managed_accounts（双网管理清单，不随自动开单
 *  开关变化）统计——熔断/减速对手动开单同样生效，故正式网关自动时也照常展示
 *  正常/周减速/熔断（此前按自动交易账号口径，正式网会整网丢失周减速状态）。
 *  分层熔断（2026-10-04，docs/13 #2）：月熔断（红）> 周减速（橙）> 日熔断（红）
 *  按优先级显示最严的一层；月熔断中显示「确认复盘」按钮（写确认时刻恢复开仓）。
 *  在跑单口径（2026-09-25 v3）：主数字 = 占上限名额的笔数（OPENED），
 *  已进止盈止损（TP1_HIT/TP2_HIT，止损已移保本）用绿色 Tag 单独标注、不占名额；
 *  满仓红色 / 差一满橙色分级预警。 */
export default function ScanStatus() {
  const { status, fetchStatus } = useScanStore()
  const last = status?.last_scan
  const tnTrading = status?.trading_enabled_testnet ?? false
  const tnEnabled = status?.testnet_enabled ?? true // 测试网总开关（总闸关=仅停新增，存量照常结算）
  const liveTrading = status?.trading_enabled_live ?? false
  const lb = status?.loss_breaker
  const [acking, setAcking] = useState(false)

  // 月熔断人工复盘确认：写确认时刻（当前 UTC 月内有效，放行至月底），刷新状态
  const ackMonthlyBreaker = async () => {
    setAcking(true)
    try {
      await scanApi.ackMonthlyBreaker()
      await fetchStatus()
      message.success('已确认复盘，月度熔断恢复开仓（本月内有效）')
    } catch {
      message.error('确认失败，请重试')
    } finally {
      setAcking(false)
    }
  }

  // 周减速人工复盘确认（2026-10-07，对称月熔断）：写确认时刻恢复全额预算
  const ackWeeklyBreaker = async () => {
    setAcking(true)
    try {
      await scanApi.ackWeeklyBreaker()
      await fetchStatus()
      message.success('已确认复盘，周度减速恢复全额预算（本周内有效）')
    } catch {
      message.error('确认失败，请重试')
    } finally {
      setAcking(false)
    }
  }

  // 熔断状态 tag（net=testnet/live，prefix=测/正）：分层取最严 + tooltip 明细
  const breakerTag = (net: 'testnet' | 'live', prefix: string, lastMargin: boolean) => {
    const s = lb?.[net]
    const name = prefix === '测' ? '测试网' : '正式网'
    const month = s?.month
    const week = s?.week
    let color = 'default'
    let text: string
    let tip: string
    if (month?.active) {
      // 月熔断（最严）：停开新仓，人工复盘确认恢复
      color = 'red'
      text = '月熔断'
      tip = `${name}月度回撤熔断已触发：UTC 本月已平仓净亏 ${month.pnl} 达阈值 -${month.cap}（风险基数 × 月阈值%），已停开新仓。复盘确认后恢复：点右侧「确认复盘」按钮（本月内有效，跨月重新武装）。`
    } else if (week?.active) {
      // 周减速：不拦开仓，在跑上限/同向上限减半（人工复盘确认可恢复常规，2026-10-07）
      color = 'orange'
      text = '周减速'
      tip = `${name}周度回撤减速生效中：UTC 本周已平仓净亏 ${week.pnl} 达阈值 -${week.cap}（风险基数 × 周阈值%），本周剩余时间在跑上限/同向上限减半（不拦开仓）。点右侧「确认复盘」恢复常规上限（本周内有效，周一 0 点自动重新武装）。`
    } else if (week?.hit && week?.acked) {
      // 周减速已人工确认：本周放行（展示口径，下周一重新武装）
      color = 'green'
      text = '周已确认'
      tip = `${name}周度回撤曾触发（UTC 本周净亏 ${week.pnl} 达阈值 -${week.cap}），已人工复盘确认：本周剩余时间在跑/同向上限恢复常规，下周一 0 点自动重新武装。`
    } else {
      const active = s?.active
      color = active === true ? 'red' : active === false ? 'green' : 'default'
      text = active === true ? '熔断中' : active === false ? '正常' : s?.enabled ? '未知' : '关'
      tip = active === true
        ? `${name}当日亏损熔断已触发：UTC 当日已平仓净亏 ${s!.day_pnl} 达阈值 -${s!.cap}，UTC 0 点自动恢复（停开新仓，AI 分析/巡检不受限）`
        : active === false
          ? `${name}未熔断：UTC 当日已平仓净亏 ${s!.day_pnl} / 阈值 -${s!.cap}（风险基数 × 熔断阈值%，系统配置可调）${month?.hit && month.acked ? '；月熔断已确认复盘' : ''}${week?.hit && week.acked ? '；周减速已确认复盘' : ''}`
          : s?.enabled
            ? `${name}熔断状态未知：钱包余额查询失败，无法判定（不影响实际开仓拦截）`
            : `${name}熔断未启用（开关关闭 / 阈值为 0 / 该网无账号）`
    }
    return (
      <Tooltip key={net} title={tip}>
        <Tag color={color} style={{ fontSize: 14, marginInlineEnd: lastMargin ? 6 : 0 }}>
          {prefix} {text}
        </Tag>
      </Tooltip>
    )
  }

  // 任一网月熔断生效中 → 显示「确认复盘」按钮（确认全局生效，docs/13 #2）；
  // 无月熔断但任一网周减速生效中 → 显示周确认按钮（恢复全额预算，2026-10-07）
  const monthActive = !!(lb?.testnet?.month?.active || lb?.live?.month?.active)
  const weekActive = !!(lb?.testnet?.week?.active || lb?.live?.week?.active)

  // 在跑单（2026-10-04 按网拆分显示）：主数字 = 测试网/正式网各自 OPENED 数
  // （TP1_HIT/TP2_HIT 已保本不占名额，绿色 Tag 全局标注）；满仓 = 正式网达上限
  // （与后端定时扫描「满仓跳过 AI 分析」判定同口径；2026-10-15 起测试网不限
  // 持仓数，OPENED 数不参与满仓判定）
  const tnOpen = status?.open_trades_testnet ?? 0
  const liveOpen = status?.open_trades_live ?? 0
  const tpHitN = status?.open_tp_hit ?? 0
  const maxN = status?.max_open_trades ?? 5
  const unbounded = maxN <= 0
  const full = !unbounded && liveOpen >= maxN
  const nearFull = !unbounded && !full && liveOpen >= maxN - 1
  const numColor = full ? '#cf1322' : nearFull ? '#fa8c16' : undefined

  const tip = unbounded
    ? `在跑 测试网 ${tnOpen}（不限） + 正式网 ${liveOpen} 笔（未设上限）${tpHitN > 0 ? `；已保本 ${tpHitN} 笔` : ''}`
    : `在跑 测试网 ${tnOpen}（不限持仓） + 正式网 ${liveOpen} 笔（上限 ${maxN}，正式网满仓时定时扫描跳过本轮 AI 分析）${tpHitN > 0 ? `；已保本 ${tpHitN} 笔（不占名额）` : ''}${full ? '；正式网满仓' : ''}`

  return (
    <Row gutter={28}>
      <Col>
        <Statistic
          title="自动成交"
          valueRender={() => (
            <Tooltip
              title={
                `自动成交开关（双网独立，可同时开启）：测试网` +
                (tnEnabled ? (tnTrading ? '已开启' : '已关闭') : '总闸已关闭') +
                ` / 正式网${liveTrading ? '已开启' : '已关闭'}（AI 分析后自动开单，正式网为实盘）。` +
                (tnEnabled
                  ? `仅控自动开新单；手动开单、AI 分析、结算巡检均不受限。在「系统配置」弹窗中可切换。`
                  : `测试网总开关已关闭：测试网合约停止全部新增（自动/手动开单、外部仓接管、` +
                    `飞书通知、自动持仓诊断与镜像），存量持仓结算/保护/挂单巡检照常，` +
                    `历史数据只读可查、统计默认切正式网。在「系统配置」弹窗中可重新开启。`)
              }
            >
              <span>
                <Tag
                  color={tnEnabled && tnTrading ? 'orange' : 'default'}
                  style={{ fontSize: 14, marginInlineEnd: 6 }}
                >
                  测 {!tnEnabled ? '总闸关' : tnTrading ? '运行中' : '关'}
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
              {monthActive && (
                <Popconfirm
                  title="确认已复盘本月回撤？"
                  description="确认后月度熔断恢复开仓，本月内有效（跨月重新武装）。"
                  okText="确认恢复"
                  cancelText="取消"
                  onConfirm={ackMonthlyBreaker}
                >
                  <Button size="small" danger type="primary" loading={acking} style={{ marginLeft: 6 }}>
                    确认复盘
                  </Button>
                </Popconfirm>
              )}
              {!monthActive && weekActive && (
                <Popconfirm
                  title="确认已复盘本周回撤？"
                  description="确认后周度减速恢复常规在跑/同向上限（软减速不拦开仓，确认只恢复上限），本周内有效（下周一重新武装）。"
                  okText="确认恢复"
                  cancelText="取消"
                  onConfirm={ackWeeklyBreaker}
                >
                  <Button size="small" type="primary" loading={acking} style={{ marginLeft: 6 }}>
                    确认复盘
                  </Button>
                </Popconfirm>
              )}
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
                  <span>测 {tnOpen} · 正 {liveOpen}</span>
                ) : (
                  <>
                    <span style={{ color: numColor, fontWeight: full || nearFull ? 600 : undefined }}>
                      测 {tnOpen} · 正 {liveOpen}
                    </span>
                    <span style={{ fontSize: 12, color: '#8c8c8c', marginLeft: 4 }}> / {maxN}每网</span>
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
