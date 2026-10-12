import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Card, Modal, Spin, Tooltip, Typography } from 'antd'
import { CloseOutlined, FullscreenOutlined, ZoomInOutlined, ZoomOutOutlined } from '@ant-design/icons'
import { marked } from 'marked'
// 权威源在仓库根 docs/CODE_WIKI.md；此处为经 scripts/sync-wiki.mjs 同步的前端副本
// （docker 构建上下文不含 docs/，故必须随源码提交一份）
import wikiRaw from '../assets/CODE_WIKI.md?raw'

const { Text } = Typography

// ── 标题锚点：GitHub 风格 slug，使文档内目录（#1-项目概述 等）可跳转 ──
function slugify(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, '') // 去标点（.、— 等），保留中英文/数字/空格/连字符
    .replace(/\s/g, '-')
}

// markdown → HTML，并为标题补 id（marked 新版已不再生成 header id）
function renderMarkdown(source: string, usedIds: Set<string>): string {
  const html = marked.parse(source) as string
  const doc = new DOMParser().parseFromString(`<div>${html}</div>`, 'text/html')
  const root = doc.body.firstElementChild
  if (!root) return html
  root.querySelectorAll('h1, h2, h3, h4, h5, h6').forEach((h) => {
    const base = slugify(h.textContent ?? '') || 'section'
    let id = base
    let n = 1
    while (usedIds.has(id)) id = `${base}-${n++}`
    usedIds.add(id)
    h.id = id
  })
  return root.innerHTML
}

// ── mermaid：按需动态加载（体积大，不进主包），渲染串行化避免内部状态竞争 ──
type MermaidInstance = (typeof import('mermaid'))['default']

let mermaidLoader: Promise<MermaidInstance> | null = null

function loadMermaid(): Promise<MermaidInstance> {
  if (!mermaidLoader) {
    mermaidLoader = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({ startOnLoad: false, theme: 'neutral' })
      return mermaid
    })
  }
  return mermaidLoader
}

let mermaidQueue: Promise<void> = Promise.resolve()
function enqueueMermaidRender(task: () => Promise<void>): Promise<void> {
  const run = mermaidQueue.then(task, task)
  mermaidQueue = run.catch(() => undefined)
  return run
}

let mermaidSeq = 0

// ── 全屏缩放查看：克隆已渲染的 SVG（脱离页面的 max-width 限制），按 viewBox 取原始尺寸 ──
interface ZoomImage {
  html: string
  width: number
  height: number
}

function prepareSvg(svg: SVGSVGElement): ZoomImage {
  const vb = svg.viewBox?.baseVal
  const rect = svg.getBoundingClientRect()
  const width = vb && vb.width > 0 ? vb.width : Math.max(1, rect.width)
  const height = vb && vb.height > 0 ? vb.height : Math.max(1, rect.height)
  const clone = svg.cloneNode(true) as SVGSVGElement
  clone.removeAttribute('style') // mermaid 会写 max-width/width:100%，全屏视图要按原始尺寸渲染
  clone.setAttribute('width', String(width))
  clone.setAttribute('height', String(height))
  return { html: clone.outerHTML, width, height }
}

const ZOOM_MIN = 0.1
const ZOOM_MAX = 20
const clampScale = (s: number) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, s))

function ZoomModal({ image, onClose }: { image: ZoomImage; onClose: () => void }) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const [view, setView] = useState({ scale: 1, x: 0, y: 0 })
  const [dragging, setDragging] = useState(false)
  const dragRef = useRef<{ px: number; py: number; bx: number; by: number } | null>(null)

  // 适屏：铺满整页可用空间（初始即“放大到整个页面”）
  const fit = useCallback(() => {
    const vw = window.innerWidth
    const vh = window.innerHeight
    const scale = clampScale(Math.min(vw / image.width, vh / image.height) * 0.94)
    setView({ scale, x: (vw - image.width * scale) / 2, y: (vh - image.height * scale) / 2 })
  }, [image])

  useEffect(() => {
    fit()
  }, [fit])

  // 以屏幕点 (clientX, clientY) 为锚点缩放，光标下的内容保持不动
  const zoomAt = useCallback((clientX: number, clientY: number, factor: number) => {
    const rect = viewportRef.current?.getBoundingClientRect()
    const ox = clientX - (rect?.left ?? 0)
    const oy = clientY - (rect?.top ?? 0)
    setView((v) => {
      const scale = clampScale(v.scale * factor)
      const k = scale / v.scale
      return { scale, x: ox - k * (ox - v.x), y: oy - k * (oy - v.y) }
    })
  }, [])

  // React 合成 wheel 事件是 passive 的，无法 preventDefault，需手动以 non-passive 绑定
  useEffect(() => {
    const el = viewportRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.0015))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoomAt])

  const centerZoom = (factor: number) => zoomAt(window.innerWidth / 2, window.innerHeight / 2, factor)

  const actualSize = () => {
    const vw = window.innerWidth
    const vh = window.innerHeight
    setView((v) => {
      const k = 1 / v.scale
      return { scale: 1, x: vw / 2 - k * (vw / 2 - v.x), y: vh / 2 - k * (vh / 2 - v.y) }
    })
  }

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    dragRef.current = { px: e.clientX, py: e.clientY, bx: view.x, by: view.y }
    try {
      e.currentTarget.setPointerCapture(e.pointerId)
    } catch {
      /* 指针已释放等场景 setPointerCapture 会抛错，忽略（拖拽期间仍可经由冒泡的 move 事件更新） */
    }
    setDragging(true)
  }
  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current
    if (!d) return
    setView((v) => ({ ...v, x: d.bx + e.clientX - d.px, y: d.by + e.clientY - d.py }))
  }
  const handlePointerUp = () => {
    dragRef.current = null
    setDragging(false)
  }

  return (
    <Modal
      open
      onCancel={onClose}
      footer={null}
      closable={false}
      width="100vw"
      style={{ top: 0, maxWidth: '100vw', margin: 0, paddingBottom: 0 }}
      styles={{
        content: { height: '100vh', padding: 0, borderRadius: 0 },
        body: { height: '100%', padding: 0, overflow: 'hidden' },
      }}
    >
      <div
        ref={viewportRef}
        className={`wiki-zoom-viewport${dragging ? ' dragging' : ''}`}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
      >
        <div
          className="wiki-zoom-stage"
          style={{
            width: image.width,
            height: image.height,
            transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
          }}
          dangerouslySetInnerHTML={{ __html: image.html }}
        />
        {/* 工具条/关闭按钮：阻止 pointerdown 冒泡，避免误触拖拽 */}
        <div className="wiki-zoom-toolbar" onPointerDown={(e) => e.stopPropagation()}>
          <Tooltip title="缩小">
            <Button type="text" size="small" icon={<ZoomOutOutlined />} onClick={() => centerZoom(1 / 1.25)} />
          </Tooltip>
          <span className="wiki-zoom-percent">{Math.round(view.scale * 100)}%</span>
          <Tooltip title="放大">
            <Button type="text" size="small" icon={<ZoomInOutlined />} onClick={() => centerZoom(1.25)} />
          </Tooltip>
          <Button type="text" size="small" onClick={fit}>适应屏幕</Button>
          <Button type="text" size="small" onClick={actualSize}>1:1</Button>
        </div>
        <Tooltip title="关闭（Esc）">
          <Button
            className="wiki-zoom-close"
            type="text"
            icon={<CloseOutlined />}
            onClick={onClose}
            onPointerDown={(e) => e.stopPropagation()}
          />
        </Tooltip>
      </div>
    </Modal>
  )
}

function MermaidBlock({ code }: { code: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const [error, setError] = useState('')
  const [zoom, setZoom] = useState<ZoomImage | null>(null)

  useEffect(() => {
    let alive = true
    const id = `wiki-mermaid-${++mermaidSeq}`
    enqueueMermaidRender(async () => {
      const mermaid = await loadMermaid()
      const { svg } = await mermaid.render(id, code)
      if (alive && ref.current) ref.current.innerHTML = svg
    }).catch((e: unknown) => {
      if (alive) setError(e instanceof Error ? e.message : String(e))
    })
    return () => {
      alive = false
    }
  }, [code])

  const openZoom = () => {
    const svg = ref.current?.querySelector('svg')
    if (svg) setZoom(prepareSvg(svg))
  }

  if (error) {
    return (
      <div className="code-wiki-mermaid">
        <Text type="danger" style={{ fontSize: 12 }}>mermaid 渲染失败：{error}</Text>
        <pre>{code}</pre>
      </div>
    )
  }
  return (
    <>
      <div className="code-wiki-mermaid" onClick={openZoom} title="点击放大查看（全屏 · 滚轮缩放 · 拖拽平移）">
        <div ref={ref}>
          <Spin size="small" />
        </div>
        <span className="code-wiki-mermaid-hint">
          <FullscreenOutlined /> 点击放大
        </span>
      </div>
      {zoom && <ZoomModal image={zoom} onClose={() => setZoom(null)} />}
    </>
  )
}

// ── 内容分段：```mermaid 围栏单独抽取，其余按 markdown 渲染 ──
type WikiSegment = { kind: 'markdown'; html: string } | { kind: 'mermaid'; code: string }

const MERMAID_FENCE = /^```mermaid[ \t]*\r?\n([\s\S]*?)^```[ \t]*$/gm

function splitSegments(raw: string): WikiSegment[] {
  const usedIds = new Set<string>()
  const out: WikiSegment[] = []
  let last = 0
  let m: RegExpExecArray | null
  MERMAID_FENCE.lastIndex = 0
  while ((m = MERMAID_FENCE.exec(raw))) {
    if (m.index > last) out.push({ kind: 'markdown', html: renderMarkdown(raw.slice(last, m.index), usedIds) })
    out.push({ kind: 'mermaid', code: m[1].trim() })
    last = MERMAID_FENCE.lastIndex
  }
  if (last < raw.length) out.push({ kind: 'markdown', html: renderMarkdown(raw.slice(last), usedIds) })
  return out
}

export default function CodeWikiPanel() {
  const segments = useMemo(() => splitSegments(wikiRaw), [])

  return (
    <Card
      title="CODE WIKI（docs/CODE_WIKI.md）"
      size="small"
      style={{ height: '100%', display: 'flex', flexDirection: 'column' }}
      styles={{ body: { flex: 1, minHeight: 0, overflow: 'auto', padding: '8px 16px 24px' } }}
    >
      <div className="code-wiki">
        {segments.map((seg, i) =>
          seg.kind === 'mermaid' ? (
            <MermaidBlock key={i} code={seg.code} />
          ) : (
            <div key={i} dangerouslySetInnerHTML={{ __html: seg.html }} />
          ),
        )}
      </div>
    </Card>
  )
}