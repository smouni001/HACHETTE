const form = document.getElementById("jobForm");
const fileInput = document.getElementById("facdemaFile");
const launchBtn = document.getElementById("launchBtn");
const errorBox = document.getElementById("errorBox");
const warningBox = document.getElementById("warningBox");
const statusPanel = document.getElementById("statusPanel");
const statusBadge = document.getElementById("statusBadge");

const jobIdValue = document.getElementById("jobIdValue");
const statusValue = document.getElementById("statusValue");
const messageValue = document.getElementById("messageValue");
const updatedAtValue = document.getElementById("updatedAtValue");
const progressValue = document.getElementById("progressValue");
const progressBar = document.getElementById("progressBar");
const progressWrap = document.getElementById("progressWrap");

const invoiceCount = document.getElementById("invoiceCount");
const lineCount = document.getElementById("lineCount");
const issuesCount = document.getElementById("issuesCount");

const downloadExcel = document.getElementById("downloadExcel");
const downloadPdfFactures = document.getElementById("downloadPdfFactures");
const downloadPdfSynthese = document.getElementById("downloadPdfSynthese");

let activeJobId = null;
let pollTimer = null;
const STATUS_CONFIG = {
  queued: { label: "En attente", badgeClass: "is-queued" },
  running: { label: "En cours", badgeClass: "is-running" },
  completed: { label: "Termine", badgeClass: "is-completed" },
  failed: { label: "Echec", badgeClass: "is-failed" },
};

function setError(message) {
  if (!message) {
    errorBox.textContent = "";
    errorBox.classList.add("hidden");
    return;
  }
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
}

function setWarnings(warnings) {
  if (!warnings || warnings.length === 0) {
    warningBox.textContent = "";
    warningBox.classList.add("hidden");
    return;
  }
  warningBox.innerHTML = warnings.map((item) => `<div>${item}</div>`).join("");
  warningBox.classList.remove("hidden");
}

function setBusy(isBusy) {
  launchBtn.disabled = isBusy;
  fileInput.disabled = isBusy;
  launchBtn.textContent = isBusy ? "Extraction en cours..." : "Extraction Excel/PDF";
}

function setMetricValue(element, value) {
  element.textContent = typeof value === "number" ? value.toLocaleString("fr-FR") : "0";
}

function setDownload(linkElement, url) {
  if (!url) {
    linkElement.href = "#";
    linkElement.classList.add("disabled");
    linkElement.setAttribute("aria-disabled", "true");
    return;
  }
  linkElement.href = url;
  linkElement.classList.remove("disabled");
  linkElement.removeAttribute("aria-disabled");
}

function normalizeProgress(statusKey, rawProgress) {
  let value = Number(rawProgress);
  if (!Number.isFinite(value)) {
    value = 0;
  }

  if (statusKey === "completed") {
    return 100;
  }
  if (statusKey === "failed") {
    return 100;
  }
  if (statusKey === "running" && value <= 0) {
    return 8;
  }
  if (statusKey === "queued" && value <= 0) {
    return 2;
  }
  return Math.max(0, Math.min(100, value));
}

function toLocalDateTime(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("fr-FR");
}

function setStatusVisual(statusKey, label) {
  const config = STATUS_CONFIG[statusKey] || { label: label || "-", badgeClass: "is-queued" };
  if (statusValue) {
    statusValue.textContent = config.label;
  }
  if (statusBadge) {
    statusBadge.textContent = config.label;
    statusBadge.className = `status-badge ${config.badgeClass}`;
  }
  if (statusPanel) {
    statusPanel.dataset.state = statusKey || "queued";
  }
  if (progressWrap) {
    progressWrap.setAttribute("aria-label", `Progression ${config.label}`);
  }
}

function applyStatus(payload) {
  const rawStatus = String(payload.status || "").toLowerCase().trim();
  const safeStatus = STATUS_CONFIG[rawStatus] ? rawStatus : "queued";
  const progress = normalizeProgress(safeStatus, payload.progress);

  jobIdValue.textContent = payload.job_id || "-";
  setStatusVisual(safeStatus, payload.status || "-");
  messageValue.textContent = payload.message || "-";
  if (updatedAtValue) {
    updatedAtValue.textContent = `Derniere mise a jour: ${toLocalDateTime(payload.updated_at)}`;
  }
  progressBar.style.width = `${progress}%`;
  if (progressValue) {
    progressValue.textContent = `${progress}%`;
  }

  const metrics = payload.metrics || {};
  setMetricValue(invoiceCount, metrics.invoice_count || 0);
  setMetricValue(lineCount, metrics.line_count || 0);
  setMetricValue(issuesCount, metrics.issues_count || 0);
  setWarnings(payload.warnings || []);

  const downloads = payload.downloads || {};
  setDownload(downloadExcel, downloads.excel);
  setDownload(downloadPdfFactures, downloads.pdf_factures);
  setDownload(downloadPdfSynthese, downloads.pdf_synthese);
}

async function fetchStatus(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  if (!response.ok) {
    const errorPayload = await response.json().catch(() => ({}));
    throw new Error(errorPayload.detail || "Impossible de lire le statut du job.");
  }
  return response.json();
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollOnce(jobId) {
  const payload = await fetchStatus(jobId);
  applyStatus(payload);
  if (payload.status === "completed" || payload.status === "failed") {
    stopPolling();
    setBusy(false);
    if (payload.status === "failed" && payload.error) {
      setError(payload.error);
    }
  }
}

function startPolling(jobId) {
  stopPolling();
  pollOnce(jobId).catch((error) => {
    stopPolling();
    setBusy(false);
    setError(error.message);
  });
  pollTimer = setInterval(async () => {
    try {
      await pollOnce(jobId);
    } catch (error) {
      stopPolling();
      setBusy(false);
      setError(error.message);
    }
  }, 1200);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("");
  setWarnings([]);

  const file = fileInput.files?.[0];
  if (!file) {
    setError("Chargez un fichier FACDEMA avant de lancer le traitement.");
    return;
  }

  const formData = new FormData();
  formData.append("facdema_file", file, file.name);

  try {
    setBusy(true);
    const response = await fetch("/api/jobs", {
      method: "POST",
      body: formData,
    });

    if (!response.ok) {
      const errorPayload = await response.json().catch(() => ({}));
      throw new Error(errorPayload.detail || "Creation du job impossible.");
    }

    const payload = await response.json();
    activeJobId = payload.job_id;
    jobIdValue.textContent = activeJobId;
    setStatusVisual("queued", payload.status);
    messageValue.textContent = payload.message;
    if (updatedAtValue) {
      updatedAtValue.textContent = "Derniere mise a jour: -";
    }
    progressBar.style.width = "2%";
    if (progressValue) {
      progressValue.textContent = "2%";
    }
    setMetricValue(invoiceCount, 0);
    setMetricValue(lineCount, 0);
    setMetricValue(issuesCount, 0);
    setDownload(downloadExcel, null);
    setDownload(downloadPdfFactures, null);
    setDownload(downloadPdfSynthese, null);

    startPolling(activeJobId);
  } catch (error) {
    setBusy(false);
    setError(error.message || "Erreur inattendue.");
  }
});
