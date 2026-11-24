// LagomChat for Linux: the chat's page in a window of Chromium (Electron). The Mac's app shows it in the system's WebKit
// (../src-tauri); the WebKitGTK of a Linux has no WebRTC, so cameras, voices and shared screens did nothing there (the owner,
// #3413, #3417). What the app does is the same as there: the start page asks for the chat's address, the page's title carries
// the unread count and the notices, Settings is a window of its own, a link that wants a new window goes to the browser.
const { app, BrowserWindow, Menu, Notification, desktopCapturer, dialog, ipcMain, session, shell } = require('electron');
const { execFile } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const here = f => path.join(__dirname, f);
// stamp.json is written at the build: the working copy the app was built from, and the people thanked
const STAMP = (() => { try { return JSON.parse(fs.readFileSync(here('stamp.json'), 'utf8')); } catch { return {}; } })();
const START = fs.existsSync(here('start.html')) ? here('start.html') : path.join(__dirname, '..', 'dist', 'index.html');
const isWeb = u => { try { return /^https?:$/.test(new URL(u).protocol); } catch { return false; } };
const originOf = u => { try { return new URL(u).origin; } catch { return ''; } };

let main = null, settings = null, dirty = false;

// the manager of this machine's chats: the working copy the app was built from runs it with `./lagom --app`, which prints
// "<address> <chats running> [<the first chat>]". An app on a machine without that working copy has no manager
// (a packaged app looks nowhere else: only a run from the working copy itself falls back to it)
const repo = () => { const p = STAMP.repo || (app.isPackaged ? '' : path.join(__dirname, '..', '..')); return p && fs.existsSync(path.join(p, 'lagom')) ? p : null; };
let managerUrl = null;
const manager = () => new Promise(done => {
  const r = repo(); if (!r) return done(null);
  execFile(path.join(r, 'lagom'), ['--app'], { cwd: r, timeout: 60000, env: { ...process.env, PATH: `/usr/local/bin:/usr/bin:/bin:${process.env.PATH || ''}` } }, (err, out) => {
    const w = String(out || '').trim().split('\n').pop().split(/\s+/).filter(Boolean);
    if (err || !isWeb(w[0])) return done(null);
    managerUrl = w[0];
    done({ url: w[0], running: /^\d+$/.test(w[1] || '') ? +w[1] : 1, first: w[2] || null });
  });
});

// "(3) Name" in the page's title: the count goes to the launcher's badge, the window keeps the name
function splitBadge(title) {
  const m = /^\((\d{1,4})\) (.*)$/s.exec(title);
  return m ? { count: +m[1], name: m[2] } : { count: 0, name: title };
}
// ' [notify]{"t":"<title>","b":"<text>"}' after the title for a moment: a notice of the system. Only its text is taken, a few a
// second at most; the same notice within 2 s is that one, not another
let lastNotice = [0, ''];
function splitNotify(title) {
  const i = title.indexOf(' [notify]'); if (i < 0) return { base: title };
  let v = {}; try { v = JSON.parse(title.slice(i + 9)) || {}; } catch {}
  const get = k => (typeof v[k] === 'string' ? v[k] : '').slice(0, 240);
  return { base: title.slice(0, i), t: get('t'), b: get('b') };
}
function notify(t, b) {
  const key = `${t}\n${b}`, now = Date.now();
  if (now - lastNotice[0] < 250 || (lastNotice[1] === key && now - lastNotice[0] < 2000)) return;
  lastNotice = [now, key];
  if (Notification.isSupported()) new Notification({ title: t || 'LagomChat', body: b }).show();
}

function pageWindow(kind, size, min) {
  const w = new BrowserWindow({
    width: size[0], height: size[1], minWidth: min[0], minHeight: min[1], title: kind === 'main' ? 'LagomChat' : 'Settings',
    icon: fs.existsSync(here('icon.png')) ? here('icon.png') : undefined,
    // the page is somebody's server: no Node in it, its own process; the preload only says "you are in the app" and the version
    webPreferences: { preload: here('preload.js'), additionalArguments: [`--ac-kind=${kind}`, `--ac-ver=${app.getVersion()}`], contextIsolation: true, sandbox: true, nodeIntegration: false, spellcheck: true },
  });
  w.webContents.setWindowOpenHandler(({ url }) => { if (isWeb(url)) shell.openExternal(url); return { action: 'deny' }; });
  return w;
}

function openMain() {
  main = pageWindow('main', [1200, 820], [420, 500]);
  main.on('page-title-updated', (e, title) => {
    e.preventDefault();
    const n = splitNotify(title); if (n.t || n.b) notify(n.t, n.b);
    const { count, name } = splitBadge(n.base);
    app.setBadgeCount(count);                              // shown by the launchers that know how (Unity, KDE); the others ignore it
    if (name) main.setTitle(name === 'LagomChat' ? name : `LagomChat · ${name}`);
  });
  main.on('closed', () => { main = null; if (settings) settings.destroy(); });
  main.loadFile(START);
}

function openSettings() {
  if (settings) return settings.focus();
  if (!main) return;
  const url = main.webContents.getURL();
  if (!isWeb(url)) return main.focus();                    // the start page: no chat yet
  const u = new URL(url); u.pathname = '/'; u.search = ''; u.hash = 'settings';
  settings = pageWindow('settings', [900, 680], [840, 400]); dirty = false;
  // the page says through its title that something is unsaved, and that it may close
  settings.on('page-title-updated', (e, title) => { e.preventDefault(); dirty = title.endsWith(' [dirty]'); if (title.endsWith(' [close]')) settings.destroy(); });
  settings.on('close', e => { if (dirty) { e.preventDefault(); settings.webContents.executeJavaScript('window.__acAskClose && window.__acAskClose()').catch(() => {}); } });
  settings.on('closed', () => { settings = null; });
  settings.setMenuBarVisibility(false);
  settings.loadURL(u.href);
}

function changeAddress() {
  if (settings) settings.destroy();
  if (!main) return;
  main.loadFile(START); app.setBadgeCount(0); main.focus();
}

async function openManager() {
  const m = await manager(), url = (m && m.url) || managerUrl;   // asked again each time: a manager started anew has a new key
  if (url && main) { main.loadURL(url); main.focus(); }
}

function about() {
  dialog.showMessageBox(main || undefined, { type: 'info', title: 'About LagomChat', message: `LagomChat ${app.getVersion()}`,
    detail: (STAMP.credits || []).length ? `Credits\n\n${STAMP.credits.join(', ')}` : '', buttons: ['OK'] });
}

function setMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: 'LagomChat', submenu: [
      { label: 'About LagomChat', click: about }, { type: 'separator' },
      { label: 'Settings…', accelerator: 'CmdOrCtrl+,', click: openSettings },
      { label: 'Change Chat Address…', click: changeAddress },
      { label: 'Chats and Server…', accelerator: 'CmdOrCtrl+Shift+M', enabled: !!repo(), click: openManager },
      { type: 'separator' }, { role: 'quit' } ] },
    { label: 'Edit', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    { label: 'View', submenu: [{ role: 'reload' }, { role: 'togglefullscreen' }, { type: 'separator' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }] },
    { label: 'Window', submenu: [{ role: 'minimize' }, { role: 'close' }] },
  ]));
}

// what a page may have: only the chat the main window is on (its settings window is the same site), and only what a chat needs
const GRANT = new Set(['media', 'display-capture', 'fullscreen', 'notifications', 'clipboard-sanitized-write', 'clipboard-read', 'speaker-selection']);
const chatOrigin = () => { const u = main && !main.isDestroyed() ? main.webContents.getURL() : ''; return isWeb(u) ? originOf(u) : ''; };
const mayHave = (permission, origin) => GRANT.has(permission) && !!origin && origin === chatOrigin();

// a shared screen: on Wayland the system asks what to show and gives the one picked; on X11 there is no such window, so ours
let picking = null;
function pickSource(sources) {
  return new Promise(done => {
    if (picking) return done(null);                        // asked again while the window is open: that one answers the first
    const w = new BrowserWindow({ width: 720, height: 520, parent: main || undefined, modal: !!main, title: 'Share a screen or a window', autoHideMenuBar: true,
      webPreferences: { preload: here('picker-preload.js'), contextIsolation: true, sandbox: true, nodeIntegration: false } });
    picking = { w, done: id => { picking = null; done(sources.find(s => s.id === id) || null); if (!w.isDestroyed()) w.destroy(); } };
    w.on('closed', () => { if (picking && picking.w === w) { picking = null; done(null); } });
    w.webContents.once('did-finish-load', () => w.webContents.send('sources', sources.map(s => ({ id: s.id, name: s.name, img: s.thumbnail.isEmpty() ? '' : s.thumbnail.toDataURL() }))));
    w.loadFile(here('picker.html'));
  });
}
ipcMain.on('pick', (e, id) => { if (picking && e.sender === picking.w.webContents) picking.done(typeof id === 'string' ? id : null); });

function setSession() {
  const s = session.defaultSession;
  s.setPermissionRequestHandler((wc, permission, cb, details) => cb(mayHave(permission, originOf(details.requestingUrl || wc.getURL()))));
  s.setPermissionCheckHandler((wc, permission, origin) => mayHave(permission, originOf(origin || (wc ? wc.getURL() : ''))));
  s.setDisplayMediaRequestHandler(async (request, cb) => {
    try {
      if (originOf(request.securityOrigin || (request.frame && request.frame.url) || '') !== chatOrigin() || !chatOrigin()) return cb({});
      const sources = await desktopCapturer.getSources({ types: ['screen', 'window'], thumbnailSize: { width: 320, height: 200 } });
      const one = sources.length === 1 ? sources[0] : sources.length ? await pickSource(sources) : null;
      cb(one ? { video: one } : {});                       // the computer's sound is not given to a shared screen on Linux by this Chromium
    } catch { cb({}); }
  });
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (main) { if (main.isMinimized()) main.restore(); main.focus(); } });
  app.whenReady().then(async () => {
    setSession(); setMenu(); openMain();
    // --probe: what the page gets in this shell, printed and out (the build's own check)
    if (process.argv.includes('--probe')) {
      if (isWeb(process.env.LAGOM_PROBE_URL || '')) main.loadURL(process.env.LAGOM_PROBE_URL);   // a chat's page instead of the start page
      main.webContents.on('did-finish-load', async () => {
        if (isWeb(process.env.LAGOM_PROBE_URL || '') && !isWeb(main.webContents.getURL())) return;
        const r = await main.webContents.executeJavaScript(`JSON.stringify({ app: window.__acApp || 0, ver: window.__acAppVer || '', title: document.title, pc: typeof RTCPeerConnection, gum: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia), gdm: !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia), form: !!document.getElementById('u') })`).catch(e => String(e));
        console.log('PROBE', r, app.getVersion(), process.versions.electron, process.versions.chrome); app.exit(0);
      });
      return;
    }
    // the manager up in the background; no chat running yet: its wizard instead of the address page. A chat of this working
    // copy runs: the address page opens it when it remembers no other address
    const m = await manager();
    if (!m || !main || isWeb(main.webContents.getURL())) return;   // it may take a while: a chat opened meanwhile stays
    if (m.running === 0) main.loadURL(m.url);
    else if (m.first && isWeb(m.first)) main.loadFile(START, { query: { chat: m.first } });
  });
  app.on('window-all-closed', () => app.quit());
}
