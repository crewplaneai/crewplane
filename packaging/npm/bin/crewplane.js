#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const packageRoot = path.resolve(__dirname, "..");

if (process.platform === "win32") {
  console.error("native Windows is not supported by the crewplane npm wrapper; use WSL.");
  process.exit(1);
}

const commandPath = path.join(packageRoot, ".venv", "bin", "orchestrator");

if (!fs.existsSync(commandPath)) {
  console.error("crewplane is installed, but its private Python environment is missing.");
  console.error("npm lifecycle scripts may have been disabled during installation.");
  console.error("Run `npm rebuild crewplane` with lifecycle scripts enabled, then retry.");
  process.exit(1);
}

const result = spawnSync(commandPath, process.argv.slice(2), { stdio: "inherit" });

if (result.error) {
  console.error(`failed to run ${commandPath}: ${result.error.message}`);
  process.exit(1);
}

if (result.signal) {
  process.kill(process.pid, result.signal);
}

process.exit(result.status === null ? 1 : result.status);
