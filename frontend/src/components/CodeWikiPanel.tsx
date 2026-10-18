import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Card, Input, Modal, Spin, Tooltip, Typography } from 'antd'
import { CloseOutlined, FullscreenOutlined, ZoomInOutlined, ZoomOutOutlined } from '@ant-design/icons'
import { marked } from 'marked'

const { Text } = Typography

// ── 标题锚点：GitHub 风格 slug，使文档内目录（#1-项目概述 等）可跳转 ──
function slugify(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, '') // 去标点（.、— 等），保留中英文/数字/空格/连字符
    .replace(/\s/g, '-')
}

// ── 数据层：浏览数据由 scripts/build_docs_wiki.mjs 生成并发布到 public/wiki，
//    经 vite publicDir 在 dev 直接服务、build 拷入 dist；此处运行时按需注入脚本加载 ──
const WIKI_BASE = '/wiki'

interface DocMeta {
  id: string
  group: string
  docKey: string
  docName: string
  nature: string
  label: string
  title: string
  path: string
}
interface DocGroup {
  id: string
  name: string
}
interface DocPayload extends DocMeta {
  md: string
}

type WikiWindow = Window & {
  DOC_GROUPS?: DocGroup[]
  DOC_INDEX?: DocMeta[]
  DOC_REGISTRY?: Record<string, DocPayload>
}

const wikiWindow = () => window as WikiWindow

// 同一 URL 只注入一次；失败则移除缓存，允许重试
const scriptCache = new Map<string, Promise<void>>()

function loadScript(src: string): Promise<void> {
  const cached = scriptCache.get(src)
  if (cached) return cached
  const task = new Promise<void>((resolve, reject) => {
    const s = document.createElement('script')
    s.src = src
    s.async = true
    s.onload = () => resolve()
    s.onerror = () => {
      scriptCache.delete(src)
      reject(new Error(`资源加载失败：${src}`))
    }
    document.head.appendChild(s)
  })
  scriptCache.set(src, task)
  return task
}

async function loadIndex(): Promise<{ groups: DocGroup[]; index: DocMeta[] }> {
  const w = wikiWindow()
  if (!w.DOC_INDEX) await loadScript(`${WIKI_BASE}/assets/docs-index.js`)
  return { groups: w.DOC_GROUPS ?? [], index: w.DOC_INDEX ?? [] }
}

async function loadDoc(id: string): Promise<DocPayload> {
  const w = wikiWindow()
  w.DOC_REGISTRY = w.DOC_REGISTRY ?? {}
  if (!w.DOC_REGISTRY[id]) await loadScript(`${WIKI_BASE}/assets/docs/${id}.js`)
  const doc = w.DOC_REGISTRY[id]
  if (!doc) throw new Error(`文档数据为空：${id}`)
  return doc
}

// ── 百分号解码：marked 会把 href 里的非 ASCII 编码（中文路径 → %E6%8A%80…），
//    还原后才能与 pathIndex 的原始路径比对；非法转义序列（如正文里字面量的 %）原样返回 ──
function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

// ── 文档相对路径归一化（用于解析正文里的跨文档 .md 链接）──
function resolvePath(curPath: string, href: string): string {
  const parts = curPath.split('/')
  parts.pop()
  for (const seg of href.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') parts.pop()
    else parts.push(seg)
  }
  return parts.join('/')
}

// ── 侧栏两级分组：性质分组顺序（概览 → 功能流程 → 技术实现 → 需求，未知性质排最后）
//    与 docKey 排序（数字升序在前、非数字在后）──
const NATURE_ORDER = ['overview', 'flow', 'tech', 'req']
const cmpNature = (a: string, b: string): number => {
  const ia = NATURE_ORDER.indexOf(a)
  const ib = NATURE_ORDER.indexOf(b)
  return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
}
const cmpDocKey = (a: string, b: string): number => {
  const na = /^\d+$/.test(a)
  const nb = /^\d+$/.test(b)
  if (na && nb) return Number(a) - Number(b)
  if (na !== nb) return na ? -1 : 1
  return a.localeCompare(b)
}
// docKey 为纯数字时显示 `NN 名称`，否则仅显示名称（如 CODE_WIKI → 代码知识库）
const rowLabel = (d: DocMeta): string => (/^\d+$/.test(d.docKey) ? `${d.docKey} ${d.docName}` : d.docName)

// ── markdown → HTML 后的语义补齐：标题 id（内链锚点）、表格横向滚动容器、正文链接重写 ──
function enhance(root: HTMLElement, docPath: string, pathIndex: Record<string, string>, usedIds: Set<string>): void {
  root.querySelectorAll('h1, h2, h3, h4, h5, h6').forEach((h) => {
    const base = slugify(h.textContent ?? '') || 'section'
    let id = base
    let n = 1
    while (usedIds.has(id)) id = `${base}-${n++}`
    usedIds.add(id)
    h.id = id
  })

  root.querySelectorAll<HTMLTableElement>('table').forEach((t) => {
    if (t.parentElement?.classList.contains('table-wrap')) return
    const wrap = document.createElement('div')
    wrap.className = 'table-wrap'
    t.parentNode?.insertBefore(wrap, t)
    wrap.appendChild(t)
  })

  root.querySelectorAll<HTMLAnchorElement>('a[href]').forEach((a) => {
    const href = a.getAttribute('href') ?? ''
    if (/^https?:/i.test(href)) {
      a.setAttribute('target', '_blank')
      a.setAttribute('rel', 'noopener')
      return
    }
    if (href.startsWith('#')) {
      // 同文档锚点：点击时由容器委托处理，不触发页面跳转
      a.dataset.anchor = safeDecode(href.slice(1))
      return
    }
    if (/\.md(#|$)/i.test(href)) {
      const [rawPath, rawAnchor] = href.split('#')
      const target = resolvePath(docPath, safeDecode(rawPath))
      const id = pathIndex[target]
      if (id) {
        a.dataset.docId = id
        if (rawAnchor) a.dataset.anchor = safeDecode(rawAnchor)
        a.setAttribute('href', `#${id}${rawAnchor ? `:${rawAnchor}` : ''}`)
      } else {
        a.classList.add('missing')
        a.setAttribute('title', `未纳入本页：${target}`)
        a.removeAttribute('href')
      }
    }
  })
}

// ── 内容分段：```mermaid 围栏单独抽取（交给 MermaidBlock），其余按 markdown 渲染 ──
type WikiSegment = { kind: 'markdown'; html: string } | { kind: 'mermaid'; code: string }

const MERMAID_FENCE = /^```mermaid[ \t]*\r?\n([\s\S]*?)^```[ \t]*$/gm

function renderSegments(raw: string, docPath: string, pathIndex: Record<string, string>): WikiSegment[] {
  const usedIds = new Set<string>()
  const out: WikiSegment[] = []
  const toMarkdown = (chunk: string) => {
    const html = marked.parse(chunk) as string
    const parsed = new DOMParser().parseFromString(`<div>${html}</div>`, 'text/html')
    const root = parsed.body.firstElementChild as HTMLElement | null
    if (!root) return html
    enhance(root, docPath, pathIndex, usedIds)
    return root.innerHTML
  }
  let last = 0
  let m: RegExpExecArray | null
  MERMAID_FENCE.lastIndex = 0
  while ((m = MERMAID_FENCE.exec(raw))) {
    if (m.index > last) out.push({ kind: 'markdown', html: toMarkdown(raw.slice(last, m.index)) })
    out.push({ kind: 'mermaid', code: m[1].trim() })
    last = MERMAID_FENCE.lastIndex
  }
  if (last < raw.length) out.push({ kind: 'markdown', html: toMarkdown(raw.slice(last)) })
  return out
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

// ── 面板：左「搜索 + 三组目录」，右「正文 + 上/下篇」──────────────────────
export default function CodeWikiPanel() {
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState('')
  const [groups, setGroups] = useState<DocGroup[]>([])
  const [index, setIndex] = useState<DocMeta[]>([])
  const [query, setQuery] = useState('')
  const [currentId, setCurrentId] = useState('')
  const [segments, setSegments] = useState<WikiSegment[]>([])
  const [docError, setDocError] = useState('')
  // 侧栏折叠态（均默认展开，记录「已收起」）：模块、模块内的性质分组
  const [closedGroups, setClosedGroups] = useState<Set<string>>(new Set())
  const [closedNatures, setClosedNatures] = useState<Set<string>>(new Set())

  const docRef = useRef<HTMLDivElement>(null)
  const navRef = useRef<HTMLDivElement>(null)
  const pendingAnchor = useRef<string | null>(null)

  const pathIndex = useMemo(() => Object.fromEntries(index.map((d) => [d.path, d.id])), [index])
  const current = useMemo(() => index.find((d) => d.id === currentId) ?? null, [index, currentId])

  const scrollToAnchor = useCallback((anchor: string) => {
    const root = docRef.current
    if (!root) return
    const target =
      root.querySelector(`#${CSS.escape(anchor)}`) ??
      root.querySelector(`#${CSS.escape(slugify(anchor))}`)
    target?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [])

  // 载入导航索引（一次性）
  useEffect(() => {
    let alive = true
    loadIndex()
      .then(({ groups: g, index: list }) => {
        if (!alive) return
        if (!list.length) throw new Error('文档索引为空（assets/docs-index.js）')
        setGroups(g)
        setIndex(list)
        setCurrentId(list[0].id)
        setStatus('ready')
      })
      .catch((e: unknown) => {
        if (!alive) return
        setError(e instanceof Error ? e.message : String(e))
        setStatus('error')
      })
    return () => {
      alive = false
    }
  }, [])

  // 切换文档 → 按需加载该篇数据并渲染
  useEffect(() => {
    if (!current) return
    let alive = true
    setSegments([])
    setDocError('')
    loadDoc(current.id)
      .then((doc) => {
        if (alive) setSegments(renderSegments(doc.md, doc.path, pathIndex))
      })
      .catch((e: unknown) => {
        if (alive) setDocError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
  }, [current, pathIndex])

  // 无锚点时切换文档需回到顶部
  useEffect(() => {
    if (!pendingAnchor.current) docRef.current?.scrollTo({ top: 0 })
  }, [currentId])

  // 正文渲染完成后再跳到目标锚点（跨文档链接：先换文档、后定位）
  useEffect(() => {
    const anchor = pendingAnchor.current
    if (!anchor || !segments.length) return
    pendingAnchor.current = null
    requestAnimationFrame(() => scrollToAnchor(anchor))
  }, [segments, scrollToAnchor])

  // 目录中的当前项保持在可视区
  useEffect(() => {
    navRef.current
      ?.querySelector<HTMLElement>('.wiki-nav-item.active')
      ?.scrollIntoView({ block: 'nearest' })
  }, [currentId])

  const openDoc = useCallback(
    (id: string, anchor?: string | null) => {
      if (id === currentId) {
        if (anchor) scrollToAnchor(anchor)
        return
      }
      pendingAnchor.current = anchor ?? null
      setCurrentId(id)
    },
    [currentId, scrollToAnchor],
  )

  // 正文链接委托：跨文档 .md → 切换文档（可带锚点）；#锚点 → 容器内滚动
  const handleDocClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const a = (e.target as HTMLElement).closest('a')
    if (!a) return
    const { docId, anchor } = a.dataset
    if (docId) {
      e.preventDefault()
      openDoc(docId, anchor ?? null)
      return
    }
    if (anchor !== undefined) {
      e.preventDefault()
      scrollToAnchor(anchor)
    }
  }

  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const matchDoc = (d: DocMeta) =>
    !terms.length || terms.every((t) => `${d.docKey} ${d.docName} ${d.label} ${d.title} ${d.path}`.toLowerCase().includes(t))
  const hitCount = terms.length ? index.filter(matchDoc).length : index.length

  // 折叠交互（模块 / 性质分组，均默认展开）
  const toggleGroup = (id: string) =>
    setClosedGroups((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  const toggleNature = (key: string) =>
    setClosedNatures((s) => {
      const n = new Set(s)
      if (n.has(key)) n.delete(key)
      else n.add(key)
      return n
    })

  const idx = index.findIndex((d) => d.id === currentId)
  const prev = idx > 0 ? index[idx - 1] : null
  const next = idx >= 0 && idx < index.length - 1 ? index[idx + 1] : null

  if (status === 'error') {
    return (
      <Card title="CODE WIKI · docs/" size="small" style={{ height: '100%' }}>
        <Text type="danger">wiki 数据加载失败：{error}</Text>
        <Text type="secondary" style={{ display: 'block', marginTop: 8, fontSize: 12 }}>
          数据由 frontend/scripts/sync-wiki.mjs 生成并发布到 public/wiki（npm run dev / build 前置自动执行）。
        </Text>
      </Card>
    )
  }

  return (
    <Card
      title={current ? `CODE WIKI · docs/${current.path}` : 'CODE WIKI · docs/'}
      size="small"
      style={{ height: '100%', display: 'flex', flexDirection: 'column' }}
      styles={{ body: { flex: 1, minHeight: 0, padding: 0, display: 'flex' } }}
    >
      <aside className="wiki-nav">
        <div className="wiki-search">
          <Input
            allowClear
            size="small"
            placeholder="搜索文档（标题 / 路径）"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setQuery('')
            }}
          />
          <div className="wiki-search-hint">
            {terms.length ? `${hitCount} / ${index.length} 篇匹配` : ''}
          </div>
        </div>
        <div className="wiki-nav-scroll" ref={navRef}>
          {groups.map((g) => {
            const items = index.filter((d) => d.group === g.id && matchDoc(d))
            if (!items.length) return null
            const closed = closedGroups.has(g.id)
            // 模块内按性质归档（概览 / 功能流程 / 技术实现 / 需求，各自成组）
            const natureMap = new Map<string, DocMeta[]>()
            for (const d of items) {
              const arr = natureMap.get(d.nature)
              if (arr) arr.push(d)
              else natureMap.set(d.nature, [d])
            }
            const natures = [...natureMap.keys()].sort(cmpNature)
            // 单一性质模块（索引与总览 / 需求与路线）省略性质层，文档直接挂在模块下
            const flat = natures.length === 1
            return (
              <div className="wiki-nav-group" key={g.id}>
                <div
                  className={`wiki-nav-group-title${closed ? ' collapsed' : ''}`}
                  onClick={() => toggleGroup(g.id)}
                >
                  <span className="nav-caret">{closed ? '▸' : '▾'}</span>
                  {g.name}
                </div>
                {!closed && natures.map((nature) => {
                  const docs = natureMap.get(nature)!.slice().sort((a, b) => cmpDocKey(a.docKey, b.docKey))
                  const natureKey = `${g.id}:${nature}`
                  const links = docs.map((d) => (
                    <a
                      key={d.id}
                      className={`wiki-nav-item ${flat ? 'wiki-nav-leaf' : 'wiki-nav-sub'}${d.id === currentId ? ' active' : ''}`}
                      title={d.title}
                      onClick={() => openDoc(d.id)}
                    >
                      {rowLabel(d)}
                    </a>
                  ))
                  if (flat) return <Fragment key={natureKey}>{links}</Fragment>
                  const natureClosed = closedNatures.has(natureKey)
                  return (
                    <Fragment key={natureKey}>
                      <div
                        className={`wiki-nav-nature${natureClosed ? ' collapsed' : ''}`}
                        onClick={() => toggleNature(natureKey)}
                      >
                        <span className="nav-caret">{natureClosed ? '▸' : '▾'}</span>
                        {docs[0].label}
                      </div>
                      {!natureClosed && links}
                    </Fragment>
                  )
                })}
              </div>
            )
          })}
          {status === 'loading' && (
            <div className="wiki-loading">
              <Spin size="small" />
            </div>
          )}
        </div>
      </aside>

      <div className="wiki-doc" ref={docRef}>
        <div className="code-wiki" onClick={handleDocClick}>
          {docError ? (
            <Text type="danger">文档加载失败：{docError}</Text>
          ) : segments.length === 0 ? (
            <div className="wiki-loading">
              加载中… <Spin size="small" />
            </div>
          ) : (
            segments.map((seg, i) =>
              seg.kind === 'mermaid' ? (
                <MermaidBlock key={`${currentId}-${i}`} code={seg.code} />
              ) : (
                <div key={`${currentId}-${i}`} dangerouslySetInnerHTML={{ __html: seg.html }} />
              ),
            )
          )}
          {!!segments.length && (
            <nav className="wiki-doc-nav">
              {prev ? (
                <a onClick={() => openDoc(prev.id)}>
                  <span className="dn-dir">← 上一篇</span>
                  <span className="dn-title">{prev.docName} · {prev.label}</span>
                </a>
              ) : (
                <span />
              )}
              {next ? (
                <a className="next" onClick={() => openDoc(next.id)}>
                  <span className="dn-dir">下一篇 →</span>
                  <span className="dn-title">{next.docName} · {next.label}</span>
                </a>
              ) : (
                <span />
              )}
            </nav>
          )}
          {!!segments.length && current && (
            <footer className="wiki-doc-foot">
              来源：docs/{current.path} · 本页为 docs 文档的只读浏览视图（以仓库内文档为准）
            </footer>
          )}
        </div>
      </div>
    </Card>
  )
}