import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig, type Plugin } from "vite"

function stripTrailingWhitespace(value: string): string {
  return value.replace(/[ \t]+$/gm, "")
}

function stripGeneratedTrailingWhitespace(): Plugin {
  return {
    name: "strip-generated-trailing-whitespace",
    generateBundle(_options, bundle) {
      for (const output of Object.values(bundle)) {
        if (output.type === "chunk") {
          output.code = stripTrailingWhitespace(output.code)
        } else if (typeof output.source === "string") {
          output.source = stripTrailingWhitespace(output.source)
        }
      }
    },
  }
}

const config = {
  plugins: [react(), tailwindcss(), stripGeneratedTrailingWhitespace()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "../src/sag/web/static",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
}

export default defineConfig(config)
