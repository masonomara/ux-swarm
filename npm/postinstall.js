#!/usr/bin/env node

const { execSync, spawnSync } = require("child_process");
const path = require("path");
const os = require("os");
const fs = require("fs");

const VENV_DIR = path.join(os.homedir(), ".local", "share", "ux-swarm", "venv");
const isWindows = process.platform === "win32";
const venvBin = isWindows ? path.join(VENV_DIR, "Scripts") : path.join(VENV_DIR, "bin");
const venvPython = path.join(venvBin, isWindows ? "python.exe" : "python");
const venvPip = path.join(venvBin, isWindows ? "pip.exe" : "pip");

function findPython() {
  for (const candidate of ["python3", "python"]) {
    const result = spawnSync(candidate, ["--version"], { encoding: "utf8" });
    if (result.status === 0) {
      const match = result.stdout.match(/(\d+)\.(\d+)/);
      if (match && (parseInt(match[1]) > 3 || (parseInt(match[1]) === 3 && parseInt(match[2]) >= 11))) {
        return candidate;
      }
    }
  }
  return null;
}

const python = findPython();
if (!python) {
  console.error("ux-swarm requires Python 3.11+. Please install it from https://python.org");
  process.exit(1);
}

console.log("Setting up ux-swarm environment...");

fs.mkdirSync(path.dirname(VENV_DIR), { recursive: true });

if (!fs.existsSync(VENV_DIR)) {
  console.log("Creating virtual environment...");
  const result = spawnSync(python, ["-m", "venv", VENV_DIR], { stdio: "inherit" });
  if (result.status !== 0) {
    console.error("Failed to create virtual environment.");
    process.exit(1);
  }
}

console.log("Installing ux-swarm...");
const install = spawnSync(venvPip, ["install", "--upgrade", "ux-swarm"], { stdio: "inherit" });
if (install.status !== 0) {
  console.error("Failed to install ux-swarm.");
  process.exit(1);
}

console.log("ux-swarm installed successfully. Run: swarm --help");
