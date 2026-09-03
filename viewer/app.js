const DEFAULT_PDF = "https://arxiv.org/pdf/1706.03762";
const DEFAULT_TRANSLATION = "./translations/attention-is-all-you-need.sample.json";
const BACKEND_URL =
  new URLSearchParams(window.location.search).get("backend") || window.location.origin;

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
const paperSearchResults = document.querySelector("#paper-search-results");
const cachedPaperSelect = document.querySelector("#cached-paper-select");
const translationUrlInput = document.querySelector("#translation-url");
const translationFileInput = document.querySelector("#translation-file");
const translationList = document.querySelector("#translation-list");
const sectionTemplate = document.querySelector("#section-template");
const paragraphTemplate = document.querySelector("#paragraph-template");
const paperMeta = document.querySelector("#paper-meta");
const searchBox = document.querySelector("#search-box");
const anchorToggle = document.querySelector("#anchor-toggle");
const translationFocusToggle = document.querySelector("#translation-focus-toggle");
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
const chatToggle = document.querySelector("#chat-toggle");
const chatDrawer = document.querySelector("#chat-drawer");
const chatClose = document.querySelector("#chat-close");
const chatClear = document.querySelector("#chat-clear");
const chatStatus = document.querySelector("#chat-status");
const chatMessages = document.querySelector("#chat-messages");
const chatContext = document.querySelector("#chat-context");
const chatForm = document.querySelector("#chat-form");
const chatQuestion = document.querySelector("#chat-question");
const chatSend = document.querySelector("#chat-send");
const referencePopover = document.querySelector("#reference-popover");

let currentData = fallbackData;
let currentPaperSourceUrl = DEFAULT_PDF;
let currentPaperFrameUrl = DEFAULT_PDF;
let currentSavedPdfName = "";
let currentPaperFile = null;
let currentPaperId = "";
let selectedChatParagraphId = "";
let loadedChatPaperId = "";
let cachedPapers = [];
let cachedSelectionRevision = 0;

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
  currentPaperFrameUrl = url;
  pdfUrlInput.value = inputValue;
  paperFrame.src = currentPaperFrameUrl;
}

function setGenerateStatus(message, tone = "neutral") {
  generateStatus.textContent = message;
  generateStatus.dataset.tone = tone;
}

function setViewPreference(name, enabled) {
  document.body.classList.toggle(name, enabled);
  localStorage.setItem(`paper-reader:${name}`, enabled ? "1" : "0");
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
  cachedPaperSelect.value = currentSavedPdfName;
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
            sourceText: paragraph.sourceText || "",
            status: paragraph.status || (paragraph.translation ? "translated" : "needs_ocr"),
            translation: paragraph.translation || "",
            note: paragraph.note || ""
          })).filter((paragraph) => paragraph.status !== "skipped")
        : []
    })).filter((section) => section.paragraphs.length > 0);
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
          status: segment.translation ? "translated" : "needs_ocr",
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

function protectMathForMarkdown(source) {
  const formulas = [];
  const register = (formula) => {
    const token = `MATHRENDER${formulas.length}TOKEN`;
    formulas.push(formula);
    return token;
  };
  let markdown = source.replace(/\$\$[\s\S]*?\$\$/g, register);
  markdown = markdown.replace(/\\\[[\s\S]*?\\\]/g, register);
  markdown = markdown.replace(/\\\([\s\S]*?\\\)/g, register);
  markdown = markdown.replace(/(^|[^\\$])\$(?!\$)([^\n]*?[^\\])\$(?!\$)/g, (match, prefix) => {
    const formula = match.slice(prefix.length);
    return `${prefix}${register(formula)}`;
  });
  return { markdown, formulas };
}

function restoreMathAfterMarkdown(element, formulas) {
  const tokenPattern = /MATHRENDER([0-9]+)TOKEN/g;
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);

  textNodes.forEach((node) => {
    const value = node.nodeValue || "";
    tokenPattern.lastIndex = 0;
    if (!tokenPattern.test(value)) return;

    tokenPattern.lastIndex = 0;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    for (const match of value.matchAll(tokenPattern)) {
      fragment.append(document.createTextNode(value.slice(cursor, match.index)));
      const math = document.createElement("span");
      math.className = "math-source";
      math.textContent = formulas[Number(match[1])] || "";
      fragment.append(math);
      cursor = match.index + match[0].length;
    }
    fragment.append(document.createTextNode(value.slice(cursor)));
    node.replaceWith(fragment);
  });
}

function protectLiteralTokensForMarkdown(source) {
  const tokens = [];
  const markdown = source.replace(/<\/?[A-Za-z][A-Za-z0-9_-]*(?:\s[^>\n]*)?>/g, (value) => {
    const token = `LITERALRENDER${tokens.length}TOKEN`;
    tokens.push(value);
    return token;
  });
  return { markdown, tokens };
}

function restoreLiteralTokensAfterMarkdown(element, tokens) {
  const tokenPattern = /LITERALRENDER([0-9]+)TOKEN/g;
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  textNodes.forEach((node) => {
    const value = node.nodeValue || "";
    tokenPattern.lastIndex = 0;
    if (!tokenPattern.test(value)) return;
    tokenPattern.lastIndex = 0;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    for (const match of value.matchAll(tokenPattern)) {
      fragment.append(document.createTextNode(value.slice(cursor, match.index)));
      const literal = document.createElement("code");
      literal.textContent = tokens[Number(match[1])] || "";
      fragment.append(literal);
      cursor = match.index + match[0].length;
    }
    fragment.append(document.createTextNode(value.slice(cursor)));
    node.replaceWith(fragment);
  });
}

function renderLatexInElement(element) {
  if (typeof window.renderMathInElement !== "function") return;
  window.renderMathInElement(element, {
    delimiters: [
      { left: "$$", right: "$$", display: true },
      { left: "\\[", right: "\\]", display: true },
      { left: "\\(", right: "\\)", display: false },
      { left: "$", right: "$", display: false }
    ],
    throwOnError: false,
    strict: "ignore"
  });
}

function renderMarkdownWithMath(element, source) {
  const protectedMath = protectMathForMarkdown(source || "");
  const protectedLiterals = protectLiteralTokensForMarkdown(protectedMath.markdown);
  if (window.marked?.parse && window.DOMPurify?.sanitize) {
    const html = window.marked.parse(protectedLiterals.markdown, { gfm: true, breaks: false });
    element.innerHTML = window.DOMPurify.sanitize(html);
    restoreMathAfterMarkdown(element, protectedMath.formulas);
    restoreLiteralTokensAfterMarkdown(element, protectedLiterals.tokens);
  } else {
    element.textContent = source || "";
  }

  renderLatexInElement(element);
}

function inferPaperId(data = currentData) {
  if (data?.paperId) return data.paperId;
  const arxivName = currentSavedPdfName.match(/^arxiv-(.+)\.pdf$/i);
  if (arxivName) return `arxiv:${arxivName[1]}`;
  const source = `${data?.paperUrl || ""} ${currentPaperSourceUrl || ""}`;
  const arxivUrl = source.match(/arxiv\.org\/(?:pdf|abs)\/([^?#\s/]+?)(?:\.pdf)?(?:[?#\s]|$)/i);
  return arxivUrl ? `arxiv:${arxivUrl[1]}` : "";
}

function chatSessionId(paperId) {
  const key = `paper-chat-session:${paperId}`;
  let sessionId = localStorage.getItem(key);
  if (!sessionId) {
    sessionId = window.crypto?.randomUUID?.() || `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    localStorage.setItem(key, sessionId);
  }
  return sessionId;
}

function paperApiPath(paperId) {
  return `${BACKEND_URL}/api/papers/${encodeURIComponent(paperId)}`;
}

function setPdfPage(page) {
  if (!page) return;
  const [base, rawFragment = ""] = currentPaperFrameUrl.split("#", 2);
  const fragment = new URLSearchParams(rawFragment);
  fragment.set("page", String(page));
  paperFrame.src = `${base}#${fragment.toString()}`;
}

function navigateToParagraph(paragraphId, page = "") {
  const target = document.getElementById(paragraphId);
  if (target) {
    if (target.matches("details")) target.open = true;
    document.querySelectorAll(".reference-target").forEach((element) => element.classList.remove("reference-target"));
    target.classList.add("reference-target");
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => target.classList.remove("reference-target"), 2600);
  }
  setPdfPage(page);
}

function structuredKindLabel(kind) {
  return { figure: "图", table: "表", algorithm: "算法" }[kind] || "结构化内容";
}

function createStructuredAsset(asset) {
  const details = document.createElement("details");
  details.className = `structured-asset structured-${asset.kind || "content"}`;
  details.id = asset.id;

  const summary = document.createElement("summary");
  const label = document.createElement("strong");
  label.textContent = `${structuredKindLabel(asset.kind)} ${asset.number || ""}`.trim();
  const caption = document.createElement("span");
  caption.textContent = asset.captionTranslation || asset.captionSource || "查看结构化内容";
  summary.append(label, caption);
  details.append(summary);

  const body = document.createElement("div");
  body.className = "structured-body";
  if (asset.kind === "figure") {
    (asset.images || []).forEach((url, index) => {
      const image = document.createElement("img");
      image.src = url;
      image.alt = asset.captionTranslation || asset.captionSource || `Figure ${asset.number || index + 1}`;
      image.loading = "lazy";
      body.append(image);
    });
  } else if (asset.kind === "table") {
    const scroller = document.createElement("div");
    scroller.className = "structured-table-wrap";
    const table = document.createElement("table");
    (asset.rows || []).forEach((row, rowIndex) => {
      const tr = document.createElement("tr");
      row.forEach((cell) => {
        const cellElement = document.createElement(rowIndex === 0 && (asset.rows || []).length > 1 ? "th" : "td");
        cellElement.title = cell.source || "";
        renderMarkdownWithMath(cellElement, cell.translation || cell.source || "");
        tr.append(cellElement);
      });
      table.append(tr);
    });
    scroller.append(table);
    body.append(scroller);
  } else if (asset.kind === "algorithm") {
    const steps = document.createElement("ol");
    steps.className = "algorithm-steps";
    (asset.steps || []).forEach((step) => {
      const item = document.createElement("li");
      item.className = `algorithm-${step.keyword || "state"}`;
      item.style.setProperty("--algorithm-indent", String(step.indent || 0));
      item.title = step.source || "";
      renderMarkdownWithMath(item, step.translation || step.source || "");
      steps.append(item);
    });
    body.append(steps);
  }

  if (asset.captionSource && asset.captionTranslation && asset.captionSource !== asset.captionTranslation) {
    const original = document.createElement("p");
    original.className = "structured-original-caption";
    original.textContent = asset.captionSource;
    body.append(original);
  }
  details.append(body);
  return details;
}

function showReferencePopover(anchor, text) {
  referencePopover.textContent = text;
  referencePopover.hidden = false;
  const rect = anchor.getBoundingClientRect();
  referencePopover.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - 372))}px`;
  referencePopover.style.top = `${Math.min(window.innerHeight - 100, rect.bottom + 8)}px`;
}

function handleReferenceLink(event) {
  const anchor = event.target.closest("a");
  if (!anchor) return;
  const href = anchor.getAttribute("href") || "";
  if (!href.startsWith("#xref:") && !href.startsWith("#cite:")) return;
  event.preventDefault();
  const [kind, encodedKey] = href.slice(1).split(":", 2);
  const key = decodeURIComponent(encodedKey || "");
  if (kind === "xref") {
    const label = currentData?.crossReferences?.labels?.[key];
    if (label?.targetAssetId || label?.targetParagraphId || label?.targetSectionId) {
      navigateToParagraph(label.targetAssetId || label.targetParagraphId || label.targetSectionId, label.page);
      return;
    }
    showReferencePopover(anchor, label ? `${label.kind || "引用"} ${label.number || key}：原文目标无法精确定位。` : `未解析的交叉引用：${key}`);
    return;
  }
  const citation = currentData?.citations?.[key];
  const details = citation
    ? `${citation.number ? `[${citation.number}] ` : ""}${citation.authors || ""}${citation.year ? ` (${citation.year})` : ""}${citation.title ? `。${citation.title}` : ""}`
    : `未解析的参考文献：${key}`;
  showReferencePopover(anchor, details);
}

function renderChatMessage(message) {
  const wrapper = document.createElement("div");
  wrapper.className = `chat-message ${message.role === "user" ? "user" : "assistant"}`;
  if (message.role === "assistant") {
    renderMarkdownWithMath(wrapper, message.content || message.answerMarkdown || "");
  } else {
    wrapper.textContent = message.content || "";
  }
  const citations = message.citations || [];
  if (citations.length) {
    const list = document.createElement("div");
    list.className = "chat-citations";
    citations.forEach((citation) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "chat-citation";
      button.textContent = `${citation.evidenceId} · ${citation.title || citation.paragraphId}`;
      button.title = citation.excerpt || "跳到证据段落";
      button.addEventListener("click", () => navigateToParagraph(citation.paragraphId, citation.page));
      list.append(button);
    });
    wrapper.append(list);
  }
  chatMessages.append(wrapper);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function openChat(paragraphId = "") {
  chatDrawer.hidden = false;
  chatToggle.setAttribute("aria-expanded", "true");
  selectedChatParagraphId = paragraphId;
  chatContext.hidden = !paragraphId;
  chatContext.textContent = paragraphId ? `当前问题将优先检索段落：${paragraphId}` : "";
  chatQuestion.focus();
}

function closeChat() {
  chatDrawer.hidden = true;
  chatToggle.setAttribute("aria-expanded", "false");
}

async function prepareChat(force = false) {
  currentPaperId = inferPaperId();
  if (!currentPaperId) {
    chatStatus.textContent = "当前译文缺少论文 ID";
    chatSend.disabled = true;
    return;
  }
  if (!force && loadedChatPaperId === currentPaperId) return;
  loadedChatPaperId = currentPaperId;
  chatMessages.innerHTML = "";
  chatSend.disabled = true;
  chatStatus.textContent = "检查本地索引…";
  try {
    const statusResponse = await fetch(`${paperApiPath(currentPaperId)}/qa-status`, { cache: "no-store" });
    const statusData = await statusResponse.json().catch(() => ({}));
    if (!statusResponse.ok) throw new Error(statusData.detail || `HTTP ${statusResponse.status}`);
    chatStatus.textContent = statusData.ready ? `索引就绪 · ${statusData.chunks} 段` : "首次提问时建立索引";
    const historyResponse = await fetch(`${paperApiPath(currentPaperId)}/chat/${encodeURIComponent(chatSessionId(currentPaperId))}`, { cache: "no-store" });
    const historyData = await historyResponse.json().catch(() => ({}));
    if (historyResponse.ok) (historyData.messages || []).forEach(renderChatMessage);
    chatSend.disabled = false;
  } catch (error) {
    chatStatus.textContent = error.message;
    chatSend.disabled = false;
  }
}

function renderTranslation(data, query = "") {
  currentData = data;
  const title = data.title || "Untitled paper";
  const coverage = data.coverage ? ` · ${data.coverage}` : "";
  const sections = filterSections(normalizeSections(data), query);
  const assetsByParagraph = new Map();
  (data.structuredContent || []).forEach((asset) => {
    if (!asset.firstReferencedBy) return;
    const assets = assetsByParagraph.get(asset.firstReferencedBy) || [];
    assets.push(asset);
    assetsByParagraph.set(asset.firstReferencedBy, assets);
  });

  paperMeta.textContent = `${title}${coverage}`;
  translationList.innerHTML = "";
  currentPaperId = inferPaperId(data);

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
    const sectionTitle = sectionNode.querySelector(".section-title");
    sectionTitle.textContent = section.title;
    renderLatexInElement(sectionTitle);
    sectionNode.querySelector(".section-pages").textContent = pageRange(section);

    const paragraphsElement = sectionNode.querySelector(".paragraphs");
    section.paragraphs.forEach((paragraph, paragraphIndex) => {
      const paragraphNode = paragraphTemplate.content.cloneNode(true);
      paragraphNode.querySelector(".paragraph-index").textContent = `${sectionIndex + 1}.${paragraphIndex + 1}`;
      paragraphNode.querySelector(".paragraph-page").textContent = paragraph.page ? `p. ${paragraph.page}` : "";
      paragraphNode.querySelector(".paragraph-anchor").textContent = paragraph.anchor;
      const paragraphElement = paragraphNode.querySelector(".paragraph");
      paragraphElement.id = paragraph.id;
      paragraphElement.dataset.status = paragraph.status;
      renderMarkdownWithMath(paragraphNode.querySelector(".paragraph-translation"), paragraph.translation);
      paragraphNode.querySelector(".paragraph-note").textContent = paragraph.note;
      paragraphNode.querySelector(".ask-paragraph").addEventListener("click", () => openChat(paragraph.id));
      paragraphsElement.append(paragraphNode);
      (assetsByParagraph.get(paragraph.id) || []).forEach((asset) => {
        paragraphsElement.append(createStructuredAsset(asset));
      });
    });

    translationList.append(sectionNode);
  });
  prepareChat().catch((error) => { chatStatus.textContent = error.message; });
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
    setGenerateStatus("Backend offline: start the local app on :8000", "warn");
  }
}

async function refreshDownloadedPapers(selectedName = null) {
  try {
    const response = await fetch(`${BACKEND_URL}/api/papers`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const papers = Array.isArray(data.papers) ? data.papers : [];
    cachedPapers = papers;
    cachedPaperSelect.replaceChildren();
    const cachePlaceholder = document.createElement("option");
    cachePlaceholder.value = "";
    cachePlaceholder.textContent = papers.length ? `已缓存论文（${papers.length}）` : "暂无已缓存论文";
    cachedPaperSelect.append(cachePlaceholder);
    generateSavedPdfSelect.innerHTML = "";

    const emptyOption = document.createElement("option");
    emptyOption.value = "";
    emptyOption.textContent = "使用上传的 PDF";
    generateSavedPdfSelect.append(emptyOption);

    papers.forEach((paper) => {
      const option = document.createElement("option");
      option.value = paper.name;
      option.textContent = `${paper.title || paper.name} · ${paper.translationUrl ? "已有译文" : "仅 PDF"}`;
      option.dataset.sourceUrl = paper.sourceUrl || "";
      option.dataset.fileUrl = paper.fileUrl || "";
      option.dataset.pdfUrl = paper.pdfUrl || "";
      option.dataset.translationUrl = paper.translationUrl || "";
      generateSavedPdfSelect.append(option);
      cachedPaperSelect.append(option.cloneNode(true));
    });

    const preferredName = selectedName === null ? currentSavedPdfName : selectedName;
    selectSavedPdf(preferredName && papers.some((paper) => paper.name === preferredName) ? preferredName : "");
  } catch {
    generateSavedPdfSelect.innerHTML = '<option value="">Backend offline</option>';
    cachedPaperSelect.innerHTML = '<option value="">缓存读取失败，点击重试</option>';
  }
}

async function openCachedPaper(name) {
  const paper = cachedPapers.find((item) => item.name === name);
  if (!paper) return;
  const revision = ++cachedSelectionRevision;
  hidePaperSearchResults();
  currentPaperFile = null;
  generatePdfInput.value = "";
  pdfFileInput.value = "";
  searchBox.value = "";
  closeChat();
  selectedChatParagraphId = "";
  loadedChatPaperId = "";
  chatSend.disabled = true;
  chatMessages.replaceChildren();
  applyPaperInfo(paper);
  renderTranslation({ title: paper.title || paper.name, paperId: paper.paperId,
    paperUrl: paper.sourceUrl, sections: [] });
  translationUrlInput.value = "";
  if (!paper.translationUrl) {
    translationList.querySelector(".empty").textContent = "这篇论文尚未生成译文";
    setGenerateStatus("已载入本地 PDF，尚未生成译文", "ok");
    return;
  }
  setGenerateStatus("正在载入缓存译文...", "busy");
  try {
    const url = new URL(paper.translationUrl, BACKEND_URL).href;
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (revision !== cachedSelectionRevision) return;
    translationUrlInput.value = url;
    renderTranslation(data);
    setGenerateStatus("已载入缓存论文及译文", "ok");
  } catch (error) {
    if (revision !== cachedSelectionRevision) return;
    setGenerateStatus(`PDF 已载入，缓存译文读取失败：${error.message}`, "warn");
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

function hidePaperSearchResults() {
  paperSearchResults.hidden = true;
  paperSearchResults.replaceChildren();
}

async function selectPaperCandidate(candidate) {
  hidePaperSearchResults();
  if (candidate.cachedName) {
    await downloadAndDisplayPaper(`${BACKEND_URL}/api/papers/${encodeURIComponent(candidate.cachedName)}`);
    return;
  }
  if (!candidate.pdfUrl) {
    setGenerateStatus("No open PDF found. Enter a PDF link or upload the file.", "warn");
    return;
  }
  pdfUrlInput.value = candidate.pdfUrl;
  generateTitleInput.value = candidate.title || generateTitleInput.value;
  await downloadAndDisplayPaper(candidate.pdfUrl);
}

function renderPaperSearchResults(data) {
  paperSearchResults.replaceChildren();
  const results = Array.isArray(data.results) ? data.results : [];
  if (!results.length) {
    const empty = document.createElement("p");
    empty.className = "paper-search-empty";
    empty.textContent = "未检索到可用论文，请手动输入 PDF 链接或上传文件。";
    paperSearchResults.append(empty);
    paperSearchResults.hidden = false;
    return;
  }

  results.forEach((candidate) => {
    const item = document.createElement("article");
    item.className = "paper-search-result";

    const content = document.createElement("div");
    content.className = "paper-search-result-content";
    const title = document.createElement("strong");
    title.textContent = candidate.title || "Untitled paper";
    const meta = document.createElement("span");
    const authors = Array.isArray(candidate.authors) ? candidate.authors.slice(0, 3).join(", ") : "";
    meta.textContent = [authors, candidate.year, candidate.venue, candidate.source].filter(Boolean).join(" · ");
    content.append(title, meta);

    const actions = document.createElement("div");
    actions.className = "paper-search-result-actions";
    if (candidate.landingUrl) {
      const sourceLink = document.createElement("a");
      sourceLink.href = candidate.landingUrl;
      sourceLink.target = "_blank";
      sourceLink.rel = "noreferrer";
      sourceLink.textContent = "来源";
      actions.append(sourceLink);
    }
    const useButton = document.createElement("button");
    useButton.type = "button";
    useButton.textContent = candidate.cachedName ? "打开" : "载入";
    useButton.disabled = !candidate.cachedName && !candidate.pdfUrl;
    useButton.title = useButton.disabled ? "没有可直接读取的开放 PDF" : "载入论文 PDF";
    useButton.addEventListener("click", () => {
      selectPaperCandidate(candidate).catch((error) => {
        setGenerateStatus(`Load failed: ${error.message}`, "warn");
      });
    });
    actions.append(useButton);
    item.append(content, actions);
    paperSearchResults.append(item);
  });
  paperSearchResults.hidden = false;
}

async function searchAndDisplayPaper(query) {
  setGenerateStatus("Searching papers with DeepSeek...", "busy");
  const response = await fetch(`${BACKEND_URL}/api/search-papers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, limit: 6 })
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  renderPaperSearchResults(data);
  if (data.autoSelect && data.results?.length) {
    setGenerateStatus("Exact paper found. Loading PDF...", "busy");
    await selectPaperCandidate(data.results[0]);
    return;
  }
  if (data.manualRequired) {
    setGenerateStatus("No paper found. Enter a PDF link or upload the file.", "warn");
    return;
  }
  const failedSources = Object.keys(data.providerErrors || {}).length;
  setGenerateStatus(
    failedSources ? `Found ${data.results.length} candidates; ${failedSources} source unavailable` : `Found ${data.results.length} candidates`,
    failedSources ? "warn" : "ok"
  );
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
  const input = pdfUrlInput.value.trim() || DEFAULT_PDF;
  hidePaperSearchResults();
  if (/^https?:\/\//i.test(input)) {
    downloadAndDisplayPaper(input).catch((error) => {
      setGenerateStatus(`Download failed: ${error.message}`, "warn");
      setPaperUrl(input);
    });
    return;
  }
  searchAndDisplayPaper(input).catch((error) => {
    renderPaperSearchResults({ results: [] });
    setGenerateStatus(`Search failed: ${error.message}. Enter a PDF link manually.`, "warn");
  });
});

pdfUrlInput.addEventListener("input", hidePaperSearchResults);

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
    const translationUrl = data.translation_url || `./translations/${outputFile.endsWith(".json") ? outputFile : `${outputFile}.json`}`;
    const counts = data.status_counts || {};
    const unresolved = (counts.needs_ocr || 0) + (counts.needs_formula_recovery || 0);
    const resultSummary = unresolved
      ? `Generated ${counts.translated || 0} translated, ${counts.needs_ocr || 0} need OCR, ${counts.needs_formula_recovery || 0} need formula recovery`
      : `Generated ${data.paragraphs} paragraphs`;
    setGenerateStatus(data.cached ? "Loaded cached translation JSON" : resultSummary, unresolved ? "warn" : "ok");
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
  if (generateSavedPdfSelect.value) openCachedPaper(generateSavedPdfSelect.value);
  else selectSavedPdf("");
});
cachedPaperSelect.addEventListener("change", () => openCachedPaper(cachedPaperSelect.value));
cachedPaperSelect.addEventListener("focus", () => refreshDownloadedPapers());

searchBox.addEventListener("input", () => {
  renderTranslation(currentData, searchBox.value);
});

anchorToggle.addEventListener("change", () => {
  document.body.classList.toggle("hide-anchors", !anchorToggle.checked);
});

translationFocusToggle.addEventListener("click", () => {
  const focused = document.body.classList.toggle("translation-focus");
  translationFocusToggle.textContent = focused ? "返回" : "专注";
  translationFocusToggle.title = focused ? "返回双栏阅读" : "只显示译文";
  translationFocusToggle.setAttribute("aria-pressed", String(focused));
});

compactToggle.addEventListener("click", () => {
  const compact = !document.body.classList.contains("compact");
  setViewPreference("compact", compact);
  compactToggle.setAttribute("aria-pressed", String(compact));
});

themeToggle.addEventListener("click", () => {
  const dark = !document.body.classList.contains("dark");
  setViewPreference("dark", dark);
  themeToggle.textContent = dark ? "浅色" : "深色";
  themeToggle.setAttribute("aria-pressed", String(dark));
});

translationList.addEventListener("click", handleReferenceLink);
chatMessages.addEventListener("click", handleReferenceLink);
chatToggle.addEventListener("click", () => {
  if (chatDrawer.hidden) openChat();
  else closeChat();
});
chatClose.addEventListener("click", closeChat);
chatClear.addEventListener("click", async () => {
  if (!currentPaperId) return;
  const sessionId = chatSessionId(currentPaperId);
  try {
    const response = await fetch(`${paperApiPath(currentPaperId)}/chat/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    chatMessages.innerHTML = "";
    chatStatus.textContent = "对话已清空";
  } catch (error) {
    chatStatus.textContent = `清空失败：${error.message}`;
  }
});
chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = chatQuestion.value.trim();
  if (!question || !currentPaperId) return;
  renderChatMessage({ role: "user", content: question });
  chatQuestion.value = "";
  chatSend.disabled = true;
  chatStatus.textContent = "检索并核对证据…";
  try {
    const response = await fetch(`${paperApiPath(currentPaperId)}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId: chatSessionId(currentPaperId),
        question,
        selectedParagraphId: selectedChatParagraphId || null,
        historyLimit: 8
      })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    renderChatMessage({ role: "assistant", content: data.answerMarkdown, citations: data.citations || [] });
    chatStatus.textContent = data.insufficientEvidence ? "论文证据不足" : `已核对 ${data.citations?.length || 0} 条证据`;
  } catch (error) {
    renderChatMessage({ role: "assistant", content: `问答失败：${error.message}`, citations: [] });
    chatStatus.textContent = "问答失败";
  } finally {
    chatSend.disabled = false;
  }
});

document.addEventListener("click", (event) => {
  if (!referencePopover.hidden && !referencePopover.contains(event.target) && !event.target.closest("a")) {
    referencePopover.hidden = true;
  }
});

const initialCompact = localStorage.getItem("paper-reader:compact") === "1";
const initialDark = localStorage.getItem("paper-reader:dark") === "1";
document.body.classList.toggle("compact", initialCompact);
document.body.classList.toggle("dark", initialDark);
compactToggle.setAttribute("aria-pressed", String(initialCompact));
themeToggle.setAttribute("aria-pressed", String(initialDark));
themeToggle.textContent = initialDark ? "浅色" : "深色";

setPaperUrl(getParam("pdf", DEFAULT_PDF));
loadTranslation(getParam("translation", DEFAULT_TRANSLATION));
checkBackend();
refreshDownloadedPapers();
