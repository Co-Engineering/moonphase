/**
 * Copy the built frontend into the app, so it travels with it.
 *
 * The shell renders the same frontend the browser and the phone use. In a
 * checkout that build sits in another workspace and is loaded from there; a
 * packaged app has no workspace around it, so the files have to come inside.
 * Copying at package time keeps one build rather than two.
 */
import { cp, rm, stat } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = join(here, '..', '..', 'web', 'dist')
const target = join(here, '..', 'web')

try {
  await stat(join(source, 'index.html'))
} catch {
  console.error(
    `No frontend build at ${source}.\n` +
      'Run `pnpm --filter @moonphase/web build` first.',
  )
  process.exit(1)
}

await rm(target, { recursive: true, force: true })
await cp(source, target, { recursive: true })
console.log(`copied ${source} -> ${target}`)
