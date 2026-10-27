import { useState, useEffect } from 'react'
import { Modal, Switch, Tag, message, Button, Spin, Alert, Space } from 'antd'
import { ApiOutlined, DeleteOutlined, WarningOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'
import { scanApi } from '../api/scan'
import ConfigSection from './ConfigSection'
import type { AccountBrief, PurgeStats, SystemConfig } from '../types'

/** 系统配置弹窗（docs/17 P1 扇出 + 2026-10-03 双网独立开关）：
 *  - 无全局「交易环境」概念（已删除）——账号清单测试网/正式网全量展示
 *  - 正式网多个 live 账号可同时启用自动交易（P1 扇出：共享一次 AI 分析，
 *    各账号独立过闸门下单）；测试网恒为 testnet-1 单账号
 *  - 自动交易开关：测试网/正式网各自独立、可同时开启——同一 AI 分析
 *    双网各开一单（镜像跟单）；测试网默认开、正式网默认关
 *  - 测试网总开关（2026-10-27，默认开）：关=仅停合约测试网「新增」（自动/手动
 *    开单、外部仓接管、飞书通知、自动持仓诊断与镜像）；存量结算/保护/挂单巡检
 *    照常；现货测试网独立；历史只读可查、统计默认切 live
 *  - 凭据只在服务器 .env 配置（安全不入库），页面仅展示账号清单与配置状态
 *  - 危险操作（2026-10-04 自交易记录页迁入）：一键清空测试网数据，
 *    预检→弹窗二次确认（含在跑单警告）→执行删除 */
export default function SystemConfigPanel() {
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  const [liveKeys, setLiveKeys] = useState<string[]>([]) // 正式网启用自动交易的账号集合
  const [tnEnabled, setTnEnabled] = useState(true)      // 测试网总开关（默认开；关=仅停合约测试网新增）
  const [tnTrading, setTnTrading] = useState(true)      // 测试网自动交易（默认开）
  const [liveTrading, setLiveTrading] = useState(false) // 正式网自动交易（默认关）
  const [tnNotify, setTnNotify] = useState(true)        // 测试网飞书通知（默认开）
  // 一键清空测试网数据（2026-10-04 自交易记录页迁入）：预检 → 二次确认 → 执行
  const [purgeInfo, setPurgeInfo] = useState<PurgeStats | null>(null)
  const [purging, setPurging] = useState(false)
  const { aiConfig, updateConfig, fetchConfig } = useScanStore()

  useEffect(() => {
    if (!open) return
    setLoading(true)
    fetchConfig().finally(() => setLoading(false))
  }, [open, fetchConfig])

  useEffect(() => {
    if (open && aiConfig) {
      const accs = aiConfig.accounts ?? []
      // 启用集合：配置值；空回落第一个已配置 live 账号 / live-1（与后端回落语义一致）
      const keys = aiConfig.live_trading_keys?.length
        ? aiConfig.live_trading_keys
        : [accs.find((a) => !a.testnet && a.configured)?.key || 'live-1']
      setLiveKeys(keys)
      setTnEnabled(aiConfig.testnet_enabled ?? true)
      setTnTrading(aiConfig.trading_enabled_testnet ?? true)
      setLiveTrading(aiConfig.trading_enabled_live ?? false)
      setTnNotify(aiConfig.feishu_testnet_notify_enabled ?? true)
    }
  }, [open, aiConfig])

  const toggleLive = (key: string, checked: boolean) => {
    setLiveKeys((prev) => (checked ? [...prev, key] : prev.filter((k) => k !== key)))
  }

  // 清空测试网数据：先预检拿将删数量（confirm=false），确认后执行（confirm=true）
  const openPurge = async () => {
    try {
      setPurgeInfo(await scanApi.trades.purgeTestnet(false))
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '预检失败，请重试')
    }
  }
  const handlePurge = async () => {
    setPurging(true)
    try {
      await scanApi.trades.purgeTestnet(true)
      message.success('测试网数据已清空')
      setPurgeInfo(null)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '清空失败，请重试')
    } finally {
      setPurging(false)
    }
  }

  const handleSave = async () => {
    // 实盘安全基线：正式网自动交易开着却没启用任何账号时拦下，
    // 防止扇出静默回落 live-1 意外开实盘单
    if (liveTrading && liveKeys.length === 0) {
      message.error('正式网自动交易已开启，需至少启用一个正式网账号')
      return
    }
    try {
      setSaving(true)
      await updateConfig({
        testnet_enabled: tnEnabled,
        trading_enabled_testnet: tnTrading,
        trading_enabled_live: liveTrading,
        live_trading_keys: liveKeys, // 后端保存时联动维护主账号 current_account_key
        feishu_testnet_notify_enabled: tnNotify,
      } as Partial<SystemConfig>)
      message.success('配置已保存（任务级即时生效，无需重启）')
      setOpen(false)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const accounts: AccountBrief[] = aiConfig?.accounts ?? []
  const liveAccounts = accounts.filter((a) => !a.testnet)
  const testnetAccounts = accounts.filter((a) => a.testnet)

  const accountRow = (a: AccountBrief, actionable: boolean) => {
    const enabled = liveKeys.includes(a.key)
    return (
      <div
        key={a.key}
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '8px 12px',
          border: '1px solid #f0f0f0',
          borderRadius: 8,
          background: actionable && enabled ? '#f6ffed' : '#fff',
        }}
      >
        <Space size={8}>
          <Tag color={a.testnet ? 'orange' : 'volcano'} style={{ marginInlineEnd: 0 }}>
            {a.testnet ? '测试网' : '正式网'}
          </Tag>
          <span style={{ fontWeight: 600 }}>{a.name}</span>
          <span style={{ color: '#999', fontSize: 12 }}>{a.key}</span>
          {a.configured ? (
            <Tag color="green" style={{ marginInlineEnd: 0 }}>已配置</Tag>
          ) : (
            <Tag color="red" style={{ marginInlineEnd: 0 }}>未配置</Tag>
          )}
        </Space>
        {actionable && (
          <Switch
            checked={enabled}
            onChange={(v) => toggleLive(a.key, v)}
            checkedChildren="交易"
            unCheckedChildren="停用"
          />
        )}
      </div>
    )
  }

  return (
    <>
      <Button
        icon={<ApiOutlined />}
        onClick={() => setOpen(true)}
      >
        系统配置
      </Button>
      <Modal
        title="系统配置"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleSave}
        confirmLoading={saving}
        width={520}
      >
        <Spin spinning={loading}>
          <div style={{ maxHeight: '64vh', overflowY: 'auto', paddingInlineEnd: 4 }}>
          <ConfigSection title="账号清单" hint="正式网账号可多选启用自动交易，扇出跟单" first>
          {accounts.length === 0 ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message="未在服务器 .env 配置任何币安 API key，账号清单为空"
            />
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 16 }}>
              {testnetAccounts.map((a) => accountRow(a, false))}
              {liveAccounts.map((a) => accountRow(a, true))}
            </div>
          )}
          </ConfigSection>

          <ConfigSection title="测试网总开关" hint="仅合约测试网；现货测试网开关独立">
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '8px 12px',
              border: '1px solid #f0f0f0',
              borderRadius: 8,
              background: tnEnabled ? '#fff' : '#fffbe6',
              marginBottom: 8,
            }}
          >
            <Space size={8}>
              <Tag color="orange" style={{ marginInlineEnd: 0 }}>测试网</Tag>
              <span>合约测试网总开关</span>
            </Space>
            <Switch
              checked={tnEnabled}
              onChange={(v) => setTnEnabled(v)}
              checkedChildren="启用"
              unCheckedChildren="关闭"
            />
          </div>
          <div style={{ color: '#999', fontSize: 12, lineHeight: 1.7, marginBottom: 16 }}>
            关闭后测试网合约只停「新增」：自动/手动开单、外部持仓自动接管、测试网飞书通知、
            自动持仓诊断与 live→testnet 镜像全部停止；存量持仓的结算、保护单恢复、挂单巡检
            与事件流照常运行（避免交易所侧留下无主单），测试网爆亏也不再拉黑正式网币种。
            历史交易/统计保留只读查看，复盘、日报、账户概览等统计默认切正式网（显式选
            测试网 scope 仍可查看）。现货测试网独立开关，不受本开关影响。默认启用，与现有行为一致。
          </div>
          </ConfigSection>

          <ConfigSection title="自动成交开关" hint="AI 分析后自动开单，双网独立">
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '8px 12px',
              border: '1px solid #f0f0f0',
              borderRadius: 8,
              background: '#fff',
              marginBottom: 8,
            }}
          >
            <Space size={8}>
              <Tag color="orange" style={{ marginInlineEnd: 0 }}>测试网</Tag>
              <span>自动成交</span>
            </Space>
            <Switch
              checked={tnTrading}
              disabled={!tnEnabled}
              onChange={(v) => setTnTrading(v)}
              checkedChildren="开"
              unCheckedChildren="关"
            />
          </div>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '8px 12px',
              border: '1px solid #f0f0f0',
              borderRadius: 8,
              background: '#fff',
              marginBottom: 8,
            }}
          >
            <Space size={8}>
              <Tag color="volcano" style={{ marginInlineEnd: 0 }}>正式网</Tag>
              <span>自动成交</span>
            </Space>
            <Switch
              checked={liveTrading}
              onChange={(v) => setLiveTrading(v)}
              checkedChildren="开"
              unCheckedChildren="关"
            />
          </div>
          <div style={{ color: '#999', fontSize: 12, lineHeight: 1.7, marginBottom: 16 }}>
            两网开关独立判定、可同时开启：同一 AI 分析结论会在测试网与正式网各开一单
            （镜像跟单，各账号独立过风控闸门）。正式网默认关闭（实盘安全基线）。
            仅控制每小时 AI 分析完成后是否自动开新单；关闭后手动「开单」按钮、AI 分析
            （由策略配置「筛选后自动 AI 分析」独立控制）、结算巡检、复盘、日报均不受影响。
            测试网总开关关闭时，测试网自动成交开关置灰、不再生效（正式网不受影响）。
          </div>

          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message="正式网多账号共用同一次 AI 分析结论，各账号独立按钱包计算仓位并下单（扇出跟单）；单账号失败不影响其他账号。"
          />
          </ConfigSection>

          <ConfigSection title="飞书通知" hint="测试网合约单据推送开关">
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '8px 12px',
              border: '1px solid #f0f0f0',
              borderRadius: 8,
              background: '#fff',
              marginBottom: 8,
            }}
          >
            <Space size={8}>
              <Tag color="orange" style={{ marginInlineEnd: 0 }}>测试网</Tag>
              <span>飞书通知</span>
            </Space>
            <Switch
              checked={tnNotify}
              disabled={!tnEnabled}
              onChange={(v) => setTnNotify(v)}
              checkedChildren="开"
              unCheckedChildren="关"
            />
          </div>
          <div style={{ color: '#999', fontSize: 12, lineHeight: 1.7, marginBottom: 16 }}>
            关闭后不推送测试网合约单据相关卡片与文本：开仓成功卡、持仓时长预警卡、
            AI 持仓建议卡、止损收紧结果卡、滞留复诊卡、开单受阻（未上架）提示。
            扫描命中卡、满仓跳过卡、结构监控告警、每日日报、现货卡片与按钮回调回执
            不受此开关影响，仍会照常发送。
          </div>
          </ConfigSection>

          <ConfigSection title="凭据配置" hint="仅存服务器 .env，页面不提供编辑与回显">
          <div style={{ color: '#999', fontSize: 12, lineHeight: 1.7, marginBottom: 16 }}>
            账号凭据为敏感信息，只在服务器 .env 中配置：测试网 BINANCE_TESTNET_API_KEY/SECRET_KEY、
            正式网 1 号 BINANCE_API_KEY/SECRET_KEY、附加账号 {'BINANCE_LIVE_{N}_API_KEY/SECRET_KEY'}（+可选 _NAME），
            页面不提供编辑与回显。新增账号后需重启服务生效。
          </div>
          </ConfigSection>

          <ConfigSection title="危险操作" hint="操作不可恢复，请谨慎" danger>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '8px 12px',
              border: '1px solid #ffccc7',
              borderRadius: 8,
              background: '#fff',
              gap: 12,
              marginBottom: 16,
            }}
          >
            <span style={{ fontSize: 12, color: '#8c8c8c', lineHeight: 1.7 }}>
              删除测试网全部交易记录/操作历史/划转流水（实盘数据与复盘统计不受影响，
              交易所侧仓位不平掉，操作不可恢复）
            </span>
            <Button danger icon={<DeleteOutlined />} loading={purging} onClick={openPurge} style={{ flexShrink: 0 }}>
              清空测试网数据
            </Button>
          </div>
          </ConfigSection>
          </div>
        </Spin>

        {/* 清空测试网数据二次确认：预检数量 + 在跑单警告 */}
        <Modal
          title="清空测试网数据"
          open={!!purgeInfo}
          onOk={handlePurge}
          confirmLoading={purging}
          onCancel={() => setPurgeInfo(null)}
          okText="确认清空"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          width={440}
        >
          <div style={{ fontSize: 13, lineHeight: 2 }}>
            将删除测试网的：
            <div>
              交易记录 <b>{purgeInfo?.records ?? 0}</b> 条
              {(purgeInfo?.running ?? 0) > 0 && (
                <span style={{ color: '#faad14' }}>（其中在跑 {purgeInfo?.running} 笔）</span>
              )}
              、操作历史 <b>{purgeInfo?.events ?? 0}</b> 条、划转流水{' '}
              <b>{purgeInfo?.transfers ?? 0}</b> 条
            </div>
          </div>
          <div
            style={{
              marginTop: 8,
              padding: '8px 12px',
              borderRadius: 6,
              background: '#fffbe6',
              border: '1px solid #ffe58f',
              fontSize: 12,
              color: '#874d00',
            }}
          >
            <WarningOutlined style={{ marginRight: 4 }} />
            {(purgeInfo?.running ?? 0) > 0
              ? '交易所侧在跑仓位不会被平掉，删除后将无人管理；'
              : ''}
            实盘数据与复盘统计不受影响，此操作不可恢复。
          </div>
        </Modal>
      </Modal>
    </>
  )
}
