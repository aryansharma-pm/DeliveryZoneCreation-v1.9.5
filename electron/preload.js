const { contextBridge, ipcRenderer } = require("electron");

// Expose a safe, minimal API to the renderer (web page)
contextBridge.exposeInMainWorld("electronAPI", {
  // App version
  appVersion: () => ipcRenderer.invoke("get-app-version"),

  // Updater
  onUpdaterStatus: (callback) => {
    ipcRenderer.on("updater-status", (_event, data) => callback(data));
  },
  installUpdate: () => ipcRenderer.send("install-update"),
  checkForUpdates: () => ipcRenderer.send("check-for-updates"),
});
