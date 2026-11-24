// before a build: one source for what the two apps share. The version is the Mac app's (tauri.conf.json, which the server
// reads too), the start page is its dist/index.html, the people thanked are the CREDITS of its main.rs; the working copy
// the app is built from is written down so that its menu's Chats and Server can run that copy's ./lagom
const fs = require('node:fs'), path = require('node:path');
const at = (...p) => path.join(__dirname, ...p);
const ver = JSON.parse(fs.readFileSync(at('..', 'src-tauri', 'tauri.conf.json'), 'utf8')).version;
const pkg = JSON.parse(fs.readFileSync(at('package.json'), 'utf8'));
if (pkg.version !== ver) { pkg.version = ver; fs.writeFileSync(at('package.json'), JSON.stringify(pkg, null, 2) + '\n'); }
const rs = fs.readFileSync(at('..', 'src-tauri', 'src', 'main.rs'), 'utf8');
const m = /const CREDITS: &\[&str\] = &\[([\s\S]*?)\];/.exec(rs);
const credits = m ? [...m[1].matchAll(/"([^"]*)"/g)].map(x => x[1]) : [];
fs.copyFileSync(at('..', 'dist', 'index.html'), at('start.html'));
fs.copyFileSync(at('..', 'src-tauri', 'icons', 'icon.png'), at('icon.png'));
fs.mkdirSync(at('res', 'icons'), { recursive: true });
for (const [f, n] of [['32x32.png', 32], ['64x64.png', 64], ['128x128.png', 128], ['128x128@2x.png', 256], ['icon.png', 512]]) fs.copyFileSync(at('..', 'src-tauri', 'icons', f), at('res', 'icons', `${n}x${n}.png`));
// LAGOM_REPO= (empty) builds an app for other machines: no working copy in it
const repo = 'LAGOM_REPO' in process.env ? process.env.LAGOM_REPO : path.resolve(__dirname, '..', '..');
fs.writeFileSync(at('stamp.json'), JSON.stringify({ repo, credits }) + '\n');
console.log(`LagomChat ${ver}: ${credits.length} credits, repo ${repo || '(none)'}`);
