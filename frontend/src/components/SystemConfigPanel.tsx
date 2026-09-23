import { useState, useEffect } from 'react'
import { Modal, Switch, Tag, message, Button, Spin, Alert, Segmented, Space } from 'antd'
import { ApiOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'
import type { AccountBrief, SystemConfig } from '../types'

/** 系统配置弹窗（docs/17 P1 扇出）：交易环境 + 启用账号 + 自动交易总开关。
 *  - 环境：测试网（单账号 testnet-1）/ 正式网（互斥二选一）
 *  - 正式网下多个 live 账号可同时启用自动交易（P1 扇出：共享一次 AI 分析，
 *    各账号独立过闸门下单）；测试网恒为 testnet-1 单账号
 *  - 凭据只在服务器 .env 配置（安全不入库），页面仅展示账号清单与配置状态 */
export default function SystemConfigPanel() {
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  const [envTestnet, setEnvTestnet] = useState(true)   // 交易环境（测试网/正式网互斥）
  const [liveKeys, setLiveKeys] = useState<string[]>([]) // 正式网启用自动交易的账号集合
  const [tradingEnabled, setTradingEnabled] = useState(false)
  const { aiConfig, updateConfig, fetchConfig } = useScanStore()

  useEffect(() => {
    if (!open) return
    setLoading(true)
    fetchConfig().finally(() => setLoading(false))
  }, [open, fetchConfig])

  useEffect(() => {
    if (open && aiConfig) {
      const accs = aiConfig.accounts ?? []
      const testnet = aiConfig.trading_testnet ?? true
      setEnvTestnet(testnet)
      // 启用集合：配置值；空回落主账号 / 第一个 live 账号（与后端回落语义一致）
      const keys = aiConfig.live_trading_keys?.length
        ? aiConfig.live_trading_keys
        : [aiConfig.current_account_key || accs.find((a) => !a.testnet)?.key || 'live-1']
      setLiveKeys(keys)
      setTradingEnabled(aiConfig.trading_enabled ?? false)
    }
  }, [open, aiConfig])

  // 正式网安全联锁：切到正式网自动关闭自动交易开关（需二次保存确认开启）；
  // 正式网内增减启用账号不联动总闸（总闸是显式安全开关）
  const handleEnvChange = (v: boolean) => {
    setEnvTestnet(v)
    if (!v) {
      setTradingEnabled(false)
      message.warning('已切换到正式网：自动交易默认关闭，确认无误后需手动开启')
    }
  }

  const toggleLive = (key: string, checked: boolean) => {
    setLiveKeys((prev) => (checked ? [...prev, key] : prev.filter((k) => k !== key)))
  }

  const handleSave = async () => {
    if (!envTestnet && liveKeys.length === 0) {
      message.error('正式网模式需至少启用一个账号执行自动交易')
      return
    }
    try {
      setSaving(true)
      const data: Partial<SystemConfig> = {
        trading_testnet: envTestnet,
        trading_enabled: tradingEnabled,
      }
      if (!envTestnet) {
        data.live_trading_keys = liveKeys
        data.current_account_key = liveKeys[0] // 主账号 = 启用集合第一个
      }
      await updateConfig(data)
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
          background: actionable && enabled ? '#f6ffed' : undefined,
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
          {envTestnet && a.testnet && <Tag color="processing" style={{ marginInlineEnd: 0 }}>当前环境</Tag>}
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
          <div style={{ marginBottom: 8 }}>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>交易环境</div>
            <Segmented
              value={envTestnet ? 'testnet' : 'live'}
              onChange={(v) => handleEnvChange(v === 'testnet')}
              options={[
                { label: '测试网', value: 'testnet' },
                { label: '正式网', value: 'live' },
              ]}
            />
          </div>

          <div style={{ fontSize: 13, fontWeight: 600, margin: '16px 0 8px' }}>
            {envTestnet ? '测试网账号' : '启用自动交易的账号（可多选，扇出跟单）'}
          </div>
          {accounts.length === 0 ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message="未在服务器 .env 配置任何币安 API key，账号清单为空"
            />
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 16 }}>
              {envTestnet
                ? testnetAccounts.map((a) => accountRow(a, false))
                : liveAccounts.map((a) => accountRow(a, true))}
            </div>
          )}

          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '8px 12px',
              border: '1px solid #f0f0f0',
              borderRadius: 8,
              marginBottom: 16,
            }}
          >
            <span style={{ fontWeight: 600 }}>自动交易总开关</span>
            <Switch
              checked={tradingEnabled}
              onChange={(v) => setTradingEnabled(v)}
              checkedChildren="开"
              unCheckedChildren="关"
            />
          </div>
          <div style={{ color: '#999', fontSize: 12, lineHeight: 1.7, marginBottom: 16 }}>
            关闭后定时扫描的 AI 分析与开新单停止（省 AI 配额）；结算巡检、复盘、日报照常执行。
            切到正式网会自动关闭此开关，确认无误后需手动开启。
          </div>

          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={
              envTestnet
                ? '切换环境后，新开仓位记入对应环境；存量在跑单按开单时的账号继续结算，互不影响。'
                : '正式网多账号共用同一次 AI 分析结论，各账号独立按钱包计算仓位并下单（扇出跟单）；单账号失败不影响其他账号。'
            }
          />

          <div style={{ color: '#999', fontSize: 12, lineHeight: 1.7 }}>
            账号凭据为敏感信息，只在服务器 .env 中配置：测试网 BINANCE_TESTNET_API_KEY/SECRET_KEY、
            正式网 1 号 BINANCE_API_KEY/SECRET_KEY、附加账号 {'BINANCE_LIVE_{N}_API_KEY/SECRET_KEY'}（+可选 _NAME），
            页面不提供编辑与回显。新增账号后需重启服务生效。
          </div>
        </Spin>
      </Modal>
    </>
  )
}
