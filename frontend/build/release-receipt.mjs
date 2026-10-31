import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { isAbsolute, join, relative, resolve } from 'node:path'

const receiptName = 'build-receipt.json'
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex')
const publicSource = (path) => {
  const parts = path.split('/')
  return !parts.some((part) => ['node_modules', 'dist', '.git', '.workflow', '__pycache__'].includes(part)
    || ['dist.previous.', '.dist-staging.', '.dist.staging.', '.dist.previous.'].some((prefix) => part.startsWith(prefix)))
    && !parts.at(-1).startsWith('.env') && !/\.(pem|key)$/i.test(path)
}

export const releaseReceipt = () => {
  let repoRoot, outputDir, initial, configMode
  const git = (...args) => execFileSync('git', args, { cwd: repoRoot, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim()
  const snapshot = () => {
    try {
      const commit = git('rev-parse', 'HEAD')
      const tree = git('rev-parse', 'HEAD^{tree}')
      const files = {}
      for (const record of git('ls-tree', '-rz', '--full-tree', 'HEAD', '--', 'frontend').split('\0')) {
        if (!record) continue
        const [metadata, path] = record.split('\t')
        const [mode, kind, oid] = metadata.split(' ')
        if (kind === 'blob' && ['100644', '100755'].includes(mode) && publicSource(path)) files[path] = oid
      }
      let dirty = false
      try { git('diff', '--quiet', 'HEAD', '--') } catch { dirty = true }
      // Only paths inside this build's outDir are excluded from input checks.
      const isInput = (path) => {
        const nativeRelative = relative(outputDir, resolve(repoRoot, path))
        const outputRelative = nativeRelative.replaceAll('\\', '/')
        return publicSource(path) && (isAbsolute(nativeRelative) || outputRelative === '..' || outputRelative.startsWith('../'))
      }
      const untracked = git('ls-files', '-z', '--others', '--exclude-standard', '--', 'frontend').split('\0').filter(Boolean)
      if (untracked.some(isInput)) dirty = true
      const inputs = {}
      const names = git('ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', 'frontend').split('\0').filter(Boolean)
      for (const path of names.filter(isInput).sort()) {
        const absolute = resolve(repoRoot, path)
        inputs[path] = existsSync(absolute) ? digest(readFileSync(absolute)) : null
      }
      return { source_commit: commit, source_tree: tree, source_dirty: dirty, source_files: files, inputs }
    } catch {
      // Standalone/Docker frontend builds remain supported. Their output has
      // no source binding and cannot be accepted by the strict release packager.
      return { source_commit: null, source_tree: null, source_dirty: true, source_files: {}, inputs: {} }
    }
  }
  return {
    name: 'testhub-release-receipt',
    apply: 'build',
    configResolved(config) {
      repoRoot = resolve(config.root, '..')
      outputDir = resolve(config.root, config.build.outDir)
      configMode = config.mode
    },
    buildStart() { initial = snapshot() },
    writeBundle() {
      const current = snapshot()
      if (JSON.stringify(current) !== JSON.stringify(initial)) throw new Error('Source inputs changed during frontend build; rebuild before release')
      const files = {}
      const visit = (directory) => {
        for (const entry of readdirSync(directory, { withFileTypes: true })) {
          const path = join(directory, entry.name)
          if (entry.isSymbolicLink()) throw new Error('Frontend output contains a symlink')
          if (entry.isDirectory()) visit(path)
          else if (entry.name !== receiptName) {
            if (entry.name.startsWith('.env') || /\.(pem|key)$/i.test(entry.name)) throw new Error('Frontend output contains a private configuration/key path')
            files[relative(outputDir, path).replaceAll('\\', '/')] = digest(readFileSync(path))
          }
        }
      }
      visit(outputDir)
      const { inputs: _inputs, ...binding } = current
      writeFileSync(join(outputDir, receiptName), JSON.stringify({ schema_version: 1, mode: configMode, ...binding, files }, null, 2) + '\n')
    },
  }
}
