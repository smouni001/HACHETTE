const form = document.getElementById("jobForm");
const programSelect = document.getElementById("programId");
const localProgramPanel = document.getElementById("localProgramPanel");
const localProgramFile = document.getElementById("localProgramFile");
const loadLocalProgramBtn = document.getElementById("loadLocalProgramBtn");
const localProgramStatus = document.getElementById("localProgramStatus");
const flowTypeSelect = document.getElementById("flowType");
const fileNameSelect = document.getElementById("fileName");
const advancedModeToggle = document.getElementById("advancedMode");
const advancedModeState = document.getElementById("advancedModeState");
const catalogStatus = document.getElementById("catalogStatus");
const dataFileLabel = document.getElementById("dataFileLabel");
const profileContext = document.getElementById("profileContext");
const fileInput = document.getElementById("dataFile");
const launchBtn = document.getElementById("launchBtn");
const warningBox = document.getElementById("warningBox");
const statusPanel = document.getElementById("statusPanel");
const statusBadge = document.getElementById("statusBadge");
const toastHost = document.getElementById("toastHost");
const alertModal = document.getElementById("alertModal");
const alertModalIcon = document.getElementById("alertModalIcon");
const alertModalTitle = document.getElementById("alertModalTitle");
const alertModalMessage = document.getElementById("alertModalMessage");
const alertModalActions = document.getElementById("alertModalActions");
const alertModalClose = document.getElementById("alertModalClose");

const jobIdValue = document.getElementById("jobIdValue");
const statusValue = document.getElementById("statusValue");
const messageValue = document.getElementById("messageValue");
const updatedAtValue = document.getElementById("updatedAtValue");
const progressValue = document.getElementById("progressValue");
const progressBar = document.getElementById("progressBar");
const progressWrap = document.getElementById("progressWrap");

const kpiLabel1 = document.getElementById("kpiLabel1");
const kpiLabel2 = document.getElementById("kpiLabel2");
const kpiLabel3 = document.getElementById("kpiLabel3");
const kpiValue1 = document.getElementById("kpiValue1");
const kpiValue2 = document.getElementById("kpiValue2");
const kpiValue3 = document.getElementById("kpiValue3");

const downloadExcel = document.getElementById("downloadExcel");
const downloadPdfFactures = document.getElementById("downloadPdfFactures");
const downloadPdfSynthese = document.getElementById("downloadPdfSynthese");

let activeJobId = null;
let pollTimer = null;
let catalogSourceProgram = "programme";
let knownPrograms = [];
let catalogProfiles = [];
let localProgramId = null;
let completionNotifiedForJob = null;
let lastCatalogFailureMessage = "";
let lastCatalogFailureKind = "";
let lastStableState = null;
const noticeHistory = new Map();
const PRIMARY_PROGRAM_ID = "idp470ra";
const CATALOG_CACHE_TTL_MS = 5 * 60 * 1000;
const LOCAL_PROGRAM_OPTION_VALUE = "__local_program__";
const CATALOG_REQUEST_TIMEOUT_MS = 25000;

const STATUS_CONFIG = {
  queued: { label: "En attente", badgeClass: "is-queued" },
  running: { label: "En cours", badgeClass: "is-running" },
  completed: { label: "Termine", badgeClass: "is-completed" },
  failed: { label: "Echec", badgeClass: "is-failed" },
};

function fallbackCatalog() {
  const profiles = [
    {
      flow_type: "output",
      file_name: "FICDEMA",
      display_name: "FICDEMA",
      description: "Flux output facture dematerialisee.",
      role_label: "facturation",
      view_mode: "invoice",
      supports_processing: true,
      supports_pdf: true,
      raw_structures: ["DEMAT_FIC", "DEMAT_ENT", "DEMAT_LIG", "DEMAT_PIE"],
    },
    {
      flow_type: "output",
      file_name: "FICSTOD",
      display_name: "FICSTOD",
      description: "Flux output stock facture dematerialisee.",
      role_label: "facturation",
      view_mode: "invoice",
      supports_processing: true,
      supports_pdf: true,
      raw_structures: ["STO_D_FIC", "STO_D_ENT", "STO_D_LIG", "STO_D_PIE"],
    },
    {
      flow_type: "input",
      file_name: "FFAC3A",
      display_name: "FFAC3A",
      description: "Flux input source a facturer.",
      role_label: "facturation",
      view_mode: "generic",
      supports_processing: true,
      supports_pdf: false,
      raw_structures: ["WTFAC"],
    },
  ];
  catalogProfiles = isAdvancedModeEnabled()
    ? profiles
    : profiles.filter((profile) => String(profile.view_mode || "").toLowerCase() === "invoice");
}

function noticeType(type) {
  const safe = String(type || "info").toLowerCase();
  if (safe === "success" || safe === "error" || safe === "warning" || safe === "info") {
    return safe;
  }
  return "info";
}

function noticeIcon(type) {
  const safe = noticeType(type);
  if (safe === "success") {
    return "OK";
  }
  if (safe === "error") {
    return "!";
  }
  if (safe === "warning") {
    return "W";
  }
  return "i";
}

function shouldThrottleNotice(key, windowMs = 2200) {
  const safeKey = String(key || "");
  if (!safeKey) {
    return false;
  }
  const now = Date.now();
  const previous = noticeHistory.get(safeKey) || 0;
  noticeHistory.set(safeKey, now);
  return now - previous < windowMs;
}

function dismissModal() {
  if (!alertModal) {
    return;
  }
  alertModal.classList.add("hidden");
  alertModalTitle.textContent = "";
  alertModalMessage.textContent = "";
  alertModalActions.innerHTML = "";
}

function renderModalAction(label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", async () => {
    try {
      if (typeof onClick === "function") {
        await onClick();
      }
    } finally {
      dismissModal();
    }
  });
  return button;
}

function openModal({
  type = "info",
  title = "Information",
  message = "",
  primaryLabel = "Fermer",
  onPrimary = null,
  secondaryLabel = "",
  onSecondary = null,
} = {}) {
  if (!alertModal) {
    return;
  }
  const safeType = noticeType(type);
  alertModalIcon.className = `modal-icon is-${safeType}`;
  alertModalIcon.textContent = noticeIcon(safeType);
  alertModalTitle.textContent = title;
  alertModalMessage.textContent = message;
  alertModalActions.innerHTML = "";

  if (secondaryLabel) {
    alertModalActions.appendChild(renderModalAction(secondaryLabel, "secondary-btn", onSecondary));
  }
  alertModalActions.appendChild(renderModalAction(primaryLabel, "", onPrimary));
  alertModal.classList.remove("hidden");
}

function showToast({ type = "info", title = "Notification", message = "", durationMs = 4800, actionLabel = "", onAction = null } = {}) {
  if (!toastHost || !message) {
    return;
  }
  const safeType = noticeType(type);
  const toast = document.createElement("div");
  toast.className = `toast-card is-${safeType}`;

  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.textContent = noticeIcon(safeType);

  const content = document.createElement("div");
  content.className = "toast-content";
  const strong = document.createElement("strong");
  strong.textContent = title;
  const text = document.createElement("p");
  text.textContent = message;
  content.appendChild(strong);
  content.appendChild(text);

  if (actionLabel) {
    const actions = document.createElement("div");
    actions.className = "toast-actions";
    const actionBtn = document.createElement("button");
    actionBtn.type = "button";
    actionBtn.className = "toast-action";
    actionBtn.textContent = actionLabel;
    actionBtn.addEventListener("click", async () => {
      if (typeof onAction === "function") {
        await onAction();
      }
      toast.remove();
    });
    actions.appendChild(actionBtn);
    content.appendChild(actions);
  }

  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "toast-close";
  closeBtn.setAttribute("aria-label", "Fermer");
  closeBtn.textContent = "x";
  closeBtn.addEventListener("click", () => toast.remove());

  toast.appendChild(icon);
  toast.appendChild(content);
  toast.appendChild(closeBtn);
  toastHost.prepend(toast);

  if (durationMs > 0) {
    window.setTimeout(() => {
      toast.remove();
    }, durationMs);
  }
}

function isStructureMismatchMessage(message) {
  const text = String(message || "").toLowerCase();
  return (
    text.includes("chargement bloque") ||
    text.includes("ne respecte pas la structure") ||
    text.includes("signature structurelle") ||
    text.includes("longueur mediane")
  );
}

function setError(message) {
  if (!message) {
    return;
  }
  const text = String(message || "");
  if (shouldThrottleNotice(`error:${text}`)) {
    return;
  }
  if (isStructureMismatchMessage(text)) {
    openModal({
      type: "error",
      title: "Erreur de chargement",
      message: text,
      primaryLabel: "Compris",
    });
    return;
  }
  showToast({
    type: "error",
    title: "Erreur",
    message: text,
    durationMs: 6500,
  });
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
  programSelect.disabled = isBusy;
  localProgramFile.disabled = isBusy;
  loadLocalProgramBtn.disabled = isBusy;
  flowTypeSelect.disabled = isBusy;
  fileNameSelect.disabled = isBusy;
  advancedModeToggle.disabled = isBusy;
  launchBtn.textContent = isBusy ? "Extraction en cours..." : "Extraction Excel/PDF";
  setAdvancedModeIndicator();
}

function isLocalProgramSelection() {
  return String(programSelect?.value || "") === LOCAL_PROGRAM_OPTION_VALUE;
}

function currentProgramId() {
  if (isLocalProgramSelection()) {
    return String(localProgramId || "").toLowerCase();
  }
  return String(programSelect?.value || knownPrograms?.[0]?.program_id || "idp470ra").toLowerCase();
}

function isPrimaryProgram(programId) {
  return String(programId || "").toLowerCase() === PRIMARY_PROGRAM_ID;
}

function currentProgramConfig() {
  const id = currentProgramId();
  return knownPrograms.find((item) => String(item?.program_id || "").toLowerCase() === id) || null;
}

function isInvoiceOnlyProgram() {
  const program = currentProgramConfig();
  return Boolean(program?.invoice_only_default);
}

function cloneProfiles(profiles) {
  return Array.isArray(profiles) ? JSON.parse(JSON.stringify(profiles)) : [];
}

function captureUiState() {
  return {
    programSelectValue: String(programSelect?.value || ""),
    localProgramId: localProgramId ? String(localProgramId) : null,
    advancedMode: isAdvancedModeEnabled(),
    flowTypeValue: String(flowTypeSelect?.value || "").toLowerCase(),
    fileNameValue: String(fileNameSelect?.value || "").toUpperCase(),
    catalogProfiles: cloneProfiles(catalogProfiles),
    catalogSourceProgram: String(catalogSourceProgram || "programme"),
    catalogStatusText: String(catalogStatus?.textContent || ""),
    localProgramStatusText: String(localProgramStatus?.textContent || ""),
  };
}

function removeKnownProgram(programId) {
  const safeId = String(programId || "").toLowerCase();
  if (!safeId) {
    return;
  }
  knownPrograms = knownPrograms.filter((item) => String(item?.program_id || "").toLowerCase() !== safeId);
}

function restoreUiState(snapshot, options = {}) {
  if (!snapshot) {
    return;
  }
  const removeProgramId = String(options.removeProgramId || "").toLowerCase();
  if (removeProgramId) {
    removeKnownProgram(removeProgramId);
  }

  localProgramId = snapshot.localProgramId ? String(snapshot.localProgramId).toLowerCase() : null;
  rebuildProgramOptions(snapshot.programSelectValue || PRIMARY_PROGRAM_ID);
  programSelect.value = snapshot.programSelectValue || PRIMARY_PROGRAM_ID;
  advancedModeToggle.checked = Boolean(snapshot.advancedMode);
  setAdvancedModeIndicator();
  toggleLocalProgramPanel(isLocalProgramSelection());

  catalogSourceProgram = snapshot.catalogSourceProgram || "programme";
  catalogProfiles = cloneProfiles(snapshot.catalogProfiles);
  if (catalogProfiles.length > 0) {
    loadFlowOptions(snapshot.flowTypeValue || "output", snapshot.fileNameValue || "FICDEMA");
  } else {
    flowTypeSelect.innerHTML = "";
    fileNameSelect.innerHTML = "";
    renderProfileContext(null);
    launchBtn.disabled = true;
  }

  if (snapshot.catalogStatusText) {
    setCatalogStatus(snapshot.catalogStatusText);
  }
  if (snapshot.localProgramStatusText) {
    setLocalProgramStatus(snapshot.localProgramStatusText);
  }
}

function cacheKeyForCatalog(programId, advancedMode) {
  const safeProgramId = String(programId || "default").toLowerCase();
  return advancedMode ? `idil.catalog.${safeProgramId}.advanced` : `idil.catalog.${safeProgramId}.standard`;
}

function readCatalogCache(programId, advancedMode) {
  try {
    const raw = localStorage.getItem(cacheKeyForCatalog(programId, advancedMode));
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw);
    const ts = Number(parsed?.timestamp || 0);
    if (!Array.isArray(parsed?.profiles) || !ts) {
      return null;
    }
    if (Date.now() - ts > CATALOG_CACHE_TTL_MS) {
      return null;
    }
    return parsed;
  } catch (error) {
    return null;
  }
}

function writeCatalogCache(programId, advancedMode, payload) {
  try {
    localStorage.setItem(
      cacheKeyForCatalog(programId, advancedMode),
      JSON.stringify({
        timestamp: Date.now(),
        profiles: payload?.profiles || [],
        default_flow_type: payload?.default_flow_type || "output",
        default_file_name: payload?.default_file_name || "FICDEMA",
      }),
    );
  } catch (error) {
    // Ignore storage errors
  }
}

function setCatalogStatus(message, isLoading = false) {
  if (!catalogStatus) {
    return;
  }
  catalogStatus.textContent = message || "";
  catalogStatus.classList.toggle("is-loading", Boolean(isLoading));
}

function clearCatalogSelections() {
  catalogProfiles = [];
  flowTypeSelect.innerHTML = "";
  fileNameSelect.innerHTML = "";
  updateUploadLabel();
}

function notifyNoFlow(programName) {
  const safeProgram = programName || "le programme selectionne";
  const throttleKey = `no-flow:${safeProgram}:${isAdvancedModeEnabled() ? "advanced" : "standard"}`;
  if (shouldThrottleNotice(throttleKey, 3000)) {
    return;
  }
  const message =
    `Aucun flux n'a ete trouve pour ${safeProgram}. ` +
    "Verifiez le programme selectionne, ou activez la vue globale pour afficher tous les fichiers.";

  if (!isAdvancedModeEnabled()) {
    openModal({
      type: "warning",
      title: "Aucun flux disponible",
      message,
      primaryLabel: "Activer la vue globale",
      onPrimary: async () => {
        advancedModeToggle.checked = true;
        setAdvancedModeIndicator();
        await loadCatalog();
      },
      secondaryLabel: "Fermer",
    });
    return;
  }

  openModal({
    type: "warning",
    title: "Aucun flux disponible",
    message,
    primaryLabel: "Fermer",
  });
}

async function fetchWithTimeout(url, options = {}, timeoutMs = CATALOG_REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function formatCatalogReadyMessage(count, advancedMode, sourceProgram, profiles = []) {
  const safeCount = Number.isFinite(count) ? count : 0;
  const safeProgram = sourceProgram || "programme";
  const profileList = Array.isArray(profiles) ? profiles : [];
  const hasInvoiceProfiles = profileList.some(
    (profile) => String(profile?.view_mode || "").toLowerCase() === "invoice",
  );
  if (advancedMode) {
    return `Mode avance actif (${safeProgram}): ${safeCount} flux Input/Output disponible(s).`;
  }
  if (!hasInvoiceProfiles) {
    return `Mode standard actif (${safeProgram}): ${safeCount} flux disponible(s).`;
  }
  return (
    `Mode standard actif (${safeProgram}): ${safeCount} fichier(s) de facturation disponible(s). ` +
    "Activez le mode avance pour afficher tous les flux."
  );
}

function fallbackPrograms() {
  knownPrograms = [
    {
      program_id: "idp470ra",
      display_name: "IDIL470 PROJET PAPYRUS",
      source_program: "IDP470RA",
      analyzer_engine: "idp470_pli",
      source_path: "IDP470RA.pli",
      invoice_only_default: true,
    },
  ];
}

function setLocalProgramStatus(message, isError = false) {
  if (!localProgramStatus) {
    return;
  }
  localProgramStatus.textContent = message || "";
  localProgramStatus.style.color = isError ? "#b42318" : "";
}

function toggleLocalProgramPanel(visible) {
  if (!localProgramPanel) {
    return;
  }
  localProgramPanel.classList.toggle("hidden", !visible);
  if (!visible) {
    setLocalProgramStatus("Aucun programme local charge.");
    return;
  }
  if (!localProgramId) {
    setLocalProgramStatus("Aucun programme local charge. Selectionnez un fichier puis cliquez sur Analyser ce programme.");
  }
}

function upsertProgram(program) {
  const normalizedId = String(program?.program_id || "").toLowerCase();
  if (!normalizedId) {
    return;
  }
  const existingIndex = knownPrograms.findIndex((item) => String(item.program_id || "").toLowerCase() === normalizedId);
  if (existingIndex >= 0) {
    knownPrograms[existingIndex] = { ...knownPrograms[existingIndex], ...program, program_id: normalizedId };
    return;
  }
  knownPrograms.push({ ...program, program_id: normalizedId });
}

function rebuildProgramOptions(defaultProgramId = "idp470ra") {
  const wanted = String(defaultProgramId || "idp470ra").toLowerCase();
  programSelect.innerHTML = "";
  knownPrograms.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.program_id;
    option.textContent = `${item.display_name || item.program_id} (${item.source_program || "-"})`;
    programSelect.appendChild(option);
  });
  const localOption = document.createElement("option");
  localOption.value = LOCAL_PROGRAM_OPTION_VALUE;
  localOption.textContent = "Charger un programme local...";
  programSelect.appendChild(localOption);
  if (!knownPrograms.length) {
    programSelect.value = LOCAL_PROGRAM_OPTION_VALUE;
    return;
  }
  if (wanted === LOCAL_PROGRAM_OPTION_VALUE) {
    programSelect.value = LOCAL_PROGRAM_OPTION_VALUE;
    return;
  }
  const found = knownPrograms.some((item) => item.program_id === wanted);
  programSelect.value = found ? wanted : knownPrograms[0].program_id;
}

async function registerLocalProgram() {
  const sourceFile = localProgramFile.files?.[0];
  if (!sourceFile) {
    setLocalProgramStatus("Selectionnez un programme local avant l'analyse.", true);
    return null;
  }

  const formData = new FormData();
  formData.append("source_file", sourceFile, sourceFile.name);
  setLocalProgramStatus("Analyse du programme local en cours...");
  loadLocalProgramBtn.disabled = true;

  try {
    const response = await fetch("/api/programs/local", {
      method: "POST",
      body: formData,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || "Impossible d'analyser ce programme local.");
    }
    const program = await response.json();
    localProgramId = String(program.program_id || "").toLowerCase();
    upsertProgram(program);
    rebuildProgramOptions(localProgramId);
    setLocalProgramStatus(
      `Programme local charge: ${program.display_name} (${program.source_program}). Analyse des flux en cours...`,
    );
    return localProgramId;
  } catch (error) {
    setLocalProgramStatus(error.message || "Echec de l'analyse du programme local.", true);
    setError(error.message || "Echec de l'analyse du programme local.");
    return null;
  } finally {
    loadLocalProgramBtn.disabled = false;
  }
}

async function loadPrograms() {
  try {
    const response = await fetch("/api/programs");
    if (!response.ok) {
      throw new Error("Programme indisponible");
    }
    const payload = await response.json();
    knownPrograms = Array.isArray(payload.programs) ? payload.programs : [];
    if (!knownPrograms.length) {
      fallbackPrograms();
      rebuildProgramOptions("idp470ra");
      return "idp470ra";
    }
    rebuildProgramOptions(String(payload.default_program_id || knownPrograms[0].program_id).toLowerCase());
    return currentProgramId();
  } catch (error) {
    fallbackPrograms();
    rebuildProgramOptions("idp470ra");
    showToast({
      type: "warning",
      title: "Mode degrade",
      message: "Liste des programmes indisponible. Programme par defaut active.",
    });
    return "idp470ra";
  }
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

  if (statusKey === "completed" || statusKey === "failed") {
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
  statusValue.textContent = config.label;
  statusBadge.textContent = config.label;
  statusBadge.className = `status-badge ${config.badgeClass}`;
  statusPanel.dataset.state = statusKey || "queued";
  progressWrap.setAttribute("aria-label", `Progression ${config.label}`);
}

function formatModeLabel(mode) {
  return String(mode || "").toLowerCase() === "invoice" ? "Facture" : "Hors facture";
}

function firstStructures(profile) {
  const values = Array.isArray(profile?.raw_structures) ? profile.raw_structures : [];
  if (values.length === 0) {
    return "Aucune";
  }
  if (values.length <= 4) {
    return values.join(", ");
  }
  return `${values.slice(0, 4).join(", ")} ... (+${values.length - 4})`;
}

function renderProfileContext(profile) {
  if (!profile) {
    profileContext.innerHTML = "";
    return;
  }
  const statusText = profile.supports_processing ? "Mapping actif" : "Mapping non detecte";
  const chips = [
    `Role: ${profile.role_label || "metier"}`,
    `Mode: ${formatModeLabel(profile.view_mode)}`,
    statusText,
    `Structures: ${firstStructures(profile)}`,
  ];
  profileContext.innerHTML = "";
  chips.forEach((entry) => {
    const chip = document.createElement("span");
    chip.className = "context-chip";
    chip.textContent = entry;
    profileContext.appendChild(chip);
  });
}

function setKpiCards(kpis) {
  const safe = Array.isArray(kpis) ? kpis.slice(0, 3) : [];
  const padded = [
    safe[0] || { label: "Indicateur 1", value: 0 },
    safe[1] || { label: "Indicateur 2", value: 0 },
    safe[2] || { label: "Indicateur 3", value: 0 },
  ];

  kpiLabel1.textContent = padded[0].label || "Indicateur 1";
  kpiLabel2.textContent = padded[1].label || "Indicateur 2";
  kpiLabel3.textContent = padded[2].label || "Indicateur 3";
  setMetricValue(kpiValue1, Number(padded[0].value || 0));
  setMetricValue(kpiValue2, Number(padded[1].value || 0));
  setMetricValue(kpiValue3, Number(padded[2].value || 0));
}

function resetKpisForProfile(profile) {
  if (profile?.view_mode === "invoice") {
    setKpiCards([
      { label: "Clients", value: 0 },
      { label: "Factures", value: 0 },
      { label: "Lignes fichier", value: 0 },
    ]);
    return;
  }
  setKpiCards([
    { label: "Enregistrements", value: 0 },
    { label: "Types detectes", value: 0 },
    { label: "Champs structures", value: 0 },
  ]);
}

function updateDownloadMode(profile, downloads = null) {
  setDownload(downloadExcel, downloads?.excel || null);
  if (profile?.supports_pdf) {
    downloadPdfFactures.style.display = "";
    downloadPdfSynthese.style.display = "";
    setDownload(downloadPdfFactures, downloads?.pdf_factures || null);
    setDownload(downloadPdfSynthese, downloads?.pdf_synthese || null);
  } else {
    downloadPdfFactures.style.display = "none";
    downloadPdfSynthese.style.display = "none";
    setDownload(downloadPdfFactures, null);
    setDownload(downloadPdfSynthese, null);
  }
}

function applyStatus(payload) {
  const rawStatus = String(payload.status || "").toLowerCase().trim();
  const safeStatus = STATUS_CONFIG[rawStatus] ? rawStatus : "queued";
  const progress = normalizeProgress(safeStatus, payload.progress);

  jobIdValue.textContent = payload.job_id || "-";
  setStatusVisual(safeStatus, payload.status || "-");
  messageValue.textContent = payload.message || "-";
  updatedAtValue.textContent = `Derniere mise a jour: ${toLocalDateTime(payload.updated_at)}`;
  progressBar.style.width = `${progress}%`;
  progressValue.textContent = `${progress}%`;

  setWarnings(payload.warnings || []);
  setKpiCards(payload.kpis || []);
  updateDownloadMode(
    {
      supports_pdf: (payload.view_mode || "generic") === "invoice",
    },
    payload.downloads || {},
  );
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
    if (payload.status === "completed" && completionNotifiedForJob !== payload.job_id) {
      completionNotifiedForJob = payload.job_id;
      showToast({
        type: "success",
        title: "Extraction terminee",
        message: "Le traitement est termine. Les fichiers Excel/PDF sont disponibles au telechargement.",
      });
    }
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

function isAdvancedModeEnabled() {
  return Boolean(advancedModeToggle?.checked);
}

function setAdvancedModeIndicator() {
  if (!advancedModeState) {
    return;
  }
  const isAdvanced = isAdvancedModeEnabled();
  advancedModeState.textContent = isAdvanced ? "Avance" : "Standard";
  advancedModeState.classList.toggle("is-on", isAdvanced);
}

function updateUploadLabel() {
  const profile = selectedProfile();
  if (!profile) {
    dataFileLabel.textContent = "Charger le fichier (obligatoire)";
    renderProfileContext(null);
    launchBtn.disabled = true;
    return;
  }

  dataFileLabel.textContent = `Charger le fichier ${profile.file_name} (obligatoire)`;
  renderProfileContext(profile);
  resetKpisForProfile(profile);
  updateDownloadMode(profile);
  if (isInvoiceOnlyProgram() && !isAdvancedModeEnabled() && profile.view_mode !== "invoice") {
    launchBtn.disabled = true;
    setError("Mode standard actif: seuls les fichiers Factures sont autorises.");
    return;
  }
  launchBtn.disabled = !profile.supports_processing;
  if (!profile.supports_processing) {
    setError(
      `Le fichier ${profile.file_name} est detecte, mais aucun mapping structurel exploitable n'a ete trouve.`,
    );
  } else {
    setError("");
  }
}

function labelForFlowType(flowType) {
  return String(flowType || "").toLowerCase() === "input" ? "Input" : "Output";
}

function optionTitle(profile) {
  const mode = formatModeLabel(profile.view_mode);
  const role = profile.role_label || "metier";
  return `${profile.description || ""} | Role: ${role} | Mode: ${mode}`;
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
    const suffix = profile.supports_processing ? "" : " (non mappe)";
    option.textContent = `${profile.display_name || profile.file_name}${suffix}`;
    option.title = optionTitle(profile);
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
    clearCatalogSelections();
    return;
  }
  flowTypeSelect.value = flowTypes.includes(defaultFlowType) ? defaultFlowType : flowTypes[0];
  rebuildFileOptions(defaultFileName);
}

async function loadCatalog(preferredSelection = null, options = {}) {
  const allowFallback = options.allowFallback !== undefined ? Boolean(options.allowFallback) : true;
  const silentNoFlowNotice = Boolean(options.silentNoFlowNotice);
  const silentFailureNotice = Boolean(options.silentFailureNotice);
  lastCatalogFailureKind = "";
  lastCatalogFailureMessage = "";

  const programId = currentProgramId();
  const primaryProgram = isPrimaryProgram(programId);
  if (isLocalProgramSelection() && !programId) {
    clearCatalogSelections();
    lastCatalogFailureKind = "missing_program";
    lastCatalogFailureMessage = "Chargez d'abord un programme local, puis lancez l'analyse.";
    setLocalProgramStatus("Aucun programme local charge. Selectionnez un fichier puis cliquez sur Analyser ce programme.");
    setCatalogStatus(lastCatalogFailureMessage);
    return false;
  }

  const advancedMode = isAdvancedModeEnabled();
  setCatalogStatus("Analyse des flux du programme en cours...", true);
  try {
    const response = await fetchWithTimeout(
      `/api/catalog?program_id=${encodeURIComponent(programId)}&advanced=${advancedMode ? "true" : "false"}`,
    );
    if (!response.ok) {
      throw new Error("Catalogue indisponible");
    }
    const payload = await response.json();
    if (payload?.program_display_name) {
      setLocalProgramStatus(`Programme actif: ${payload.program_display_name} (${payload.source_program || "-"})`);
    }
    writeCatalogCache(programId, advancedMode, payload);
    catalogSourceProgram = payload?.source_program || "programme";
    catalogProfiles = Array.isArray(payload.profiles) ? payload.profiles : [];
    if (catalogProfiles.length === 0) {
      clearCatalogSelections();
      const noFlowMessage =
        `Aucun flux exploitable detecte pour ${catalogSourceProgram}. ` +
        "Verifiez votre selection de programme ou la source chargee.";
      lastCatalogFailureKind = "no_flow";
      lastCatalogFailureMessage = noFlowMessage;
      setCatalogStatus(noFlowMessage);
      if (!silentNoFlowNotice) {
        if (primaryProgram) {
          notifyNoFlow(catalogSourceProgram);
        } else {
          openModal({
            type: "error",
            title: "Aucun fichier detecte",
            message: noFlowMessage,
            primaryLabel: "Fermer",
          });
        }
      }
      return false;
    }
    const preferredFlow = String(preferredSelection?.flow_type || payload.default_flow_type || "output").toLowerCase();
    const preferredFile = String(preferredSelection?.file_name || payload.default_file_name || "FICDEMA").toUpperCase();
    loadFlowOptions(preferredFlow, preferredFile);
    setCatalogStatus(formatCatalogReadyMessage(catalogProfiles.length, advancedMode, catalogSourceProgram, catalogProfiles));
    lastStableState = captureUiState();
    return true;
  } catch (error) {
    const timeout = String(error?.name || "") === "AbortError";
    const failureMessage = timeout
      ? "Le chargement du catalogue a depasse le delai. Verifiez le programme source puis relancez."
      : `Impossible de charger les flux pour ${catalogSourceProgram || programId || "le programme selectionne"}.`;

    lastCatalogFailureKind = "error";
    lastCatalogFailureMessage = failureMessage;

    if (!allowFallback || !primaryProgram) {
      clearCatalogSelections();
      setCatalogStatus(failureMessage);
      if (!silentFailureNotice) {
        openModal({
          type: "error",
          title: "Chargement programme impossible",
          message: failureMessage,
          primaryLabel: "Fermer",
        });
      }
      return false;
    }

    fallbackCatalog();
    if (preferredSelection) {
      loadFlowOptions(preferredSelection.flow_type, preferredSelection.file_name);
    } else {
      loadFlowOptions("output", "FICDEMA");
    }
    setError("Catalogue non charge. Mode de secours active.");
    if (timeout) {
      setError("Le chargement du catalogue a depasse le delai. Verifiez le programme source puis relancez.");
    }
    setCatalogStatus(
      `Mode secours actif: ${catalogProfiles.length} fichier(s) affiche(s), verification structurelle indisponible.`,
    );
    showToast({
      type: "warning",
      title: "Catalogue en mode secours",
      message: "Le catalogue principal n'est pas joignable. Verification structurelle limitee.",
    });
    lastStableState = captureUiState();
    return true;
  }
}

if (alertModalClose) {
  alertModalClose.addEventListener("click", () => {
    dismissModal();
  });
}

if (alertModal) {
  alertModal.addEventListener("click", (event) => {
    if (event.target === alertModal) {
      dismissModal();
    }
  });
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && alertModal && !alertModal.classList.contains("hidden")) {
    dismissModal();
  }
});

flowTypeSelect.addEventListener("change", () => {
  rebuildFileOptions();
});

fileNameSelect.addEventListener("change", () => {
  updateUploadLabel();
});

programSelect.addEventListener("change", async () => {
  setError("");
  const localSelected = isLocalProgramSelection();
  toggleLocalProgramPanel(localSelected);
  if (localSelected && !localProgramId) {
    clearCatalogSelections();
    setCatalogStatus("Mode programme local: chargez et analysez votre source pour afficher les flux.");
    return;
  }

  const nextProgramId = currentProgramId();
  const strictCatalogMode = !isPrimaryProgram(nextProgramId);
  const loaded = await loadCatalog(null, {
    allowFallback: !strictCatalogMode,
    silentNoFlowNotice: strictCatalogMode,
    silentFailureNotice: strictCatalogMode,
  });
  if (!loaded && strictCatalogMode) {
    const reason = lastCatalogFailureMessage || "Aucun flux exploitable n'a ete detecte.";
    if (lastStableState) {
      restoreUiState(lastStableState);
      openModal({
        type: "error",
        title: "Chargement programme impossible",
        message: `${reason} Retour a l'etat precedent.`,
        primaryLabel: "Compris",
      });
      return;
    }
    openModal({
      type: "error",
      title: "Chargement programme impossible",
      message: reason,
      primaryLabel: "Fermer",
    });
  }
});

loadLocalProgramBtn.addEventListener("click", async () => {
  const snapshotBeforeLoad = captureUiState();
  const newProgramId = await registerLocalProgram();
  if (!newProgramId) {
    return;
  }
  toggleLocalProgramPanel(true);
  const strictCatalogMode = !isPrimaryProgram(newProgramId);
  const loaded = await loadCatalog(null, {
    allowFallback: !strictCatalogMode,
    silentNoFlowNotice: strictCatalogMode,
    silentFailureNotice: strictCatalogMode,
  });

  if (!loaded && strictCatalogMode) {
    const reason =
      lastCatalogFailureMessage ||
      `Aucun fichier exploitable n'a ete detecte pour ${String(newProgramId).toUpperCase()}.`;
    restoreUiState(snapshotBeforeLoad, { removeProgramId: newProgramId });
    openModal({
      type: "error",
      title: "Programme sans flux exploitable",
      message: `${reason} Retour a l'etat precedent.`,
      primaryLabel: "Compris",
    });
    return;
  }

  const activeProgram = currentProgramConfig();
  const sourceProgram = activeProgram?.source_program || String(newProgramId).toUpperCase();
  const flowCount = Array.isArray(catalogProfiles) ? catalogProfiles.length : 0;
  setLocalProgramStatus(
    `Programme local analyse: ${activeProgram?.display_name || sourceProgram} (${sourceProgram}). ${flowCount} flux detecte(s).`,
  );
  showToast({
    type: "success",
    title: "Programme local analyse",
    message: `${sourceProgram}: ${flowCount} flux detecte(s), catalogue pret.`,
  });
});

advancedModeToggle.addEventListener("change", () => {
  setAdvancedModeIndicator();
  const currentSelection = {
    flow_type: String(flowTypeSelect.value || "output").toLowerCase(),
    file_name: String(fileNameSelect.value || "FICDEMA").toUpperCase(),
  };
  loadCatalog(currentSelection);
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("");
  setWarnings([]);

  const file = fileInput.files?.[0];
  const selectedProgramId = currentProgramId();
  const profile = selectedProfile();
  const advancedMode = isAdvancedModeEnabled();

  if (!selectedProgramId) {
    setError("Chargez et analysez d'abord un programme local.");
    return;
  }
  if (!profile) {
    setError("Selection flux/fichier invalide.");
    return;
  }
  if (isInvoiceOnlyProgram() && !advancedMode && profile.view_mode !== "invoice") {
    setError("Mode standard actif: seuls les fichiers Factures sont autorises.");
    return;
  }
  if (!profile.supports_processing) {
    setError(`Le fichier ${profile.file_name} n'est pas encore exploitable automatiquement.`);
    return;
  }
  if (!file) {
    setError(`Chargez le fichier ${profile.file_name} avant de lancer le traitement.`);
    return;
  }

  const formData = new FormData();
  formData.append("program_id", selectedProgramId);
  formData.append("flow_type", profile.flow_type);
  formData.append("file_name", profile.file_name);
  formData.append("advanced_mode", advancedMode ? "true" : "false");
  formData.append("data_file", file, file.name);

  try {
    setBusy(true);
    completionNotifiedForJob = null;
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
    updatedAtValue.textContent = "Derniere mise a jour: -";
    progressBar.style.width = "2%";
    progressValue.textContent = "2%";
    resetKpisForProfile(profile);
    updateDownloadMode(profile);

    startPolling(activeJobId);
  } catch (error) {
    setBusy(false);
    setError(error.message || "Erreur inattendue.");
  }
});

(async function initCatalog() {
  setAdvancedModeIndicator();
  const selectedProgramId = await loadPrograms();
  toggleLocalProgramPanel(isLocalProgramSelection());
  const selectedProgram = knownPrograms.find((item) => item.program_id === selectedProgramId);
  catalogSourceProgram = selectedProgram?.source_program || "programme";
  const advancedMode = isAdvancedModeEnabled();
  const cached = readCatalogCache(selectedProgramId, advancedMode);
  if (cached && Array.isArray(cached.profiles) && cached.profiles.length > 0) {
    catalogProfiles = cached.profiles;
    loadFlowOptions(
      String(cached.default_flow_type || "output").toLowerCase(),
      String(cached.default_file_name || "FICDEMA").toUpperCase(),
    );
    setCatalogStatus(
      `${formatCatalogReadyMessage(catalogProfiles.length, advancedMode, catalogSourceProgram, catalogProfiles)} (charge rapidement depuis le cache local)`,
    );
    await loadCatalog();
    return;
  }

  fallbackCatalog();
  loadFlowOptions("output", "FICDEMA");
  setCatalogStatus("Initialisation du catalogue...", true);
  await loadCatalog();
})();
