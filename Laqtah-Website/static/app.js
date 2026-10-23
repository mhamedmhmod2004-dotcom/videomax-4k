'use strict';

const byId = (id) => document.getElementById(id);
const ui = {
  form: byId('analyzeForm'), url: byId('videoUrl'), paste: byId('pasteBtn'),
  analyze: byId('analyzeBtn'), result: byId('result'), panel: byId('jobPanel'),
  title: byId('videoTitle'), uploader: byId('videoUploader'), platform: byId('platformName'),
  image: byId('previewImage'), placeholder: byId('thumbFallback'), duration: byId('videoDuration'),
  quality: byId('qualityGrid'), qualitySection: byId('qualitySection'), audioNote: byId('audioNote'),
  download: byId('downloadBtn'), toast: byId('toast'), jobText: byId('jobText'),
  jobPercent: byId('jobPercent'), progress: byId('progressFill'), track: byId('progressTrack'),
  jobSubtext: byId('jobSubtext'), fileLink: byId('fileLink')
};
let currentUrl = '';
let activeJob = '';
let jobTimer = null;
let busy = false;
let errorTimeout = null;

function notify(message) {
  ui.toast.textContent = message;
  ui.toast.hidden = false;
  clearTimeout(errorTimeout);
  errorTimeout = setTimeout(() => { ui.toast.hidden = true; }, 6500);
}
function apiError(body, status) {
  return typeof body?.detail === 'string' ? body.detail : `حصل خطأ أثناء الاتصال بالخادم (${status}).`;
}
async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(path, {
      headers: {'Content-Type': 'application/json'},
      ...options
    });
  } catch (_error) {
    throw new Error('تعذّر الاتصال بالخادم. شغّل الموقع باستخدام ملف التشغيل، وليس بفتح HTML مباشرة.');
  }
  let body = {};
  try { body = await res.json(); } catch (_error) { /* Handle non-JSON service errors. */ }
  if (!res.ok) throw new Error(apiError(body, res.status));
  return body;
}
function secondsLabel(seconds) {
  if (!seconds || seconds < 0) return '';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return (h ? [h, m, s] : [m, s]).map(n => String(n).padStart(2, '0')).join(':');
}
function setLoading(isLoading) {
  ui.analyze.disabled = isLoading || busy;
  ui.analyze.querySelector('span').textContent = isLoading ? 'جاري تحليل الفيديو...' : 'تحليل الرابط';
  ui.paste.disabled = isLoading || busy;
}
function resetPreview() {
  currentUrl = '';
  ui.result.hidden = true;
  if (!busy) ui.panel.hidden = true;
}
function qualityDescription(height) {
  if (height >= 4320) return '8K';
  if (height >= 2160) return '4K';
  if (height >= 1440) return '2K';
  if (height >= 1080) return 'FHD';
  if (height >= 720) return 'HD';
  return '';
}
function renderQualities(qualities) {
  ui.quality.replaceChildren();
  const items = [{value: 'best', label: 'أعلى جودة', badge: 'تلقائي'}];
  const allowed = new Set([144, 240, 360, 480, 720, 1080, 1440, 2160, 4320]);
  for (const height of qualities) {
    if (allowed.has(height)) items.push({value: String(height), label: height + 'p', badge: qualityDescription(height)});
  }
  for (const [index, item] of items.entries()) {
    const label = document.createElement('label');
    label.className = 'quality-option' + (index === 0 ? ' selected' : '');
    const radio = document.createElement('input');
    radio.type = 'radio'; radio.name = 'quality'; radio.value = item.value; radio.checked = index === 0;
    const text = document.createElement('span'); text.textContent = item.label;
    label.append(radio, text);
    if (item.badge) {
      const badge = document.createElement('em'); badge.textContent = item.badge;
      label.appendChild(badge);
    }
    label.addEventListener('click', () => {
      for (const option of ui.quality.children) option.classList.remove('selected');
      label.classList.add('selected');
      radio.checked = true;
    });
    ui.quality.appendChild(label);
  }
}
function renderInfo(info) {
  ui.title.textContent = info.title || 'فيديو بدون عنوان';
  ui.uploader.textContent = info.uploader || 'المصدر الأصلي';
  ui.platform.textContent = info.platform || 'فيديو';
  const time = secondsLabel(info.duration);
  ui.duration.textContent = time;
  ui.duration.hidden = !time;
  ui.image.hidden = true;
  ui.placeholder.hidden = false;
  ui.image.onload = () => { ui.image.hidden = false; ui.placeholder.hidden = true; };
  ui.image.onerror = () => { ui.image.hidden = true; ui.placeholder.hidden = false; };
  if (info.thumbnail) ui.image.src = info.thumbnail;
  else ui.image.removeAttribute('src');
  renderQualities(Array.isArray(info.qualities) ? info.qualities : []);
  document.querySelector('input[name="saveFormat"][value="mp4"]').checked = true;
  updateFormat();
  ui.result.hidden = false;
  ui.panel.hidden = true;
  ui.result.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
function updateFormat() {
  const mode = document.querySelector('input[name="saveFormat"]:checked')?.value ?? 'mp4';
  byId('videoOption').classList.toggle('selected', mode === 'mp4');
  byId('audioOption').classList.toggle('selected', mode === 'mp3');
  ui.qualitySection.hidden = mode === 'mp3';
  ui.audioNote.hidden = mode !== 'mp3';
}
function setJobStatus(state, percent = 0) {
  const value = Math.min(100, Math.max(0, Number(percent) || 0));
  const labels = {
    queued: ['في انتظار بدء التنزيل...', 'قد يستغرق بدء المعالجة بضع لحظات.'],
    downloading: ['جاري تنزيل الفيديو...', 'يرجى إبقاء الصفحة مفتوحة حتى اكتمال التجهيز.'],
    processing: ['جاري دمج الصوت والصورة...', 'هذه الخطوة قد تستغرق وقتًا مع الفيديوهات الكبيرة.'],
    ready: ['اكتمل تجهيز الملف بنجاح', 'اضغط الزر أدناه لحفظه على جهازك.'],
    error: ['تعذّر إكمال التنزيل', 'جرّب رابطًا آخر أو اختر فيديو عامًا.']
  };
  const [title, detail] = labels[state] || labels.queued;
  ui.jobText.textContent = title;
  ui.jobSubtext.textContent = detail;
  ui.jobPercent.textContent = Math.floor(value) + '%';
  ui.progress.style.width = value + '%';
  ui.track.setAttribute('aria-valuenow', String(Math.floor(value)));
  ui.panel.hidden = false;
}
function stopPolling() {
  if (jobTimer) { clearInterval(jobTimer); jobTimer = null; }
}
async function readJob() {
  if (!activeJob) return;
  try {
    const id = activeJob;
    const data = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (id !== activeJob) return;
    setJobStatus(data.status, data.progress);
    if (data.status === 'ready') {
      stopPolling();
      ui.fileLink.href = data.file_url;
      ui.fileLink.hidden = false;
      ui.download.disabled = false;
      busy = false;
      setLoading(false);
      ui.fileLink.focus({preventScroll: true});
    } else if (data.status === 'error') {
      stopPolling();
      busy = false;
      ui.download.disabled = false;
      setLoading(false);
      notify(data.error || 'تعذّر تنزيل الملف.');
    }
  } catch (err) {
    stopPolling(); busy = false;
    ui.download.disabled = false;
    setLoading(false);
    notify(err.message);
  }
}

ui.paste.addEventListener('click', async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (!text.trim()) throw new Error('الحافظة فارغة.');
    ui.url.value = text.trim();
    resetPreview();
    ui.url.focus();
  } catch (_err) { notify('الصق الرابط يدويًا باستخدام الضغط المطوّل ثم لصق، أو Ctrl + V.'); }
});
ui.url.addEventListener('input', () => { if (!busy) resetPreview(); });
for (const radio of document.querySelectorAll('input[name="saveFormat"]')) {
  radio.addEventListener('change', updateFormat);
}
ui.form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  const url = ui.url.value.trim();
  try {
    const parsed = new URL(url);
    if (!['https:', 'http:'].includes(parsed.protocol)) throw new Error();
  } catch (_err) { notify('أدخل رابط فيديو صحيحًا يبدأ بـ https://'); return; }
  setLoading(true);
  resetPreview();
  try {
    const info = await api('/api/info', {method: 'POST', body: JSON.stringify({url})});
    currentUrl = url;
    renderInfo(info);
  } catch (err) { notify(err.message); }
  finally { setLoading(false); }
});
ui.download.addEventListener('click', async () => {
  if (busy || !currentUrl) return;
  busy = true;
  ui.download.disabled = true;
  ui.fileLink.hidden = true;
  setLoading(false);
  setJobStatus('queued', 0);
  const mode = document.querySelector('input[name="saveFormat"]:checked')?.value || 'mp4';
  const quality = document.querySelector('input[name="quality"]:checked')?.value || 'best';
  try {
    const job = await api('/api/download', {
      method: 'POST', body: JSON.stringify({url: currentUrl, format: mode, quality})
    });
    activeJob = job.id;
    await readJob();
    if (busy) jobTimer = setInterval(readJob, 1200);
  } catch (err) {
    busy = false;
    ui.download.disabled = false;
    setLoading(false);
    ui.panel.hidden = true;
    notify(err.message);
  }
});
if (location.protocol === 'file:') notify('للتنزيل الفعلي، شغّل خادم الموقع ثم افتح http://127.0.0.1:8000');
