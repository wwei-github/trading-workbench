import { useState, useEffect } from 'react'
import { Modal, Form, Switch, Radio, Tag, message, Button, Spin, Alert, Descriptions } from 'antd'
import { ApiOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'
import type { SystemConfig } from '../types'

/** 系统配置弹窗（2026-09-20 新增）：交易环境切换 + 自动交易总开关 + 双环境 key 状态。
 *  凭据只在服务器 .env 配置（安全不入库），此处仅展示已配置/未配置状态。 */
export default function SystemConfigPanel() {
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  const { aiConfig, updateConfig, fetchConfig } = useScanStore()

  useEffect(() => {
    if (!open) return
    setLoading(true)
    fetchConfig().finally(() => setLoading(false))
  }, [open, fetchConfig])

  useEffect(() => {
    if (open && aiConfig) {
      form.setFieldsValue({
        system_enabled: aiConfig.system_enabled ?? true,
        trading_testnet: aiConfig.trading_testnet ?? true,
        trading_enabled: aiConfig.trading_enabled ?? false,
      })
    }
  }, [open, aiConfig, form])

  // 正式网安全联锁：切到正式网自动关闭自动交易开关（需二次保存确认开启）
  const handleEnvChange = (v: boolean) => {
    if (!v) {
      form.setFieldValue('trading_enabled', false)
      message.warning('已切换到正式网：自动交易默认关闭，确认无误后需手动开启')
    }
  }

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      setSaving(true)
      const data: Partial<SystemConfig> = {
        system_enabled: values.system_enabled,
        trading_testnet: values.trading_testnet,
        trading_enabled: values.trading_enabled,
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

  const keyStatus = (ok?: boolean) =>
    ok ? <Tag color="green">已配置</Tag> : <Tag color="red">未配置</Tag>

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
        <Form form={form} layout="vertical">
          <Form.Item
            label="系统总开关"
            name="system_enabled"
            valuePropName="checked"
            tooltip="关闭后所有定时任务（定时扫描/结算巡检/复盘归因/补丁/日报）都不执行；手动触发不受影响。注意：结算巡检也停，在跑单的止损补挂随之暂停"
          >
            <Switch checkedChildren="开" unCheckedChildren="关" />
          </Form.Item>

          <Form.Item
            label="交易环境"
            name="trading_testnet"
            rules={[{ required: true }]}
            tooltip="当前自动交易的执行环境（运行时可切，下一轮任务生效）。存量在跑单按开单时的环境继续结算，切环境互不影响。默认测试网；切到正式网会自动关闭自动交易"
          >
            <Radio.Group
              options={[
                { value: true, label: '测试网' },
                { value: false, label: '正式网' },
              ]}
              optionType="button"
              buttonStyle="solid"
              onChange={(e) => handleEnvChange(e.target.value)}
            />
          </Form.Item>

          <Form.Item
            label="自动交易总开关"
            name="trading_enabled"
            valuePropName="checked"
            tooltip="关闭后停止开新单（定时扫描的 AI 分析照常），存量在跑单的结算巡检、止损补挂照常执行，不会裸仓"
          >
            <Switch checkedChildren="开" unCheckedChildren="关" />
          </Form.Item>

          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message="切换环境后，新开仓位记入对应环境；交易记录/资金分析页的默认视图将跟随当前环境"
          />

          <Descriptions
            title="币安 API 凭据状态（服务器 .env 配置）"
            column={1}
            size="small"
            style={{ marginBottom: 8 }}
          >
            <Descriptions.Item label="正式网 key">{keyStatus(aiConfig?.binance_key_live)}</Descriptions.Item>
            <Descriptions.Item label="测试网 key">
              {keyStatus(aiConfig?.binance_key_testnet)}
              {aiConfig?.binance_key_testnet && !aiConfig?.binance_key_live && (
                <span style={{ color: '#999', marginLeft: 8 }}>（回落正式网 key）</span>
              )}
            </Descriptions.Item>
          </Descriptions>
          <div style={{ color: '#999', fontSize: 12, lineHeight: 1.7 }}>
            凭据为敏感信息，只在服务器 .env 中配置（BINANCE_API_KEY / BINANCE_SECRET_KEY、
            BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_SECRET_KEY），页面不提供编辑与回显。
          </div>
        </Form>
        </Spin>
      </Modal>
    </>
  )
}
