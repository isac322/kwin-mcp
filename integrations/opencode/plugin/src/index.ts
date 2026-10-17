import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Plugin, Skill } from "@opencode/plugin";
import { AbsolutePath } from "@opencode/schema/schema";

const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url));
const SKILL_NAME = "kwin-desktop-automation";
const MCP_NAME = "kwin-mcp";
const SKILL_PATH = join(PLUGIN_DIR, "..", "skill", SKILL_NAME, "SKILL.md");

const MCP_CONFIG = {
  type: "local" as const,
  command: ["uvx", "kwin-mcp"],
};

function loadBundledSkill(): Skill.Info | null {
  if (!existsSync(SKILL_PATH)) {
    return null;
  }

  try {
    const raw = readFileSync(SKILL_PATH, "utf8");
    const frontmatter = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
    const body = frontmatter ? raw.slice(frontmatter[0].length) : raw;
    const name = frontmatter?.[1].match(/^name:\s*(.+)$/m)?.[1].trim() ?? SKILL_NAME;
    const description = frontmatter?.[1].match(/^description:\s*(.+)$/m)?.[1].trim();

    return {
      id: Skill.ID.make(SKILL_NAME),
      name: Skill.Name.make(name),
      ...(description ? { description } : {}),
      path: AbsolutePath.make(SKILL_PATH),
      content: body,
    };
  } catch {
    return null;
  }
}

export default Plugin.define({
  id: "kwin-mcp-opencode",
  async setup(ctx) {
    await ctx.mcp.transform((editor) => {
      if (!editor.get(MCP_NAME)) {
        editor.set(MCP_NAME, MCP_CONFIG);
      }
    });

    const skill = loadBundledSkill();
    if (skill) {
      await ctx.skill.transform((editor) => {
        if (!editor.get(SKILL_NAME)) {
          editor.add(skill);
        }
      });
    }
  },
});
