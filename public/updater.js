// Only runs inside the Electron app — window.electronAPI is injected by preload.js
if (!window.electronAPI) return;

const updateBanner      = document.getElementById("updateBanner");
const updateBannerText  = document.getElementById("updateBannerText");
const updateInstallBtn  = document.getElementById("updateInstallBtn");
const updateDismissBtn  = document.getElementById("updateDismissBtn");

// Show app version in the hero version pill (Electron provides exact version)
window.electronAPI.appVersion().then(({ version, buildNumber }) => {
  const pill = document.getElementById("appVersionPill");
  if (pill) {
    pill.textContent = buildNumber ? `v${version} (build ${buildNumber})` : `v${version}`;
  }
});

function showBanner(text, showInstall = false, type = "info") {
  updateBannerText.textContent = text;
  updateInstallBtn.hidden = !showInstall;
  updateBanner.className  = `update-banner update-banner-${type}`;
  updateBanner.hidden     = false;
}

function hideBanner() {
  updateBanner.hidden = true;
}

window.electronAPI.onUpdaterStatus((data) => {
  switch (data.type) {
    case "available":
      showBanner(
        `Update v${data.version} available — downloading in background…`,
        false,
        "info"
      );
      break;

    case "downloading":
      showBanner(
        `Downloading update… ${data.percent}%`,
        false,
        "info"
      );
      break;

    case "downloaded":
      showBanner(
        `Update v${data.version} ready. Restart to install.`,
        true,
        "success"
      );
      break;

    case "up-to-date":
      showBanner("You're on the latest version.", false, "success");
      setTimeout(hideBanner, 3000);
      break;

    case "checking":
      // Silent — no banner for routine check
      break;
  }
});

updateInstallBtn.addEventListener("click", () => {
  window.electronAPI.installUpdate();
});

updateDismissBtn.addEventListener("click", hideBanner);
