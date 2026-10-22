// demo-kit.mjs — shared helpers for the factory's Demo station.
//
// The factory copies this file to /projects/.demo-tools/demo-kit.mjs before each demo, next to
// `playwright-core` and `ffmpeg-static` (installed there with
// `npm i --prefix /projects/.demo-tools playwright-core ffmpeg-static`).
//
// A demo script only describes what to show; this kit does the rest:
//
//   import * as kit from "/projects/.demo-tools/demo-kit.mjs";
//   const browser = await kit.launch();
//   const account = kit.seedAccount("/projects/worktrees/HES-104");          // admin@hestia.local
//   const before = await kit.signIn(browser, "http://127.0.0.1:5181", account, OUT);
//   const after = await kit.signIn(browser, "http://127.0.0.1:5182", account, OUT);
//   const shots = await kit.compare(browser, {
//     before: { baseURL: "http://127.0.0.1:5181", storageState: before },
//     after: { baseURL: "http://127.0.0.1:5182", storageState: after },
//     path: "/cleanings", screen: "cleanings-list", state: "filled", outDir: OUT,
//     ready: (page) => kit.settle(page),            // optional: reach the state before the shot
//   });
//   const video = await kit.recordVideo(browser, {
//     baseURL: "http://127.0.0.1:5182", storageState: after, outDir: OUT, name: "filter-by-cleaner",
//     run: async (page, beat) => { await page.goto("/cleanings"); await beat(); ... },
//   });
//   kit.writeManifest(OUT, { issue, head, base_sha, summary, screens: [{ title, criterion, shots }],
//                           videos: [{ title, criterion, steps, ...video }], gaps: [] });
//   await browser.close();
//
// Themes are "light" and "night" (the app stores "dark" under localStorage `hestia:theme`).
// Viewports are "1440" (1440x900) and "390" (390x844).

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";

const TOOLS = process.env.DEMO_TOOLS || "/projects/.demo-tools/package.json";
const req = createRequire(TOOLS);
const { chromium } = req("playwright-core");

export const VIEWPORTS = { 1440: { width: 1440, height: 900 }, 390: { width: 390, height: 844 } };
const STORED_THEME = { light: "light", night: "dark" };
const GIF_LIMIT = 8 * 1024 * 1024;

const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

function chromiumPath() {
  if (process.env.DEMO_CHROMIUM) return process.env.DEMO_CHROMIUM;
  if (existsSync("/usr/bin/chromium")) return "/usr/bin/chromium";
  for (const root of [path.join(os.homedir(), ".cache/ms-playwright"), "/opt/pw-browsers"]) {
    if (!existsSync(root)) continue;
    for (const dir of readdirSync(root).sort().reverse()) {
      for (const bin of ["chrome-linux/chrome", "chrome-linux/headless_shell", "chrome-headless-shell-linux64/chrome-headless-shell"]) {
        const p = path.join(root, dir, bin);
        if (existsSync(p)) return p;
      }
    }
  }
  return undefined; // let playwright-core try its own default
}

function ffmpegPath() {
  if (process.env.DEMO_FFMPEG) return process.env.DEMO_FFMPEG;
  try {
    return req("ffmpeg-static");
  } catch {
    return "ffmpeg";
  }
}

/** Chromium for Playwright, with the flags the sandbox needs. */
export async function launch() {
  return chromium.launch({ executablePath: chromiumPath(), args: ["--no-sandbox"] });
}

/** A seeded local account: the shared password comes from the header of supabase/seed.sql. */
export function seedAccount(worktree, email = "admin@hestia.local") {
  const seed = readFileSync(path.join(worktree, "supabase/seed.sql"), "utf8");
  const match = seed.match(/Shared password for every loginable account:\s*(\S+)/);
  if (!match) throw new Error("supabase/seed.sql has no shared password line");
  return { email, password: match[1] };
}

/** A fresh context and page at a viewport and theme. Pass `videoDir` to record. */
export async function openPage(browser, { baseURL, viewport = "1440", theme = "light", storageState, videoDir }) {
  const size = VIEWPORTS[viewport] || viewport;
  const context = await browser.newContext({
    baseURL,
    viewport: size,
    colorScheme: theme === "night" ? "dark" : "light",
    storageState,
    deviceScaleFactor: 1,
    locale: "pt-BR",
    recordVideo: videoDir ? { dir: videoDir, size } : undefined,
  });
  await context.addInitScript((stored) => {
    try {
      localStorage.setItem("hestia:theme", stored);
    } catch {}
  }, STORED_THEME[theme] || theme);
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  return { context, page };
}

/** Wait until the page has loaded, fonts are ready, no loading panel shows and the network is quiet. */
export async function settle(page, { quietMs = 600 } = {}) {
  await page.waitForLoadState("domcontentloaded");
  await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
  await page.waitForFunction(() => document.fonts.ready.then(() => true), null, { timeout: 8000 }).catch(() => {});
  await page
    .waitForFunction(() => !document.querySelector('[data-variant="loading"]'), null, { timeout: 20000 })
    .catch(() => {});
  await page.waitForTimeout(quietMs);
}

/** Sign in through /login and save the session; returns the storage state path to reuse. */
export async function signIn(browser, baseURL, account, outDir) {
  const work = path.join(outDir, "_work");
  mkdirSync(work, { recursive: true });
  const statePath = path.join(work, `auth-${slug(new URL(baseURL).port || baseURL)}.json`);
  const { context, page } = await openPage(browser, { baseURL });
  await page.goto("/login");
  await page.getByLabel("E-mail").fill(account.email);
  await page.getByLabel("Senha").fill(account.password);
  await page.getByRole("button", { name: "Entrar" }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 25000 });
  await settle(page);
  await context.storageState({ path: statePath });
  await context.close();
  return statePath;
}

/** File name in the manifest convention: <screen>__<state>__<viewport>__<theme>__<variant>.png */
export function shotName({ screen, state, viewport, theme, variant }) {
  return `${slug(screen)}__${slug(state)}__${viewport}__${theme}__${variant}.png`;
}

/** Screenshot the current page into outDir; returns the file name. */
export async function shot(page, outDir, parts) {
  const name = shotName(parts);
  await page.screenshot({ path: path.join(outDir, name), animations: "disabled", caret: "hide" });
  return name;
}

/**
 * Before/after screenshots of one screen state at every viewport and theme.
 * `ready(page, side)` reaches the state (click a filter, open a sheet) after `path` loads.
 * `before: null` marks a new screen. `design` maps "<viewport>__<theme>" to an HTML mock file.
 * Returns the manifest `shots` array for the screen.
 */
export async function compare(browser, { before, after, path: route, screen, state, outDir,
  viewports = ["1440", "390"], themes = ["light", "night"], ready, design = {} }) {
  const shots = [];
  for (const viewport of viewports) {
    for (const theme of themes) {
      const entry = { viewport, theme, before: null, after: null, design: null };
      for (const [variant, side] of [["before", before], ["after", after]]) {
        if (!side) continue;
        const { context, page } = await openPage(browser, { ...side, viewport, theme });
        try {
          await page.goto(route);
          await settle(page);
          if (ready) {
            await ready(page, variant);
            await settle(page);
          }
          entry[variant] = await shot(page, outDir, { screen, state, viewport, theme, variant });
        } finally {
          await context.close();
        }
      }
      const mock = design[`${viewport}__${theme}`] || design[viewport];
      if (mock) entry.design = await designShot(browser, { htmlFile: mock, outDir, screen, state, viewport, theme });
      shots.push(entry);
    }
  }
  return shots;
}

/** Screenshot a design mock (an HTML file) at the same viewport and theme. */
export async function designShot(browser, { htmlFile, outDir, screen, state, viewport, theme }) {
  const { context, page } = await openPage(browser, { viewport, theme });
  try {
    await page.goto(`file://${path.resolve(htmlFile)}`);
    await settle(page, { quietMs: 300 });
    return await shot(page, outDir, { screen, state, viewport, theme, variant: "design" });
  } finally {
    await context.close();
  }
}

/** webm -> <name>.mp4 (H.264, faststart) and <name>.gif (at most 8 MB); deletes the webm. */
export function convert(webm, outDir, name) {
  const ff = ffmpegPath();
  const mp4 = `${slug(name)}.mp4`;
  const gif = `${slug(name)}.gif`;
  execFileSync(ff, ["-y", "-loglevel", "error", "-i", webm, "-c:v", "libx264", "-pix_fmt", "yuv420p",
    "-movflags", "+faststart", "-crf", "28", path.join(outDir, mp4)]);
  for (const [fps, width] of [[8, 960], [6, 800], [5, 640], [4, 520]]) {
    execFileSync(ff, ["-y", "-loglevel", "error", "-i", webm, "-vf",
      `fps=${fps},scale=${width}:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse`, path.join(outDir, gif)]);
    if (statSync(path.join(outDir, gif)).size <= GIF_LIMIT) break;
  }
  rmSync(webm, { force: true });
  return { mp4, gif };
}

/**
 * Record one use case on a fresh page. `run(page, beat)` drives it; call `await beat()` after each
 * step so a viewer can follow (about a second). Returns { mp4, gif, seconds } for the manifest.
 */
export async function recordVideo(browser, { baseURL, storageState, viewport = "1440", theme = "light", outDir, name, run }) {
  const videoDir = path.join(outDir, "_work", "video", slug(name));
  mkdirSync(videoDir, { recursive: true });
  const { context, page } = await openPage(browser, { baseURL, viewport, theme, storageState, videoDir });
  const beat = (ms = 1000) => page.waitForTimeout(ms);
  const started = Date.now();
  try {
    await run(page, beat);
    await beat(800);
  } finally {
    await context.close(); // flushes the video
  }
  const seconds = Math.round((Date.now() - started) / 1000);
  const webm = readdirSync(videoDir).filter((f) => f.endsWith(".webm")).map((f) => path.join(videoDir, f))[0];
  if (!webm) throw new Error(`no video recorded for ${name}`);
  return { ...convert(webm, outDir, name), seconds };
}

/** Check every file the manifest names exists in outDir, then write manifest.json. */
export function writeManifest(outDir, manifest) {
  const missing = [];
  const need = (f) => f && !existsSync(path.join(outDir, f)) && missing.push(f);
  for (const s of manifest.screens || []) for (const sh of s.shots || []) ["before", "after", "design"].forEach((k) => need(sh[k]));
  for (const v of manifest.videos || []) ["mp4", "gif"].forEach((k) => need(v[k]));
  if (missing.length) throw new Error(`manifest names missing files: ${missing.join(", ")}`);
  if (!(manifest.videos || []).length) throw new Error("manifest has no video");
  writeFileSync(path.join(outDir, "manifest.json"), JSON.stringify(manifest, null, 2));
  return path.join(outDir, "manifest.json");
}
