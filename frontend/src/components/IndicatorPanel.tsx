/**
 * 指标选择面板（2026-10-16）
 * 插件 registry 全量驱动（1290 个：标准 + 社区 + K线形态）：
 * - 顶部搜索框 + 分类过滤；搜索或选分类时出扁平结果，否则分「常用指标」「社区明星」两区
 * - 每个条目勾选启用；启用后按插件 inputConfig 自动生成参数控件
 *   （数值/开关/下拉/颜色；group 分组标题、active 依赖灰显）
 * - 参数改动实时生效（setIndicatorInput），重载图标重置为插件默认
 * 纯受控组件：状态与动作由父级（ChartCore → scanStore）注入
 */
import { Fragment, memo, useMemo, useState } from 'react'
import { Checkbox, Empty, Input, InputNumber, Select, Tooltip, Typography } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { isInputActive } from 'oakscriptjs'
import {
  ALL_INDICATORS,
  CATEGORY_FILTERS,
  commonIndicators,
  filterIndicators,
  SOURCE_OPTIONS,
  starIndicators,
  type ChartIndicatorDef,
  type InputConfig,
} from '../constants/indicators'
import type { IndicatorSettings } from '../stores/scanStore'

const { Text } = Typography

// 参数当前值（未设过则回退插件默认）
const valueOf = (inputs: Record<string, unknown>, cfg: InputConfig) =>
  inputs[cfg.id] !== undefined ? inputs[cfg.id] : cfg.defval

// 单个参数控件：按 inputConfig.type 分派
function InputControl({
  cfg,
  value,
  active,
  onChange,
}: {
  cfg: InputConfig
  value: unknown
  active: boolean
  onChange: (v: unknown) => void
}) {
  const disabled = !active
  switch (cfg.type) {
    case 'bool':
      return (
        <Checkbox
          checked={!!value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
        />
      )
    case 'int':
    case 'float':
    case 'time':
      return (
        <InputNumber
          size="small"
          disabled={disabled}
          value={typeof value === 'number' ? value : undefined}
          min={cfg.min}
          max={cfg.max}
          step={cfg.step ?? (cfg.type === 'int' ? 1 : 0.1)}
          style={{ width: 72 }}
          onChange={(v) => onChange(v)}
        />
      )
    case 'color':
      return (
        <input
          type="color"
          disabled={disabled}
          value={typeof value === 'string' && value ? value : '#000000'}
          onChange={(e) => onChange(e.target.value)}
          style={{
            width: 28,
            height: 22,
            padding: 0,
            border: '1px solid #2a2e39',
            borderRadius: 4,
            background: '#131722',
            cursor: disabled ? 'not-allowed' : 'pointer',
          }}
        />
      )
    case 'source':
      return (
        <Select
          size="small"
          disabled={disabled}
          value={typeof value === 'string' ? value : undefined}
          options={SOURCE_OPTIONS.map((v) => ({ label: v, value: v }))}
          style={{ width: 84 }}
          onChange={(v) => onChange(v)}
        />
      )
    case 'string':
    case 'timeframe':
    case 'session':
      if (cfg.options?.length) {
        return (
          <Select
            size="small"
            disabled={disabled}
            value={typeof value === 'string' ? value : undefined}
            options={cfg.options.map((v) => ({ label: v, value: v }))}
            style={{ minWidth: 96, maxWidth: 150 }}
            onChange={(v) => onChange(v)}
          />
        )
      }
      return (
        <Input
          size="small"
          disabled={disabled}
          value={typeof value === 'string' ? value : ''}
          style={{ width: 110 }}
          onChange={(e) => onChange(e.target.value)}
        />
      )
    default:
      return null
  }
}

// 参数区：按 group 分段（同 group 的输入归到一个小标题下）
function InputsEditor({
  def,
  inputs,
  onInput,
  onReset,
}: {
  def: ChartIndicatorDef
  inputs: Record<string, unknown>
  onInput: (inputId: string, value: unknown) => void
  onReset: () => void
}) {
  return (
    <div style={{ marginLeft: 22, marginTop: 2, display: 'flex', flexDirection: 'column', gap: 2 }}>
      {def.inputConfig.map((cfg, i) => {
        const prev = def.inputConfig[i - 1]
        const groupStart = cfg.group && cfg.group !== prev?.group
        const active = isInputActive(cfg, inputs, def.inputConfig)
        return (
          <Fragment key={cfg.id}>
            {groupStart && (
              <Text style={{ color: '#6f7889', fontSize: 10, marginTop: 3 }}>{cfg.group}</Text>
            )}
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, opacity: active ? 1 : 0.45 }}>
              <Text style={{ color: '#9aa3b2', fontSize: 11, minWidth: 78, flexShrink: 0 }}>
                {cfg.title || cfg.id}
              </Text>
              <InputControl
                cfg={cfg}
                value={valueOf(inputs, cfg)}
                active={active}
                onChange={(v) => onInput(cfg.id, v)}
              />
              {i === 0 && (
                <Tooltip title="重置参数">
                  <ReloadOutlined
                    onClick={onReset}
                    style={{ color: '#8b93a7', fontSize: 11, cursor: 'pointer' }}
                  />
                </Tooltip>
              )}
            </div>
          </Fragment>
        )
      })}
    </div>
  )
}

const IndicatorRow = memo(function IndicatorRow({
  def,
  setting,
  onToggle,
  onInput,
  onReset,
}: {
  def: ChartIndicatorDef
  setting?: { enabled: boolean; inputs: Record<string, unknown> }
  onToggle: () => void
  onInput: (inputId: string, value: unknown) => void
  onReset: () => void
}) {
  const enabled = !!setting?.enabled
  const inputs = setting?.inputs ?? def.defaultInputs
  const hasParams = def.inputConfig.length > 0
  return (
    <div style={{ marginTop: 3 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Checkbox checked={enabled} onChange={onToggle}>
          <span style={{ color: '#d1d4dc', fontSize: 12 }}>{def.name}</span>
        </Checkbox>
      </div>
      {enabled && hasParams && (
        <InputsEditor def={def} inputs={inputs} onInput={onInput} onReset={onReset} />
      )}
    </div>
  )
})

function Section({
  title,
  defs,
  settings,
  onToggle,
  onInput,
  onReset,
  last,
}: {
  title: string
  defs: ChartIndicatorDef[]
  settings: IndicatorSettings
  onToggle: (id: string) => void
  onInput: (id: string, inputId: string, value: unknown) => void
  onReset: (id: string) => void
  last?: boolean
}) {
  if (!defs.length) return null
  return (
    <div style={last ? undefined : { marginBottom: 8 }}>
      <Text style={{ color: '#9aa3b2', fontSize: 11, fontWeight: 600 }}>{title}</Text>
      {defs.map((def) => (
        <IndicatorRow
          key={def.id}
          def={def}
          setting={settings[def.id]}
          onToggle={() => onToggle(def.id)}
          onInput={(inputId, v) => onInput(def.id, inputId, v)}
          onReset={() => onReset(def.id)}
        />
      ))}
    </div>
  )
}

interface Props {
  settings: IndicatorSettings
  onToggle: (id: string) => void
  onInput: (id: string, inputId: string, value: unknown) => void
  onReset: (id: string) => void
}

export default function IndicatorPanel({ settings, onToggle, onInput, onReset }: Props) {
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('全部')
  const searched = query.trim().length > 0 || category !== '全部'
  const results = useMemo(
    () => (searched ? filterIndicators(query, category) : []),
    [searched, query, category],
  )
  const common = useMemo(commonIndicators, [])
  const stars = useMemo(starIndicators, [])
  const enabledCount = Object.values(settings).filter((s) => s.enabled).length

  return (
    <div
      style={{
        background: '#1b1f2a',
        border: '1px solid #2a2e39',
        borderRadius: 6,
        padding: '8px 10px',
        width: 400,
        maxHeight: 460,
        display: 'flex',
        flexDirection: 'column',
      }}>
      {/* 头部：搜索 + 分类过滤 + 已启用计数（不随内容滚动） */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, paddingBottom: 6 }}>
        <div style={{ display: 'flex', gap: 6 }}>
          <Input
            size="small"
            allowClear
            placeholder={`搜索指标（共 ${ALL_INDICATORS.length} 个）`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ flex: 1 }}
          />
          <Select
            size="small"
            value={category}
            options={CATEGORY_FILTERS.map((c) => ({ label: c, value: c }))}
            style={{ width: 132 }}
            onChange={setCategory}
          />
        </div>
        <Text style={{ color: '#6f7889', fontSize: 10 }}>
          已启用 {enabledCount} 个 · 输入关键词或选分类浏览全部指标
        </Text>
      </div>
      {/* 内容区（滚动） */}
      <div style={{ overflowY: 'auto', flex: 1, paddingRight: 2 }}>
        {searched ? (
          results.length ? (
            results.map((def) => (
              <IndicatorRow
                key={def.id}
                def={def}
                setting={settings[def.id]}
                onToggle={() => onToggle(def.id)}
                onInput={(inputId, v) => onInput(def.id, inputId, v)}
                onReset={() => onReset(def.id)}
              />
            ))
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无匹配指标" />
          )
        ) : (
          <>
            <Section
              title="常用指标"
              defs={common}
              settings={settings}
              onToggle={onToggle}
              onInput={onInput}
              onReset={onReset}
            />
            <Section
              title="社区明星指标"
              defs={stars}
              settings={settings}
              onToggle={onToggle}
              onInput={onInput}
              onReset={onReset}
              last
            />
          </>
        )}
      </div>
    </div>
  )
}
