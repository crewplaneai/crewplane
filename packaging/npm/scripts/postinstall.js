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
const UV_VERSION = "0.12.5";
const UV_RELEASE_BASE_URL = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}`;
const UV_ARCHIVES = {
  "darwin:arm64": {
    target: "aarch64-apple-darwin",
    sha256: "5bb0e5fe008a773c3dbcb97ff79cd89e1241464fe9d2f986d52ad8f1b037bd62",
  },
  "darwin:x64": {
    target: "x86_64-apple-darwin",
    sha256: "b3b2137477cf96c9686ebfb71524614cec780c673fd73e59bce099aef02e70e8",
  },
  "linux:arm64:gnu": {
    target: "aarch64-unknown-linux-gnu",
    sha256: "9bf43b4d1a07665bf64d4c4e710930b382321a785e0eb10aac07f46471f86a31",
  },
  "linux:arm64:musl": {
    target: "aarch64-unknown-linux-musl",
    sha256: "8767a0e77f2cd45436401b1b42bf7e9ed5a4a91a74a5305d6fe93249d0f6dbc5",
  },
  "linux:x64:gnu": {
    target: "x86_64-unknown-linux-gnu",
    sha256: "68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2",
  },
  "linux:x64:musl": {
    target: "x86_64-unknown-linux-musl",
    sha256: "a4742988791c9aeae68c78150d6cba762062ad2a47e53738c2779d2b596bfcdb",
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
