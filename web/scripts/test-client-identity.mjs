import assert from "node:assert/strict"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { build } from "esbuild"

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
    requests.push({ input: String(input), headers: new Headers(init?.headers) })
    return new Response(JSON.stringify({}), { status: 200 })
  }
  await api.getTerminal({ watchlistCodes: ["300476"] })
  await api.getSignalChart("300476", ["300476"])
  assert.equal(requests[0].headers.has("X-Client-ID"), true)
  assert.equal(requests[0].input.includes("watchlist_codes"), false)
  assert.equal(requests[1].input.includes("watchlist_codes"), false)
} finally {
  await rm(tempDir, { recursive: true, force: true })
}
