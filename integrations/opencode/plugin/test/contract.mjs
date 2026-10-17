import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const plugin = (await import(pathToFileURL(join(root, "dist", "index.js")).href)).default;

assert.equal(typeof plugin, "object");
assert.equal(plugin.id, "kwin-mcp-opencode");
assert.equal(typeof plugin.setup, "function");

const mcp = new Map([["user-server", { type: "local", command: ["user-tool"] }]]);
const skills = new Map();
const mcpTransform = async (callback) => {
  callback({
    get: (id) => mcp.get(id),
    set: (id, config) => mcp.set(id, config),
  });
};
const skillTransform = async (callback) => {
  callback({
    get: (id) => skills.get(id),
    add: (skill) => skills.set(skill.id, skill),
  });
};

const context = {
  mcp: { transform: mcpTransform },
  skill: { transform: skillTransform },
};

await plugin.setup(context);
await plugin.setup(context);

assert.deepEqual(mcp.get("user-server"), { type: "local", command: ["user-tool"] });
assert.deepEqual(mcp.get("kwin-mcp"), { type: "local", command: ["uvx", "kwin-mcp"] });
assert.equal(mcp.size, 2);

const skill = skills.get("kwin-desktop-automation");
assert.ok(skill);
assert.equal(skill.id, "kwin-desktop-automation");
assert.equal(skill.name, "kwin-desktop-automation");
assert.ok(skill.path.endsWith("skill/kwin-desktop-automation/SKILL.md"));
assert.ok(skill.content.includes("Drive Linux KDE Plasma 6 Wayland desktops"));
assert.equal(skills.size, 1);

const source = await readFile(join(root, "src", "index.ts"), "utf8");
assert.ok(source.includes("catch {"), "missing bundled skills must be handled safely");

console.log("OpenCode V2 plugin contract checks passed");
