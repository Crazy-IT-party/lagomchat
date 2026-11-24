// the window that asks which screen or window to share: it is told the list and answers with one id, or with nothing
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('picker', {
  onSources: f => ipcRenderer.once('sources', (_e, list) => f(list)),
  pick: id => ipcRenderer.send('pick', id),
});
