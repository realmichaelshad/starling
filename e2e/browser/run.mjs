/**
 * The one command: `node e2e/browser/run.mjs` from the repository root.
 *
 * 1. Builds the e2e extension into a stable path. Chrome derives an unpacked extension's id
 *    from that path, so the id is stable for as long as the checkout is.
 * 2. Launches Chrome once with nothing else running, only to read that id from the service
 *    worker's URL. The gateway has to be told which extension to trust BEFORE it starts.
 * 3. Starts the stack through `tools/stack_driver.py`, holding its standard input open. The
 *    driver stops everything the moment that pipe closes, which is the only shutdown signal
 *    that survives a crash, a Ctrl-C or a kill on Windows, where Node cannot send CTRL_BREAK.
 * 4. Runs the journeys with Playwright Test.
 * 5. Stops everything and proves nothing was left behind: the gateway's port is free, and
 *    every process the harness recorded is gone.
 *
 * Options: `--headed` (watch it), `--grep <pattern>`, `--journey <n>` (one file), `--skip-build`,
 * `--list`. Everything a run writes lands under `e2e/.run/browser/<run>/`.
 */
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import process from "node:process";
import { resolveBrowsersPath } from "./lib/browsers.mjs";
import {
  BROWSER_RUN_ROOT, EXTENSION_DIR, EXTENSION_OUT_DIR, GATEWAY_PORT, REPO_ROOT, SESSION_FILE,
  STATE_FILE, ensureDir, venvPython,
} from "./lib/paths.mjs";

const options = parseArgs(process.argv.slice(2));
const runId = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 15) + "-" + Math.random().toString(36).slice(2, 6);
const runDir = ensureDir(path.join(BROWSER_RUN_ROOT, runId));
const log = (...parts) => console.log(`[run ${new Date().toISOString().slice(11, 19)}]`, ...parts);

/** Everything started here, so a failure anywhere still takes all of it down. */
const started = { driver: null, runner: null };
let shuttingDown = false;

async function main() {
  resolveBrowsersPath();
  ensureDir(path.join(runDir, "screenshots"));
  ensureDir(path.join(runDir, "traces"));
  log(`run ${runId} -> ${path.relative(REPO_ROOT, runDir)}`);

  if (!options.skipBuild) buildExtension();
  else log("skipping the extension build (--skip-build)");
  if (!fs.existsSync(path.join(EXTENSION_DIR, "manifest.json"))) {
    throw new Error(`no extension build in ${EXTENSION_DIR}`);
  }

  const extensionId = await readExtensionId();
  log(`extension id ${extensionId}`);

  await requirePortFree(GATEWAY_PORT);
  const driver = await startStack(extensionId);
  log(`gateway ${driver.state.gateway_url} (run ${driver.state.run_id})`);

  fs.writeFileSync(SESSION_FILE, JSON.stringify({
    runId,
    runDir,
    extensionId,
    controlUrl: driver.controlUrl,
    controlToken: driver.controlToken,
    state: driver.state,
  }, null, 2) + "\n");

  const code = await runJourneys();
  await shutdown();
  const strays = await processCheck(driver.state);
  console.log("\n--- process check ---");
  console.log(JSON.stringify(strays, null, 2));
  if (strays.stray.length) {
    console.error("processes were left behind");
    return 1;
  }
  return code;
}

// --------------------------------------------------------------------------- steps

function buildExtension() {
  log("building the extension (wxt build --mode e2e)");
  ensureDir(EXTENSION_OUT_DIR);
  const started = Date.now();
  // Through the shell, as ONE fixed string: since Node 20.12 a `.cmd` file cannot be spawned
  // directly on Windows (EINVAL, null exit status), and `npx` is one there. Nothing in the
  // command comes from outside this file.
  const result = spawnSync(
    "npx --yes pnpm@11.9.0 --dir extension exec wxt build --mode e2e",
    { cwd: REPO_ROOT, env: { ...process.env, STARLING_OUT_DIR: EXTENSION_OUT_DIR }, stdio: "inherit", shell: true },
  );
  if (result.status !== 0) {
    throw new Error(`the extension build failed (exit ${result.status}${result.error ? `, ${result.error.message}` : ""})`);
  }
  log(`built in ${((Date.now() - started) / 1000).toFixed(1)}s`);
}

/**
 * Chrome computes an unpacked extension's id from its absolute path, so it cannot be known
 * before a build exists, and the gateway must be told it before it starts. One throwaway
 * launch, with no harness and no proxy, is the only way to ask.
 */
async function readExtensionId() {
  const { chromium } = await import("playwright");
  const profileDir = ensureDir(path.join(runDir, "profiles", "id-probe"));
  const context = await chromium.launchPersistentContext(profileDir, {
    channel: "chromium",
    headless: true,
    args: [`--disable-extensions-except=${EXTENSION_DIR}`, `--load-extension=${EXTENSION_DIR}`],
  });
  try {
    const worker = context.serviceWorkers()[0] ?? await context.waitForEvent("serviceworker", { timeout: 30_000 });
    return new URL(worker.url()).host;
  } finally {
    await context.close().catch(() => undefined);
  }
}

function startStack(extensionId) {
  return new Promise((resolve, reject) => {
    const driver = spawn(
      venvPython(),
      [path.join("e2e", "browser", "tools", "stack_driver.py"),
        "--gateway-port", String(GATEWAY_PORT),
        "--extension-id", extensionId,
        "--state", STATE_FILE],
      { cwd: REPO_ROOT, stdio: ["pipe", "pipe", "pipe"], env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONUTF8: "1" } },
    );
    started.driver = driver;
    const driverLog = fs.createWriteStream(path.join(runDir, "stack-driver.log"));
    let buffered = "";
    let settled = false;
    driver.stdout.setEncoding("utf8");
    driver.stdout.on("data", chunk => {
      driverLog.write(chunk);
      buffered += chunk;
      const ready = buffered.split("\n").find(line => line.startsWith("READY "));
      if (ready && !settled) {
        settled = true;
        const payload = JSON.parse(ready.slice("READY ".length));
        resolve({ controlUrl: payload.control_url, controlToken: payload.control_token, state: payload.state });
      }
    });
    driver.stderr.setEncoding("utf8");
    driver.stderr.on("data", chunk => driverLog.write(chunk));
    driver.on("exit", code => {
      if (!settled) {
        settled = true;
        reject(new Error(`the stack driver exited with ${code} before it was ready; see ${path.join(runDir, "stack-driver.log")}`));
      }
    });
  });
}

function runJourneys() {
  // Playwright's own command-line entry point, run by this same Node: no `npx`, so no `.cmd`
  // file to spawn on Windows and nothing fetched from a registry.
  const cli = path.join(REPO_ROOT, "e2e", "browser", "node_modules", "playwright", "cli.js");
  const args = [cli, "test", "--config", path.join("e2e", "browser", "playwright.config.mjs")];
  if (options.grep) args.push("--grep", options.grep);
  if (options.journey) args.push(`e2e/browser/tests/0${options.journey}`);
  if (options.list) args.push("--list");
  log(`running the journeys${options.headed ? " (headed)" : ""}`);
  return new Promise(resolve => {
    const runner = spawn(process.execPath, args, {
      cwd: REPO_ROOT,
      stdio: "inherit",
      env: {
        ...process.env,
        // `resolveBrowsersPath()` already put PLAYWRIGHT_BROWSERS_PATH in this process's
        // environment when the cache needed resolving; the workers inherit it from here,
        // which is the only moment early enough for Playwright to read it.
        STARLING_E2E_RUN_DIR: runDir,
        STARLING_E2E_HEADED: options.headed ? "1" : "",
      },
    });
    started.runner = runner;
    runner.on("exit", code => {
      started.runner = null;
      resolve(code ?? 1);
    });
  });
}

// --------------------------------------------------------------------------- shutdown

async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  if (started.runner) {
    killTree(started.runner.pid);
    started.runner = null;
  }
  const driver = started.driver;
  started.driver = null;
  if (!driver || driver.exitCode !== null) return;
  log("stopping the stack");
  driver.stdin.end();
  const stopped = await Promise.race([
    new Promise(resolve => driver.on("exit", () => resolve(true))),
    new Promise(resolve => setTimeout(() => resolve(false), 90_000)),
  ]);
  if (!stopped) {
    log("the driver did not stop in time; stopping the stack from its state file");
    killTree(driver.pid);
    spawnSync(venvPython(), ["-m", "e2e.harness", "down", "--state", STATE_FILE], { cwd: REPO_ROOT, stdio: "inherit" });
  }
}

/** Windows kills only the named process, so take the tree. */
function killTree(pid) {
  if (!pid) return;
  if (process.platform === "win32") spawnSync("taskkill", ["/T", "/F", "/PID", String(pid)], { stdio: "ignore" });
  else try { process.kill(-pid, "SIGKILL"); } catch { try { process.kill(pid, "SIGKILL"); } catch { /* gone */ } }
}

/** Nothing of this run may still be alive: not a process it recorded, not its port. */
async function processCheck(state) {
  const pids = Object.entries(state.pids ?? {});
  const stray = [];
  for (const [name, pid] of pids) {
    if (isAlive(pid)) stray.push({ name, pid });
  }
  const portBusy = !(await isPortFree(GATEWAY_PORT));
  if (portBusy) stray.push({ name: "gateway-port", pid: GATEWAY_PORT });
  const chromium = chromiumProcesses();
  if (chromium.length) stray.push({ name: "chromium", pid: chromium.join(",") });
  return { checked: pids.map(([name, pid]) => `${name}:${pid}`), gatewayPortFree: !portBusy, chromium, stray };
}

function isAlive(pid) {
  if (!pid || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

/** Test Chromiums come out of Playwright's cache; a browser of the user's own never does. */
function chromiumProcesses() {
  if (process.platform !== "win32") return [];
  const marker = (process.env.PLAYWRIGHT_BROWSERS_PATH || "ms-playwright").replace(/\\/g, "\\\\");
  const query = `SELECT ProcessId FROM Win32_Process WHERE Name='chrome.exe' AND CommandLine LIKE '%${path.basename(marker)}%'`;
  const result = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command",
    `Get-CimInstance -Query "${query}" | ForEach-Object { $_.ProcessId }`], { encoding: "utf8" });
  return (result.stdout || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean);
}

function isPortFree(port) {
  return new Promise(resolve => {
    const socket = net.connect({ port, host: "127.0.0.1" });
    socket.setTimeout(1000);
    socket.on("connect", () => { socket.destroy(); resolve(false); });
    socket.on("timeout", () => { socket.destroy(); resolve(true); });
    socket.on("error", () => resolve(true));
  });
}

async function requirePortFree(port) {
  if (await isPortFree(port)) return;
  throw new Error(
    `port ${port} is in use. The extension is built against that origin, so the journeys need it. `
    + `Stop the other harness first: backend\\.venv\\Scripts\\python.exe -m e2e.harness down --state e2e/.run/browser-state.json`,
  );
}

// --------------------------------------------------------------------------- arguments

function parseArgs(argv) {
  const parsed = { headed: false, skipBuild: false, list: false, grep: null, journey: null };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--headed") parsed.headed = true;
    else if (arg === "--skip-build") parsed.skipBuild = true;
    else if (arg === "--list") parsed.list = true;
    else if (arg === "--grep") parsed.grep = argv[++index];
    else if (arg === "--journey") parsed.journey = argv[++index];
    else throw new Error(`unknown option ${arg}`);
  }
  return parsed;
}

for (const signal of ["SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"]) {
  process.on(signal, () => {
    shutdown().finally(() => process.exit(130));
  });
}

main()
  .then(code => process.exit(code))
  .catch(async error => {
    console.error(error);
    await shutdown();
    process.exit(1);
  });
