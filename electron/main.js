const { app, BrowserWindow, shell, dialog, Menu, nativeTheme, ipcMain } = require("electron");
const { autoUpdater } = require("electron-updater");
const path = require("path");
const net  = require("net");

let mainWindow = null;
let serverPort = null;

// ── Auto-updater config ───────────────────────────────────────────────────────
autoUpdater.autoDownload    = true;   // download silently in background
autoUpdater.autoInstallOnAppQuit = true; // install when user quits

autoUpdater.on("checking-for-update", () => {
  sendToWindow("updater-status", { type: "checking" });
});

autoUpdater.on("update-available", (info) => {
  sendToWindow("updater-status", {
    type:    "available",
    version: info.version,
    notes:   info.releaseNotes || "",
  });
});

autoUpdater.on("update-not-available", () => {
  sendToWindow("updater-status", { type: "up-to-date" });
});

autoUpdater.on("download-progress", (progress) => {
  sendToWindow("updater-status", {
    type:    "downloading",
    percent: Math.round(progress.percent),
  });
});

autoUpdater.on("update-downloaded", (info) => {
  sendToWindow("updater-status", {
    type:    "downloaded",
    version: info.version,
  });
});

autoUpdater.on("error", (err) => {
  // Silently ignore update errors — don't interrupt the user
  console.error("Auto-updater error:", err.message);
});

// ── IPC handlers ─────────────────────────────────────────────────────────────
ipcMain.on("install-update", () => {
  autoUpdater.quitAndInstall();
});

ipcMain.on("check-for-updates", () => {
  autoUpdater.checkForUpdates().catch(() => {});
});

ipcMain.handle("get-app-version", () => {
  const { buildNumber } = require("../build-number.json");
  return { version: app.getVersion(), buildNumber };
});

// ── Helpers ───────────────────────────────────────────────────────────────────
function sendToWindow(channel, data) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, data);
  }
}

function findFreePort(preferred) {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.listen(preferred, "127.0.0.1", () => {
      const port = probe.address().port;
      probe.close(() => resolve(port));
    });
    probe.on("error", () => {
      const fallback = net.createServer();
      fallback.listen(0, "127.0.0.1", () => {
        const port = fallback.address().port;
        fallback.close(() => resolve(port));
      });
    });
  });
}

async function startExpressServer() {
  const port = await findFreePort(3000);
  const { startServer } = require("../server");
  serverPort = await startServer(port);
}

// ── Window ────────────────────────────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    width:     1300,
    height:    840,
    minWidth:  920,
    minHeight: 620,
    title: "Delivery Zone Manager",
    show:  false,
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      sandbox:          true,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  if (process.platform !== "darwin") {
    mainWindow.setMenuBarVisibility(false);
  } else {
    buildMacMenu();
  }

  mainWindow.loadURL(`http://127.0.0.1:${serverPort}/login`);
  mainWindow.once("ready-to-show", () => mainWindow.show());

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.on("closed", () => { mainWindow = null; });
}

// ── Mac menu ──────────────────────────────────────────────────────────────────
function buildMacMenu() {
  const template = [
    {
      label: app.name,
      submenu: [
        { role: "about" },
        { type: "separator" },
        {
          label: "Check for Updates…",
          click: () => {
            autoUpdater.checkForUpdates().catch(() => {});
            sendToWindow("updater-status", { type: "checking" });
          },
        },
        { type: "separator" },
        { role: "services" },
        { type: "separator" },
        { role: "hide" },
        { role: "hideOthers" },
        { role: "unhide" },
        { type: "separator" },
        { role: "quit" },
      ],
    },
    {
      label: "Edit",
      submenu: [
        { role: "undo" }, { role: "redo" },
        { type: "separator" },
        { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: "Window",
      submenu: [{ role: "minimize" }, { role: "zoom" }, { role: "close" }],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ── App lifecycle ─────────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  nativeTheme.themeSource = "light";

  try {
    await startExpressServer();
    createWindow();

    // Check for updates 5 seconds after launch (non-blocking)
    setTimeout(() => {
      autoUpdater.checkForUpdates().catch(() => {});
    }, 5000);

  } catch (err) {
    dialog.showErrorBox(
      "Startup Error",
      `Delivery Zone Manager failed to start.\n\n${err.message}`
    );
    app.quit();
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
