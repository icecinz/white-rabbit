// Convert the unchanged MIT Hermes client with the pinned Node built-in API.
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const sourceVendor = join(dirname(fileURLToPath(import.meta.url)), 'vendor', 'hermes_gateway')
const runtimeVendor = join(dirname(fileURLToPath(import.meta.url)), 'rabbit_runtime', 'hermes', 'vendor', 'hermes_gateway')
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex')

export function compileVendor() {
  const manifest = JSON.parse(readFileSync(join(runtimeVendor, 'upstream.json'), 'utf8'))
  if (process.version !== manifest.build.node_version) throw new Error('pinned_node_version_required')
  const source = readFileSync(join(sourceVendor, manifest.source.vendored_path))
  const license = readFileSync(join(runtimeVendor, manifest.license.vendored_path))
  if (sha256(source) !== manifest.source.sha256) throw new Error('upstream_source_sha_mismatch')
  if (sha256(license) !== manifest.license.sha256) throw new Error('upstream_license_sha_mismatch')
  const header = `// Generated from unchanged Hermes ${manifest.commit} ${manifest.source.upstream_path}.\n// SPDX-License-Identifier: MIT; see adjacent LICENSE.\n`
  const output = header + stripTypeScriptTypes(source.toString('utf8'), manifest.build.options)
  if (sha256(output) !== manifest.build.output_sha256) throw new Error('compiled_output_sha_mismatch')
  return { manifest, output, sha256: sha256(output), outputPath: join(runtimeVendor, manifest.build.output) }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const args = process.argv.slice(2)
  if (args.length > 1 || (args.length === 1 && args[0] !== '--check')) throw new Error('usage_build_or_check')
  const compiled = compileVendor()
  if (args[0] === '--check') {
    if (readFileSync(compiled.outputPath, 'utf8') !== compiled.output) throw new Error('compiled_artifact_mismatch')
  } else {
    writeFileSync(compiled.outputPath, compiled.output, 'utf8')
  }
  console.log(JSON.stringify({ mode: args[0] === '--check' ? 'check' : 'build', node: process.version,
    source_sha256: compiled.manifest.source.sha256, output_sha256: compiled.sha256,
    output_bytes: Buffer.byteLength(compiled.output) }))
}
