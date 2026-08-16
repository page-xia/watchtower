import assert from "node:assert/strict"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { build } from "esbuild"

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const projectRoot = path.resolve(__dirname, "..")
const tempDir = await mkdtemp(path.join(tmpdir(), "watchtower-market-refresh-"))

try {
  const outfile = path.join(tempDir, "marketRefresh.mjs")
  await build({
    entryPoints: [path.join(projectRoot, "src", "lib", "marketRefresh.ts")],
    bundle: true,
    format: "esm",
    platform: "browser",
    outfile,
  })

  const module = await import(pathToFileURL(outfile).href)

  assert.deepEqual(
    module.pollingDecision({
      baseIntervalMs: 10000,
      session: "morning",
      hasData: true,
      documentHidden: false,
    }),
    { enabled: true, intervalMs: 10000 },
  )

  assert.deepEqual(
    module.pollingDecision({
      baseIntervalMs: 10000,
      session: "lunch_break",
      hasData: true,
      documentHidden: false,
    }),
    { enabled: true, intervalMs: 30000 },
  )

  assert.deepEqual(
    module.pollingDecision({
      baseIntervalMs: 10000,
      session: "post_close",
      hasData: true,
      documentHidden: false,
    }),
    { enabled: false, intervalMs: null },
  )

  assert.deepEqual(
    module.pollingDecision({
      baseIntervalMs: 10000,
      session: "post_close",
      hasData: false,
      documentHidden: false,
    }),
    { enabled: true, intervalMs: null },
  )

  assert.deepEqual(
    module.pollingDecision({
      baseIntervalMs: 10000,
      policy: {
        traffic_mode: "finalizing",
        should_poll: true,
        final_refresh: true,
        poll_interval_ms: 30000,
      },
      hasData: true,
      documentHidden: false,
    }),
    { enabled: false, intervalMs: null },
  )

  assert.equal(
    module.shouldReconnectTerminalStream({
      market: { frozen: true },
      source_status: { market_session: "post_close" },
    }),
    false,
  )

  assert.equal(
    module.shouldReconnectTerminalStream({
      market: { frozen: false },
      source_status: { market_session: "afternoon" },
    }),
    true,
  )
} finally {
  await rm(tempDir, { recursive: true, force: true })
}
