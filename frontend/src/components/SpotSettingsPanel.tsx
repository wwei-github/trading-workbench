/**
 * 现货·策略配置（docs/18 §5）
 * - 环境与账号区块：现货测试网/正式网独立切换（与合约零关联，2026-09-23 拍板）；
 *   正式网下各 live 账号独立开关（可多选，P1 扇出执行；P0 仅决定持仓聚合范围）
 * - 屯币参数区块：单份金额 / 投入上限 / 定投 / 保留垫 / 手续费（只影响评分与展示）
 * 账号注册表共用 .env（BINANCE_LIVE_{N}_*），凭据不落库，页面只读展示
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Form,
  InputNumber,
  Segmented,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
  message,
} from 'antd'
import { spotApi } from '../api/spot'
import type { SpotAccountBrief, SpotConfig, SpotSettings } from '../types/spot'

// 账号行（同合约 SystemConfigPanel 风格：名称/key/配置态 + 开关）
function AccountRow({
  account,
  actionable,
  enabled,
  onToggle,
}: {
  account: SpotAccountBrief
  actionable: boolean
  enabled: boolean
  onToggle?: (key: string, checked: boolean) => void
}) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        padding: '6px 12px',
        border: '1px solid #f0f0f0',
        borderRadius: 8,
        background: actionable && enabled ? 'rgba(82, 196, 26, 0.06)' : undefined,
      }}
    >
      <Space>
        <Tag color={account.testnet ? 'blue' : 'gold'} style={{ marginInlineEnd: 0 }}>
          {account.testnet ? '测试网' : '正式网'}
        </Tag>
        <span style={{ fontWeight: 600 }}>{account.name}</span>
        <span style={{ color: '#999', fontSize: 12 }}>{account.key}</span>
        {account.configured ? (
          <Tag color="green" style={{ marginInlineEnd: 0 }}>已配置</Tag>
        ) : (
          <Tag color="red" style={{ marginInlineEnd: 0 }}>未配置</Tag>
        )}
      </Space>
      {actionable && onToggle && (
        <Switch
          checked={enabled}
          onChange={(v) => onToggle(account.key, v)}
          checkedChildren="交易"
          unCheckedChildren="停用"
        />
      )}
    </div>
  )
}

function EnvAccountCard() {
  const [cfg, setCfg] = useState<SpotConfig | null>(null)
  const [testnet, setTestnet] = useState(true)
  const [liveKeys, setLiveKeys] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const c = await spotApi.getConfig()
      setCfg(c)
      setTestnet(c.spot_trading_testnet)
      setLiveKeys(c.spot_enabled_keys)
    } catch {
      // axios 拦截器统一提示
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const save = async (nextTestnet: boolean, nextKeys: string[]) => {
    setSaving(true)
    try {
      const res = await spotApi.updateConfig({
        spot_trading_testnet: nextTestnet,
        spot_enabled_keys: nextKeys,
      })
      setCfg(res)
      setTestnet(res.spot_trading_testnet)
      setLiveKeys(res.spot_enabled_keys)
      if (res.rejected.length > 0) {
        message.warning(`以下账号不在注册表或非正式网账号，已忽略：${res.rejected.join('、')}`)
      } else {
        message.success('现货环境已保存')
      }
    } catch {
      // axios 拦截器统一提示
    } finally {
      setSaving(false)
    }
  }

  const handleEnvChange = (v: boolean) => {
    setTestnet(v)
    if (!v) {
      message.info('现货已切到正式网：P0 仅评分与持仓展示，无自动下单面（P1 接入时将加确认交互）')
    }
    save(v, liveKeys)
  }

  const toggleLive = (key: string, checked: boolean) => {
    const next = checked ? [...liveKeys, key] : liveKeys.filter((k) => k !== key)
    setLiveKeys(next)
    if (next.length === 0) {
      message.info('未启用任何账号：现货正式网将回落主账号（live-1）')
    }
    save(testnet, next)
  }

  const accounts = cfg?.accounts ?? []
  const testnetAccounts = accounts.filter((a) => a.testnet)
  const liveAccounts = accounts.filter((a) => !a.testnet)

  return (
    <Card size="small" title="现货环境与账号" style={{ marginBottom: 12 }}>
      <Spin spinning={loading}>
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <div>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
              交易环境（与合约相互独立）
            </div>
            <Segmented
              value={testnet ? 'testnet' : 'live'}
              onChange={(v) => handleEnvChange(v === 'testnet')}
              options={[
                { label: '测试网', value: 'testnet' },
                { label: '正式网', value: 'live' },
              ]}
            />
          </div>

          <div>
            <div style={{ fontSize: 13, fontWeight: 600, margin: '8px 0' }}>
              {testnet ? '测试网账号' : '启用屯币的正式网账号（可多选）'}
            </div>
            {accounts.length === 0 ? (
              <Alert
                type="warning"
                showIcon
                message="未在服务器 .env 配置任何币安 API key，账号清单为空"
              />
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {testnet
                  ? testnetAccounts.map((a) => (
                      <AccountRow key={a.key} account={a} actionable={false} enabled={false} />
                    ))
                  : liveAccounts.map((a) => (
                      <AccountRow
                        key={a.key}
                        account={a}
                        actionable
                        enabled={liveKeys.includes(a.key)}
                        onToggle={toggleLive}
                      />
                    ))}
              </div>
            )}
          </div>

          <Typography.Text type="secondary" style={{ fontSize: 12, lineHeight: 1.7 }}>
            账号注册表与合约共用服务器 .env（BINANCE_LIVE_{'{N}'}_*），凭据不落库、页面不回显；
            现货环境切换不影响合约环境，反之亦然。总览持仓为启用账号的聚合。
          </Typography.Text>
        </Space>
      </Spin>
    </Card>
  )
}

export default function SpotSettingsPanel() {
  const [form] = Form.useForm<SpotSettings>()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      form.setFieldsValue(await spotApi.getSettings())
    } catch {
      // 错误信息由 axios 拦截器统一提示
    } finally {
      setLoading(false)
    }
  }, [form])

  useEffect(() => {
    load()
  }, [load])

  const save = async () => {
    try {
      const values = await form.validateFields()
      setSaving(true)
      const res = await spotApi.updateSettings(values)
      if (res.rejected.length > 0) {
        message.warning(`以下字段超出范围被忽略：${res.rejected.join('、')}`)
      } else {
        message.success('屯币参数已保存')
      }
      form.setFieldsValue(res.settings)
    } catch {
      // 校验失败/请求失败
    } finally {
      setSaving(false)
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <EnvAccountCard />

      <Card size="small" title="屯币参数">
        {loading ? (
          <div style={{ textAlign: 'center', padding: 60 }}>
            <Spin />
          </div>
        ) : (
          <Form<SpotSettings>
            form={form}
            layout="inline"
            style={{ rowGap: 12 }}
            initialValues={{
              per_order_usdt: 100,
              max_total_usdt: 3000,
              dca_enabled: true,
              dca_amount: 50,
              usdt_reserve_pct: 0.2,
              fee_rate: 0.001,
            }}
          >
            <Space wrap size={24}>
              <Form.Item
                name="per_order_usdt"
                label="单份金额 (U)"
                rules={[{ required: true, message: '10 ~ 1000' }]}
              >
                <InputNumber min={10} max={1000} step={10} style={{ width: 120 }} />
              </Form.Item>
              <Form.Item
                name="max_total_usdt"
                label="单币投入上限 (U)"
                rules={[{ required: true, message: '100 ~ 100000' }]}
              >
                <InputNumber min={100} max={100000} step={100} style={{ width: 140 }} />
              </Form.Item>
              <Form.Item name="dca_enabled" label="周度定投" valuePropName="checked">
                <Switch />
              </Form.Item>
              <Form.Item name="dca_amount" label="定投金额 (U/周)">
                <InputNumber min={0} max={1000} step={10} style={{ width: 120 }} />
              </Form.Item>
              <Form.Item name="usdt_reserve_pct" label="USDT 保留垫">
                <InputNumber min={0} max={0.9} step={0.05} style={{ width: 100 }} />
              </Form.Item>
              <Form.Item name="fee_rate" label="手续费率">
                <InputNumber min={0} max={0.01} step={0.0005} style={{ width: 110 }} />
              </Form.Item>
              <Form.Item>
                <Button type="primary" onClick={save} loading={saving}>
                  保存
                </Button>
              </Form.Item>
            </Space>
          </Form>
        )}
        <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 12 }}>
          买入份数 = min(规则档位, 评分映射)：日RSI 超卖档（&lt;30/25/20 → 1/2/3 份）与 30 日回撤档
          （&gt;15%/25%/35% → 1/2/3 份）取高，布林下轨 +1，单日上限 3 份；卖出只动波段仓（≤30%），
          底仓永不卖（docs/18 §3.3）。定投执行在 P1 开放。
        </Typography.Text>
      </Card>
    </Space>
  )
}
