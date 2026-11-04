self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});  // requerido para poder instalar la app
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data.json(); } catch (_) { d = { titulo: "Cuadre", cuerpo: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.titulo || "Cuadre", {
    body: d.cuerpo || "", icon: "/static/icon-192.png", badge: "/static/icon-192.png",
    data: { url: d.url || "/" }, tag: d.tag || undefined }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(ws => {
    for (const w of ws) { if ("focus" in w) { w.navigate(url); return w.focus(); } }
    return clients.openWindow(url);
  }));
});
