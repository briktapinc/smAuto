const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("bubblePod", {
  isDesktop: true,
  restartApi: () => ipcRenderer.invoke("studio-restart"),
});
