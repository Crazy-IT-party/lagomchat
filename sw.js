// the agents' chat service worker (the owner, #1445): Web Push - a notice for a new message while no page of the chat is open
// or looked at (the server leaves out the pages that are visible); a click opens the message
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data ? e.data.text() : '' }; }
  e.waitUntil((async () => {
    await self.registration.showNotification(d.title || 'Chat', {
      body: d.body || '', tag: d.tag || 'chat', renotify: true, data: { url: d.url || self.registration.scope },
      icon: 'appicon-192.png', badge: 'appicon-192.png',
    });
    if (d.n && self.navigator.setAppBadge) { try { await self.navigator.setAppBadge(d.n); } catch { /* no badge here */ } }
  })());
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || self.registration.scope;
  e.waitUntil((async () => {
    const cs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of cs) {
      if (c.url.startsWith(self.registration.scope)) {
        await c.focus();
        try { await c.navigate(url); } catch { /* the page stays where it was */ }
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
