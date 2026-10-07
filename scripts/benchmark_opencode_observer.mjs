// Native plugin hooks only: no tools, prompts, argument mutation, or decisions.
import { spawn } from "node:child_process";

export default async ({ directory }) => {
  const send = (event) => new Promise((resolve) => {
    const child = spawn("python3", ["/opt/benchmark-native-hook.py"], {
      stdio: ["pipe", "ignore", "ignore"],
    });
    child.on("error", () => resolve());
    child.on("close", () => resolve());
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify({ cwd: directory, ...event }));
  });
  return {
    "tool.execute.before": async (input, output) => {
      await send({ hook_event_name: "PreToolUse", tool_name: input.tool,
        tool_use_id: input.callID, session_id: input.sessionID, tool_input: output.args });
    },
    "tool.execute.after": async (input, output) => {
      await send({ hook_event_name: "PostToolUse", tool_name: input.tool,
        tool_use_id: input.callID, session_id: input.sessionID, tool_input: input.args,
        tool_response: output });
    },
  };
};
