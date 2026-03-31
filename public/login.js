// ── Storage keys (shared with app.js) ────────────────────────────────────────
const COOKIE_STORAGE_KEY   = "dzm.cookie";
const COOKIE_REMEMBER_KEY  = "dzm.cookie.remember";
const ENV_STORAGE_KEY      = "dzm.env";
const EMAIL_STORAGE_KEY    = "dzm.email";
const EMAIL_REMEMBER_KEY   = "dzm.remember.email";
const EMAIL_HISTORY_KEY    = "dzm.email.history";

// ── DOM ───────────────────────────────────────────────────────────────────────
const envTabs          = document.getElementById("envTabs");
const step1            = document.getElementById("step1");
const step2            = document.getElementById("step2");
const step3            = document.getElementById("step3");
const emailInput       = document.getElementById("emailInput");
const emailSuggestions = document.getElementById("emailSuggestions");
const rememberEmailChk = document.getElementById("rememberEmailChk");
const otpInput         = document.getElementById("otpInput");
const emailDisplay     = document.getElementById("emailDisplay");
const sendOtpBtn       = document.getElementById("sendOtpBtn");
const verifyOtpBtn     = document.getElementById("verifyOtpBtn");
const resendOtpBtn     = document.getElementById("resendOtpBtn");
const backToEmailBtn   = document.getElementById("backToEmailBtn");
const step1Error       = document.getElementById("step1Error");
const step2Error       = document.getElementById("step2Error");
const resendTimerText  = document.getElementById("resendTimerText");

// ── State ─────────────────────────────────────────────────────────────────────
let currentEnv       = storageGet(ENV_STORAGE_KEY) || "sit";
let sessionCookies   = "";
let requestId        = null;
let resendTimerId    = null;

// ── Helpers ───────────────────────────────────────────────────────────────────
function storageGet(key)        { try { return localStorage.getItem(key);    } catch (_) { return null; } }
function storageSet(key, value) { try { localStorage.setItem(key, value);    } catch (_) {} }
function storageRemove(key)     { try { localStorage.removeItem(key);        } catch (_) {} }

// Email history — stores up to 5 unique emails for autocomplete suggestions
function getEmailHistory() {
  try { return JSON.parse(storageGet(EMAIL_HISTORY_KEY) || "[]"); } catch (_) { return []; }
}

function addEmailToHistory(email) {
  const history = getEmailHistory().filter(e => e !== email);
  history.unshift(email);
  storageSet(EMAIL_HISTORY_KEY, JSON.stringify(history.slice(0, 5)));
}

function populateSuggestions() {
  const history = getEmailHistory();
  emailSuggestions.replaceChildren(
    ...history.map(e => {
      const opt = document.createElement("option");
      opt.value = e;
      return opt;
    })
  );
}

function showStep(n) {
  step1.hidden = n !== 1;
  step2.hidden = n !== 2;
  step3.hidden = n !== 3;
}

function showError(step, msg) {
  const el = step === 1 ? step1Error : step2Error;
  el.textContent = msg;
  el.hidden = !msg;
}

function clearErrors() {
  step1Error.hidden = true;
  step2Error.hidden = true;
}

function setBusy(busy) {
  sendOtpBtn.disabled    = busy;
  verifyOtpBtn.disabled  = busy;
  resendOtpBtn.disabled  = busy;
  emailInput.disabled    = busy;
  otpInput.disabled      = busy;
  envTabs.querySelectorAll(".env-tab").forEach(t => t.disabled = busy);
}

function startCountdown(seconds) {
  clearInterval(resendTimerId);
  resendOtpBtn.disabled = true;
  let remaining = seconds;

  function tick() {
    if (remaining <= 0) {
      clearInterval(resendTimerId);
      resendTimerText.textContent = "";
      resendOtpBtn.disabled = false;
      return;
    }
    resendTimerText.textContent = `Resend available in ${remaining}s`;
    remaining--;
  }
  tick();
  resendTimerId = setInterval(tick, 1000);
}

// ── Environment ───────────────────────────────────────────────────────────────
const loginEnvMeta = {};

function applyEnv(env) {
  currentEnv = env;
  storageSet(ENV_STORAGE_KEY, env);
  envTabs.querySelectorAll(".env-tab").forEach(tab => {
    tab.classList.toggle("active", tab.dataset.env === env);
  });
  // Update footer with env type
  updateLoginFooter();
}

function updateLoginFooter() {
  const footer = document.getElementById("loginFooterVersion");
  if (!footer) return;
  const base    = footer.dataset.version || footer.textContent.split("·")[0].trim();
  const type    = loginEnvMeta[currentEnv] && loginEnvMeta[currentEnv].type;
  footer.textContent = type ? `${base}  ·  ${type}` : base;
}

envTabs.addEventListener("click", e => {
  const tab = e.target.closest(".env-tab");
  if (tab && !tab.disabled) applyEnv(tab.dataset.env);
});

// ── API calls ─────────────────────────────────────────────────────────────────
async function apiPost(url, body) {
  const res  = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

// ── Send OTP ──────────────────────────────────────────────────────────────────
async function doSendOtp(email) {
  clearErrors();
  setBusy(true);
  try {
    const data = await apiPost("/api/login/send-otp", { email, env: currentEnv });
    sessionCookies = data.sessionCookies || "";
    requestId      = data.requestId      || null;

    emailDisplay.textContent = email;
    otpInput.value = "";
    showStep(2);
    startCountdown(data.resendTimer || 30);
    requestAnimationFrame(() => otpInput.focus());
  } catch (err) {
    showError(1, err.message);
  } finally {
    setBusy(false);
  }
}

// ── Verify OTP ────────────────────────────────────────────────────────────────
async function doVerifyOtp() {
  clearErrors();
  const email = emailDisplay.textContent.trim();
  const otp   = otpInput.value.trim();
  if (!otp) { showError(2, "Please enter the OTP."); return; }

  setBusy(true);
  try {
    const data = await apiPost("/api/login/verify-otp", {
      email,
      otp,
      requestId,
      sessionCookies,
      env: currentEnv,
    });

    // Persist cookie + email so main app picks them up
    storageSet(COOKIE_STORAGE_KEY,  data.cookieString);
    storageSet(COOKIE_REMEMBER_KEY, "1");
    storageSet(EMAIL_STORAGE_KEY, email);

    // Remember email for next login if checkbox is checked
    addEmailToHistory(email);
    if (rememberEmailChk.checked) {
      storageSet(EMAIL_REMEMBER_KEY, email);
    } else {
      storageRemove(EMAIL_REMEMBER_KEY);
    }

    clearInterval(resendTimerId);
    showStep(3);

    // Redirect to main app after short delay
    setTimeout(() => { window.location.href = "/"; }, 1200);
  } catch (err) {
    showError(2, err.message);
  } finally {
    setBusy(false);
  }
}

// ── Event listeners ───────────────────────────────────────────────────────────
sendOtpBtn.addEventListener("click", () => {
  const email = emailInput.value.trim();
  if (!email) { showError(1, "Please enter your email address."); return; }
  if (!email.includes("@")) { showError(1, "Please enter a valid email address."); return; }
  doSendOtp(email);
});

emailInput.addEventListener("keydown", e => { if (e.key === "Enter") sendOtpBtn.click(); });

verifyOtpBtn.addEventListener("click", doVerifyOtp);
otpInput.addEventListener("keydown",   e => { if (e.key === "Enter") verifyOtpBtn.click(); });

// Only allow digits in OTP field
otpInput.addEventListener("input", () => {
  otpInput.value = otpInput.value.replace(/\D/g, "").slice(0, 6);
});

resendOtpBtn.addEventListener("click", () => {
  const email = emailDisplay.textContent.trim();
  if (email) doSendOtp(email);
});

backToEmailBtn.addEventListener("click", () => {
  clearInterval(resendTimerId);
  clearErrors();
  showStep(1);
  requestAnimationFrame(() => emailInput.focus());
});

// ── Version + environments display ───────────────────────────────────────────
Promise.all([
  fetch("/api/version").then(r => r.json()).catch(() => ({})),
  fetch("/api/environments").then(r => r.json()).catch(() => ({})),
]).then(([versionData, envData]) => {
  const footer = document.getElementById("loginFooterVersion");
  if (footer && versionData.version) {
    const label = versionData.buildNumber
      ? `v${versionData.version} (build ${versionData.buildNumber})`
      : `v${versionData.version}`;
    footer.dataset.version = label;
    footer.textContent     = label;
  }
  if (Array.isArray(envData.environments)) {
    for (const e of envData.environments) loginEnvMeta[e.key] = e;
  }
  updateLoginFooter();
});

// ── Init ──────────────────────────────────────────────────────────────────────
applyEnv(currentEnv);
showStep(1);

// Pre-fill remembered email and populate autocomplete suggestions
populateSuggestions();
const rememberedEmail = storageGet(EMAIL_REMEMBER_KEY);
if (rememberedEmail) {
  emailInput.value        = rememberedEmail;
  rememberEmailChk.checked = true;
}
