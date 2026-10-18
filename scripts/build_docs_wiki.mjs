#!/usr/bin/env node
// ============================================================
// docs-wiki 增量生成器
//
// 把仓库 docs/ 下的文档生成为 docs-wiki 静态浏览页的数据文件。
// 核心：**只重生成变更的文档**，不是每次都全量生成。
//
// 检测机制：docs-wiki/.build-manifest.json 记录每篇源文档的内容 sha256
//   - 源文档内容未变 且 产出文件仍在 且 生成器版本未变 → 跳过（不读不写）
//   - 任一不符 → 只重生成该篇的 assets/docs/<id>.js
//   - 清单里存在、但源文档已删除的 → 删除其产出
//   - docs-index.js（导航元数据，无正文）仅内容有变时写盘
//
// 用法：
//   node scripts/build_docs_wiki.mjs            # 增量（默认）
//   node scripts/build_docs_wiki.mjs --force    # 全量重生成
//   node scripts/build_docs_wiki.mjs --out=DIR  # 指定产出目录（默认 docs-wiki）
//   node scripts/build_docs_wiki.mjs --quiet    # 只打印汇总
// ============================================================
import { createHash } from 'node:crypto'
import {
  existsSync, mkdirSync, readFileSync, writeFileSync, readdirSync, rmSync,
} from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const SELF = fileURLToPath(import.meta.url)
const REPO = resolve(dirname(SELF), '..')
const DOCS = join(REPO, 'docs')

const argv = process.argv.slice(2)
const FORCE = argv.includes('--force')
const QUIET = argv.includes('--quiet')
const outArg = argv.find((a) => a.startsWith('--out='))
const OUT = resolve(REPO, outArg ? outArg.slice('--out='.length) : 'docs-wiki')

const DOCS_OUT = join(OUT, 'assets', 'docs')
const INDEX_OUT = join(OUT, 'assets', 'docs-index.js')
const MANIFEST_OUT = join(OUT, '.build-manifest.json')

// —— 分组定义（overview 固定两篇；其余按目录自动发现，新增/删除文档零维护）——
const GROUPS = [
  { id: 'overview', name: '项目概览' },
  { id: 'tech', name: '技术文档' },
  { id: 'flow', name: '流程文档' },
]
const OVERVIEW = [
  { id: 'code-wiki', label: '代码知识库', file: 'CODE_WIKI.md' },
  { id: 'doc-map', label: '文档地图', file: '00-文档地图.md' },
]
const DIRS = [
  { group: 'tech', dir: '技术文档', prefix: 'tech' },
  { group: 'flow', dir: '项目功能流程文档', prefix: 'flow' },
]

const sha = (s) => createHash('sha256').update(s).digest('hex').slice(0, 16)

// —— 收集文档元数据 ——
function firstHeading(md) {
  for (const line of md.split('\n')) {
    const t = line.trim()
    if (t.startsWith('# ')) return t.slice(2).trim()
  }
  return ''
}

function collect() {
  const list = []
  for (const o of OVERVIEW) {
    const abs = join(DOCS, o.file)
    if (!existsSync(abs)) continue
    const md = readFileSync(abs, 'utf8')
    list.push({ id: o.id, group: 'overview', label: o.label, path: o.file, md, title: firstHeading(md) || o.label })
  }
  for (const d of DIRS) {
    const abs = join(DOCS, d.dir)
    if (!existsSync(abs)) continue
    const files = readdirSync(abs).filter((f) => f.endsWith('.md')).sort()
    for (const f of files) {
      const m = /^(\d+)-(.+)\.md$/.exec(f)
      const id = m ? `${d.prefix}-${m[1]}` : `${d.prefix}-${sha(f)}`
      const label = m ? `${m[1]} · ${m[2]}` : f.replace(/\.md$/, '')
      const md = readFileSync(join(abs, f), 'utf8')
      list.push({ id, group: d.group, label, path: `${d.dir}/${f}`, md, title: firstHeading(md) || label })
    }
  }
  return list
}

// —— 产出内容 ——
function docFileContent(doc) {
  const payload = JSON.stringify(doc).replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029')
  return `window.DOC_REGISTRY=window.DOC_REGISTRY||{};\nwindow.DOC_REGISTRY[${JSON.stringify(doc.id)}]=${payload};\n`
}

function indexFileContent(docs) {
  const groups = JSON.stringify(GROUPS)
  const index = JSON.stringify(docs.map((d) => ({
    id: d.id, group: d.group, label: d.label, title: d.title, path: d.path,
  })))
  return [
    '/* 自动生成，请勿手改 — 由 scripts/build_docs_wiki.mjs 产出 */',
    `window.DOC_GROUPS=${groups};`,
    `window.DOC_INDEX=${index};`,
    '',
  ].join('\n')
}

// —— 静态依赖：缺了才补（正常已随仓库提交，不覆盖已有文件）——
function ensureStatic() {
  const jsDir = join(OUT, '_shared', 'js')
  const libs = [
    ['marked.min.js', join(REPO, 'frontend/node_modules/marked/lib/marked.umd.js')],
    ['mermaid.min.js', join(REPO, 'frontend/node_modules/mermaid/dist/mermaid.min.js')],
  ]
  const copied = []
  for (const [name, src] of libs) {
    const dst = join(jsDir, name)
    if (existsSync(dst) || !existsSync(src)) continue
    mkdirSync(jsDir, { recursive: true })
    writeFileSync(dst, readFileSync(src))
    copied.push(name)
  }
  return copied
}

function readManifest() {
  if (!existsSync(MANIFEST_OUT)) return null
  try {
    const m = JSON.parse(readFileSync(MANIFEST_OUT, 'utf8'))
    return m && m.v === 1 && m.docs ? m : null
  } catch { return null }
}

function writeIfChanged(path, content) {
  if (existsSync(path) && readFileSync(path, 'utf8') === content) return false
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, content)
  return true
}

// —— 主流程 ——
const t0 = Date.now()
const builderHash = sha(readFileSync(SELF, 'utf8'))
const prev = FORCE ? null : readManifest()
const fullRebuild = !prev || prev.builder !== builderHash

const docs = collect()
const nextManifest = { v: 1, builder: builderHash, docs: {} }
const touched = []
const skipped = []

mkdirSync(DOCS_OUT, { recursive: true })

for (const doc of docs) {
  const h = sha(doc.md)
  const outFile = join(DOCS_OUT, `${doc.id}.js`)
  const prevEntry = prev?.docs?.[doc.id]
  const unchanged = !fullRebuild && prevEntry && prevEntry.hash === h && prevEntry.path === doc.path && existsSync(outFile)
  if (unchanged) {
    skipped.push(doc.id)
  } else if (writeIfChanged(outFile, docFileContent(doc))) {
    touched.push(doc.id)
  } else {
    skipped.push(doc.id)
  }
  nextManifest.docs[doc.id] = { hash: h, path: doc.path }
}

// 清理：源文档已删除 → 删除对应产出
const removed = []
if (prev) {
  const ids = new Set(docs.map((d) => d.id))
  for (const id of Object.keys(prev.docs)) {
    if (ids.has(id)) continue
    rmSync(join(DOCS_OUT, `${id}.js`), { force: true })
    removed.push(id)
  }
}

const indexChanged = writeIfChanged(INDEX_OUT, indexFileContent(docs))
writeIfChanged(MANIFEST_OUT, JSON.stringify(nextManifest, null, 2) + '\n')
const staticCopied = ensureStatic()

// —— 报告 ——
const ms = Date.now() - t0
console.log(`[docs-wiki] ${fullRebuild ? '全量' : '增量'}生成：重生成 ${touched.length} 篇 / 跳过 ${skipped.length} 篇` +
  (removed.length ? ` / 删除 ${removed.length} 篇` : '') +
  (indexChanged ? ' / 导航索引已更新' : '') +
  (staticCopied.length ? ` / 补齐依赖 ${staticCopied.join(',')}` : '') +
  `（${ms}ms）`)
if (!QUIET && touched.length) {
  console.log(`[docs-wiki] 重生成：${touched.join(', ')}`)
}
if (!QUIET && removed.length) {
  console.log(`[docs-wiki] 删除：${removed.join(', ')}`)
}
if (!QUIET && fullRebuild) {
  console.log(`[docs-wiki] 触发全量的原因：${prev ? '生成器已变更' : '无历史清单'}`)
}
