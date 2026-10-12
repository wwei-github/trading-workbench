import { useEffect, useMemo, useRef, useState } from 'react'
import { Card, Spin, Typography } from 'antd'
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

function MermaidBlock({ code }: { code: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const [error, setError] = useState('')

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

  if (error) {
    return (
      <div className="code-wiki-mermaid">
        <Text type="danger" style={{ fontSize: 12 }}>mermaid 渲染失败：{error}</Text>
        <pre>{code}</pre>
      </div>
    )
  }
  return (
    <div className="code-wiki-mermaid">
      <div ref={ref}>
        <Spin size="small" />
      </div>
    </div>
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