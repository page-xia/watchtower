import assert from "node:assert/strict"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { build } from "esbuild"
import { readFile } from "node:fs/promises"

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const projectRoot = path.resolve(__dirname, "..")
const tempDir = await mkdtemp(path.join(tmpdir(), "watchtower-client-identity-"))

try {
  const outfile = path.join(tempDir, "clientIdentity.mjs")
  await build({
    entryPoints: [path.join(projectRoot, "src", "lib", "clientIdentity.ts")],
    bundle: true,
    format: "esm",
    platform: "node",
    outfile,
  })

  const module = await import(pathToFileURL(outfile).href)
  const storage = new Map()
  const fakeWindow = {
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
    },
  }
  const first = module.clientIdFromStorage(fakeWindow)
  const second = module.clientIdFromStorage(fakeWindow)

  assert.equal(module.CLIENT_ID_KEY, "watchtower.client-id.v1")
  assert.match(first, /^[A-Za-z0-9_-]{8,64}$/)
  assert.equal(first, second)
  globalThis.window = {
    localStorage: {
      getItem: () => { throw new Error("storage disabled") },
      setItem: () => { throw new Error("storage disabled") },
    },
  }
  assert.equal(module.getClientId(), module.getClientId())
  delete globalThis.window

  const revisionOutfile = path.join(tempDir, "personalizationRevision.mjs")
  await build({
    entryPoints: [path.join(projectRoot, "src", "lib", "personalizationRevision.ts")],
    bundle: true,
    format: "esm",
    platform: "node",
    outfile: revisionOutfile,
  })
  const revision = await import(pathToFileURL(revisionOutfile).href)
  assert.equal(revision.shouldRefreshPersonalizationRevision(11, 10), false)
  assert.equal(revision.shouldRefreshPersonalizationRevision(11, 13), true)

  const apiOutfile = path.join(tempDir, "api.mjs")
  await build({
    entryPoints: [path.join(projectRoot, "src", "lib", "api.ts")],
    bundle: true,
    format: "esm",
    platform: "browser",
    outfile: apiOutfile,
  })
  const api = await import(pathToFileURL(apiOutfile).href)
  assert.match(api.clientHeaders().get("X-Client-ID"), /^[A-Za-z0-9_-]{8,64}$/)
  const requests = []
  globalThis.fetch = async (input, init) => {
    requests.push({ input: String(input), headers: new Headers(init?.headers), body: init?.body })
    return new Response(JSON.stringify({}), { status: 200 })
  }
  await api.getTerminal({ watchlistCodes: ["300476"] })
  await api.getSignalChart("300476", ["300476"])
  assert.equal(requests[0].headers.has("X-Client-ID"), true)
  assert.equal(requests[0].input.includes("watchlist_codes"), false)
  assert.equal(requests[1].input.includes("watchlist_codes"), false)

  const pushOutfile = path.join(tempDir, "pushSubscription.mjs")
  await build({
    entryPoints: [path.join(projectRoot, "src", "lib", "pushSubscription.ts")],
    bundle: true,
    format: "esm",
    platform: "browser",
    outfile: pushOutfile,
  })
  const push = await import(pathToFileURL(pushOutfile).href)
  await push.fetchPushSubscription()
  await push.savePushSubscription({ webhook_url: "https://example.test/hook", enabled: true, codes: ["300476"] })
  await push.sendTestPush("https://example.test/hook")
  for (const request of requests.slice(2)) {
    assert.equal(request.headers.has("X-Client-ID"), true)
    assert.equal(request.input.includes("client_id"), false)
    assert.equal(String(request.body ?? "").includes("client_id"), false)
  }

  const [appSource, detailSource] = await Promise.all([
    readFile(path.join(projectRoot, "src", "App.tsx"), "utf8"),
    readFile(path.join(projectRoot, "src", "components", "detail", "StockDetail.tsx"), "utf8"),
  ])
  assert.match(appSource, /personalizationHydrated/)
  assert.match(appSource, /personalization_status === "unavailable"/)
  assert.doesNotMatch(detailSource, /\{ code, watchlistCodes \}/)
} finally {
  await rm(tempDir, { recursive: true, force: true })
}
