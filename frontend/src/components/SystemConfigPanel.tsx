import { useState, useEffect } from 'react'
import { Modal, Switch, Tag, message, Button, Spin, Alert, Space } from 'antd'
import { ApiOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'
import type { AccountBrief, SystemConfig } from '../types'

/** 系统配置弹窗：交易账号互斥开关组（docs/17 P0）+ 自动交易总开关。
 *  测试网与各正式网账号同时只开一个（选中正式网账号记入 current_account_key）；
 *  凭据只在服务器 .env 配置（安全不入库），页面仅展示账号清单与配置状态。 */
export default function SystemConfigPanel() {
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  const [envSel, setEnvSel] = useState<string>('testnet') // 当前选中的账号 key
  const [tradingEnabled, setTradingEnabled] = useState(false)
  const { aiConfig, updateConfig, fetchConfig } = useScanStore()

  useEffect(() => {
    if (!open) return
    setLoading(true)
    fetchConfig().finally(() => setLoading(false))
  }, [open, fetchConfig])

  useEffect(() => {
    if (open && aiConfig) {
      // 按当前配置还原选中账号：测试网模式 → 测试网账号 key（testnet-1），
      // 正式网 → current_account_key（回落 live-1）；账号清单为空时保留环境别名兜底
      const accs = aiConfig.accounts ?? []
      const key = (aiConfig.trading_testnet ?? true)
        ? (accs.find((a) => a.testnet)?.key ?? 'testnet')
        : (aiConfig.current_account_key || accs.find((a) => !a.testnet)?.key || 'live-1')
      setEnvSel(key)
      setTradingEnabled(aiConfig.trading_enabled ?? false)
    }
  }, [open, aiConfig])

  // 正式网安全联锁：切到正式网账号自动关闭自动交易开关（需二次保存确认开启）
  const pickAccount = (key: string) => {
    if (envSel === key) return
    setEnvSel(key)
    if (!key.startsWith('testnet')) {
      setTradingEnabled(false)
      message.warning('已切换到正式网账号：自动交易默认关闭，确认无误后需手动开启')
    }
  }

  const handleSave = async () => {
    const testnet = envSel === 'testnet' || envSel.startsWith('testnet-')
    try {
      setSaving(true)
      const data: Partial<SystemConfig> = {
        trading_testnet: testnet,
        trading_enabled: tradingEnabled,
      }
      if (!testnet) data.current_account_key = envSel // 仅选中正式网账号时下发
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

  const accountRow = (a: AccountBrief) => (
    <div
      key={a.key}
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        padding: '8px 12px',
        border: '1px solid #f0f0f0',
        borderRadius: 8,
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
      <Switch checked={envSel === a.key} onChange={() => pickAccount(a.key)} />
    </div>
  )

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
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message="开关互斥：同一时间只有一个账号执行自动交易。切换账号后，新开仓位记入该账号；存量在跑单按开单时的账号继续结算，互不影响。"
          />

          {accounts.length === 0 ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message="未在服务器 .env 配置任何币安 API key，账号清单为空"
            />
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 16 }}>
              {accounts.map(accountRow)}
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
            切到正式网账号会自动关闭此开关，确认无误后需手动开启。
          </div>

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
