import { fileURLToPath } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'
import { dwcVitestConfig } from 'dwc-plugin-test-kit/vitest'

/**
 * Vitest for the DWC 3.7 frontend (Vue 3.5 + Vuetify 4).
 *
 * dwc-plugin-test-kit supplies the `@/…` → stub aliases for the modules DWC 3.7
 * externalises from a plugin bundle, happy-dom, and a deduped single copy of Vue/Vuetify.
 *
 * Tests are plain JS on purpose: DWC's plugin builder type-checks every `*.ts` under the
 * plugin directory against DWC's own sources, and test files importing `vitest` do not
 * belong in that check.
 */
const config = dwcVitestConfig({
    // vue() comes from here rather than from the kit so it resolves against this
    // repo's own node_modules.
    plugins: [vue()],
    test: {
        include: ['tests/**/*.test.js'],
        coverage: {
            include: ['src/**'],
        },
    },
})

// The kit's machine stub only covers model/isConnected/sendCode/getFileList; QA also
// needs startSbcPlugin (backend recovery) and request (daemon HTTP API). Patch the alias
// rather than passing a `resolve` override, which would replace the kit's dedupe list.
config.resolve.alias['@/stores/machine'] = fileURLToPath(
    new URL('./tests/dwc-stubs/machine.js', import.meta.url),
)

export default defineConfig(config)
