#!/usr/bin/env node

const { spawnSync } = require("child_process");
const path = require("path");
const os = require("os");
const fs = require("fs");

const VENV_DIR = path.join(os.homedir(), ".local", "share", "ux-swarm", "venv");
const isWindows = process.platform === "win32";
const venvBin = isWindows ? path.join(VENV_DIR, "Scripts") : path.join(VENV_DIR, "bin");
const binary = path.join(venvBin, isWindows ? "swarm.exe" : "swarm");

if (!fs.existsSync(binary)) {
  console.error("ux-swarm is not installed. Try reinstalling: npm install -g ux-swarm");
  process.exit(1);
}

const result = spawnSync(binary, process.argv.slice(2), { stdio: "inherit" });
process.exit(result.status ?? 1);
