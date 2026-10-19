import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const packedArtifact = process.argv[2];
assert.ok(packedArtifact, "usage: node test/opencode-smoke.mjs <packed-tarball>");
const artifactPath = resolve(packedArtifact);
const timeoutMs = 45_000;
const pollIntervalMs = 250;
const password = `ci-${randomUUID()}`;
let serverProcess;
let serverOutput = "";

function sanitize(text) {
  return text
    .replaceAll(password, "[REDACTED]")
    .replaceAll(/Basic\s+[^\s]+/gi, "Basic [REDACTED]")
    .replaceAll(/authorization:\s*[^\r\n]+/gi, "authorization: [REDACTED]");
}

function sleep(ms) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
}

function run(command, args, { timeoutMs: commandTimeoutMs = 120_000, ...options } = {}) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"], ...options });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
    }, commandTimeoutMs);
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("close", (code, signal) => {
      clearTimeout(timer);
      if (timedOut) {
        reject(new Error(`${command} ${args.join(" ")} timed out after ${commandTimeoutMs}ms`));
      } else if (code !== 0) {
        reject(
          new Error(
            `${command} ${args.join(" ")} exited with ${signal ? `signal ${signal}` : `code ${code}`}\\n${sanitize(stdout)}${sanitize(stderr)}`,
          ),
        );
      } else {
        resolvePromise({ stdout, stderr });
      }
    });
  });
}

async function freePort() {
  const listener = createServer();
  await new Promise((resolvePromise, reject) => {
    listener.once("error", reject);
    listener.listen(0, "127.0.0.1", resolvePromise);
  });
  const address = listener.address();
  assert.ok(address && typeof address === "object");
  const port = address.port;
  await new Promise((resolvePromise, reject) => listener.close((error) => (error ? reject(error) : resolvePromise())));
  return port;
}
function registryEntries(value) {
  const data = value?.data ?? value;
  return Array.isArray(data) ? data : [];
}

async function request(baseUrl, endpoint, directory) {
  const response = await fetch(`${baseUrl}${endpoint}`, {
    signal: AbortSignal.timeout(5_000),
    headers: {
      authorization: `Basic ${Buffer.from(`opencode:${password}`).toString("base64")}`,
      "x-opencode-directory": encodeURIComponent(directory),
    },
  });
  const text = await response.text();
  let body;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(`${endpoint} returned invalid JSON (HTTP ${response.status})`);
  }
  if (!response.ok) {
    throw new Error(`${endpoint} returned HTTP ${response.status}: ${sanitize(text)}`);
  }
  return body;
}

async function stopServer() {
  if (!serverProcess) return;
  if (!serverProcess.killed) serverProcess.kill("SIGTERM");
  await Promise.race([serverProcess.closed, sleep(5_000)]);
  if (!serverProcess.closedValue) serverProcess.kill("SIGKILL");
  await serverProcess.closed;
  serverProcess = undefined;
}

try {
  const consumer = await mkdtemp(join(tmpdir(), "kwin-mcp-opencode-smoke-"));
  try {
    await writeFile(join(consumer, "opencode.json"), JSON.stringify({
      plugins: [`@isac322/kwin-mcp-opencode@file:${artifactPath}`],
    }));

    await run("npm", ["install", "--prefix", consumer, "--no-audit", "--no-fund", artifactPath, "@opencode/cli@2.0.32"]);
    const packageJson = JSON.parse(
      await readFile(join(consumer, "node_modules", "@isac322", "kwin-mcp-opencode", "package.json"), "utf8"),
    );
    const sourcePackageJson = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
    assert.equal(packageJson.version, sourcePackageJson.version, "packed plugin version must match source");
    const fixture = await readFile(
      new URL("../skill/kwin-desktop-automation/SKILL.md", import.meta.url),
      "utf8",
    );
    const expectedSkillBody = fixture.replace(/\r\n/g, "\n").replace(/^---[\s\S]*?---\s*/, "").trim();

    const home = join(consumer, "home");
    const xdgConfig = join(consumer, "xdg-config");
    const xdgData = join(consumer, "xdg-data");
    const xdgCache = join(consumer, "xdg-cache");
    const xdgState = join(consumer, "xdg-state");
    await Promise.all([home, xdgConfig, xdgData, xdgCache, xdgState].map((path) => mkdir(path, { recursive: true })));
    const port = await freePort();
    const env = {
      ...process.env,
      PATH: `${join(consumer, "node_modules", ".bin")}:${process.env.PATH ?? ""}`,
      HOME: home,
      XDG_CONFIG_HOME: xdgConfig,
      XDG_DATA_HOME: xdgData,
      XDG_CACHE_HOME: xdgCache,
      XDG_STATE_HOME: xdgState,
      OPENCODE_SERVER_PASSWORD: password,
    };
    const cli = join(consumer, "node_modules", ".bin", "opencode");
    const child = spawn(cli, ["serve", "--hostname", "127.0.0.1", "--port", String(port)], {
      cwd: consumer,
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let closedResolve;
    let closedValue = false;
    const closed = new Promise((resolvePromise) => {
      closedResolve = resolvePromise;
    });
    child.stdout.on("data", (chunk) => (serverOutput += chunk));
    child.stderr.on("data", (chunk) => (serverOutput += chunk));
    child.once("error", (error) => {
      serverOutput += String(error);
      closedValue = true;
      closedResolve();
    });
    child.once("close", (code, signal) => {
      serverOutput += `\nOpenCode exited (${signal ?? code})`;
      closedValue = true;
      closedResolve();
    });
    serverProcess = { kill: (signal) => child.kill(signal), closed, closedValue: false };
    Object.defineProperty(serverProcess, "closedValue", { get: () => closedValue });

    const baseUrl = `http://127.0.0.1:${port}`;
    const deadline = Date.now() + timeoutMs;
    let latest;
    let completed = false;
    while (Date.now() < deadline) {
      if (closedValue) {
        throw new Error(`OpenCode server exited before registries were ready:\n${sanitize(serverOutput)}`);
      }

      let snapshot;
      try {
        const info = await request(baseUrl, "/api/info", consumer);
        const [plugins, skills, mcp] = await Promise.all([
          request(baseUrl, "/api/plugin", consumer),
          request(baseUrl, "/api/skill", consumer),
          request(baseUrl, "/api/mcp", consumer),
        ]);
        snapshot = { info, plugins: registryEntries(plugins), skills: registryEntries(skills), mcp: registryEntries(mcp) };
      } catch (error) {
        if (Date.now() >= deadline) throw error;
        await sleep(pollIntervalMs);
        continue;
      }

      latest = snapshot;
      assert.equal(latest.info?.version, "2.0.32", "OpenCode server must be V2.0.32");
      const plugin = latest.plugins.find((entry) => entry.id === "kwin-mcp-opencode");
      if (plugin?.state?.status === "failed") {
        throw new Error(`OpenCode plugin failed to load: ${JSON.stringify(plugin)}`);
      }
      const skill = latest.skills.find((entry) => entry.id === "kwin-desktop-automation");
      const mcp = latest.mcp.find((entry) => entry.name === "kwin-mcp" || entry.id === "kwin-mcp");
      if (plugin?.state?.status === "active" && skill && mcp) {
        assert.match(skill.description ?? "", /launch|click|desktop|kwin-mcp/i);
        assert.match(skill.path ?? "", /kwin-desktop-automation[\\/]SKILL\.md$/);
        const content = String(skill.content ?? "").replace(/^---[\s\S]*?---\s*/, "").trim();
        assert.ok(content.length > 100, "registered skill content must contain a meaningful body");
        assert.match(content, /Drive Linux KDE Plasma 6 Wayland desktops/);
        assert.equal(content.replace(/\r\n/g, "\n"), expectedSkillBody, "registered skill must match bundled fixture");
        console.log(`OpenCode V2 server registered plugin ${plugin.id}, skill ${skill.id}, and MCP ${mcp.name ?? mcp.id}`);
        console.log(
          `MCP status: ${mcp.status?.status ?? mcp.status ?? mcp.state?.status ?? "unknown"} ` +
            "(registration accepted even when native uvx dependencies are unavailable)",
        );
        completed = true;
        break;
      }
      await sleep(pollIntervalMs);
    }
    assert.ok(completed, "OpenCode V2 registries did not become ready before the timeout");
    assert.ok(latest, "OpenCode server never became ready");
    const plugin = latest.plugins.find((entry) => entry.id === "kwin-mcp-opencode");
    assert.equal(plugin?.state?.status, "active", "plugin must become active");
    assert.ok(latest.skills.some((entry) => entry.id === "kwin-desktop-automation"), "skill must be registered");
    assert.ok(latest.mcp.some((entry) => entry.name === "kwin-mcp" || entry.id === "kwin-mcp"), "MCP must be registered");
  } finally {
    await stopServer();
    await rm(consumer, { recursive: true, force: true });
  }
} catch (error) {
  console.error(sanitize(error instanceof Error ? error.stack ?? error.message : String(error)));
  process.exitCode = 1;
}
