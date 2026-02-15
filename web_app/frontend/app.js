const form = document.getElementById("jobForm");
const flowTypeSelect = document.getElementById("flowType");
const fileNameSelect = document.getElementById("fileName");
const dataFileLabel = document.getElementById("dataFileLabel");
const fileInput = document.getElementById("dataFile");
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

const clientCount = document.getElementById("clientCount");
const invoiceCount = document.getElementById("invoiceCount");
const lineCount = document.getElementById("lineCount");

const downloadExcel = document.getElementById("downloadExcel");
const downloadPdfFactures = document.getElementById("downloadPdfFactures");
const downloadPdfSynthese = document.getElementById("downloadPdfSynthese");

let activeJobId = null;
let pollTimer = null;
let catalogProfiles = [];

const STATUS_CONFIG = {
  queued: { label: "En attente", badgeClass: "is-queued" },
  running: { label: "En cours", badgeClass: "is-running" },
  completed: { label: "Termine", badgeClass: "is-completed" },
  failed: { label: "Echec", badgeClass: "is-failed" },
};

function fallbackCatalog() {
  catalogProfiles = [
    {
      flow_type: "output",
      file_name: "FICDEMA",
      display_name: "FICDEMA",
      description: "Flux output facture dematerialisee.",
    },
    {
      flow_type: "output",
      file_name: "FICSTOD",
      display_name: "FICSTOD",
      description: "Flux output stock facture dematerialisee.",
    },
    {
      flow_type: "input",
      file_name: "FFAC3A",
      display_name: "FFAC3A",
      description: "Flux input source a facturer.",
    },
  ];
}

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
  flowTypeSelect.disabled = isBusy;
  fileNameSelect.disabled = isBusy;
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
  setMetricValue(clientCount, metrics.client_count || 0);
  setMetricValue(invoiceCount, metrics.invoice_count || 0);
  setMetricValue(lineCount, metrics.line_count || 0);
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

function selectedProfile() {
  const flowType = String(flowTypeSelect.value || "").toLowerCase();
  const fileName = String(fileNameSelect.value || "").toUpperCase();
  return catalogProfiles.find(
    (profile) =>
      String(profile.flow_type || "").toLowerCase() === flowType &&
      String(profile.file_name || "").toUpperCase() === fileName,
  );
}

function updateUploadLabel() {
  const profile = selectedProfile();
  if (!profile) {
    dataFileLabel.textContent = "Charger le fichier (obligatoire)";
    return;
  }
  dataFileLabel.textContent = `Charger le fichier ${profile.file_name} (obligatoire)`;
}

function labelForFlowType(flowType) {
  return String(flowType || "").toLowerCase() === "input" ? "Input" : "Output";
}

function rebuildFileOptions(targetFileName = null) {
  const selectedFlow = String(flowTypeSelect.value || "").toLowerCase();
  const availableProfiles = catalogProfiles.filter(
    (profile) => String(profile.flow_type || "").toLowerCase() === selectedFlow,
  );

  fileNameSelect.innerHTML = "";
  availableProfiles.forEach((profile) => {
    const option = document.createElement("option");
    option.value = profile.file_name;
    option.textContent = profile.display_name || profile.file_name;
    option.title = profile.description || "";
    fileNameSelect.appendChild(option);
  });

  if (targetFileName) {
    const normalizedTarget = String(targetFileName).toUpperCase();
    const found = availableProfiles.some(
      (profile) => String(profile.file_name || "").toUpperCase() === normalizedTarget,
    );
    if (found) {
      fileNameSelect.value = normalizedTarget;
    }
  }
  updateUploadLabel();
}

function loadFlowOptions(defaultFlowType = "output", defaultFileName = "FICDEMA") {
  const flowTypeSet = new Set(
    catalogProfiles.map((profile) => String(profile.flow_type || "").toLowerCase()).filter(Boolean),
  );
  const flowTypes = Array.from(flowTypeSet).sort();

  flowTypeSelect.innerHTML = "";
  flowTypes.forEach((flowType) => {
    const option = document.createElement("option");
    option.value = flowType;
    option.textContent = labelForFlowType(flowType);
    flowTypeSelect.appendChild(option);
  });

  if (flowTypes.length === 0) {
    return;
  }

  if (flowTypes.includes(defaultFlowType)) {
    flowTypeSelect.value = defaultFlowType;
  } else {
    flowTypeSelect.value = flowTypes[0];
  }
  rebuildFileOptions(defaultFileName);
}

async function loadCatalog() {
  try {
    const response = await fetch("/api/catalog");
    if (!response.ok) {
      throw new Error("Catalogue indisponible");
    }
    const payload = await response.json();
    catalogProfiles = Array.isArray(payload.profiles) ? payload.profiles : [];
    if (catalogProfiles.length === 0) {
      fallbackCatalog();
      loadFlowOptions("output", "FICDEMA");
      return;
    }
    loadFlowOptions(
      String(payload.default_flow_type || "output").toLowerCase(),
      String(payload.default_file_name || "FICDEMA").toUpperCase(),
    );
  } catch (error) {
    fallbackCatalog();
    loadFlowOptions("output", "FICDEMA");
    setError("Catalogue non charge. Mode de secours active.");
  }
}

flowTypeSelect.addEventListener("change", () => {
  rebuildFileOptions();
});

fileNameSelect.addEventListener("change", () => {
  updateUploadLabel();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("");
  setWarnings([]);

  const file = fileInput.files?.[0];
  const profile = selectedProfile();

  if (!profile) {
    setError("Selection flux/fichier invalide.");
    return;
  }
  if (!file) {
    setError(`Chargez le fichier ${profile.file_name} avant de lancer le traitement.`);
    return;
  }

  const formData = new FormData();
  formData.append("flow_type", profile.flow_type);
  formData.append("file_name", profile.file_name);
  formData.append("data_file", file, file.name);

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
    setMetricValue(clientCount, 0);
    setMetricValue(invoiceCount, 0);
    setMetricValue(lineCount, 0);
    setDownload(downloadExcel, null);
    setDownload(downloadPdfFactures, null);
    setDownload(downloadPdfSynthese, null);

    startPolling(activeJobId);
  } catch (error) {
    setBusy(false);
    setError(error.message || "Erreur inattendue.");
  }
});

loadCatalog().catch(() => {
  fallbackCatalog();
  loadFlowOptions("output", "FICDEMA");
});
