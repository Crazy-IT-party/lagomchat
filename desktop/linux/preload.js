// what the page learns from the app: that it is in it (the main window or the settings window) and the app's version.
// Read-only marks; the page gets no command of the app
const { contextBridge } = require('electron');
const arg = k => (process.argv.find(a => a.startsWith(`--${k}=`)) || '').split('=').slice(1).join('=');
const kind = arg('ac-kind');
if (location.protocol === 'http:' || location.protocol === 'https:') {
  contextBridge.exposeInMainWorld(kind === 'settings' ? '__acAppSet' : '__acApp', 1);
  contextBridge.exposeInMainWorld('__acAppVer', arg('ac-ver'));
}
