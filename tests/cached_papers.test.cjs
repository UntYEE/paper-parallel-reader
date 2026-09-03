const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../viewer/app.js'), 'utf8');
const openFunction = source.slice(source.indexOf('async function openCachedPaper('),
  source.indexOf('async function downloadAndDisplayPaper('));

function fixture(fetch) {
  const element = () => ({ value: 'old', replaceChildren() {} });
  const context = vm.createContext({
    URL, fetch, BACKEND_URL: 'http://localhost:8000',
    cachedPapers: [
      { name: 'a.pdf', title: 'A', paperId: 'a', translationUrl: '/viewer/translations/a.json' },
      { name: 'b.pdf', title: 'B', paperId: 'b' }
    ],
    cachedSelectionRevision: 0, currentPaperFile: {}, selectedChatParagraphId: 'old',
    loadedChatPaperId: 'old', chatSend: {}, chatMessages: element(),
    generatePdfInput: element(), pdfFileInput: element(), searchBox: element(),
    translationUrlInput: element(), translationList: { querySelector: () => ({}) },
    hidePaperSearchResults() {}, closeChat() {},
    applyPaperInfo(paper) { context.selected = paper.name; },
    renderTranslation(data) { context.rendered = data; },
    setGenerateStatus(message, tone) { context.status = { message, tone }; }
  });
  vm.runInContext(openFunction, context);
  return context;
}

test('cached selection loads only the local translation and clears uploaded files', async () => {
  const urls = [];
  const c = fixture(async (url) => {
    urls.push(url);
    return { ok: true, json: async () => ({ title: 'A translated' }) };
  });
  await c.openCachedPaper('a.pdf');
  assert.deepEqual(urls, ['http://localhost:8000/viewer/translations/a.json']);
  assert.equal(c.selected, 'a.pdf');
  assert.equal(c.rendered.title, 'A translated');
  assert.equal(c.currentPaperFile, null);
  assert.equal(c.generatePdfInput.value, '');
});

test('PDF-only selection clears previous translation without a network request', async () => {
  const c = fixture(() => { throw new Error('unexpected request'); });
  await c.openCachedPaper('b.pdf');
  assert.equal(c.rendered.title, 'B');
  assert.equal(c.rendered.sections.length, 0);
  assert.equal(c.translationUrlInput.value, '');
});

test('failed cached translation does not show an unrelated fallback sample', async () => {
  const c = fixture(async () => ({ ok: false, status: 404 }));
  await c.openCachedPaper('a.pdf');
  assert.equal(c.rendered.title, 'A');
  assert.equal(c.rendered.sections.length, 0);
  assert.equal(c.status.tone, 'warn');
});

test('slow previous translation cannot overwrite a newer selection', async () => {
  let resolve;
  const c = fixture(() => new Promise((done) => { resolve = done; }));
  const first = c.openCachedPaper('a.pdf');
  await c.openCachedPaper('b.pdf');
  resolve({ ok: true, json: async () => ({ title: 'Stale A' }) });
  await first;
  assert.equal(c.rendered.title, 'B');
  assert.equal(c.selected, 'b.pdf');
});
