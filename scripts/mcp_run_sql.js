// 通过 cloudbase-mcp (stdio JSON-RPC) 执行一条 SQL。
// 用法: node scripts/mcp_run_sql.js "ALTER TABLE ..."
const { spawn } = require("child_process");
const path = require("path");
const os = require("os");

const toolName = process.argv[2];
const toolArgs = process.argv[3] ? JSON.parse(process.argv[3]) : {};

const cli = path.join(os.homedir(), "AppData/Roaming/npm/node_modules/@cloudbase/cloudbase-mcp/dist/cli.cjs");
const child = spawn(process.execPath, [cli, "--envId", "server-d2g7x597t019f5cb0"], {
  stdio: ["pipe", "pipe", "inherit"],
});

let buffer = "";
const pending = new Map();
let nextId = 1;

function send(method, params) {
  const id = nextId++;
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

child.stdout.on("data", (chunk) => {
  buffer += chunk.toString("utf8");
  let idx;
  while ((idx = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, idx).trim();
    buffer = buffer.slice(idx + 1);
    if (!line) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      continue;
    }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
    }
  }
});

(async () => {
  try {
    await send("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "ddl-runner", version: "0.1" },
    });
    child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
    if (!toolName) {
      const tools = await send("tools/list", {});
      for (const tool of tools.tools || []) {
        console.log(`${tool.name} :: ${(tool.description || "").slice(0, 80)}`);
      }
      return;
    }
    const result = await send("tools/call", {
      name: toolName,
      arguments: toolArgs,
    });
    const text = (result.content || []).map((c) => c.text || "").join("\n");
    console.log(text);
  } catch (err) {
    console.error("ERROR:", err.message);
    process.exitCode = 1;
  } finally {
    child.kill();
  }
})();
