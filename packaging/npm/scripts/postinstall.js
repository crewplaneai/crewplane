#!/usr/bin/env node
"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const packageRoot = path.resolve(__dirname, "..");
const packageJson = require(path.join(packageRoot, "package.json"));
const packageName = packageJson.crewplane.pythonPackage;
const packageVersion =
  process.env.CREWPLANE_VERSION || packageJson.crewplane.pythonPackageVersion;
const DEFAULT_PYTHON = "3.13";
// BEGIN GENERATED UV BOOTSTRAP METADATA
const UV_VERSION = "0.13.7";
const UV_RELEASE_BASE_URL = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}`;
const UV_ARCHIVES = {
  "darwin:arm64": {
    target: "aarch64-apple-darwin",
    sha256: "81b811b57e4624df7ed3aac148e0359100d9962f3d9dce00aec533dc2e086731",
  },
  "darwin:x64": {
    target: "x86_64-apple-darwin",
    sha256: "20c47e4c1f630b8abaff8c291fa9b9aa300fef92c4fbff95adf198b55287cda7",
  },
  "linux:arm64:gnu": {
    target: "aarch64-unknown-linux-gnu",
    sha256: "71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc",
  },
  "linux:arm64:musl": {
    target: "aarch64-unknown-linux-musl",
    sha256: "2d3ec076a0c4c0e8250bb6418c44080828b544f1a3be0b6e46adf9fb6923de45",
  },
  "linux:x64:gnu": {
    target: "x86_64-unknown-linux-gnu",
    sha256: "c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83",
  },
  "linux:x64:musl": {
    target: "x86_64-unknown-linux-musl",
    sha256: "287491da03f2a785c70fa75b13e1f849918c2f2b5415fe84876b8214eeb792f9",
  },
};
// END GENERATED UV BOOTSTRAP METADATA
const venvDir = path.join(packageRoot, ".venv");

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    stdio: "inherit",
    ...options,
    env: { ...process.env, ...options.env },
  });
  if (result.error) {
    throw new Error(`failed to run ${command}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`${command} exited with status ${result.status}`);
  }
}

function commandWorks(command, args) {
  const result = spawnSync(command, args, { stdio: "ignore" });
  return !result.error && result.status === 0;
}

function ensureSupportedPlatform() {
  if (process.platform === "win32") {
    throw new Error("native Windows is not supported by Crewplane npm wrapper; use WSL");
  }
}

function selectUvArchive() {
  let platform = `${process.platform}:${process.arch}`;
  if (process.platform === "linux") {
    const report = process.report?.getReport();
    const libc = report?.header?.glibcVersionRuntime ? "gnu" : "musl";
    platform = `${platform}:${libc}`;
  }
  const archive = UV_ARCHIVES[platform];
  if (!archive) {
    throw new Error(`unsupported platform for automatic uv installation: ${platform}`);
  }
  return archive;
}

function downloadFile(url, destination) {
  if (commandWorks("curl", ["--version"])) {
    run("curl", [
      "--proto",
      "=https",
      "--tlsv1.2",
      "-LsSf",
      "-o",
      destination,
      url,
    ]);
  } else if (commandWorks("wget", ["--version"])) {
    run("wget", ["-qO", destination, url]);
  } else {
    throw new Error("curl or wget is required to bootstrap uv");
  }
}

function verifyFileSha256(file, expectedSha256) {
  const actualSha256 = crypto
    .createHash("sha256")
    .update(fs.readFileSync(file))
    .digest("hex");
  if (actualSha256 !== expectedSha256) {
    throw new Error("uv archive checksum mismatch");
  }
}

function installUvBinaries(tempDir, target) {
  const archiveDir = path.join(tempDir, `uv-${target}`);
  const installDir = path.join(os.homedir(), ".local", "bin");
  fs.mkdirSync(installDir, { recursive: true });
  for (const executable of ["uv", "uvx"]) {
    const source = path.join(archiveDir, executable);
    if (!fs.existsSync(source)) {
      throw new Error(`uv archive did not contain the ${executable} executable`);
    }
    const destination = path.join(installDir, executable);
    fs.copyFileSync(source, destination);
    fs.chmodSync(destination, 0o755);
  }
}

function bootstrapUv() {
  console.error("uv was not found; installing uv for the current user without sudo.");
  const { target, sha256 } = selectUvArchive();
  const archiveName = `uv-${target}.tar.gz`;
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "crewplane-uv-"));
  try {
    const archivePath = path.join(tempDir, archiveName);
    downloadFile(`${UV_RELEASE_BASE_URL}/${archiveName}`, archivePath);
    verifyFileSha256(archivePath, sha256);
    run("tar", ["-xzf", archivePath, "-C", tempDir]);
    installUvBinaries(tempDir, target);
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

function locateUv() {
  if (process.env.CREWPLANE_UV_BIN) {
    if (!fs.existsSync(process.env.CREWPLANE_UV_BIN)) {
      throw new Error(`CREWPLANE_UV_BIN does not exist: ${process.env.CREWPLANE_UV_BIN}`);
    }
    return process.env.CREWPLANE_UV_BIN;
  }

  if (commandWorks("uv", ["--version"])) {
    return "uv";
  }

  bootstrapUv();

  const candidates = [
    path.join(os.homedir(), ".local", "bin", "uv"),
    path.join(os.homedir(), ".cargo", "bin", "uv"),
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  throw new Error("uv was installed but could not be found under the user tool directories");
}

function pythonPath() {
  return path.join(venvDir, "bin", "python");
}

function installCrewplane(uv) {
  const venvArgs = [
    "venv",
    "--python",
    process.env.CREWPLANE_INSTALL_PYTHON || DEFAULT_PYTHON,
  ];
  venvArgs.push(venvDir);
  run(uv, venvArgs);

  const args = ["pip", "install", "--python", pythonPath()];
  if (process.env.CREWPLANE_INSTALL_FIND_LINKS) {
    args.push("--find-links", process.env.CREWPLANE_INSTALL_FIND_LINKS);
  }
  if (
    process.env.CREWPLANE_INSTALL_NO_INDEX &&
    process.env.CREWPLANE_INSTALL_NO_INDEX !== "0"
  ) {
    args.push("--no-index");
  }
  args.push(`${packageName}==${packageVersion}`);
  run(uv, args);
}

try {
  ensureSupportedPlatform();
  installCrewplane(locateUv());
} catch (error) {
  console.error(`crewplane postinstall failed: ${error.message}`);
  console.error("Provider CLIs and credentials are not managed by this npm package.");
  process.exit(1);
}
