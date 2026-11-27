import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // Build output folders (.output, .output-e2e, .output-store) can hold
    // tool worktrees with their own copies of the tests; never run those.
    exclude: ["**/node_modules/**", "**/dist/**", "**/.output*/**", "**/.wxt/**"],
  },
});
