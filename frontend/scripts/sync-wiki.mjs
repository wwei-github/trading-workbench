// 同步仓库根 docs/CODE_WIKI.md → frontend/src/assets/CODE_WIKI.md（提交到 git 的前端副本）。
//
// 背景：docker-compose 前端构建上下文仅 ./frontend，docs/ 不进镜像，
// 故前端必须自带一份副本并在构建时经 Vite `?raw` 打包。
// 本地开发/构建时（docs/ 存在）该脚本确保副本与权威源一致；
// Docker 构建时（docs/ 不存在）为 no-op，直接使用已提交的副本。
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = resolve(here, '../../docs/CODE_WIKI.md')
const dest = resolve(here, '../src/assets/CODE_WIKI.md')

if (!existsSync(source)) {
  console.log('[sync-wiki] docs/CODE_WIKI.md 不存在（Docker 构建上下文），跳过')
  process.exit(0)
}

const content = readFileSync(source, 'utf8')
if (existsSync(dest) && readFileSync(dest, 'utf8') === content) {
  console.log('[sync-wiki] 副本已是最新')
  process.exit(0)
}

mkdirSync(dirname(dest), { recursive: true })
writeFileSync(dest, content)
console.log('[sync-wiki] 已同步 docs/CODE_WIKI.md → src/assets/CODE_WIKI.md')