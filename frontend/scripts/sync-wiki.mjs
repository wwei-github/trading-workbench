// 前端构建前置（predev / prebuild）：生成 docs wiki 浏览数据并发布到 public/wiki/。
//
// 应用内 CODE WIKI 面板与 /wiki/ 独立页都读它；vite 的 publicDir 会直接服务
// public/（dev）并拷入 dist（build），故 wiki 的「生成 + 发布」规则只收在这一处，
// CI 无需再手写拷贝步骤。
//
// 实际生成仍是增量：scripts/build_docs_wiki.mjs 按 docs-wiki/.build-manifest.json
// 的内容 sha256 比对，只重生成 docs/ 中有变更的篇目。
//
// Docker 构建时（前端构建上下文仅 ./frontend，docs/ 与生成器不存在）为 no-op：
// 复用已生成的 public/wiki，缺失时应用内面板会提示（生产链路走 CI，不受影响）。
import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repo = resolve(here, '../..')
const docsDir = resolve(repo, 'docs')
const generator = resolve(repo, 'scripts/build_docs_wiki.mjs')
const publishDir = resolve(here, '../public/wiki')

if (!existsSync(docsDir) || !existsSync(generator)) {
  console.log(
    existsSync(publishDir)
      ? '[sync-wiki] docs/ 或生成器不存在（Docker 构建上下文），复用已生成的 public/wiki'
      : '[sync-wiki] docs/ 或生成器不存在（Docker 构建上下文），且 public/wiki 未生成，跳过',
  )
  process.exit(0)
}

const r = spawnSync(process.execPath, [generator, `--publish=${publishDir}`], {
  cwd: repo,
  stdio: 'inherit',
})
process.exit(r.status ?? 1)
