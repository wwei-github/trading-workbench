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
//   node scripts/build_docs_wiki.mjs                  # 增量（默认）
//   node scripts/build_docs_wiki.mjs --force          # 全量重生成
//   node scripts/build_docs_wiki.mjs --out=DIR        # 指定产出目录（默认 docs-wiki）
//   node scripts/build_docs_wiki.mjs --publish=DIR    # 生成后把浏览页镜像到 DIR（供 vite publicDir）
//   node scripts/build_docs_wiki.mjs --quiet          # 只打印汇总
// ============================================================
import { createHash } from 'node:crypto'
import {
  cpSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync, readdirSync, rmSync,
} from 'node:fs'
import { basename, dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const SELF = fileURLToPath(import.meta.url)
const REPO = resolve(dirname(SELF), '..')
const DOCS = join(REPO, 'docs')

const argv = process.argv.slice(2)
const FORCE = argv.includes('--force')
const QUIET = argv.includes('--quiet')
const outArg = argv.find((a) => a.startsWith('--out='))
const OUT = resolve(REPO, outArg ? outArg.slice('--out='.length) : 'docs-wiki')
const publishArg = argv.find((a) => a.startsWith('--publish='))
const PUBLISH = publishArg ? resolve(REPO, publishArg.slice('--publish='.length)) : null

const DOCS_OUT = join(OUT, 'assets', 'docs')
const INDEX_OUT = join(OUT, 'assets', 'docs-index.js')
const MANIFEST_OUT = join(OUT, '.build-manifest.json')

// —— 模块定义（顺序即侧栏顺序；dir 为 docs/ 下的一级目录）——
// 配对模块：目录下固定 `功能流程/` 与 `技术实现/` 两个性质子目录。
// flat 模块（需求文档）：目录下直接平铺 NN-*.md，nature = req。
const MODULES = [
  { id: 'overview', name: '索引与总览', dir: '总览与索引' },
  { id: 'scan',     name: '扫描与筛选', dir: '扫描与筛选' },
  { id: 'signal',   name: '信号与形态', dir: '信号与形态' },
  { id: 'ai',       name: 'AI 决策',    dir: 'AI决策' },
  { id: 'exec',     name: '交易执行',   dir: '交易执行' },
  { id: 'risk',     name: '风控',       dir: '风控' },
  { id: 'review',   name: '复盘归因',   dir: '复盘归因' },
  { id: 'spot',     name: '现货屯币',   dir: '现货屯币' },
  { id: 'tactic',   name: '战法',       dir: '战法' },
  { id: 'frontend', name: '前端与查询', dir: '前端与查询' },
  { id: 'infra',    name: '基建与通知', dir: '基建与通知' },
  { id: 'req',      name: '需求与路线', dir: '需求文档', flat: true },
]
// 配对模块的两个性质子目录（flat 模块不用）
const NATURES = [
  { n: 'flow', label: '功能流程', dir: '功能流程', prefix: 'flow' },
  { n: 'tech', label: '技术实现', dir: '技术实现', prefix: 'tech' },
]
// 根目录两篇（nature = overview，归入 overview 模块）
const ROOT_DOCS = [
  { id: 'doc-map',   docKey: '00',        docName: '文档地图',   file: '00-文档地图.md' },
  { id: 'code-wiki', docKey: 'CODE_WIKI', docName: '代码知识库', file: 'CODE_WIKI.md' },
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

// 从文件名 `NN-名称.md` 解析出编号与名称（无编号时 num 为空、name 取整体）
function parseName(file) {
  const m = /^(\d+)-(.+)\.md$/.exec(file)
  if (m) return { num: m[1], name: m[2] }
  return { num: '', name: file.replace(/\.md$/, '') }
}

function collect() {
  const list = []
  // 根目录两篇（归入 overview 模块，nature = overview）
  for (const o of ROOT_DOCS) {
    const abs = join(DOCS, o.file)
    if (!existsSync(abs)) continue
    const md = readFileSync(abs, 'utf8')
    list.push({
      id: o.id, group: 'overview', docKey: o.docKey, docName: o.docName,
      nature: 'overview', label: '概览', path: o.file, md,
      title: firstHeading(md) || o.docName,
    })
  }
  for (const m of MODULES) {
    const abs = join(DOCS, m.dir)
    if (!existsSync(abs)) continue
    if (m.flat) {
      const files = readdirSync(abs).filter((f) => f.endsWith('.md')).sort()
      for (const f of files) {
        const p = parseName(f)
        const md = readFileSync(join(abs, f), 'utf8')
        list.push({
          id: `req-${p.num || sha(f)}`,
          group: m.id, docKey: p.num || f.replace(/\.md$/, ''), docName: p.name,
          nature: 'req', label: '需求', path: `${m.dir}/${f}`, md,
          title: firstHeading(md) || p.name,
        })
      }
    } else {
      for (const n of NATURES) {
        const sub = join(abs, n.dir)
        if (!existsSync(sub)) continue
        const files = readdirSync(sub).filter((f) => f.endsWith('.md')).sort()
        for (const f of files) {
          const p = parseName(f)
          const md = readFileSync(join(sub, f), 'utf8')
          list.push({
            id: `${n.prefix}-${p.num || sha(f)}`,
            group: m.id, docKey: p.num || f.replace(/\.md$/, ''), docName: p.name,
            nature: n.n, label: n.label, path: `${m.dir}/${n.dir}/${f}`, md,
            title: firstHeading(md) || p.name,
          })
        }
      }
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
  const groups = JSON.stringify(MODULES.map((m) => ({ id: m.id, name: m.name })))
  const index = JSON.stringify(docs.map((d) => ({
    id: d.id, group: d.group, docKey: d.docKey, docName: d.docName,
    nature: d.nature, label: d.label, title: d.title, path: d.path,
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

// —— 发布：把浏览页产出镜像到 DIR（如 frontend/public/wiki，由 vite 直接提供服务并拷入 dist）——
// 产出目录 docs-wiki/ 保留 .build-manifest.json 供下次增量比对；发布目录只放运行时所需文件，
// 且整体重建（先删后拷）以保证与产出目录严格一致、无残留。
function publish() {
  if (!PUBLISH) return null
  if (PUBLISH === OUT) throw new Error(`--publish 目录不能与产出目录相同：${PUBLISH}`)
  rmSync(PUBLISH, { recursive: true, force: true })
  cpSync(OUT, PUBLISH, {
    recursive: true,
    filter: (src) => basename(src) !== '.build-manifest.json',
  })
  // 入口统一命名 index.html，使 /wiki/ 可直接打开
  const html = join(PUBLISH, 'docs-wiki.html')
  if (existsSync(html)) renameSync(html, join(PUBLISH, 'index.html'))
  return PUBLISH
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
const published = publish()

// —— 报告 ——
const ms = Date.now() - t0
console.log(`[docs-wiki] ${fullRebuild ? '全量' : '增量'}生成：重生成 ${touched.length} 篇 / 跳过 ${skipped.length} 篇` +
  (removed.length ? ` / 删除 ${removed.length} 篇` : '') +
  (indexChanged ? ' / 导航索引已更新' : '') +
  (staticCopied.length ? ` / 补齐依赖 ${staticCopied.join(',')}` : '') +
  (published ? ` / 已发布到 ${relative(REPO, published)}` : '') +
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
