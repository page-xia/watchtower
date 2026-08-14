import assert from "node:assert/strict"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { build } from "esbuild"

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const projectRoot = path.resolve(__dirname, "..")
const tempDir = await mkdtemp(path.join(tmpdir(), "watchtower-local-watchlist-"))

try {
  const outfile = path.join(tempDir, "localWatchlist.mjs")
  await build({
    entryPoints: [path.join(projectRoot, "src", "lib", "localWatchlist.ts")],
    bundle: true,
    format: "esm",
    platform: "browser",
    outfile,
  })

  const module = await import(pathToFileURL(outfile).href)
  assert.equal(typeof module.localWatchlistPlaceholders, "function")

  const [item] = module.localWatchlistPlaceholders([
    {
      code: "300476",
      name: "胜宏科技",
      themes: ["PCB"],
      core: true,
      position: false,
      notes: "",
    },
  ])

  assert.equal(item.code, "300476")
  assert.equal(item.name, "胜宏科技")
  assert.equal(item.sector, "PCB")
  assert.equal(item.watchlisted, true)
  assert.equal(item.core, true)
  assert.equal(Number.isNaN(item.price), true)
} finally {
  await rm(tempDir, { recursive: true, force: true })
}
