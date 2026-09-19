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
const UV_VERSION = "0.12.13";
const UV_RELEASE_BASE_URL = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}`;
const UV_ARCHIVES = {
  "darwin:arm64": {
    target: "aarch64-apple-darwin",
    sha256: "7e6ddb9316acc00f2296c82ff4d99977870ee34b2f0ddcae9444d714db9364ed",
  },
  "darwin:x64": {
    target: "x86_64-apple-darwin",
    sha256: "5e287ef61cb6a9b61b3a83fef124fd143e400468a7dac794230147a810e17119",
  },
  "linux:arm64:gnu": {
    target: "aarch64-unknown-linux-gnu",
    sha256: "2eaa5d94f5db7b3a1a092156b9420459e42ab0217d917fe74a876309cef9b5e9",
  },
  "linux:arm64:musl": {
    target: "aarch64-unknown-linux-musl",
    sha256: "f44bc1037a17889fe562fffd2002d4ed108e499fbe68b4f022af244dc7b8244f",
  },
  "linux:x64:gnu": {
    target: "x86_64-unknown-linux-gnu",
    sha256: "745765a3b6e360ad76743599ae5c42e9278c7edf8bbff9fc76d05bf2623a04dd",
  },
  "linux:x64:musl": {
    target: "x86_64-unknown-linux-musl",
    sha256: "4e2bfd0c9007b1032a50e539e965fd0a6037d87ad93ae1580d220a92d4c94098",
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
