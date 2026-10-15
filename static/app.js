(function () {
  "use strict";
  const isMobile = window.innerWidth <= 820 ||
                   window.matchMedia("(pointer:coarse)").matches;

  // ============================================================
  // THEMES
  // ============================================================
  const THEMES = {
    gold:  { accent: [212,175,55], light: [240,217,122] },
    green: { accent: [34,197,94],  light: [74,222,128] },
    red:   { accent: [239,68,68],  light: [248,113,113] },
    cyan:  { accent: [6,182,212],  light: [34,211,238] },
  };

  function getTheme() {
    return document.documentElement.getAttribute("data-theme") || "gold";
  }

  function applyTheme(name) {
    if (!THEMES[name]) name = "gold";
    document.documentElement.setAttribute("data-theme", name);
    try { localStorage.setItem("rwh_theme", name); } catch (e) {}
    document.querySelectorAll(".theme-dot").forEach(d => {
      d.classList.toggle("active", d.getAttribute("data-theme-val") === name);
    });
    if (window.__bg && window.__bg.setTheme) window.__bg.setTheme(name);
    if (window.__cpu && window.__cpu.setTheme) window.__cpu.setTheme(name);
    if (window.toast) window.toast("Theme: " + name, "ok");
  }
  window.applyTheme = applyTheme;

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".theme-dot").forEach(d => {
      d.classList.toggle("active", d.getAttribute("data-theme-val") === getTheme());
    });
  });

  // ============================================================
  // BG — 4 animations, state continuous across theme + resize
  // ============================================================
  const BgAnim = (function () {
    const cv = document.getElementById("bg-canvas");
    if (!cv) return { setTheme: () => {} };
    const ctx = cv.getContext("2d");
    let W = 0, H = 0, dpr = 1;
    let theme = getTheme();
    let parts = [];
    let columns = [];
    let lastDraw = 0;
    let t0 = performance.now();
    let _lastW = 0, _lastH = 0;
    let _greenLast = 0;

    function cellW() { return 16 * dpr; }
    function cellH() { return 20 * dpr; }

    function seedParticles() {
      const n = isMobile ? 32 : 80;
      parts = [];
      for (let i = 0; i < n; i++) {
        parts.push({
          x: Math.random() * W,
          y: Math.random() * H,
          vx: (Math.random() - 0.5) * 0.35 * dpr,
          vy: (Math.random() - 0.5) * 0.35 * dpr,
          r: (Math.random() * 1.4 + 0.6) * dpr,
          a: Math.random() * 0.5 + 0.3,
        });
      }
    }

    function seedColumns() {
      const cw = cellW();
      const n = Math.ceil(W / cw) + 1;
      columns = [];
      for (let i = 0; i < n; i++) {
        columns.push({
          x: i * cw,
          headY: -Math.random() * H,
          speed: (60 + Math.random() * 140) * dpr,
          chars: [],
          lastCommit: 0,
        });
      }
    }

    function rescaleColumns(sx, sy) {
      const cw = cellW();
      const n = Math.ceil(W / cw) + 1;
      const old = columns;
      columns = [];
      for (let i = 0; i < n; i++) {
        const prev = old[i];
        columns.push({
          x: i * cw,
          headY: prev ? prev.headY * sy : -Math.random() * H,
          speed: prev ? prev.speed : (60 + Math.random() * 140) * dpr,
          chars: prev ? prev.chars.map(c => ({ y: c.y * sy, ch: c.ch, born: c.born })) : [],
          lastCommit: prev ? prev.lastCommit * sy : 0,
        });
      }
    }

    function fullSeed() {
      seedParticles();
      seedColumns();
    }

    function resize() {
      dpr = Math.min(2, window.devicePixelRatio || 1);
      const nw = Math.floor(window.innerWidth * dpr);
      const nh = Math.floor(window.innerHeight * dpr);

      if (_lastW > 0 && Math.abs(nw - _lastW) < 6 && Math.abs(nh - _lastH) < 100) return;

      const ow = _lastW, oh = _lastH;
      _lastW = nw; _lastH = nh;
      W = cv.width = nw;
      H = cv.height = nh;

      if (ow === 0 || oh === 0 || !parts.length) {
        fullSeed();
        return;
      }

      const sx = W / ow, sy = H / oh;
      for (const p of parts) {
        p.x *= sx; p.y *= sy;
        if (p.x < 0) p.x = 0;
        if (p.x > W) p.x = W;
        if (p.y < 0) p.y = 0;
        if (p.y > H) p.y = H;
      }
      rescaleColumns(sx, sy);
    }

    function rgba(c, a) { return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")"; }

    function drawGold(A, L) {
      const g = ctx.createRadialGradient(W * 0.5, H * 0.45, 0, W * 0.5, H * 0.45, Math.max(W, H) * 0.75);
      g.addColorStop(0, rgba(A, 0.08));
      g.addColorStop(1, "transparent");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W, H);
      for (const p of parts) {
        p.x += p.vx; p.y += p.vy;
        if (p.x < 0) { p.x = 0; p.vx *= -1; }
        if (p.x > W) { p.x = W; p.vx *= -1; }
        if (p.y < 0) { p.y = 0; p.vy *= -1; }
        if (p.y > H) { p.y = H; p.vy *= -1; }
      }
      const maxDist = (isMobile ? 110 : 140) * dpr;
      const maxD2 = maxDist * maxDist;
      ctx.lineWidth = 1 * dpr;
      for (let i = 0; i < parts.length; i++) {
        for (let j = i + 1; j < parts.length; j++) {
          const dx = parts[i].x - parts[j].x;
          const dy = parts[i].y - parts[j].y;
          const d2 = dx * dx + dy * dy;
          if (d2 < maxD2) {
            const k = 1 - Math.sqrt(d2) / maxDist;
            ctx.strokeStyle = rgba(A, k * 0.22);
            ctx.beginPath();
            ctx.moveTo(parts[i].x, parts[i].y);
            ctx.lineTo(parts[j].x, parts[j].y);
            ctx.stroke();
          }
        }
      }
      for (const p of parts) {
        ctx.fillStyle = rgba(L, p.a * 0.7);
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    function drawGreen(A, L) {
      const now = performance.now();
      let dt = _greenLast ? Math.min(0.1, (now - _greenLast) / 1000) : 0.03;
      _greenLast = now;

      const cw = cellW();
      const ch = cellH();
      const fontSize = Math.floor(14 * dpr);
      ctx.font = fontSize + "px 'Courier New', monospace";
      ctx.textBaseline = "top";
      ctx.textAlign = "left";

      for (const col of columns) {
        // move head
        col.headY += col.speed * dt;

        // commit new char at cell boundaries
        while (col.headY - col.lastCommit >= ch) {
          col.lastCommit += ch;
          col.chars.push({
            y: col.lastCommit,
            ch: Math.random() < 0.5 ? "0" : "1",
            born: now,
          });
          if (col.chars.length > 32) col.chars.shift();
        }

        // reset when head far past bottom
        if (col.headY > H + 60 * dpr) {
          col.headY = -Math.random() * H * 0.4 - ch;
          col.lastCommit = col.headY;
          col.chars = [];
          col.speed = (60 + Math.random() * 140) * dpr;
          continue;
        }

        // draw trail (older = dimmer, green)
        for (const c of col.chars) {
          const age = (now - c.born) / 1000;
          const life = Math.max(0, 1 - age / 3.2);
          if (life <= 0) continue;
          ctx.fillStyle = rgba(A, life * life * 0.85);
          ctx.fillText(c.ch, col.x, c.y);
        }

        // drop dead
        col.chars = col.chars.filter(c => (now - c.born) / 1000 < 3.2);

        // head — bright glowing char (flickers)
        const hc = Math.random() < 0.5 ? "0" : "1";
        ctx.shadowColor = rgba(L, 1);
        ctx.shadowBlur = 12 * dpr;
        ctx.fillStyle = rgba(L, 1);
        ctx.fillText(hc, col.x, col.headY);
        ctx.shadowBlur = 0;
      }
    }

    function drawRed(A, L, t) {
      const g = ctx.createRadialGradient(W * 0.5, H * 0.5, 0, W * 0.5, H * 0.5, Math.max(W, H) * 0.7);
      g.addColorStop(0, rgba(A, 0.10));
      g.addColorStop(1, "transparent");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W, H);

      const yMid = H * 0.5;
      const beatPos = ((t * 0.35) % 1);
      ctx.strokeStyle = rgba(L, 0.75);
      ctx.lineWidth = 2 * dpr;
      ctx.shadowColor = rgba(L, 0.9);
      ctx.shadowBlur = 10 * dpr;
      ctx.beginPath();
      const step = 3 * dpr;
      for (let x = 0; x <= W; x += step) {
        const nx = x / W;
        const d1 = Math.abs(nx - beatPos);
        const beat = Math.exp(-d1 * d1 * 3200) * 70 * dpr;
        const d2 = Math.abs(nx - beatPos - 0.045);
        const small = Math.exp(-d2 * d2 * 3600) * 22 * dpr;
        const base = Math.sin(x * 0.008 + t * 1.2) * 3 * dpr
                   + Math.sin(x * 0.024 + t * 0.7) * 1.4 * dpr;
        const y = yMid + base - beat + small;
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.shadowBlur = 0;

      const headX = beatPos * W;
      const headY = yMid - 70 * dpr;
      ctx.fillStyle = rgba(L, 1);
      ctx.shadowColor = rgba(A, 1);
      ctx.shadowBlur = 16 * dpr;
      ctx.beginPath();
      ctx.arc(headX, headY, 3 * dpr, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
    }

    function drawCyan(A, L, t) {
      for (let k = 0; k < 5; k++) {
        ctx.strokeStyle = rgba(A, 0.12 + k * 0.02);
        ctx.lineWidth = (1.4 - k * 0.18) * dpr;
        ctx.beginPath();
        for (let x = 0; x <= W; x += 4 * dpr) {
          const y = H * (0.22 + k * 0.13)
                  + Math.sin(x * 0.004 + t * (0.55 + k * 0.18)) * (36 - k * 3) * dpr
                  + Math.sin(x * 0.014 - t * 0.85) * (10 - k) * dpr;
          if (x === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }
      for (const p of parts) {
        p.y -= Math.abs(p.vy) * 3.5 + 0.25 * dpr;
        p.x += Math.sin(t * 1.2 + p.y * 0.008) * 0.5;
        if (p.y < -12) { p.y = H + 12; p.x = Math.random() * W; }
        ctx.strokeStyle = rgba(L, p.a * 0.55);
        ctx.lineWidth = 1 * dpr;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r * 1.9, 0, Math.PI * 2);
        ctx.stroke();
        ctx.fillStyle = rgba(L, p.a * 0.18);
        ctx.fill();
      }
    }

    function loop(now) {
      if (now - lastDraw < 32) { requestAnimationFrame(loop); return; }
      lastDraw = now;
      if (document.hidden) { requestAnimationFrame(loop); return; }

      const t = (now - t0) / 1000;
      const th = THEMES[theme] || THEMES.gold;
      const A = th.accent, L = th.light;

      ctx.clearRect(0, 0, W, H);

      try {
        if (theme === "gold") drawGold(A, L, t);
        else if (theme === "green") drawGreen(A, L, t);
        else if (theme === "red") drawRed(A, L, t);
        else if (theme === "cyan") drawCyan(A, L, t);
        else drawGold(A, L, t);
      } catch (e) {}

      requestAnimationFrame(loop);
    }

    let _resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(_resizeTimer);
      _resizeTimer = setTimeout(resize, 180);
    });
    resize();
    requestAnimationFrame(loop);

    return {
      setTheme(n) {
        theme = n;
        if (n === "green") _greenLast = 0;   // fresh dt on entry, no giant jump
      }
    };
  })();
  window.__bg = BgAnim;

  // ============================================================
  // CPU — radar visual
  // ============================================================
  const Cpu = (function () {
    const canvas = document.getElementById("cpu-canvas");
    if (!canvas) return null;
    const ctx = canvas.getContext("2d");
    let W = 0, H = 0, dpr = 1;
    let theme = getTheme();
    let target = 0;
    let current = 0;
    let angle = 0;
    let rings = [];
    let lastDraw = 0;

    function resize() {
      dpr = Math.min(2, window.devicePixelRatio || 1);
      const r = canvas.getBoundingClientRect();
      W = canvas.width = Math.max(1, Math.floor(r.width * dpr));
      H = canvas.height = Math.max(1, Math.floor(r.height * dpr));
      rings = [0.35, 0.55, 0.75, 0.95].map((rr, i) => ({ r: rr, phase: i * 0.7, pulse: 0 }));
    }

    function draw(now) {
      if (now - lastDraw < 40) { requestAnimationFrame(draw); return; }
      lastDraw = now;
      if (document.hidden) { requestAnimationFrame(draw); return; }

      const th = THEMES[theme] || THEMES.gold;
      const A = th.accent, L = th.light;
      const rgba = (c, a) => "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")";

      ctx.clearRect(0, 0, W, H);
      current += (target - current) * 0.14;
      angle += 0.02 + (current / 100) * 0.05;

      const cx = W / 2, cy = H / 2;
      const baseR = Math.min(W, H) * 0.42;

      ctx.strokeStyle = rgba(A, 0.08);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx, 0); ctx.lineTo(cx, H);
      ctx.moveTo(0, cy); ctx.lineTo(W, cy);
      ctx.stroke();

      rings.forEach((ring, i) => {
        ring.pulse += (current / 100 - ring.pulse) * 0.08;
        const r = baseR * ring.r;
        ctx.strokeStyle = rgba(A, 0.18 + ring.pulse * 0.45);
        ctx.lineWidth = (1 + ring.pulse * 1.2) * dpr;
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.stroke();
        if (i === rings.length - 1) {
          const ticks = 32;
          for (let t = 0; t < ticks; t++) {
            const a = (t / ticks) * Math.PI * 2;
            const inner = r + 4 * dpr;
            const outer = r + (t % 4 === 0 ? 10 : 6) * dpr;
            ctx.strokeStyle = rgba(L, t % 4 === 0 ? 0.5 : 0.2);
            ctx.lineWidth = 1 * dpr;
            ctx.beginPath();
            ctx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner);
            ctx.lineTo(cx + Math.cos(a) * outer, cy + Math.sin(a) * outer);
            ctx.stroke();
          }
        }
      });

      const sweepGrad = ctx.createConicGradient
        ? ctx.createConicGradient(angle, cx, cy) : null;
      if (sweepGrad) {
        sweepGrad.addColorStop(0, rgba(L, 0.55));
        sweepGrad.addColorStop(0.12, rgba(L, 0));
        sweepGrad.addColorStop(1, rgba(L, 0));
        ctx.fillStyle = sweepGrad;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, baseR * 0.95, 0, Math.PI * 2);
        ctx.fill();
      } else {
        ctx.save();
        ctx.translate(cx, cy);
        ctx.rotate(angle);
        const lg = ctx.createLinearGradient(0, 0, baseR, 0);
        lg.addColorStop(0, rgba(L, 0.5));
        lg.addColorStop(1, "transparent");
        ctx.fillStyle = lg;
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.arc(0, 0, baseR * 0.95, -0.3, 0.3);
        ctx.fill();
        ctx.restore();
      }

      const pulseR = 4 * dpr + (current / 100) * 6 * dpr;
      ctx.fillStyle = rgba(L, 0.9);
      ctx.shadowColor = rgba(A, 1);
      ctx.shadowBlur = 14 * dpr;
      ctx.beginPath();
      ctx.arc(cx, cy, pulseR, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;

      const bars = 24;
      for (let i = 0; i < bars; i++) {
        const a = (i / bars) * Math.PI * 2 - Math.PI / 2;
        const active = (i / bars) * 100 < current;
        const rr = baseR * 0.88;
        const len = active ? 6 * dpr : 3 * dpr;
        ctx.strokeStyle = active ? rgba(L, 0.85) : rgba(A, 0.15);
        ctx.lineWidth = 2 * dpr;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr);
        ctx.lineTo(cx + Math.cos(a) * (rr - len), cy + Math.sin(a) * (rr - len));
        ctx.stroke();
      }

      requestAnimationFrame(draw);
    }

    function fmtUptime(s) {
      s = s || 0;
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const sec = Math.floor(s % 60);
      if (h > 0) return h + "h " + m + "m";
      if (m > 0) return m + "m " + sec + "s";
      return sec + "s";
    }

    async function poll() {
      try {
        const r = await fetch("/api/stats", { credentials: "same-origin" });
        if (!r.ok) return;
        const d = await r.json();
        target = d.cpu || 0;
        const el = document.getElementById("cpu-load");
        if (el) el.innerHTML = (d.cpu || 0).toFixed(0) + "<small>%</small>";
        const cores = document.getElementById("cpu-cores");
        if (cores) cores.textContent = d.cores;
        const ram = document.getElementById("cpu-ram");
        if (ram) ram.textContent = (d.ram_used_mb || 0) + "/" + (d.ram_total_mb || 0) + "MB";
        const up = document.getElementById("cpu-uptime");
        if (up) up.textContent = fmtUptime(d.uptime);
      } catch (e) {}
    }

    window.addEventListener("resize", resize);
    resize();
    requestAnimationFrame(draw);
    poll();
    setInterval(poll, 2000);

    return { setTheme(n) { theme = n; } };
  })();
  window.__cpu = Cpu;

  // ============================================================
  // SIDEBAR
  // ============================================================
  window.toggleSidebar = function () { document.body.classList.toggle("sidebar-open"); };
  window.closeSidebar  = function () { document.body.classList.remove("sidebar-open"); };
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeSidebar(); });

  // ============================================================
  // NAV
  // ============================================================
  const navLinks = document.querySelectorAll("[data-nav]");
  if (isMobile) {
    window.addEventListener("load", () => {
      navLinks.forEach(a => {
        const href = a.getAttribute("href");
        if (!href || href.startsWith("#")) return;
        const l = document.createElement("link");
        l.rel = "prefetch"; l.as = "document"; l.href = href;
        document.head.appendChild(l);
      });
    });
  }
  navLinks.forEach(a => {
    a.addEventListener("click", () => {
      if (window.innerWidth <= 820) {
        document.body.classList.add("nav-leaving");
        document.body.classList.remove("sidebar-open");
      }
      const bar = document.getElementById("page-loading");
      if (bar) bar.classList.add("active");
    });
  });

  // ============================================================
  // count-up
  // ============================================================
  document.querySelectorAll("[data-count]").forEach(el => {
    const target = parseInt(el.getAttribute("data-count") || "0", 10);
    const dur = 600, t0 = performance.now();
    (function tick(t) {
      const p = Math.min(1, (t - t0) / dur);
      el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(tick);
    })(t0);
  });

  // ============================================================
  // TOAST
  // ============================================================
  window.toast = function (msg, level) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast";
    if (level === "err") el.style.borderLeftColor = "var(--err)";
    if (level === "ok")  el.style.borderLeftColor = "var(--ok)";
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; }, 3000);
    setTimeout(() => el.remove(), 3400);
  };

  // ============================================================
  // SSE stream
  // ============================================================
  const buffers = new Map();
  let flushScheduled = false;
  function scheduleFlush() {
    if (flushScheduled) return;
    flushScheduled = true;
    requestAnimationFrame(flush);
  }
  function flush() {
    flushScheduled = false;
    for (const [key, lines] of buffers) {
      const boxes = document.querySelectorAll(key);
      if (!boxes.length || !lines.length) continue;
      boxes.forEach(box => {
        let caret = box.querySelector(".caret");
        if (!caret) {
          caret = document.createElement("span");
          caret.className = "caret";
          box.appendChild(caret);
        }
        const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
        const frag = document.createDocumentFragment();
        for (const d of lines) {
          const el = document.createElement("div");
          el.className = "logline " + (d.level || "info");
          el.innerHTML = '<span class="ts">' + (d.ts || "--") + "</span>" + escapeHtml(d.msg || "");
          frag.appendChild(el);
        }
        box.insertBefore(frag, caret);
        while (box.childNodes.length > 400) box.removeChild(box.firstChild);
        if (nearBottom) box.scrollTop = box.scrollHeight;
      });
      lines.length = 0;
    }
  }
  let streamES = null, reconnectTimer = null, backoff = 1200;
  function startStream() {
    if (streamES) return;
    streamES = new EventSource("/stream");
    streamES.onopen = () => { backoff = 1200; };
    streamES.onmessage = ev => {
      let d; try { d = JSON.parse(ev.data); } catch { return; }
      const key = d.kind === "bot"
        ? '[data-bot="' + d.id + '"]'
        : '[data-app="' + d.id + '"]';
      if (!buffers.has(key)) buffers.set(key, []);
      buffers.get(key).push(d);
      scheduleFlush();
    };
    streamES.onerror = () => {
      if (streamES) { streamES.close(); streamES = null; }
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(startStream, backoff);
      backoff = Math.min(30000, Math.round(backoff * 1.6));
    };
  }
  window.connectStream = startStream;
  if (document.querySelector("[data-bot],[data-app]")) startStream();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !streamES) startStream();
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  // ============================================================
  // LOG COPY / CLEAR
  // ============================================================
  window.copyLogs = function (id) {
    const box = document.getElementById(id);
    if (!box) return;
    let text = "";
    box.querySelectorAll(".logline").forEach(el => {
      text += el.innerText + "\n";
    });
    if (!text.trim()) text = box.innerText || "";
    if (!text.trim()) {
      if (window.toast) window.toast("Logs empty", "err");
      return;
    }
    const done = () => { if (window.toast) window.toast("Logs copied ✓", "ok"); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
    } else {
      fallbackCopy(text, done);
    }
  };

  function fallbackCopy(text, done) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      done();
    } catch (e) {
      if (window.toast) window.toast("copy failed — select manually", "err");
    }
  }

  window.clearLogs = function (id) {
    const box = document.getElementById(id);
    if (!box) return;
    box.innerHTML = '<span class="caret"></span>';
    if (window.toast) window.toast("Logs cleared", "ok");
  };


})();
