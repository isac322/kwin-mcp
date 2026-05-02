import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Plugin } from "@opencode-ai/plugin";

const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url));
const SKILL_NAME = "kwin-desktop-automation";
const MCP_NAME = "kwin-mcp";

const MCP_CONFIG = {
  type: "local" as const,
  command: ["uvx", "kwin-mcp"],
  enabled: true,
};

function findSkillsDir(): string | null {
  const dir = join(PLUGIN_DIR, "..", "skill");
  return existsSync(join(dir, SKILL_NAME, "SKILL.md")) ? dir : null;
}

const plugin: Plugin = async () => ({
  config: async (input) => {
    input.mcp ??= {};
    const mcp = input.mcp as Record<string, unknown>;
    if (!Object.prototype.hasOwnProperty.call(mcp, MCP_NAME)) {
      mcp[MCP_NAME] = MCP_CONFIG;
    }
    const skillsDir = findSkillsDir();
    if (skillsDir) {
      const cfg = input as typeof input & {
        skills?: { paths?: string[]; urls?: string[] };
      };
      cfg.skills ??= {};
      cfg.skills.paths ??= [];
      if (!cfg.skills.paths.includes(skillsDir)) {
        cfg.skills.paths.push(skillsDir);
      }
    }
  },
});

export default plugin;
