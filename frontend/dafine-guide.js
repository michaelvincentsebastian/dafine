/**
 * Dafine Onboarding Guide
 * ========================
 * Drop this <script> tag at the bottom of each page that needs a guide step:
 *
 *   <script src="dafine-guide.js"></script>
 *   <script>DafineGuide.init('account-settings');</script>
 *
 * Valid page keys: 'account-settings' | 'main' | 'dashboard' | 'history'
 *
 * Storage key: 'dafine_guide_v1'
 * Shape: { completed: string[], dismissed: boolean }
 */

(function () {
  'use strict';

  // ─── Design tokens (mirrors the app's Tailwind config) ─────────────────────
  const T = {
    bg:           '#131313',
    surface:      '#1a1a1a',
    surfaceHigh:  '#242424',
    border:       '#262626',
    borderMuted:  '#2e2e2e',
    yellow:       '#f0d000',
    yellowDim:    '#c9ae00',
    yellowGlow:   'rgba(240,208,0,0.08)',
    yellowRing:   'rgba(240,208,0,0.25)',
    muted:        '#777777',
    textPrimary:  '#f5f4f0',
    textSec:      '#999999',
    error:        '#e57373',
    fontMono:     "'DM Mono', monospace",
    fontDisp:     "'Syne', sans-serif",
  };

  // ─── Guide steps ───────────────────────────────────────────────────────────
  // Each step: { page, id, title, body, tip?, highlight?, action? }
  const STEPS = [
    // ── Page 1: account-settings ──────────────────────────────
    {
      page:      'account-settings',
      id:        'welcome',
      title:     'Welcome to Dafine',
      body:      'Before you can clean data, you need one thing: a free <strong>OpenRouter API key</strong>. This is how Dafine talks to the AI. It takes about 60 seconds to set up.',
      tip:       null,
      highlight: null,
      action:    { label: 'Show me how →', next: 'openrouter-step' },
    },
    {
      page:      'account-settings',
      id:        'openrouter-step',
      title:     'Step 1 — Get your OpenRouter key',
      body:      `<ol style="padding-left:18px;line-height:2.2;color:${T.textSec};font-size:11px;font-family:${T.fontMono}">
                    <li>Open <a href="https://openrouter.ai" target="_blank" style="color:${T.yellow};border-bottom:1px solid ${T.yellowRing}">openrouter.ai</a> in a new tab</li>
                    <li>Click <strong style="color:${T.textPrimary}">Sign in</strong> → create a free account</li>
                    <li>Go to <strong style="color:${T.textPrimary}">Keys</strong> in the top nav</li>
                    <li>Click <strong style="color:${T.textPrimary}">+ Create Key</strong> → copy the key</li>
                    <li>It starts with <code style="background:#1a1a1a;border:1px solid ${T.border};padding:1px 6px;color:${T.yellow}">sk-or-</code></li>
                  </ol>`,
      tip:       'The free tier includes models like GPT-4o mini — more than enough for data cleaning.',
      highlight: 'api-key-section',
      action:    { label: 'Got my key →', next: 'paste-key' },
    },
    {
      page:      'account-settings',
      id:        'paste-key',
      title:     'Step 2 — Paste it here',
      body:      'Paste your key into the <strong>OpenRouter API Key</strong> field below, then click <strong>Save API Key</strong>. The key is encrypted on our server — it never appears in plain text again.',
      tip:       'You only have to do this once. You can update it anytime from this page.',
      highlight: 'api-key-section',
      action:    { label: 'Key saved, continue →', href: 'main.html', stepComplete: 'account-settings' },
    },

    // ── Page 2: main (clean) ───────────────────────────────────
    {
      page:      'main',
      id:        'clean-welcome',
      title:     'Your first data clean',
      body:      'This is where the magic happens. Drop any messy CSV, Excel, or Parquet file and Dafine will profile every column, choose the right fix for each one, and hand the plan to an AI that writes clean DuckDB SQL.',
      tip:       null,
      highlight: null,
      action:    { label: 'Walk me through it →', next: 'upload-step' },
    },
    {
      page:      'main',
      id:        'upload-step',
      title:     'Step 1 — Drop your file',
      body:      `<ul style="list-style:none;padding:0;line-height:2.2;color:${T.textSec};font-size:11px;font-family:${T.fontMono}">
                    <li>→ Drag a file onto the drop zone, or click <strong style="color:${T.textPrimary}">Choose File</strong></li>
                    <li>→ Supported: <code style="color:${T.yellow}">.csv  .xlsx  .parquet  .db  .sqlite</code></li>
                    <li>→ Give the session an optional <strong style="color:${T.textPrimary}">title</strong> so you can find it in History</li>
                  </ul>`,
      tip:       "Don't have a messy file handy? Download a sample CSV from Kaggle or use any spreadsheet export.",
      highlight: 'upload-zone',
      action:    { label: 'Got it →', next: 'preview-step' },
    },
    {
      page:      'main',
      id:        'preview-step',
      title:     'Step 2 — Preview & add context',
      body:      `After upload, click <strong>Preview Dataset</strong>. You'll see:<br><br>
                  <span style="font-family:${T.fontMono};font-size:11px;color:${T.textSec};line-height:2">
                  → Row &amp; column counts, null totals<br>
                  → Each column's inferred type<br>
                  → The first 10 rows of data<br>
                  → A <strong style="color:${T.textPrimary}">Column Context</strong> grid — type hints here (e.g. <em style="color:${T.yellow}">1=Male, 2=Female</em>) so the AI handles your data correctly
                  </span>`,
      tip:       'Column context is optional but powerful. The AI ignores columns you leave blank — only add notes where the column name alone is ambiguous.',
      highlight: null,
      action:    { label: 'Understood →', next: 'clean-step' },
    },
    {
      page:      'main',
      id:        'clean-step',
      title:     'Step 3 — Clean with AI',
      body:      `Click <strong>Clean with AI</strong> and Dafine will:<br><br>
                  <span style="font-family:${T.fontMono};font-size:11px;color:${T.textSec};line-height:2.1">
                  <span style="color:${T.yellow}">01</span> Profile every column (skewness, IQR, nulls, modes)<br>
                  <span style="color:${T.yellow}">02</span> Build a precise per-column prompt<br>
                  <span style="color:${T.yellow}">03</span> Send it to the AI — you'll see a progress bar<br>
                  <span style="color:${T.yellow}">04</span> Execute the generated SQL in DuckDB<br>
                  <span style="color:${T.yellow}">05</span> Return your cleaned file + an outlier report
                  </span>`,
      tip:       'The whole process usually takes 15–45 seconds depending on file size and model speed.',
      highlight: null,
      action:    { label: 'Ready to clean →', stepComplete: 'main', dismiss: true },
    },

    // ── Page 3: dashboard ─────────────────────────────────────
    {
      page:      'dashboard',
      id:        'dash-welcome',
      title:     'Build your first chart',
      body:      'The Dashboard lets you explore any cleaned dataset visually — bar, line, scatter, pie, or donut charts. No code, no exports needed. Just pick a dataset and configure the axes.',
      tip:       null,
      highlight: null,
      action:    { label: 'Show me →', next: 'load-dataset' },
    },
    {
      page:      'dashboard',
      id:        'load-dataset',
      title:     'Step 1 — Load a cleaned dataset',
      body:      `Click <strong>Load Dataset</strong> at the top right. A panel will list all your cleaning sessions. Pick any one — Dafine will download the cleaned file and load it into the browser.`,
      tip:       'You must complete at least one cleaning session first. Head to Clean if you haven\'t yet.',
      highlight: 'load-btn',
      action:    { label: 'Got it →', next: 'configure-chart' },
    },
    {
      page:      'dashboard',
      id:        'configure-chart',
      title:     'Step 2 — Configure your chart',
      body:      `Use the right-hand panel:<br><br>
                  <span style="font-family:${T.fontMono};font-size:11px;color:${T.textSec};line-height:2.1">
                  → Pick a <strong style="color:${T.textPrimary}">chart type</strong> from the icon toolbar<br>
                  → Set the <strong style="color:${T.textPrimary}">X-Axis</strong> (categories or labels)<br>
                  → Add one or more <strong style="color:${T.textPrimary}">Y-Axis</strong> columns (click + Add for multi-series)<br>
                  → Choose an <strong style="color:${T.textPrimary}">Aggregation</strong> — Sum, Avg, Count, etc.<br>
                  → Optionally add <strong style="color:${T.textPrimary}">Filters</strong> and <strong style="color:${T.textPrimary}">Sort / Rank Limit</strong>
                  </span>`,
      tip:       'The data table at the bottom always reflects your active filters — great for spot-checking.',
      highlight: null,
      action:    { label: 'Makes sense →', next: 'save-dash' },
    },
    {
      page:      'dashboard',
      id:        'save-dash',
      title:     'Step 3 — Save your dashboard',
      body:      'When you\'re happy with the chart, add a title in the <strong>Dashboard Title</strong> field and click <strong>Save</strong>. The full configuration (axes, filters, sort, type) is saved to your browser. You can reopen it anytime from the History page.',
      tip:       'Saved dashboards live in your browser\'s localStorage. Clearing browser data will remove them.',
      highlight: null,
      action:    { label: 'Saved! →', href: 'history.html', stepComplete: 'dashboard' },
    },

    // ── Page 4: history ───────────────────────────────────────
    {
      page:      'history',
      id:        'hist-welcome',
      title:     'Your session archive',
      body:      'History keeps a record of every cleaning session and every saved dashboard. You can re-download cleaned files, inspect the SQL the AI generated, and see its reasoning — all without re-running anything.',
      tip:       null,
      highlight: null,
      action:    { label: 'Take a look →', next: 'hist-tabs' },
    },
    {
      page:      'history',
      id:        'hist-tabs',
      title:     'Two tabs, two views',
      body:      `<span style="font-family:${T.fontMono};font-size:11px;color:${T.textSec};line-height:2.2">
                  <strong style="color:${T.yellow}">Cleaning History</strong> — every AI clean you've run.<br>
                  Click <strong style="color:${T.textPrimary}">Details</strong> on any row to see:<br>
                  &nbsp;&nbsp;→ Column context you provided<br>
                  &nbsp;&nbsp;→ The generated SQL (hidden by default — click Reveal)<br>
                  &nbsp;&nbsp;→ The AI's reasoning behind its decisions<br><br>
                  <strong style="color:${T.yellow}">Dashboard History</strong> — saved chart configs.<br>
                  Click <strong style="color:${T.textPrimary}">Open</strong> to jump straight back into that dashboard.
                  </span>`,
      tip:       null,
      highlight: null,
      action:    { label: 'Got it →', next: 'hist-actions' },
    },
    {
      page:      'history',
      id:        'hist-actions',
      title:     'Download & manage',
      body:      `For any cleaning session you can:<br><br>
                  <span style="font-family:${T.fontMono};font-size:11px;color:${T.textSec};line-height:2.1">
                  <span style="color:${T.yellow}">↓</span> <strong style="color:${T.textPrimary}">Download</strong> — re-downloads the cleaned file as CSV<br>
                  <span style="color:${T.yellow}">⊙</span> <strong style="color:${T.textPrimary}">Dashboard</strong> — open this session in the explorer<br>
                  <span style="color:#e57373}">✕</span> <strong style="color:${T.textPrimary}">Delete</strong> — removes the record <em>and</em> the stored file permanently
                  </span>`,
      tip:       "Deleting a session can't be undone. Download the file first if you still need it.",
      highlight: null,
      action:    { label: "I'm all set ✓", stepComplete: 'history', dismiss: true },
    },
  ];

  // ─── State helpers ─────────────────────────────────────────────────────────
  const STORE_KEY = 'dafine_guide_v1';

  function getState() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{"completed":[],"dismissed":false}'); }
    catch { return { completed: [], dismissed: false }; }
  }

  function setState(patch) {
    const s = Object.assign(getState(), patch);
    localStorage.setItem(STORE_KEY, JSON.stringify(s));
  }

  function isPageComplete(page) {
    return getState().completed.includes(page);
  }

  // ─── DOM helpers ───────────────────────────────────────────────────────────
  function $(sel) { return document.querySelector(sel); }

  function injectFonts() {
    if (document.getElementById('dafine-guide-fonts')) return;
    const l = document.createElement('link');
    l.id   = 'dafine-guide-fonts';
    l.rel  = 'stylesheet';
    l.href = 'https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&display=swap';
    document.head.appendChild(l);
  }

  // ─── Highlight ring ────────────────────────────────────────────────────────
  let _highlightEl = null;

  function setHighlight(targetId) {
    clearHighlight();
    if (!targetId) return;
    const el = document.getElementById(targetId);
    if (!el) return;
    _highlightEl = el;
    el.style.setProperty('outline',         `2px solid ${T.yellow}`, 'important');
    el.style.setProperty('outline-offset',  '3px',                   'important');
    el.style.setProperty('box-shadow',      `0 0 0 5px ${T.yellowGlow}`, 'important');
    el.style.setProperty('transition',      'outline 0.2s, box-shadow 0.2s', 'important');
    // Scroll into view
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function clearHighlight() {
    if (!_highlightEl) return;
    _highlightEl.style.removeProperty('outline');
    _highlightEl.style.removeProperty('outline-offset');
    _highlightEl.style.removeProperty('box-shadow');
    _highlightEl.style.removeProperty('transition');
    _highlightEl = null;
  }

  // ─── Guide card ────────────────────────────────────────────────────────────
  let _card = null;
  let _currentPage = null;
  let _currentStepIndex = 0;
  let _pageSteps = [];

  function buildCard() {
    if (_card) return;
    const card = document.createElement('div');
    card.id = 'dafine-guide-card';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-label', 'Dafine user guide');
    card.innerHTML = `
      <div id="dg-inner">
        <div id="dg-header">
          <div id="dg-eyebrow">
            <span id="dg-logo">Da<span>+</span>fine</span>
            <span id="dg-step-label"></span>
          </div>
          <button id="dg-close" aria-label="Close guide" title="Dismiss guide">×</button>
        </div>
        <div id="dg-body">
          <h2 id="dg-title"></h2>
          <div id="dg-content"></div>
          <div id="dg-tip" style="display:none">
            <span id="dg-tip-icon">💡</span>
            <p id="dg-tip-text"></p>
          </div>
        </div>
        <div id="dg-footer">
          <div id="dg-progress"></div>
          <button id="dg-action"></button>
        </div>
      </div>`;
    document.body.appendChild(card);
    _card = card;

    // Inject styles
    const style = document.createElement('style');
    style.id = 'dafine-guide-styles';
    style.textContent = `
      #dafine-guide-card {
        position: fixed;
        bottom: 28px;
        right: 28px;
        z-index: 99999;
        width: 360px;
        max-width: calc(100vw - 32px);
        background: ${T.bg};
        border: 1px solid ${T.border};
        box-shadow: 0 24px 64px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,255,255,0.03);
        animation: dg-slide-in 0.35s cubic-bezier(0.16,1,0.3,1) forwards;
        font-family: ${T.fontMono};
      }
      @keyframes dg-slide-in {
        from { opacity:0; transform: translateY(20px); }
        to   { opacity:1; transform: translateY(0); }
      }
      @keyframes dg-slide-out {
        from { opacity:1; transform: translateY(0); }
        to   { opacity:0; transform: translateY(16px); }
      }
      #dg-inner { padding: 0; }

      /* ── header ── */
      #dg-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 16px 10px;
        border-bottom: 1px solid ${T.border};
        background: ${T.surface};
      }
      #dg-eyebrow { display: flex; align-items: center; gap: 10px; }
      #dg-logo {
        font-family: ${T.fontDisp};
        font-size: 13px;
        font-weight: 800;
        color: ${T.textPrimary};
        letter-spacing: -0.3px;
      }
      #dg-logo span { color: ${T.yellow}; }
      #dg-step-label {
        font-family: ${T.fontMono};
        font-size: 9px;
        text-transform: uppercase;
        letter-spacing: 0.14em;
        color: ${T.yellow};
        background: ${T.yellowGlow};
        border: 1px solid ${T.yellowRing};
        padding: 2px 8px;
      }
      #dg-close {
        background: none;
        border: 1px solid ${T.border};
        color: ${T.muted};
        cursor: pointer;
        font-size: 16px;
        line-height: 1;
        padding: 2px 7px;
        transition: color 0.15s, border-color 0.15s;
      }
      #dg-close:hover { color: ${T.textPrimary}; border-color: ${T.muted}; }

      /* ── body ── */
      #dg-body { padding: 18px 18px 14px; }
      #dg-title {
        font-family: ${T.fontDisp};
        font-size: 17px;
        font-weight: 800;
        letter-spacing: -0.4px;
        color: ${T.textPrimary};
        margin: 0 0 10px;
        line-height: 1.2;
      }
      #dg-content {
        font-family: ${T.fontMono};
        font-size: 11.5px;
        color: ${T.textSec};
        line-height: 1.85;
      }
      #dg-content strong { color: ${T.textPrimary}; font-weight: 500; }
      #dg-content a { color: ${T.yellow}; }
      #dg-content code {
        font-family: ${T.fontMono};
        font-size: 10px;
        background: ${T.surfaceHigh};
        border: 1px solid ${T.border};
        padding: 1px 5px;
        color: ${T.yellow};
      }

      /* ── tip ── */
      #dg-tip {
        display: flex;
        gap: 8px;
        margin-top: 12px;
        padding: 10px 12px;
        background: ${T.yellowGlow};
        border: 1px solid ${T.yellowRing};
        border-left: 2px solid ${T.yellow};
      }
      #dg-tip-icon { font-size: 13px; flex-shrink: 0; margin-top: 1px; }
      #dg-tip-text {
        font-family: ${T.fontMono};
        font-size: 10px;
        color: #aaa;
        line-height: 1.7;
        margin: 0;
      }

      /* ── footer ── */
      #dg-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 16px 14px;
        border-top: 1px solid ${T.border};
        background: ${T.surface};
      }
      #dg-progress {
        display: flex;
        gap: 5px;
        align-items: center;
      }
      .dg-dot {
        width: 6px;
        height: 6px;
        border: 1px solid ${T.border};
        border-radius: 50%;
        background: transparent;
        transition: background 0.2s, border-color 0.2s;
      }
      .dg-dot.done { background: ${T.yellowDim}; border-color: ${T.yellowDim}; }
      .dg-dot.active { background: ${T.yellow}; border-color: ${T.yellow}; width: 14px; border-radius: 3px; }

      #dg-action {
        font-family: ${T.fontDisp};
        font-size: 12px;
        font-weight: 700;
        background: ${T.yellow};
        color: #0f0f0f;
        border: none;
        padding: 8px 16px;
        cursor: pointer;
        letter-spacing: 0.02em;
        transition: background 0.15s, transform 0.1s;
      }
      #dg-action:hover { background: ${T.yellowDim}; transform: translateY(-1px); }
      #dg-action:active { transform: translateY(0); }

      /* ── mobile ── */
      @media (max-width: 480px) {
        #dafine-guide-card { bottom: 0; right: 0; width: 100%; max-width: 100%; border-left: none; border-right: none; border-bottom: none; }
      }

      /* ── skip link ── */
      #dg-skip {
        position: fixed;
        bottom: 28px;
        right: 28px;
        z-index: 99999;
        font-family: ${T.fontMono};
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        color: ${T.muted};
        background: ${T.bg};
        border: 1px solid ${T.border};
        padding: 8px 14px;
        cursor: pointer;
        transition: color 0.15s, border-color 0.15s;
        display: none;
      }
      #dg-skip:hover { color: ${T.textPrimary}; border-color: ${T.muted}; }
    `;
    document.head.appendChild(style);

    // Wire up close button
    document.getElementById('dg-close').addEventListener('click', dismissGuide);

    // Wire up action button
    document.getElementById('dg-action').addEventListener('click', handleAction);
  }

  function renderStep(stepIndex) {
    if (stepIndex < 0 || stepIndex >= _pageSteps.length) return;
    _currentStepIndex = stepIndex;
    const step = _pageSteps[stepIndex];

    // Highlight
    setHighlight(step.highlight || null);

    // Step label
    const total = _pageSteps.length;
    document.getElementById('dg-step-label').textContent = `${stepIndex + 1} / ${total}`;

    // Title & content
    document.getElementById('dg-title').textContent   = step.title;
    document.getElementById('dg-content').innerHTML   = step.body;

    // Tip
    const tipEl = document.getElementById('dg-tip');
    if (step.tip) {
      document.getElementById('dg-tip-text').textContent = step.tip;
      tipEl.style.display = 'flex';
    } else {
      tipEl.style.display = 'none';
    }

    // Progress dots
    const prog = document.getElementById('dg-progress');
    prog.innerHTML = '';
    for (let i = 0; i < total; i++) {
      const dot = document.createElement('span');
      dot.className = 'dg-dot' + (i < stepIndex ? ' done' : i === stepIndex ? ' active' : '');
      prog.appendChild(dot);
    }

    // Action button
    const btn    = document.getElementById('dg-action');
    const action = step.action || {};
    btn.textContent = action.label || 'Next →';

    // Animate card body on step change
    const body = document.getElementById('dg-body');
    body.style.opacity = '0';
    body.style.transform = 'translateY(6px)';
    requestAnimationFrame(() => {
      body.style.transition = 'opacity 0.25s ease, transform 0.25s ease';
      body.style.opacity    = '1';
      body.style.transform  = 'translateY(0)';
    });
  }

  function handleAction() {
    const step   = _pageSteps[_currentStepIndex];
    const action = step.action || {};

    // Mark page complete if requested
    if (action.stepComplete) {
      const s = getState();
      if (!s.completed.includes(action.stepComplete)) {
        s.completed.push(action.stepComplete);
        setState(s);
      }
    }

    // Redirect
    if (action.href) {
      clearHighlight();
      window.location.href = action.href;
      return;
    }

    // Dismiss
    if (action.dismiss) {
      dismissGuide();
      return;
    }

    // Go to named step
    if (action.next) {
      const idx = _pageSteps.findIndex(s => s.id === action.next);
      if (idx !== -1) { renderStep(idx); return; }
    }

    // Default: next step
    if (_currentStepIndex < _pageSteps.length - 1) {
      renderStep(_currentStepIndex + 1);
    } else {
      dismissGuide();
    }
  }

  function dismissGuide() {
    clearHighlight();
    const card = document.getElementById('dafine-guide-card');
    if (!card) return;
    card.style.animation = 'dg-slide-out 0.25s ease forwards';
    setTimeout(() => { card.remove(); _card = null; showSkipRestore(); }, 250);
  }

  function showSkipRestore() {
    // Show a subtle "Resume guide" button if guide was dismissed mid-flow
    if (getState().dismissed) return;
    const skip = document.createElement('button');
    skip.id          = 'dg-skip';
    skip.textContent = '? Resume guide';
    skip.style.display = 'block';
    skip.addEventListener('click', () => {
      skip.remove();
      buildCard();
      renderStep(_currentStepIndex);
    });
    document.body.appendChild(skip);
  }

  // ─── Public API ────────────────────────────────────────────────────────────
  function init(page) {
    _currentPage  = page;
    _pageSteps    = STEPS.filter(s => s.page === page);
    if (!_pageSteps.length) return;

    // Already completed? Don't auto-show.
    if (isPageComplete(page)) return;

    // Wait for DOM ready
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => { injectFonts(); buildCard(); renderStep(0); });
    } else {
      injectFonts();
      buildCard();
      renderStep(0);
    }
  }

  // ─── Expose globally ───────────────────────────────────────────────────────
  window.DafineGuide = { init, dismissGuide };

})();