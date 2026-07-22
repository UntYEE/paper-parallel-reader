const DEFAULT_PDF = "https://arxiv.org/pdf/1706.03762";
const DEFAULT_TRANSLATION = "./translations/attention-is-all-you-need.sample.json";
const BACKEND_URL =
  new URLSearchParams(window.location.search).get("backend") || "http://127.0.0.1:8787";

const fallbackData = {
  title: "Attention Is All You Need",
  paperUrl: DEFAULT_PDF,
  coverage: "Sample structure only",
  sections: [
    {
      id: "abstract",
      title: "Abstract",
      pageStart: 1,
      paragraphs: [
        {
          id: "abstract-p1",
          page: 1,
          anchor: "Abstract, paragraph 1",
          translation:
            "主流的序列转导模型通常基于复杂的循环神经网络或卷积神经网络，并包含编码器和解码器。表现最好的模型还会通过注意力机制连接编码器和解码器。本文提出一种新的简单网络架构 Transformer，它完全基于注意力机制，彻底去掉循环和卷积。"
        }
      ]
    }
  ]
};

const paperFrame = document.querySelector("#paper-frame");
const pdfUrlInput = document.querySelector("#pdf-url");
const pdfFileInput = document.querySelector("#pdf-file");
const translationUrlInput = document.querySelector("#translation-url");
const translationFileInput = document.querySelector("#translation-file");
const translationList = document.querySelector("#translation-list");
const sectionTemplate = document.querySelector("#section-template");
const paragraphTemplate = document.querySelector("#paragraph-template");
const paperMeta = document.querySelector("#paper-meta");
const searchBox = document.querySelector("#search-box");
const anchorToggle = document.querySelector("#anchor-toggle");
const compactToggle = document.querySelector("#compact-toggle");
const themeToggle = document.querySelector("#theme-toggle");
const generatorForm = document.querySelector("#generator-form");
const generateTitleInput = document.querySelector("#generate-title");
const generateOutputInput = document.querySelector("#generate-output");
const generatePagesInput = document.querySelector("#generate-pages");
const generateParallelismInput = document.querySelector("#generate-parallelism");
const generateSourceModeInput = document.querySelector("#generate-source-mode");
const generateSavedPdfSelect = document.querySelector("#generate-saved-pdf");
const generatePdfInput = document.querySelector("#generate-pdf");
const generateDryRunInput = document.querySelector("#generate-dry-run");
const generateForceInput = document.querySelector("#generate-force");
const generateButton = document.querySelector("#generate-button");
const generateStatus = document.querySelector("#generate-status");

let currentData = fallbackData;
let currentPaperSourceUrl = DEFAULT_PDF;
let currentSavedPdfName = "";
let currentPaperFile = null;

function savedPdfNameFromUrl(url) {
  try {
    const parsed = new URL(url, window.location.href);
    if (!parsed.pathname.includes("/api/papers/")) return "";
    return decodeURIComponent(parsed.pathname.split("/").pop() || "");
  } catch {
    return "";
  }
}

function isBackendPaperUrl(url) {
  try {
    const parsed = new URL(url, window.location.href);
    const backend = new URL(BACKEND_URL);
    return parsed.origin === backend.origin && parsed.pathname.startsWith("/api/papers/");
  } catch {
    return false;
  }
}

function getParam(name, fallback) {
  const value = new URLSearchParams(window.location.search).get(name);
  return value || fallback;
}

function setPaperUrl(url, inputValue = url) {
  currentPaperSourceUrl = inputValue;
  pdfUrlInput.value = inputValue;
  paperFrame.src = url;
}

function setGenerateStatus(message, tone = "neutral") {
  generateStatus.textContent = message;
  generateStatus.dataset.tone = tone;
}

function outputNameFromPdf(name) {
  return name.replace(/\.pdf$/i, ".json");
}

function applyPaperInfo(data) {
  if (!data?.name) return;
  const localPdfUrl = data.fileUrl ? `${BACKEND_URL}${data.fileUrl}` : data.pdfUrl;
  setPaperUrl(localPdfUrl, data.sourceUrl || localPdfUrl);
  selectSavedPdf(data.name);
  if (data.title) {
    generateTitleInput.value = data.title;
  }
  generateOutputInput.value = data.outputName || outputNameFromPdf(data.name);
}

function selectSavedPdf(name) {
  currentSavedPdfName = name || "";
  generateSavedPdfSelect.value = currentSavedPdfName;
}

function pageRange(section) {
  if (section.pageStart && section.pageEnd && section.pageStart !== section.pageEnd) {
    return `pp. ${section.pageStart}-${section.pageEnd}`;
  }
  if (section.pageStart || section.pageEnd) return `p. ${section.pageStart || section.pageEnd}`;
  return "";
}

function normalizeSections(data) {
  if (Array.isArray(data?.sections)) {
    return data.sections.map((section, sectionIndex) => ({
      id: section.id || `section-${sectionIndex + 1}`,
      title: section.title || `Section ${sectionIndex + 1}`,
      pageStart: section.pageStart || "",
      pageEnd: section.pageEnd || "",
      paragraphs: Array.isArray(section.paragraphs)
        ? section.paragraphs.map((paragraph, paragraphIndex) => ({
            id: paragraph.id || `${section.id || `section-${sectionIndex + 1}`}-p${paragraphIndex + 1}`,
            page: paragraph.page || "",
            anchor: paragraph.anchor || "",
            translation: paragraph.translation || "",
            note: paragraph.note || ""
          }))
        : []
    }));
  }

  if (Array.isArray(data?.segments)) {
    return [
      {
        id: "legacy",
        title: "Legacy Segments",
        pageStart: "",
        pageEnd: "",
        paragraphs: data.segments.map((segment, index) => ({
          id: segment.id || `segment-${index + 1}`,
          page: segment.page || "",
          anchor: segment.source ? `Segment ${index + 1}` : "",
          translation: segment.translation || "",
          note: ""
        }))
      }
    ];
  }

  return [];
}

function filterSections(sections, query) {
  const normalizedQuery = query.trim().toLowerCase();
  if (!normalizedQuery) return sections;

  return sections
    .map((section) => {
      const sectionText = `${section.title} ${pageRange(section)}`.toLowerCase();
      const paragraphs = section.paragraphs.filter((paragraph) => {
        const paragraphText = `${paragraph.anchor} ${paragraph.translation} ${paragraph.note}`.toLowerCase();
        return sectionText.includes(normalizedQuery) || paragraphText.includes(normalizedQuery);
      });
      return { ...section, paragraphs };
    })
    .filter((section) => section.paragraphs.length > 0);
}

function renderTranslation(data, query = "") {
  currentData = data;
  const title = data.title || "Untitled paper";
  const coverage = data.coverage ? ` · ${data.coverage}` : "";
  const sections = filterSections(normalizeSections(data), query);

  paperMeta.textContent = `${title}${coverage}`;
  translationList.innerHTML = "";

  if (!sections.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No matching paragraphs";
    translationList.append(empty);
    return;
  }

  sections.forEach((section, sectionIndex) => {
    const sectionNode = sectionTemplate.content.cloneNode(true);
    const sectionElement = sectionNode.querySelector(".translation-section");
    sectionElement.id = section.id;
    sectionNode.querySelector(".section-number").textContent = `${sectionIndex + 1}`;
    sectionNode.querySelector(".section-title").textContent = section.title;
    sectionNode.querySelector(".section-pages").textContent = pageRange(section);

    const paragraphsElement = sectionNode.querySelector(".paragraphs");
    section.paragraphs.forEach((paragraph, paragraphIndex) => {
      const paragraphNode = paragraphTemplate.content.cloneNode(true);
      paragraphNode.querySelector(".paragraph-index").textContent = `${sectionIndex + 1}.${paragraphIndex + 1}`;
      paragraphNode.querySelector(".paragraph-page").textContent = paragraph.page ? `p. ${paragraph.page}` : "";
      paragraphNode.querySelector(".paragraph-anchor").textContent = paragraph.anchor;
      paragraphNode.querySelector(".paragraph-translation").textContent = paragraph.translation;
      paragraphNode.querySelector(".paragraph-note").textContent = paragraph.note;
      paragraphsElement.append(paragraphNode);
    });

    translationList.append(sectionNode);
  });
}

async function loadTranslation(url) {
  translationUrlInput.value = url;
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderTranslation(data, searchBox.value);
  } catch (error) {
    console.warn("Failed to load translation JSON. Using fallback sample.", error);
    renderTranslation(fallbackData, searchBox.value);
  }
}

async function checkBackend() {
  try {
    const response = await fetch(`${BACKEND_URL}/api/health`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const keyState = data.deepseek_api_key_configured ? "key ready" : "key missing";
    setGenerateStatus(`Backend ready, ${keyState}`, data.deepseek_api_key_configured ? "ok" : "warn");
  } catch {
    setGenerateStatus("Backend offline: start uvicorn on :8787", "warn");
  }
}

async function refreshDownloadedPapers(selectedName = currentSavedPdfName) {
  try {
    const response = await fetch(`${BACKEND_URL}/api/papers`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const papers = Array.isArray(data.papers) ? data.papers : [];
    generateSavedPdfSelect.innerHTML = "";

    const emptyOption = document.createElement("option");
    emptyOption.value = "";
    emptyOption.textContent = "Use uploaded PDF";
    generateSavedPdfSelect.append(emptyOption);

    papers.forEach((paper) => {
      const option = document.createElement("option");
      option.value = paper.name;
      option.textContent = paper.translationUrl ? `${paper.name} (cached)` : paper.name;
      option.dataset.sourceUrl = paper.sourceUrl || "";
      option.dataset.fileUrl = paper.fileUrl || "";
      option.dataset.pdfUrl = paper.pdfUrl || "";
      option.dataset.translationUrl = paper.translationUrl || "";
      generateSavedPdfSelect.append(option);
    });

    selectSavedPdf(selectedName && papers.some((paper) => paper.name === selectedName) ? selectedName : "");
  } catch {
    generateSavedPdfSelect.innerHTML = '<option value="">Backend offline</option>';
  }
}

async function downloadAndDisplayPaper(url) {
  const existingName = savedPdfNameFromUrl(url);
  if (isBackendPaperUrl(url) && existingName) {
    await refreshDownloadedPapers(existingName);
    const response = await fetch(`${BACKEND_URL}/api/inspect-paper/${encodeURIComponent(existingName)}`, {
      cache: "no-store"
    });
    const data = await response.json().catch(() => ({}));
    if (response.ok && data.ok) {
      applyPaperInfo(data);
    } else {
      const localPdfUrl = url.startsWith("http") ? url : `${BACKEND_URL}${url}`;
      setPaperUrl(localPdfUrl, localPdfUrl);
      selectSavedPdf(existingName);
      generateOutputInput.value = outputNameFromPdf(existingName);
    }
    setGenerateStatus(`Loaded local ${existingName}`, "ok");
    return;
  }

  const formData = new FormData();
  formData.set("url", url);
  setGenerateStatus("Checking local PDF cache...", "busy");
  const cacheResponse = await fetch(`${BACKEND_URL}/api/check-paper-cache`, {
    method: "POST",
    body: formData
  });
  const cacheData = await cacheResponse.json().catch(() => ({}));
  if (!cacheResponse.ok || !cacheData.ok) {
    throw new Error(cacheData.detail || `HTTP ${cacheResponse.status}`);
  }
  if (cacheData.cached) {
    await refreshDownloadedPapers(cacheData.name);
    applyPaperInfo(cacheData);
    setGenerateStatus(`Loaded cached ${cacheData.name}`, "ok");
    return;
  }

  setGenerateStatus(`Downloading ${cacheData.name}...`, "busy");
  const downloadResponse = await fetch(`${BACKEND_URL}/api/download-paper`, {
    method: "POST",
    body: formData
  });
  const data = await downloadResponse.json().catch(() => ({}));
  if (!downloadResponse.ok || !data.ok) {
    throw new Error(data.detail || `HTTP ${downloadResponse.status}`);
  }

  await refreshDownloadedPapers(data.name);
  applyPaperInfo(data);
  setGenerateStatus(data.cached ? `Loaded cached ${data.name}` : `Downloaded ${data.name}`, "ok");
}

async function uploadAndDisplayPaper(file, sourceUrl = "") {
  const formData = new FormData();
  formData.set("pdf", file);
  formData.set("source_url", sourceUrl || `file:${file.name}`);
  formData.set("output_name", file.name);
  setGenerateStatus("Saving local PDF...", "busy");
  const response = await fetch(`${BACKEND_URL}/api/upload-paper`, {
    method: "POST",
    body: formData
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.ok) {
    throw new Error(data.detail || `HTTP ${response.status}`);
  }

  currentPaperFile = null;
  await refreshDownloadedPapers(data.name);
  applyPaperInfo(data);
  setGenerateStatus(`Saved local ${data.name}`, "ok");
}

function readJsonFile(file) {
  const reader = new FileReader();
  reader.addEventListener("load", () => {
    try {
      const data = JSON.parse(String(reader.result));
      renderTranslation(data, searchBox.value);
    } catch (error) {
      console.error("Invalid translation JSON", error);
      renderTranslation(fallbackData, searchBox.value);
    }
  });
  reader.readAsText(file);
}

document.querySelector("#paper-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const url = pdfUrlInput.value.trim() || DEFAULT_PDF;
  if (/^https?:\/\//i.test(url)) {
    downloadAndDisplayPaper(url).catch((error) => {
      setGenerateStatus(`Download failed: ${error.message}`, "warn");
      setPaperUrl(url);
    });
    return;
  }
  setPaperUrl(url);
});

document.querySelector("#translation-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadTranslation(translationUrlInput.value.trim() || DEFAULT_TRANSLATION);
});

generatorForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const [selectedGeneratePdfFile] = generatePdfInput.files;
  const pdfFile = selectedGeneratePdfFile || currentPaperFile;
  const savedPdf = generateSavedPdfSelect.value;
  if (!savedPdf && !pdfFile) {
    setGenerateStatus("Choose a downloaded PDF or upload a PDF", "warn");
    return;
  }

  const title = generateTitleInput.value.trim();
  const outputName = generateOutputInput.value.trim();
  const selectedSavedOption = generateSavedPdfSelect.selectedOptions[0];
  let paperUrl = selectedSavedOption?.dataset.sourceUrl || currentPaperSourceUrl || DEFAULT_PDF;
  if (!savedPdf && pdfFile && (!paperUrl || paperUrl === DEFAULT_PDF)) {
    paperUrl = `local:${pdfFile.name}`;
  }
  if (!title || !outputName) {
    setGenerateStatus("Title and output are required", "warn");
    return;
  }

  const formData = new FormData();
  formData.set("title", title);
  formData.set("paper_url", paperUrl);
  formData.set("output_name", outputName);
  if (savedPdf) {
    formData.set("saved_pdf", savedPdf);
  } else {
    formData.set("pdf", pdfFile);
  }
  formData.set("dry_run", generateDryRunInput.checked ? "true" : "false");
  formData.set("force", generateForceInput.checked ? "true" : "false");
  if (generatePagesInput.value.trim()) {
    formData.set("pages", generatePagesInput.value.trim());
  }
  if (generateParallelismInput.value.trim()) {
    formData.set("parallelism", generateParallelismInput.value.trim());
  }
  formData.set("source_mode", generateSourceModeInput.value || "auto");

  generateButton.disabled = true;
  setGenerateStatus("Generating translation JSON...", "busy");
  try {
    const response = await fetch(`${BACKEND_URL}/api/generate`, {
      method: "POST",
      body: formData
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data.detail || `HTTP ${response.status}`);
    }

    const outputFile = data.output ? data.output.split("/").pop() : outputName;
    const translationUrl = `./translations/${outputFile.endsWith(".json") ? outputFile : `${outputFile}.json`}`;
    setGenerateStatus(
      data.cached ? "Loaded cached translation JSON" : `Generated ${data.paragraphs} paragraphs`,
      "ok"
    );
    const generatedPdfUrl = data.file_url ? `${BACKEND_URL}${data.file_url}` : data.pdf_url;
    if (generatedPdfUrl) {
      setPaperUrl(generatedPdfUrl, paperUrl);
      await refreshDownloadedPapers(data.pdf_name || (pdfFile ? pdfFile.name : savedPdf));
    }
    loadTranslation(translationUrl);
  } catch (error) {
    setGenerateStatus(`Generate failed: ${error.message}`, "warn");
  } finally {
    generateButton.disabled = false;
  }
});

pdfFileInput.addEventListener("change", async () => {
  const [file] = pdfFileInput.files;
  if (!file) return;
  currentPaperFile = file;
  currentSavedPdfName = "";
  generateSavedPdfSelect.value = "";
  generateOutputInput.value = outputNameFromPdf(file.name);
  setPaperUrl(URL.createObjectURL(file), `local:${file.name}`);
  try {
    const sourceUrl = /^https?:\/\//i.test(pdfUrlInput.value.trim()) ? pdfUrlInput.value.trim() : "";
    await uploadAndDisplayPaper(file, sourceUrl);
  } catch (error) {
    setGenerateStatus(`Local save failed: ${error.message}`, "warn");
  }
});

generatePdfInput.addEventListener("change", async () => {
  const [file] = generatePdfInput.files;
  if (!file) return;
  currentPaperFile = file;
  currentSavedPdfName = "";
  generateSavedPdfSelect.value = "";
  generateOutputInput.value = outputNameFromPdf(file.name);
  setPaperUrl(URL.createObjectURL(file), `local:${file.name}`);
  try {
    const sourceUrl = /^https?:\/\//i.test(pdfUrlInput.value.trim()) ? pdfUrlInput.value.trim() : "";
    await uploadAndDisplayPaper(file, sourceUrl);
  } catch (error) {
    setGenerateStatus(`Local save failed: ${error.message}`, "warn");
  }
});

translationFileInput.addEventListener("change", () => {
  const [file] = translationFileInput.files;
  if (!file) return;
  readJsonFile(file);
});

generateSavedPdfSelect.addEventListener("change", () => {
  const option = generateSavedPdfSelect.selectedOptions[0];
  const fileUrl = option?.dataset.fileUrl;
  const sourceUrl = option?.dataset.sourceUrl;
  currentSavedPdfName = generateSavedPdfSelect.value;
  if (fileUrl) {
    currentPaperFile = null;
    setPaperUrl(`${BACKEND_URL}${fileUrl}`, sourceUrl || `${BACKEND_URL}${fileUrl}`);
    generateOutputInput.value = outputNameFromPdf(currentSavedPdfName);
    if (option.dataset.translationUrl) {
      loadTranslation(option.dataset.translationUrl.replace("/viewer/", "./"));
      setGenerateStatus("Cached translation available", "ok");
    }
  }
});

searchBox.addEventListener("input", () => {
  renderTranslation(currentData, searchBox.value);
});

anchorToggle.addEventListener("change", () => {
  document.body.classList.toggle("hide-anchors", !anchorToggle.checked);
});

compactToggle.addEventListener("click", () => {
  document.body.classList.toggle("compact");
});

themeToggle.addEventListener("click", () => {
  document.body.classList.toggle("dark");
  themeToggle.textContent = document.body.classList.contains("dark") ? "Light" : "Dark";
});

setPaperUrl(getParam("pdf", DEFAULT_PDF));
loadTranslation(getParam("translation", DEFAULT_TRANSLATION));
checkBackend();
refreshDownloadedPapers();
