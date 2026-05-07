/**
 * drop.pensa.ar — vanilla JS upload client.
 *
 * No build step, no bundler, no framework. We use XMLHttpRequest (not
 * fetch) because we need real upload progress events, which fetch
 * still doesn't expose in 2026.
 */
(function () {
  'use strict';

  // --- DOM refs ---
  const $ = (id) => document.getElementById(id);
  const dropzone = $('dropzone');
  const fileInput = $('file-input');
  const uploadView = $('upload-view');
  const progressView = $('progress-view');
  const resultView = $('result-view');
  const oneShotInput = $('one-shot');
  const ttlInputs = document.querySelectorAll('input[name="ttl"]');

  const progressName = $('progress-name');
  const progressFill = $('progress-fill');
  const progressPct = $('progress-pct');
  const progressRate = $('progress-rate');
  const cancelBtn = $('cancel-btn');

  const resultUrl = $('result-url');
  const copyBtn = $('copy-btn');
  const deleteBtn = $('delete-btn');
  const newBtn = $('new-btn');
  const metaFilename = $('meta-filename');
  const metaSize = $('meta-size');
  const metaSha = $('meta-sha');
  const metaExpires = $('meta-expires');
  const qrEl = $('qr-code');
  const toast = $('toast');

  // --- state ---
  let currentXhr = null;
  let currentResult = null;     // last successful upload payload
  let expiryInterval = null;    // countdown timer for "expires" field

  // --- helpers ---
  function show(view) {
    uploadView.classList.add('hidden');
    progressView.classList.add('hidden');
    resultView.classList.add('hidden');
    view.classList.remove('hidden');
  }

  function showToast(msg, isError) {
    toast.textContent = msg;
    toast.classList.toggle('error', !!isError);
    toast.classList.remove('hidden');
    // Force reflow so the transition replays on consecutive toasts
    void toast.offsetWidth;
    toast.classList.add('show');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      toast.classList.remove('show');
      // Hide after the fade-out completes so it doesn't catch clicks
      setTimeout(() => toast.classList.add('hidden'), 250);
    }, isError ? 4500 : 2500);
  }

  function fmtBytes(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
    return (n / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
  }

  function fmtRate(bps) {
    if (!isFinite(bps) || bps <= 0) return '—';
    return fmtBytes(bps) + '/s';
  }

  function fmtTimeLeft(targetMs) {
    const now = Date.now();
    const left = Math.max(0, targetMs - now);
    if (left === 0) return 'expired';
    const s = Math.floor(left / 1000);
    if (s < 60) return s + 's left';
    const m = Math.floor(s / 60);
    if (m < 60) return m + 'm left';
    const h = Math.floor(m / 60);
    if (h < 24) return h + 'h ' + (m % 60) + 'm left';
    const d = Math.floor(h / 24);
    return d + 'd ' + (h % 24) + 'h left';
  }

  // --- upload ---
  function getSelectedTtl() {
    for (const r of ttlInputs) if (r.checked) return r.value;
    return '3600';
  }

  function startUpload(file) {
    if (!file) return;

    // Build query params. We send TTL and one_shot in the URL, file in the body.
    const params = new URLSearchParams({
      expires_in: getSelectedTtl(),
      one_shot: oneShotInput.checked ? 'true' : 'false',
    });

    const fd = new FormData();
    fd.append('file', file, file.name);

    const xhr = new XMLHttpRequest();
    currentXhr = xhr;
    xhr.open('POST', '/upload?' + params.toString(), true);

    progressName.textContent = file.name;
    progressFill.style.width = '0%';
    progressPct.textContent = '0%';
    progressRate.textContent = '—';
    show(progressView);

    const startedAt = Date.now();

    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = (e.loaded / e.total) * 100;
      progressFill.style.width = pct.toFixed(1) + '%';
      progressPct.textContent = pct.toFixed(0) + '%';
      const elapsed = (Date.now() - startedAt) / 1000;
      const rate = elapsed > 0 ? e.loaded / elapsed : 0;
      progressRate.textContent = fmtRate(rate);
    };

    xhr.onload = () => {
      currentXhr = null;
      let payload = null;
      try { payload = JSON.parse(xhr.responseText); } catch (_) {}

      if (xhr.status >= 200 && xhr.status < 300 && payload) {
        showResult(payload);
      } else {
        const msg = (payload && payload.detail) || ('upload failed (HTTP ' + xhr.status + ')');
        showToast(msg, true);
        show(uploadView);
      }
    };

    xhr.onerror = () => {
      currentXhr = null;
      showToast('network error', true);
      show(uploadView);
    };

    xhr.onabort = () => {
      currentXhr = null;
      showToast('upload cancelled');
      show(uploadView);
    };

    xhr.send(fd);
  }

  function cancelUpload() {
    if (currentXhr) currentXhr.abort();
  }

  // --- result ---
  function showResult(payload) {
    currentResult = payload;

    resultUrl.value = payload.url;
    metaFilename.textContent = payload.filename;
    metaSize.textContent = fmtBytes(payload.size_bytes);
    metaSha.textContent = payload.sha256;
    metaSha.title = payload.sha256;

    const expiresMs = Date.parse(payload.expires_at);
    updateExpiry(expiresMs);
    if (expiryInterval) clearInterval(expiryInterval);
    expiryInterval = setInterval(() => updateExpiry(expiresMs), 1000);

    renderQR(payload.url);
    show(resultView);
  }

  function updateExpiry(targetMs) {
    metaExpires.textContent = fmtTimeLeft(targetMs);
    if (Date.now() >= targetMs && expiryInterval) {
      clearInterval(expiryInterval);
      expiryInterval = null;
    }
  }

  function renderQR(url) {
    qrEl.innerHTML = '';
    try {
      // typeNumber=0 = auto-detect, error correction "M" = 15% recovery
      const qr = qrcode(0, 'M');
      qr.addData(url);
      qr.make();
      // cellSize=4 px, margin=0 (we already have white padding around).
      qrEl.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
    } catch (e) {
      qrEl.textContent = 'QR error';
    }
  }

  function copyUrl() {
    const url = resultUrl.value;
    const onSuccess = () => {
      copyBtn.textContent = 'copied';
      copyBtn.classList.add('copied');
      setTimeout(() => {
        copyBtn.textContent = 'copy';
        copyBtn.classList.remove('copied');
      }, 1600);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(url).then(onSuccess, fallbackCopy);
    } else {
      fallbackCopy();
    }
    function fallbackCopy() {
      resultUrl.select();
      try {
        document.execCommand('copy');
        onSuccess();
      } catch (e) {
        showToast('could not copy — select manually', true);
      }
    }
  }

  function deleteCurrent() {
    if (!currentResult) return;
    if (!confirm('Delete this file now? This cannot be undone.')) return;

    // The delete_url already contains the token. Note: the DELETE endpoint
    // is implemented in step 7. For step 4 we just call it; if the server
    // returns a 404/405 we surface it gracefully.
    const xhr = new XMLHttpRequest();
    xhr.open('DELETE', currentResult.delete_url, true);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        showToast('file deleted');
        currentResult = null;
        if (expiryInterval) { clearInterval(expiryInterval); expiryInterval = null; }
        show(uploadView);
      } else {
        showToast('could not delete: HTTP ' + xhr.status, true);
      }
    };
    xhr.onerror = () => showToast('network error', true);
    xhr.send();
  }

  function reset() {
    currentResult = null;
    fileInput.value = '';
    if (expiryInterval) { clearInterval(expiryInterval); expiryInterval = null; }
    show(uploadView);
  }

  // --- wire events ---
  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fileInput.click();
    }
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files && fileInput.files[0]) {
      startUpload(fileInput.files[0]);
    }
  });

  // Drag & drop. We have to preventDefault on dragover/drop to suppress
  // the browser's default of navigating to the file.
  ['dragenter', 'dragover'].forEach((ev) => {
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add('is-dragover');
    });
  });
  ['dragleave', 'drop'].forEach((ev) => {
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove('is-dragover');
    });
  });
  dropzone.addEventListener('drop', (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files[0]) startUpload(files[0]);
  });

  // Prevent the document itself from accepting drops elsewhere.
  ['dragover', 'drop'].forEach((ev) => {
    document.addEventListener(ev, (e) => {
      if (e.target === dropzone || dropzone.contains(e.target)) return;
      e.preventDefault();
    });
  });

  cancelBtn.addEventListener('click', cancelUpload);
  copyBtn.addEventListener('click', copyUrl);
  deleteBtn.addEventListener('click', deleteCurrent);
  newBtn.addEventListener('click', reset);
})();
