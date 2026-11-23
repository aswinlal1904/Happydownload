(() => {
  const state = {
    downloads: [],
    selectedId: null,
    defaultDir: "",
    settings: null,
    orphans: [],
    clipboardCandidate: null,
    seenCompletions: new Set(),
    initializedCompletions: false,
    queueFilter: "all",
    queueSearch: "",
  };

  const els = {
    capBadge: document.getElementById("cap-badge"),
    globalSpeed: document.getElementById("global-speed"),
    diskFree: document.getElementById("disk-free"),
    sseIndicator: document.getElementById("sse-indicator"),
    sseText: document.getElementById("sse-text"),
    pauseAllBtn: document.getElementById("pause-all-btn"),
    resumeAllBtn: document.getElementById("resume-all-btn"),
    guideToggleBtn: document.getElementById("guide-toggle-btn"),
    guideCloseBtn: document.getElementById("guide-close-btn"),
    guideCard: document.getElementById("guide-card"),
    settingsToggleBtn: document.getElementById("settings-toggle-btn"),
    settingsCard: document.getElementById("settings-card"),
    scheduleBadge: document.getElementById("schedule-badge"),
    setMaxConcurrent: document.getElementById("set-max-concurrent"),
    setCompleteAction: document.getElementById("set-complete-action"),
    setAutoResume: document.getElementById("set-auto-resume"),
    setPreventSleep: document.getElementById("set-prevent-sleep"),
    setClipboardWatch: document.getElementById("set-clipboard-watch"),
    setNotifications: document.getElementById("set-notifications"),
    setAutoCategorize: document.getElementById("set-auto-categorize"),
    setCookiesBrowser: document.getElementById("set-cookies-browser"),
    setSchedEnabled: document.getElementById("set-sched-enabled"),
    setSchedStart: document.getElementById("set-sched-start"),
    setSchedEnd: document.getElementById("set-sched-end"),
    setSchedCap: document.getElementById("set-sched-cap"),
    saveSettingsBtn: document.getElementById("save-settings-btn"),
    ffmpegBanner: document.getElementById("ffmpeg-banner"),
    clipboardBanner: document.getElementById("clipboard-banner"),
    orphanBanner: document.getElementById("orphan-banner"),
    form: document.getElementById("download-form"),
    urlInput: document.getElementById("url-input"),
    batchToggleBtn: document.getElementById("batch-toggle-btn"),
    batchUrlsWrap: document.getElementById("batch-urls-wrap"),
    batchUrlsInput: document.getElementById("batch-urls-input"),
    destInput: document.getElementById("dest-input"),
    destDiskBadge: document.getElementById("dest-disk-badge"),
    browseBtn: document.getElementById("browse-btn"),
    probeBtn: document.getElementById("probe-btn"),
    startBtn: document.getElementById("start-btn"),
    filenameInput: document.getElementById("filename-input"),
    shaInput: document.getElementById("sha-input"),
    maxConnInput: document.getElementById("max-conn-input"),
    maxConnVal: document.getElementById("max-conn-val"),
    speedLimitInput: document.getElementById("speed-limit-input"),
    adaptiveInput: document.getElementById("adaptive-input"),
    mirrorsInput: document.getElementById("mirrors-input"),
    headersInput: document.getElementById("headers-input"),
    probePreview: document.getElementById("probe-preview"),
    feedback: document.getElementById("form-feedback"),
    downloadsList: document.getElementById("downloads-list"),
    queueCount: document.getElementById("queue-count"),
    queueSearchInput: document.getElementById("queue-search"),
    queueFilters: document.getElementById("queue-filters"),
    retryFailedBtn: document.getElementById("retry-failed-btn"),
    clearCompletedBtn: document.getElementById("clear-completed-btn"),
    inspectorBadge: document.getElementById("inspector-badge"),
    inspectorBody: document.getElementById("inspector-body"),
  };

  function fmtBytes(bytes) {
    if (bytes === null || bytes === undefined) return "Unknown";
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let val = bytes / 1024;
    let idx = 0;
    while (val >= 1024 && idx < units.length - 1) {
      val /= 1024;
      idx++;
    }
    return `${val.toFixed(val >= 100 ? 1 : 2)} ${units[idx]}`;
  }

  function fmtSpeed(bps) {
    if (!bps || bps <= 0) return "0 B/s";
    return `${fmtBytes(bps)}/s`;
  }

  function fmtEta(sec) {
    if (sec === null || sec === undefined || !isFinite(sec) || sec < 0) return "--";
    const s = Math.round(sec);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    if (h > 0) return `${h}h ${m}m ${r}s`;
    if (m > 0) return `${m}m ${r}s`;
    return `${r}s`;
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function parseHeaders(raw) {
    const out = {};
    if (!raw) return out;
    for (const line of raw.split(/\r?\n/)) {
      const idx = line.indexOf(":");
      if (idx > 0) {
        const k = line.slice(0, idx).trim();
        const v = line.slice(idx + 1).trim();
        if (k) out[k] = v;
      }
    }
    return out;
  }

  function parseMirrors(raw) {
    if (!raw) return [];
    return raw
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter((s) => /^https?:\/\//i.test(s));
  }

  function playDoneChime() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const now = ctx.currentTime;
      [523.25, 659.25, 783.99].forEach((freq, i) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.06, now + i * 0.11);
        gain.gain.exponentialRampToValueAtTime(0.001, now + i * 0.11 + 0.28);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(now + i * 0.11);
        osc.stop(now + i * 0.11 + 0.3);
      });
    } catch {
      // Audio unavailable
    }
  }

  function notifyCompleted(item) {
    if (!state.settings || !state.settings.desktop_notifications) return;
    playDoneChime();
    if ("Notification" in window && Notification.permission === "granted") {
      new Notification("Happydownload Complete", {
        body: `${item.filename} (${fmtBytes(item.total_bytes)})`,
      });
    }
  }

  function showFeedback(msg, type = "error") {
    els.feedback.textContent = msg;
    els.feedback.className = `feedback ${type}`;
  }

  function clearFeedback() {
    els.feedback.className = "feedback hidden";
    els.feedback.textContent = "";
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `Request failed (${res.status})`);
    }
    return data;
  }

  async function refreshDiskInfo(path) {
    try {
      const info = await api("/api/disk-info", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
      const fs = info.filesystem ? ` • ${info.filesystem}` : "";
      els.destDiskBadge.textContent = `${fmtBytes(info.free_bytes)} free${fs}`;
      els.diskFree.textContent = fmtBytes(info.free_bytes);
    } catch {
      els.destDiskBadge.textContent = "Drive check unavailable";
    }
  }

  function populateSettingsForm(s) {
    if (!s) return;
    const active = document.activeElement;
    if (els.settingsCard.contains(active) && !els.settingsCard.classList.contains("hidden")) {
      // Do not clobber while user edits settings
      els.scheduleBadge.textContent = s.schedule_enabled
        ? s.schedule_currently_full_speed
          ? "Full-Speed Window"
          : `Capped (${s.schedule_day_limit_kbps} KB/s)`
        : "Scheduler Off";
      return;
    }
    els.setMaxConcurrent.value = s.max_concurrent_downloads ?? 2;
    els.setCompleteAction.value = s.on_all_complete_action || "none";
    els.setAutoResume.checked = !!s.auto_resume_on_startup;
    els.setPreventSleep.checked = !!s.prevent_sleep_while_downloading;
    els.setClipboardWatch.checked = !!s.clipboard_watch;
    els.setNotifications.checked = !!s.desktop_notifications;
    if (els.setAutoCategorize) els.setAutoCategorize.checked = !!s.auto_categorize_folders;
    if (els.setCookiesBrowser) els.setCookiesBrowser.value = s.cookies_from_browser || "none";
    els.setSchedEnabled.checked = !!s.schedule_enabled;
    els.setSchedStart.value = s.schedule_full_start || "23:00";
    els.setSchedEnd.value = s.schedule_full_end || "07:00";
    els.setSchedCap.value = s.schedule_day_limit_kbps ?? 1024;
    els.scheduleBadge.textContent = s.schedule_enabled
      ? s.schedule_currently_full_speed
        ? "Full-Speed Window"
        : `Capped (${s.schedule_day_limit_kbps} KB/s)`
      : "Scheduler Off";
  }

  function renderBanners() {
    if (state.clipboardCandidate) {
      els.clipboardBanner.classList.remove("hidden");
      els.clipboardBanner.innerHTML = `
        <div><strong>Link detected in clipboard:</strong> <span class="mono">${escapeHtml(state.clipboardCandidate.slice(0, 96))}</span></div>
        <div class="banner-actions">
          <button type="button" id="clip-use-btn" class="btn btn-primary btn-sm">Use Link</button>
          <button type="button" id="clip-dismiss-btn" class="btn btn-ghost btn-sm">Dismiss</button>
        </div>
      `;
    } else {
      els.clipboardBanner.classList.add("hidden");
    }

    if (state.orphans && state.orphans.length > 0) {
      const totalOrphanBytes = state.orphans.reduce((acc, o) => acc + (o.bytes || 0), 0);
      els.orphanBanner.classList.remove("hidden");
      els.orphanBanner.innerHTML = `
        <div><strong>${state.orphans.length} leftover untracked file(s)</strong> found in downloads folder (${fmtBytes(totalOrphanBytes)}).</div>
        <div class="banner-actions">
          <button type="button" id="clean-orphans-btn" class="btn btn-secondary btn-sm">Clean Up Leftovers</button>
        </div>
      `;
    } else {
      els.orphanBanner.classList.add("hidden");
    }
  }

  async function initInfo() {
    try {
      const info = await api("/api/info");
      state.defaultDir = info.default_download_dir;
      els.destInput.value = info.default_download_dir;
      if (info.disk) {
        const fs = info.disk.filesystem ? ` • ${info.disk.filesystem}` : "";
        els.destDiskBadge.textContent = `${fmtBytes(info.disk.free_bytes)} free${fs}`;
        els.diskFree.textContent = fmtBytes(info.disk.free_bytes);
      }
      if (info.settings) {
        state.settings = info.settings;
        populateSettingsForm(info.settings);
      }
      if (info.orphans) {
        state.orphans = info.orphans;
        renderBanners();
      }
      if (info.capabilities && els.capBadge) {
        const c = info.capabilities;
        const base = c.ytdlp_available ? `yt-dlp ${c.ytdlp_version} + HLS` : "Built-in HTML/HLS";
        els.capBadge.textContent = c.ffmpeg_available ? `${base} + FFmpeg` : base;
        if (els.ffmpegBanner) {
          if (!c.ffmpeg_available) {
            els.ffmpegBanner.classList.remove("hidden");
            els.ffmpegBanner.innerHTML = `
              <div><strong>FFmpeg not detected on this PC:</strong> Needed for 1080p/4K video+audio merging &amp; MP4 remuxing (Happydownload will also auto-install it when needed).</div>
              <div class="banner-actions">
                <button type="button" id="install-ffmpeg-btn" class="btn btn-primary btn-sm">Auto-Install Portable FFmpeg</button>
              </div>
            `;
          } else {
            els.ffmpegBanner.classList.add("hidden");
          }
        }
      }
      const listData = await api("/api/downloads");
      applySnapshot(listData);
    } catch (err) {
      showFeedback(`Failed to connect to local engine: ${err.message}`);
    }
  }

  function applySnapshot(data) {
    state.downloads = data.downloads || [];
    if (data.settings) {
      state.settings = data.settings;
      populateSettingsForm(data.settings);
    }
    if (data.orphans !== undefined) {
      state.orphans = data.orphans || [];
    }
    if (data.clipboard_candidate !== undefined) {
      state.clipboardCandidate = data.clipboard_candidate;
    }
    if (data.disk) {
      els.diskFree.textContent = fmtBytes(data.disk.free_bytes);
    }
    if (Array.isArray(data.recent_completions)) {
      if (!state.initializedCompletions) {
        data.recent_completions.forEach((c) => state.seenCompletions.add(`${c.id}:${c.ts}`));
        state.initializedCompletions = true;
      } else {
        data.recent_completions.forEach((c) => {
          const key = `${c.id}:${c.ts}`;
          if (!state.seenCompletions.has(key)) {
            state.seenCompletions.add(key);
            notifyCompleted(c);
          }
        });
      }
    }

    els.globalSpeed.textContent = fmtSpeed(data.total_speed_bps || 0);
    els.queueCount.textContent = `${state.downloads.length} task${state.downloads.length === 1 ? "" : "s"}`;

    if (!state.selectedId && state.downloads.length > 0) {
      state.selectedId = state.downloads[0].id;
    } else if (state.selectedId && !state.downloads.some((d) => d.id === state.selectedId)) {
      state.selectedId = state.downloads[0]?.id || null;
    }

    renderBanners();
    renderList();
    renderInspector();
  }

  function renderList() {
    const hasFailed = state.downloads.some((d) => d.status === "failed");
    const hasDone = state.downloads.some((d) => ["completed", "cancelled"].includes(d.status));
    if (els.retryFailedBtn) els.retryFailedBtn.classList.toggle("hidden", !hasFailed);
    if (els.clearCompletedBtn) els.clearCompletedBtn.classList.toggle("hidden", !hasDone);

    if (state.downloads.length === 0) {
      els.downloadsList.innerHTML =
        '<div class="empty-state">Paste a link above to start a crash-proof, adaptive multi-stream download.</div>';
      return;
    }

    const q = (state.queueSearch || "").trim().toLowerCase();
    const filtered = state.downloads.filter((d) => {
      if (state.queueFilter === "active" && !["downloading", "probing", "merging", "waiting_network", "queued", "verifying", "paused"].includes(d.status)) {
        return false;
      }
      if (state.queueFilter === "completed" && d.status !== "completed") {
        return false;
      }
      if (state.queueFilter === "failed" && !["failed", "cancelled"].includes(d.status)) {
        return false;
      }
      if (q) {
        const hay = `${d.filename || ""} ${d.url || ""} ${d.source_page_url || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    if (filtered.length === 0) {
      els.downloadsList.innerHTML =
        '<div class="empty-state">No downloads match the current filter.</div>';
      return;
    }

    els.downloadsList.innerHTML = filtered
      .map((d) => {
        const active = d.id === state.selectedId ? "selected" : "";
        const totalStr = d.total_bytes ? fmtBytes(d.total_bytes) : "Unknown size";
        const speedStr =
          d.status === "downloading"
            ? `${fmtSpeed(d.speed_bps)} &bull; ${d.active_connections}/${d.target_connections} conns &bull; ETA ${fmtEta(d.eta_seconds)}`
            : d.status === "merging"
            ? "Merging HD video + audio with FFmpeg..."
            : d.status === "verifying"
            ? `Verifying SHA-256 (${d.verify_progress}%)`
            : d.status === "waiting_network"
            ? "Retrying automatically on reconnect..."
            : d.status === "queued"
            ? "Queued (waiting for active slot)"
            : d.status === "completed"
            ? `Completed &bull; Avg ${fmtSpeed(d.avg_speed_bps)}`
            : "Ready to resume without loss";

        const canPause = ["downloading", "probing", "waiting_network", "queued"].includes(d.status);
        const canResume = ["paused", "failed"].includes(d.status);
        const canCancel = !["completed", "cancelled"].includes(d.status);

        return `
          <div class="dl-item ${active}" data-id="${d.id}">
            <div class="dl-head">
              <div class="dl-title">${escapeHtml(d.filename)}</div>
              <span class="status-chip status-${d.status}">${d.status.replace("_", " ")}</span>
            </div>
            <div class="progress-track">
              <div class="progress-fill" style="width: ${d.progress}%"></div>
            </div>
            <div class="dl-meta mono">
              <span>${fmtBytes(d.downloaded_bytes)} / ${totalStr} (${d.progress}%)</span>
              <span>${speedStr}</span>
            </div>
            ${d.error_message ? `<div class="dl-error">${escapeHtml(d.error_message)}</div>` : ""}
            <div class="dl-actions">
              ${canPause ? `<button class="btn btn-secondary btn-sm" data-act="pause" data-id="${d.id}">Pause</button>` : ""}
              ${canResume ? `<button class="btn btn-primary btn-sm" data-act="resume" data-id="${d.id}">Resume</button>` : ""}
              ${d.status === "completed" ? `<button class="btn btn-secondary btn-sm" data-act="open-folder" data-id="${d.id}">Open Folder</button>` : ""}
              ${d.status === "completed" ? `<button class="btn btn-secondary btn-sm" data-act="verify" data-id="${d.id}">Verify SHA-256</button>` : ""}
              ${canCancel ? `<button class="btn btn-ghost btn-sm" data-act="cancel" data-id="${d.id}">Cancel</button>` : ""}
              <button class="btn btn-ghost btn-sm" data-act="delete" data-id="${d.id}" title="Remove from list (keep file on disk)">Remove</button>
              ${d.status === "completed" ? `<button class="btn btn-danger btn-sm" data-act="delete-disk" data-id="${d.id}" title="Delete downloaded file from disk">Delete File</button>` : ""}
            </div>
          </div>
        `;
      })
      .join("");
  }

  function drawChart(canvas, history, peakBps) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 460;
    const height = canvas.clientHeight || 130;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    ctx.strokeStyle = "rgba(142, 160, 188, 0.12)";
    ctx.lineWidth = 1;
    for (let i = 1; i <= 3; i++) {
      const y = (height / 4) * i;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
    }

    if (!history || history.length < 2) {
      ctx.fillStyle = "#8ea0bc";
      ctx.font = "12px Segoe UI, sans-serif";
      ctx.fillText("Waiting for throughput samples...", 12, height / 2);
      return;
    }

    const maxVal = Math.max(1024, peakBps || 0, ...history.map((p) => p.bps || 0)) * 1.12;
    const stepX = width / Math.max(1, history.length - 1);

    ctx.beginPath();
    history.forEach((pt, idx) => {
      const x = idx * stepX;
      const y = height - (pt.bps / maxVal) * (height - 14) - 6;
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo((history.length - 1) * stepX, height);
    ctx.lineTo(0, height);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, height);
    grad.addColorStop(0, "rgba(56, 189, 248, 0.35)");
    grad.addColorStop(1, "rgba(16, 185, 129, 0.02)");
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    history.forEach((pt, idx) => {
      const x = idx * stepX;
      const y = height - (pt.bps / maxVal) * (height - 14) - 6;
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.fillStyle = "#8ea0bc";
    ctx.font = "11px Consolas, monospace";
    ctx.fillText(`Peak: ${fmtSpeed(peakBps)}`, 8, 14);
  }

  function renderInspector() {
    const d = state.downloads.find((x) => x.id === state.selectedId);
    if (!d) {
      els.inspectorBadge.textContent = "No selection";
      els.inspectorBody.innerHTML =
        '<div class="empty-state">Select a download to view its adaptive speed graph, segment map, worker connections, and resilience log.</div>';
      return;
    }

    els.inspectorBadge.textContent =
      d.mode === "hls"
        ? "HLS Multi-Stream"
        : d.accept_ranges
        ? "Multi-Segment Range"
        : "Single Stream";
    const segCells = (d.segment_map || [])
      .map((s) => `<div class="seg-cell s${s}"></div>`)
      .join("");

    const workersRows =
      d.workers && d.workers.length > 0
        ? d.workers
            .map(
              (w) => `
          <tr>
            <td class="mono">#${w.worker_id}</td>
            <td class="mono">${w.segment_id ?? "--"}</td>
            <td>${escapeHtml(w.state)}</td>
            <td class="mono">${fmtSpeed(w.speed_bps)}</td>
            <td class="mono">${w.retries}</td>
          </tr>`
            )
            .join("")
        : '<tr><td colspan="5" style="color: var(--muted);">No active worker threads</td></tr>';

    const logLines = (d.logs || [])
      .slice()
      .reverse()
      .map((l) => {
        const timeStr = new Date(l.ts * 1000).toLocaleTimeString();
        return `<div class="log-line ${l.level}"><span class="mono">[${timeStr}]</span> ${escapeHtml(l.message)}</div>`;
      })
      .join("");

    const activeEl = document.activeElement;
    const isEditingLive =
      activeEl &&
      (activeEl.id === "live-max-conn" ||
        activeEl.id === "live-limit-kb" ||
        activeEl.id === "update-url-input");

    const canUpdateUrl = !["completed", "cancelled"].includes(d.status);

    if (!isEditingLive) {
      els.inspectorBody.innerHTML = `
        <div class="telemetry-stats">
          <div class="stat-box"><span>Current Speed</span><strong class="mono">${fmtSpeed(d.speed_bps)}</strong></div>
          <div class="stat-box"><span>Average Speed</span><strong class="mono">${fmtSpeed(d.avg_speed_bps)}</strong></div>
          <div class="stat-box"><span>Peak Observed</span><strong class="mono">${fmtSpeed(d.peak_bps)}</strong></div>
          <div class="stat-box"><span>Connections</span><strong class="mono">${d.active_connections} / ${d.target_connections}</strong></div>
          <div class="stat-box"><span>Keep-Alive Reuses</span><strong class="mono">${d.keepalive_reuses || 0}</strong></div>
          <div class="stat-box"><span>Remaining ETA</span><strong class="mono">${fmtEta(d.eta_seconds)}</strong></div>
        </div>

        <div class="adaptive-banner">
          <strong>Adaptive Engine:</strong> ${escapeHtml(d.adaptive_reason || "Ready")}
          ${d.audio_url ? ` &bull; <strong>HD Video+Audio FFmpeg Merge</strong>` : ""}
          ${d.mirrors && d.mirrors.length ? ` &bull; <strong>${d.mirrors.length} mirror(s)</strong>` : ""}
          ${d.elapsed_seconds ? ` &bull; Elapsed: <span class="mono">${fmtEta(d.elapsed_seconds)}</span>` : ""}
        </div>

        <div class="chart-wrap">
          <canvas id="speed-chart"></canvas>
        </div>

        <div class="seg-map-label">
          <span>Segment Integrity Map (Cyan = Active, Green = Saved)</span>
          <span class="mono">${d.progress}%</span>
        </div>
        <div class="seg-map">${segCells}</div>

        <div class="live-controls">
          <label>Max Conns:
            <input id="live-max-conn" type="number" min="1" max="64" value="${d.config.max_connections}" />
          </label>
          <label>Limit (KB/s):
            <input id="live-limit-kb" type="number" min="0" step="128" value="${Math.round((d.config.speed_limit_bps || 0) / 1024)}" />
          </label>
          <label style="display:flex;align-items:center;gap:5px;">
            <input id="live-adaptive" type="checkbox" ${d.config.adaptive_enabled ? "checked" : ""} />
            Adaptive
          </label>
          <button id="apply-live-btn" class="btn btn-secondary btn-sm" data-id="${d.id}">Apply Live</button>
        </div>

        ${
          canUpdateUrl
            ? `
        <div class="url-update-box">
          <input id="update-url-input" type="url" placeholder="Expired link? Paste refreshed URL or backup mirror..." />
          <button id="replace-url-btn" class="btn btn-secondary btn-sm" data-id="${d.id}">Replace Link</button>
          <button id="add-mirror-btn" class="btn btn-ghost btn-sm" data-id="${d.id}">Add Mirror</button>
        </div>`
            : ""
        }

        ${
          d.actual_sha256
            ? `<div class="adaptive-banner mono" style="display:flex;justify-content:space-between;align-items:center;gap:8px;word-break:break-all;">
                <div><strong>SHA-256:</strong> ${escapeHtml(d.actual_sha256)}</div>
                <button type="button" id="copy-sha-btn" class="btn btn-ghost btn-sm" data-sha="${escapeHtml(d.actual_sha256)}">Copy</button>
              </div>`
            : ""
        }

        <table class="worker-table">
          <thead>
            <tr><th>Worker</th><th>Segment</th><th>State</th><th>Speed</th><th>Retries</th></tr>
          </thead>
          <tbody>${workersRows}</tbody>
        </table>

        <div class="log-box">${logLines || '<div class="log-line">No events yet.</div>'}</div>
      `;
    }

    drawChart(document.getElementById("speed-chart"), d.speed_history, d.peak_bps);
  }

  // Event Listeners
  if (els.guideToggleBtn && els.guideCard) {
    els.guideToggleBtn.addEventListener("click", () => {
      els.guideCard.classList.toggle("hidden");
    });
  }
  if (els.guideCloseBtn && els.guideCard) {
    els.guideCloseBtn.addEventListener("click", () => {
      els.guideCard.classList.add("hidden");
    });
  }

  els.settingsToggleBtn.addEventListener("click", () => {
    els.settingsCard.classList.toggle("hidden");
  });

  els.saveSettingsBtn.addEventListener("click", async () => {
    try {
      if (els.setNotifications.checked && "Notification" in window && Notification.permission === "default") {
        Notification.requestPermission().catch(() => {});
      }
      const data = await api("/api/settings", {
        method: "PATCH",
        body: JSON.stringify({
          max_concurrent_downloads: parseInt(els.setMaxConcurrent.value || "2", 10),
          on_all_complete_action: els.setCompleteAction.value,
          auto_resume_on_startup: els.setAutoResume.checked,
          prevent_sleep_while_downloading: els.setPreventSleep.checked,
          clipboard_watch: els.setClipboardWatch.checked,
          desktop_notifications: els.setNotifications.checked,
          auto_categorize_folders: els.setAutoCategorize ? els.setAutoCategorize.checked : false,
          cookies_from_browser: els.setCookiesBrowser ? els.setCookiesBrowser.value : "none",
          schedule_enabled: els.setSchedEnabled.checked,
          schedule_full_start: els.setSchedStart.value || "23:00",
          schedule_full_end: els.setSchedEnd.value || "07:00",
          schedule_day_limit_kbps: parseInt(els.setSchedCap.value || "1024", 10),
        }),
      });
      state.settings = data.settings;
      els.settingsCard.classList.add("hidden");
      showFeedback("Engine settings saved.", "success");
    } catch (err) {
      showFeedback(err.message);
    }
  });

  els.pauseAllBtn.addEventListener("click", async () => {
    try {
      await api("/api/downloads/pause-all", { method: "POST", body: "{}" });
    } catch (err) {
      showFeedback(err.message);
    }
  });

  els.resumeAllBtn.addEventListener("click", async () => {
    try {
      await api("/api/downloads/resume-all", { method: "POST", body: "{}" });
    } catch (err) {
      showFeedback(err.message);
    }
  });

  if (els.ffmpegBanner) {
    els.ffmpegBanner.addEventListener("click", async (e) => {
      if (e.target.id === "install-ffmpeg-btn") {
        const btn = e.target;
        btn.disabled = true;
        btn.textContent = "Installing FFmpeg...";
        try {
          const res = await api("/api/ffmpeg/install", { method: "POST", body: "{}" });
          els.ffmpegBanner.classList.add("hidden");
          if (res.capabilities && els.capBadge) {
            const c = res.capabilities;
            const base = c.ytdlp_available ? `yt-dlp ${c.ytdlp_version} + HLS` : "Built-in HTML/HLS";
            els.capBadge.textContent = c.ffmpeg_available ? `${base} + FFmpeg` : base;
          }
          showFeedback(res.message || "Portable FFmpeg installed successfully!", "success");
        } catch (err) {
          showFeedback(err.message);
          btn.disabled = false;
          btn.textContent = "Auto-Install Portable FFmpeg";
        }
      }
    });
  }

  els.clipboardBanner.addEventListener("click", async (e) => {
    if (e.target.id === "clip-use-btn" && state.clipboardCandidate) {
      els.urlInput.value = state.clipboardCandidate;
      await api("/api/clipboard/dismiss", {
        method: "POST",
        body: JSON.stringify({ url: state.clipboardCandidate }),
      });
      state.clipboardCandidate = null;
      renderBanners();
      els.probeBtn.click();
    } else if (e.target.id === "clip-dismiss-btn") {
      await api("/api/clipboard/dismiss", {
        method: "POST",
        body: JSON.stringify({ url: state.clipboardCandidate }),
      });
      state.clipboardCandidate = null;
      renderBanners();
    }
  });

  els.orphanBanner.addEventListener("click", async (e) => {
    if (e.target.id === "clean-orphans-btn") {
      try {
        const res = await api("/api/orphans/clean", { method: "POST", body: "{}" });
        state.orphans = [];
        renderBanners();
        showFeedback(`Cleaned ${res.removed} leftover file(s), freed ${fmtBytes(res.freed_bytes)}.`, "success");
        refreshDiskInfo(els.destInput.value || state.defaultDir);
      } catch (err) {
        showFeedback(err.message);
      }
    }
  });

  els.maxConnInput.addEventListener("input", () => {
    els.maxConnVal.textContent = els.maxConnInput.value;
  });

  els.destInput.addEventListener("change", () => {
    if (els.destInput.value.trim()) refreshDiskInfo(els.destInput.value.trim());
  });

  els.browseBtn.addEventListener("click", async () => {
    els.browseBtn.disabled = true;
    try {
      const data = await api("/api/pick-folder", {
        method: "POST",
        body: JSON.stringify({ initial_dir: els.destInput.value || state.defaultDir }),
      });
      if (data.path) {
        els.destInput.value = data.path;
        if (data.disk) {
          const fs = data.disk.filesystem ? ` • ${data.disk.filesystem}` : "";
          els.destDiskBadge.textContent = `${fmtBytes(data.disk.free_bytes)} free${fs}`;
        }
      }
    } catch (err) {
      showFeedback(err.message);
    } finally {
      els.browseBtn.disabled = false;
    }
  });

  els.probeBtn.addEventListener("click", async () => {
    const url = els.urlInput.value.trim();
    if (!url) return showFeedback("Please enter a download or video page URL first.");
    clearFeedback();
    els.probeBtn.disabled = true;
    els.probeBtn.textContent = "Inspecting...";
    try {
      const info = await api("/api/probe", {
        method: "POST",
        body: JSON.stringify({
          url,
          dest_dir: els.destInput.value.trim() || null,
          custom_headers: parseHeaders(els.headersInput.value),
        }),
      });
      if (info.kind === "videos" && Array.isArray(info.videos)) {
        state.lastProbedVideos = info.videos;
        els.probePreview.className = "probe-preview video-picker";
        els.probePreview.innerHTML = `
          <div class="video-picker-header">
            <div><strong>Found ${info.videos.length} video(s)</strong> on page &bull; Select qualities to download</div>
            <div class="banner-actions">
              <select id="video-quality-preset" class="preset-select" title="Apply quality preset to all videos">
                <option value="best">Preset: Best Available (4K / 1080p)</option>
                <option value="1080">Preset: 1080p Full HD</option>
                <option value="720">Preset: 720p HD</option>
                <option value="480">Preset: 480p Standard</option>
                <option value="audio">Preset: Audio Only</option>
              </select>
              <button type="button" id="dl-selected-videos-btn" class="btn btn-primary btn-sm">Download Selected</button>
              <button type="button" id="dl-all-videos-btn" class="btn btn-secondary btn-sm">Download All (${info.videos.length})</button>
            </div>
          </div>
          <div class="video-picker-list">
            ${info.videos
              .map(
                (v, vIdx) => `
              <div class="video-pick-row">
                <label class="video-pick-left">
                  <input type="checkbox" class="video-pick-chk" data-vidx="${vIdx}" checked />
                  ${v.thumbnail ? `<img src="${escapeHtml(v.thumbnail)}" alt="" class="video-thumb" loading="lazy" />` : ""}
                  <div>
                    <div class="video-pick-title">${escapeHtml(v.title)}</div>
                    <div class="video-pick-meta">${escapeHtml(v.extractor)}${v.duration ? ` &bull; ${fmtEta(v.duration)}` : ""} &bull; ${v.formats.length} format(s)</div>
                  </div>
                </label>
                <select class="video-pick-select" data-vidx="${vIdx}">
                  ${v.formats
                    .map(
                      (f, fIdx) =>
                        `<option value="${fIdx}">${escapeHtml(f.label)}${f.filesize_approx ? ` (~${fmtBytes(f.filesize_approx)})` : ""}</option>`
                    )
                    .join("")}
                </select>
              </div>`
              )
              .join("")}
          </div>
        `;
      } else {
        state.lastProbedVideos = null;
        els.probePreview.className = "probe-preview";
        els.probePreview.innerHTML = `
          <div><strong>Filename:</strong> ${escapeHtml(info.filename)}</div>
          <div><strong>Size:</strong> <span class="mono">${fmtBytes(info.total_bytes)}</span></div>
          <div><strong>Multi-Stream Resume:</strong> ${info.accept_ranges ? "Supported (Fast + Lossless)" : "Single-Stream Only"}</div>
          <div><strong>Disk Check:</strong> ${info.storage_ok ? `<span style="color:#6ee7b7">OK (${fmtBytes(info.free_bytes)} free)</span>` : `<span style="color:#fda4af">${escapeHtml(info.storage_warning)}</span>`}</div>
        `;
        if (!els.filenameInput.value.trim() && info.filename) {
          els.filenameInput.placeholder = info.filename;
        }
      }
    } catch (err) {
      els.probePreview.className = "probe-preview hidden";
      showFeedback(`Link inspection failed: ${err.message}`);
    } finally {
      els.probeBtn.disabled = false;
      els.probeBtn.textContent = "Inspect Link";
    }
  });

  els.probePreview.addEventListener("change", (e) => {
    if (e.target.id === "video-quality-preset" && state.lastProbedVideos) {
      const preset = e.target.value;
      state.lastProbedVideos.forEach((v, vIdx) => {
        const sel = els.probePreview.querySelector(`.video-pick-select[data-vidx="${vIdx}"]`);
        if (!sel || !v.formats.length) return;
        let chosenIdx = 0;
        if (preset === "audio") {
          const aIdx = v.formats.findIndex((f) => !f.has_video && f.has_audio);
          if (aIdx >= 0) chosenIdx = aIdx;
        } else if (preset !== "best") {
          const targetH = parseInt(preset, 10);
          const mIdx = v.formats.findIndex((f) => f.has_video && f.height && f.height <= targetH);
          if (mIdx >= 0) chosenIdx = mIdx;
        }
        sel.value = String(chosenIdx);
      });
    }
  });

  els.probePreview.addEventListener("click", async (e) => {
    const btn = e.target.closest("#dl-selected-videos-btn, #dl-all-videos-btn");
    if (!btn || !state.lastProbedVideos) return;
    const downloadAll = btn.id === "dl-all-videos-btn";
    const items = [];
    const limitKb = parseInt(els.speedLimitInput.value || "0", 10);
    const maxConn = parseInt(els.maxConnInput.value || "16", 10);
    const adaptive = els.adaptiveInput.checked;

    state.lastProbedVideos.forEach((v, vIdx) => {
      const chk = els.probePreview.querySelector(`.video-pick-chk[data-vidx="${vIdx}"]`);
      if (!downloadAll && (!chk || !chk.checked)) return;
      const sel = els.probePreview.querySelector(`.video-pick-select[data-vidx="${vIdx}"]`);
      const fIdx = parseInt(sel ? sel.value : "0", 10) || 0;
      const fmt = v.formats[fIdx] || v.formats[0];
      if (!fmt) return;
      const ext = fmt.ext || (fmt.protocol === "hls" ? "ts" : "mp4");
      const fname = v.title.toLowerCase().endsWith(`.${ext}`) ? v.title : `${v.title}.${ext}`;
      items.push({
        url: fmt.url,
        filename: fname,
        mode: fmt.protocol === "hls" ? "hls" : "http_range",
        source_page_url: v.page_url,
        audio_url: fmt.audio_url || null,
        custom_headers: {
          ...parseHeaders(els.headersInput.value),
          ...(fmt.audio_headers || {}),
          ...(fmt.http_headers || {}),
        },
        probe_first: fmt.protocol !== "hls",
      });
    });

    if (items.length === 0) return showFeedback("Select at least one video to download.");
    btn.disabled = true;
    try {
      const res = await api("/api/downloads/batch", {
        method: "POST",
        body: JSON.stringify({
          dest_dir: els.destInput.value.trim() || null,
          max_connections: maxConn,
          adaptive_enabled: adaptive,
          speed_limit_bps: Math.max(0, limitKb) * 1024,
          items,
        }),
      });
      els.urlInput.value = "";
      els.probePreview.className = "probe-preview hidden";
      state.lastProbedVideos = null;
      if (res.downloads && res.downloads[0]) state.selectedId = res.downloads[0].id;
      showFeedback(`Queued ${res.created_count} video(s) for download.`, "success");
      const listData = await api("/api/downloads");
      applySnapshot(listData);
    } catch (err) {
      showFeedback(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  if (els.batchToggleBtn && els.batchUrlsWrap) {
    els.batchToggleBtn.addEventListener("click", () => {
      const hidden = els.batchUrlsWrap.classList.toggle("hidden");
      els.urlInput.required = hidden;
      if (!hidden) els.batchUrlsInput.focus();
    });
  }

  if (els.queueSearchInput) {
    els.queueSearchInput.addEventListener("input", () => {
      state.queueSearch = els.queueSearchInput.value;
      renderList();
    });
  }

  if (els.queueFilters) {
    els.queueFilters.addEventListener("click", (e) => {
      const pill = e.target.closest(".filter-pill[data-filter]");
      if (!pill) return;
      state.queueFilter = pill.dataset.filter;
      els.queueFilters.querySelectorAll(".filter-pill").forEach((b) => b.classList.toggle("active", b === pill));
      renderList();
    });
  }

  if (els.retryFailedBtn) {
    els.retryFailedBtn.addEventListener("click", async () => {
      try {
        const res = await api("/api/downloads/retry-failed", { method: "POST", body: "{}" });
        showFeedback(`Retrying ${res.retried} failed download(s).`, "success");
        const listData = await api("/api/downloads");
        applySnapshot(listData);
      } catch (err) {
        showFeedback(err.message);
      }
    });
  }

  if (els.clearCompletedBtn) {
    els.clearCompletedBtn.addEventListener("click", async () => {
      try {
        const res = await api("/api/downloads/clear-completed", { method: "POST", body: "{}" });
        showFeedback(`Cleared ${res.cleared} finished item(s) from list (files kept on disk).`, "success");
        const listData = await api("/api/downloads");
        applySnapshot(listData);
      } catch (err) {
        showFeedback(err.message);
      }
    });
  }

  els.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const singleUrl = els.urlInput.value.trim();
    const batchOpen = els.batchUrlsWrap && !els.batchUrlsWrap.classList.contains("hidden");
    const batchLines = batchOpen && els.batchUrlsInput
      ? els.batchUrlsInput.value
          .split(/\r?\n/)
          .map((s) => s.trim())
          .filter((s) => /^https?:\/\//i.test(s))
      : [];
    if (singleUrl && /^https?:\/\//i.test(singleUrl) && !batchLines.includes(singleUrl)) {
      batchLines.unshift(singleUrl);
    }
    if (batchLines.length === 0) return showFeedback("Please paste at least one valid HTTP/HTTPS link.");
    clearFeedback();
    if (
      state.settings?.desktop_notifications &&
      "Notification" in window &&
      Notification.permission === "default"
    ) {
      Notification.requestPermission().catch(() => {});
    }
    els.startBtn.disabled = true;
    els.startBtn.textContent = "Starting...";
    try {
      const limitKb = parseInt(els.speedLimitInput.value || "0", 10);
      const maxConn = parseInt(els.maxConnInput.value || "16", 10);
      const customHdrs = parseHeaders(els.headersInput.value);

      if (batchLines.length > 1) {
        const res = await api("/api/downloads/batch", {
          method: "POST",
          body: JSON.stringify({
            dest_dir: els.destInput.value.trim() || null,
            max_connections: maxConn,
            adaptive_enabled: els.adaptiveInput.checked,
            speed_limit_bps: Math.max(0, limitKb) * 1024,
            custom_headers: customHdrs,
            items: batchLines.map((u) => ({ url: u })),
          }),
        });
        if (res.downloads && res.downloads[0]) state.selectedId = res.downloads[0].id;
        els.urlInput.value = "";
        if (els.batchUrlsInput) els.batchUrlsInput.value = "";
        els.probePreview.className = "probe-preview hidden";
        showFeedback(`Queued ${res.created_count} download(s) from ${batchLines.length} links!`, "success");
      } else {
        const data = await api("/api/downloads", {
          method: "POST",
          body: JSON.stringify({
            url: batchLines[0],
            dest_dir: els.destInput.value.trim() || null,
            filename: els.filenameInput.value.trim() || null,
            expected_sha256: els.shaInput.value.trim() || null,
            max_connections: maxConn,
            adaptive_enabled: els.adaptiveInput.checked,
            speed_limit_bps: Math.max(0, limitKb) * 1024,
            mirrors: parseMirrors(els.mirrorsInput.value),
            custom_headers: customHdrs,
          }),
        });
        state.selectedId = data.download.id;
        els.urlInput.value = "";
        if (els.batchUrlsInput) els.batchUrlsInput.value = "";
        els.filenameInput.value = "";
        els.shaInput.value = "";
        els.mirrorsInput.value = "";
        els.probePreview.className = "probe-preview hidden";
        const cnt = data.created_count || 1;
        showFeedback(
          cnt > 1
            ? `Extracted & queued ${cnt} videos from page!`
            : `Added download: ${data.download.filename}`,
          "success"
        );
      }
      const listData = await api("/api/downloads");
      applySnapshot(listData);
    } catch (err) {
      showFeedback(`Could not start download: ${err.message}`);
    } finally {
      els.startBtn.disabled = false;
      els.startBtn.textContent = "Download";
    }
  });

  els.downloadsList.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (btn) {
      e.stopPropagation();
      const id = btn.dataset.id;
      const act = btn.dataset.act;
      try {
        if (act === "delete") {
          await api(`/api/downloads/${id}/delete`, {
            method: "POST",
            body: JSON.stringify({ remove_file: false }),
          });
        } else if (act === "delete-disk") {
          await api(`/api/downloads/${id}/delete`, {
            method: "POST",
            body: JSON.stringify({ remove_file: true }),
          });
          showFeedback("Removed task and deleted file from disk.", "success");
        } else {
          await api(`/api/downloads/${id}/${act}`, { method: "POST", body: "{}" });
        }
        const listData = await api("/api/downloads");
        applySnapshot(listData);
      } catch (err) {
        showFeedback(err.message);
      }
      return;
    }

    const card = e.target.closest(".dl-item[data-id]");
    if (card) {
      state.selectedId = card.dataset.id;
      renderList();
      renderInspector();
    }
  });

  els.inspectorBody.addEventListener("click", async (e) => {
    const copyShaBtn = e.target.closest("#copy-sha-btn");
    if (copyShaBtn) {
      const sha = copyShaBtn.dataset.sha;
      if (sha && navigator.clipboard) {
        navigator.clipboard.writeText(sha).catch(() => {});
        copyShaBtn.textContent = "Copied!";
        setTimeout(() => {
          copyShaBtn.textContent = "Copy";
        }, 1500);
      }
      return;
    }

    const applyBtn = e.target.closest("#apply-live-btn");
    if (applyBtn) {
      const id = applyBtn.dataset.id;
      const maxConn = parseInt(document.getElementById("live-max-conn").value || "16", 10);
      const limitKb = parseInt(document.getElementById("live-limit-kb").value || "0", 10);
      const adaptive = document.getElementById("live-adaptive").checked;
      try {
        await api(`/api/downloads/${id}`, {
          method: "PATCH",
          body: JSON.stringify({
            max_connections: maxConn,
            speed_limit_bps: Math.max(0, limitKb) * 1024,
            adaptive_enabled: adaptive,
          }),
        });
        showFeedback("Live engine settings updated.", "success");
      } catch (err) {
        showFeedback(err.message);
      }
      return;
    }

    const replaceBtn = e.target.closest("#replace-url-btn, #add-mirror-btn");
    if (replaceBtn) {
      const id = replaceBtn.dataset.id;
      const input = document.getElementById("update-url-input");
      const newUrl = (input?.value || "").trim();
      if (!newUrl) return showFeedback("Please paste a valid URL first.");
      const addAsMirror = replaceBtn.id === "add-mirror-btn";
      try {
        await api(`/api/downloads/${id}/update-url`, {
          method: "POST",
          body: JSON.stringify({ url: newUrl, add_as_mirror: addAsMirror }),
        });
        if (input) input.value = "";
        showFeedback(
          addAsMirror
            ? "Backup mirror verified and added."
            : "Download link replaced without losing progress — click Resume if paused.",
          "success"
        );
        const listData = await api("/api/downloads");
        applySnapshot(listData);
      } catch (err) {
        showFeedback(err.message);
      }
    }
  });

  function connectSSE() {
    const es = new EventSource("/api/events");
    es.onopen = () => {
      els.sseIndicator.className = "status-dot-wrap connected";
      els.sseText.textContent = "Live";
    };
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        applySnapshot(data);
      } catch {
        // ignore parse glitch
      }
    };
    es.onerror = () => {
      els.sseIndicator.className = "status-dot-wrap error";
      els.sseText.textContent = "Reconnecting...";
    };
  }

  initInfo();
  connectSSE();
})();
