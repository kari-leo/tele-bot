import test from 'node:test'
import assert from 'node:assert/strict'
import { validateManifest } from '../../tooling/check-artifacts.mjs'
import {compatibility} from '../../tooling/common.mjs'

const files = ['package/lib/index.js', 'package/lib/index.d.ts', 'package/cordis.patch.yml']
const valid = () => ({
  type: 'module', main: 'lib/index.js', types: 'lib/index.d.ts',
  exports: { '.': { types: './lib/index.d.ts', default: './lib/index.js' } },
  engines: { dsh: compatibility.candidate }, dsh: { bundle: { patch: './cordis.patch.yml' } },
  peerDependencies: { '@telebot/core': '0.1.0', '@deepseek-ai/cordis': compatibility.hostPeers['@deepseek-ai/cordis'] },
})

test('release guard rejects nonportable dependencies and duplicate host ownership', () => {
  for (const spec of ['workspace:*', 'link:../../plugins/core', 'file:D:/development/core']) {
    const manifest = valid()
    manifest.dependencies = { helper: spec }
    assert.throws(() => validateManifest(manifest, files), /Nonportable/)
  }
  const manifest = valid()
  manifest.dependencies = { '@deepseek-ai/cordis': '4.0.6-alpha.1' }
  assert.throws(() => validateManifest(manifest, files), /must remain a peer/)
})

test('release guard detects absent runtime resources and accidental data/source inclusion', () => {
  assert.throws(() => validateManifest(valid(), files.slice(0, 2)), /Missing shipped target/)
  for (const filename of ['package/src/index.ts', 'package/.env', 'package/data/knowledge.sqlite', '../escape.js']) {
    assert.throws(() => validateManifest(valid(), [...files, filename]))
  }
  assert.doesNotThrow(() => validateManifest(valid(), files))
  assert.throws(() => validateManifest({ ...valid(), name: '@telebot/feishu' }, [...files, 'package/lib/business/adapters/oauth.js']), /Removed OAuth/)
})
