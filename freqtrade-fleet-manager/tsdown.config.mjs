/**
 * Build config: the host library (lib/index.js — ESM, dsh-family peers
 * external, resolved at runtime from the running dsh installation) and the
 * browser bundle (lib/client.js — one CJS closure factory registered through
 * window.__ModuleLoader__.load, with React/Cordis/slots externals preserved
 * from the loader module table).
 */
import { defineConfig } from 'tsdown'

const PLUGIN_ID = 'freqtrade-fleet-manager'

/**
 * Externals resolved from the browser loader module table: the shell's
 * platform modules (react, cordis, ui-slots) plus the settings-slot owner and
 * the client runtime whose `client` entry provides the inject face types.
 */
const CLIENT_EXTERNALS = [
  'react',
  'react/jsx-runtime',
  'react-dom',
  'react-dom/client',
  '@deepseek-ai/cordis',
  '@deepseek-ai/dsh-client-ui-slots',
  '@deepseek-ai/dsh-client-ui-settings',
  '@deepseek-ai/dsh-client-runtime/client',
]

export default defineConfig([
  {
    entry: { index: 'src/index.js' },
    outDir: 'lib',
    format: ['esm'],
    platform: 'node',
    target: 'node22',
    dts: false,
    sourcemap: true,
    clean: true,
    deps: { neverBundle: [/^@deepseek-ai\//] },
    outputOptions: { entryFileNames: '[name].js' },
  },
  {
    entry: { client: 'client/index.js' },
    outDir: 'lib',
    format: ['cjs'],
    platform: 'browser',
    target: ['es2022'],
    dts: false,
    sourcemap: true,
    clean: false,
    external: [...CLIENT_EXTERNALS],
    define: {
      'process.env.NODE_ENV': JSON.stringify(process.env.NODE_ENV ?? 'production'),
    },
    // tsdown auto-externalizes package dependencies; anything NOT in the
    // loader module table must inline instead — a require() the table cannot
    // answer is a guaranteed runtime throw, so the rule is the table list.
    noExternal: (id) => (CLIENT_EXTERNALS.includes(id) ? undefined : true),
    outputOptions: {
      entryFileNames: 'client.js',
      banner: `window.__ModuleLoader__.load({ id: ${JSON.stringify(PLUGIN_ID)}, factory: (require) => {`,
      intro: 'var module = { exports: {} }; var exports = module.exports;',
      footer: 'return module.exports; } });',
    },
  },
])