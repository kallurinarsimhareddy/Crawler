/* CareerCrawler presentation mode.
 *
 * Everything here is simulated in the browser. There are no fetches, no API
 * calls and no storage: the crawl is a timed script over deterministic sample
 * data derived from the domain the presenter types in.
 */
(function () {
  "use strict";

  var ATS = {
    Workday:         { color: "#f2a33a", path: function (d, s) { return "https://" + s + ".wd5.myworkdayjobs.com/External"; } },
    Greenhouse:      { color: "#24a47f", path: function (d, s) { return "https://boards.greenhouse.io/" + s; } },
    iCIMS:           { color: "#2f7de1", path: function (d, s) { return "https://careers-" + s + ".icims.com/jobs"; } },
    SmartRecruiters: { color: "#7a5af8", path: function (d, s) { return "https://jobs.smartrecruiters.com/" + cap(s); } },
    PeopleAdmin:     { color: "#c2410c", path: function (d, s) { return "https://" + s + ".peopleadmin.com/postings"; } },
    Lever:           { color: "#0f766e", path: function (d, s) { return "https://jobs.lever.co/" + s; } },
    "Custom / Unknown": { color: "#64748b", path: function (d) { return "https://" + d + "/careers"; } }
  };
  var ATS_ROTATION = ["Workday", "Greenhouse", "iCIMS", "SmartRecruiters", "Workday", "Lever", "PeopleAdmin", "Custom / Unknown"];

  // Hand-picked profiles so the headline example always matches the script.
  var KNOWN = {
    "example.com": { name: "Example Corporation", ats: "Workday", jobs: 247, url: "https://example.com/careers" }
  };

  var SAMPLE_BATCH = "https://northwind-traders.com, https://contoso.com, https://fabrikam.io, https://adventure-works.com";

  var ROLE_TITLES = [
    "Senior Software Engineer", "Data Analyst", "Product Manager", "Cloud Infrastructure Engineer",
    "Registered Nurse", "Account Executive", "HR Business Partner", "Financial Analyst",
    "DevOps Engineer", "Customer Success Manager", "Mechanical Engineer", "Marketing Specialist",
    "Business Systems Analyst", "Security Engineer", "Operations Manager", "UX Designer"
  ];
  var LOCATIONS = ["Austin, TX", "Remote, US", "New York, NY", "Chicago, IL", "Seattle, WA", "Atlanta, GA", "Denver, CO", "Boston, MA"];
  var AVATAR_COLORS = ["#3b66f5", "#7a5af8", "#0ea5a4", "#e0613a", "#d63384", "#16813f", "#b45309", "#475569"];

  // Seed data for the dashboard before anyone presses the button.
  var SEED = [
    { domain: "example.com" },
    { domain: "globex.com", name: "Globex Corporation", ats: "Greenhouse", jobs: 132 },
    { domain: "initech.com", name: "Initech", ats: "iCIMS", jobs: 58 },
    { domain: "umbrella-health.org", name: "Umbrella Health", ats: "SmartRecruiters", jobs: 311 },
    { domain: "statecollege.edu", name: "State College", ats: "PeopleAdmin", jobs: 44 },
    { domain: "hooli.xyz", name: "Hooli", ats: "Custom / Unknown", jobs: 0, status: "Failed", url: "https://hooli.xyz", note: "No careers page found" }
  ];

  // --- helpers -------------------------------------------------------------

  function $(id) { return document.getElementById(id); }
  function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }
  function fmt(n) { return Math.round(n).toLocaleString("en-US"); }
  function hash(str) {
    var h = 2166136261;
    for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  function rng(seed) {
    var s = seed || 1;
    return function () { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; };
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function initials(name) {
    var parts = name.replace(/[^A-Za-z0-9 ]/g, " ").split(/\s+/).filter(Boolean);
    return ((parts[0] || "?")[0] + ((parts[1] || "")[0] || "")).toUpperCase();
  }

  function parseDomain(raw) {
    var s = raw.trim();
    if (!s) return null;
    if (!/^[a-z]+:\/\//i.test(s)) s = "https://" + s;
    try {
      var host = new URL(s).hostname.toLowerCase().replace(/^www\./, "");
      if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(host)) return null;
      return host;
    } catch (e) { return null; }
  }

  function profileFor(domain, overrides) {
    var known = KNOWN[domain] || {};
    var o = Object.assign({}, known, overrides || {});
    var h = hash(domain);
    var r = rng(h);
    var stem = domain.split(".")[0];
    var name = o.name || stem.split("-").map(cap).join(" ");
    var ats = o.ats || ATS_ROTATION[h % ATS_ROTATION.length];
    var jobs = o.jobs != null ? o.jobs : 24 + Math.floor(r() * 380);
    var slug = stem.replace(/-/g, "");
    var url = o.url || ATS[ats].path(domain, slug);
    var roles = [];
    for (var i = 0; i < 4 && i < jobs; i++) {
      roles.push({ title: ROLE_TITLES[Math.floor(r() * ROLE_TITLES.length)], loc: LOCATIONS[Math.floor(r() * LOCATIONS.length)] });
    }
    return {
      domain: domain, name: name, ats: ats, jobs: jobs, url: url, roles: roles,
      status: o.status || "Completed", note: o.note || "",
      duration: o.duration || (18 + r() * 40).toFixed(1) + " s",
      color: AVATAR_COLORS[h % AVATAR_COLORS.length]
    };
  }

  // --- state ---------------------------------------------------------------

  var state = { rows: [], running: 0, busy: false, selected: null };

  // --- dashboard -----------------------------------------------------------

  function atsChip(ats) {
    return '<span class="ats-chip" style="--chip:' + ATS[ats].color + '">' + esc(ats) + "</span>";
  }
  function badge(status) {
    var cls = status === "Completed" ? "badge-completed" : status === "Failed" ? "badge-failed" : status === "Running" ? "badge-running" : "badge-queued";
    var dot = status === "Running" ? '<span class="pulse"></span>' : "";
    return '<span class="badge ' + cls + '">' + dot + esc(status) + "</span>";
  }

  function rowHtml(p) {
    return (
      "<td><div class=\"co\"><span class=\"co-avatar\" style=\"background:" + p.color + "\">" + esc(initials(p.name)) + "</span>" + esc(p.name) + "</div></td>" +
      '<td><span class="url mono truncate" title="' + esc(p.url) + '">' + esc(p.url.replace(/^https?:\/\//, "")) + "</span></td>" +
      "<td>" + atsChip(p.ats) + "</td>" +
      '<td class="num tabular">' + (p.status === "Running" ? "…" : fmt(p.jobs)) + "</td>" +
      "<td>" + badge(p.status) + "</td>"
    );
  }

  function renderTable(animateFirst) {
    var body = $("recent-body");
    body.innerHTML = "";
    state.rows.forEach(function (p, i) {
      var tr = document.createElement("tr");
      tr.innerHTML = rowHtml(p);
      tr.tabIndex = 0;
      if (p === state.selected) tr.classList.add("selected");
      if (animateFirst && i === 0) tr.classList.add("row-in");
      tr.addEventListener("click", function () { select(p); });
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(p); } });
      body.appendChild(tr);
    });
    $("recent-count").textContent = state.rows.length + " crawls";
  }

  function animateNumber(el, to, opts) {
    opts = opts || {};
    var from = parseFloat(el.dataset.v || "0");
    var suffix = opts.suffix || "";
    var dur = opts.duration || 700;
    var start = performance.now();
    el.dataset.v = to;
    function step(now) {
      var t = Math.min(1, (now - start) / dur);
      var e = 1 - Math.pow(1 - t, 3);
      el.textContent = fmt(from + (to - from) * e) + suffix;
      if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
    if (opts.bump && to !== from) {
      el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump");
    }
  }

  function renderCards(bump) {
    var done = state.rows.filter(function (r) { return r.status !== "Running"; });
    var ok = done.filter(function (r) { return r.status === "Completed"; });
    var jobs = ok.reduce(function (a, r) { return a + r.jobs; }, 0);
    animateNumber($("c-companies"), done.length, { bump: bump });
    animateNumber($("c-jobs"), jobs, { bump: bump });
    animateNumber($("c-running"), state.running, { bump: bump, duration: 250 });
    animateNumber($("c-success"), done.length ? (ok.length / done.length) * 100 : 0, { suffix: "%", bump: bump });
    $("c-running-foot").textContent = state.running ? "crawl in progress" : "idle";
  }

  function select(p, flash) {
    state.selected = p;
    $("d-company").textContent = p.name;
    var a = $("d-url");
    a.textContent = p.url;
    a.dataset.url = p.url;
    $("d-ats").innerHTML = atsChip(p.ats);
    $("d-jobs").textContent = fmt(p.jobs);
    $("d-duration").textContent = p.duration;
    var st = $("d-status");
    st.className = "badge " + (p.status === "Completed" ? "badge-completed" : "badge-failed");
    st.textContent = p.status;
    $("d-roles").innerHTML = p.status === "Completed"
      ? p.roles.map(function (r) { return "<li><span>" + esc(r.title) + '</span><span class="loc">' + esc(r.loc) + "</span></li>"; }).join("")
      : '<li><span class="muted">' + esc(p.note || "No jobs collected") + "</span></li>";
    Array.prototype.forEach.call($("recent-body").children, function (tr, i) {
      tr.classList.toggle("selected", state.rows[i] === p);
    });
    if (flash) {
      var d = $("detail");
      d.classList.remove("updated"); void d.offsetWidth; d.classList.add("updated");
    }
  }

  // --- live crawl ----------------------------------------------------------

  var STAGE_MS = [1300, 1100, 2400, 1000];

  function log(html) {
    var box = $("log");
    var t = new Date().toLocaleTimeString("en-US", { hour12: false });
    var div = document.createElement("div");
    div.innerHTML = '<span class="t">' + t + "</span>" + html;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  function setStage(i, meta) {
    var items = $("stages").children;
    for (var k = 0; k < items.length; k++) {
      items[k].classList.toggle("done", k < i || (i === 4 && k === 4));
      items[k].classList.toggle("active", k === i && i < 4);
      if (k > i) items[k].querySelector(".stage-meta").textContent = "";
    }
    if (meta != null && items[i]) items[i].querySelector(".stage-meta").textContent = meta;
  }
  function setMeta(i, text) { $("stages").children[i].querySelector(".stage-meta").textContent = text; }

  function setProgress(pct) {
    pct = Math.max(0, Math.min(100, pct));
    $("progress-fill").style.width = pct + "%";
    $("ls-pct").textContent = Math.round(pct) + "%";
    document.querySelector(".progress").setAttribute("aria-valuenow", String(Math.round(pct)));
  }

  function setStat(id, text) {
    var el = $(id);
    if (el.textContent === text) return;
    el.textContent = text;
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  }

  function setRunButton(loading, label) {
    var b = $("run");
    b.classList.toggle("is-loading", loading);
    b.disabled = loading;
    b.querySelector(".btn-label").textContent = label;
    $("site").disabled = loading;
  }

  function tween(ms, onTick) {
    return new Promise(function (resolve) {
      var start = performance.now();
      function step(now) {
        var t = Math.min(1, (now - start) / ms);
        onTick(t);
        if (t < 1) requestAnimationFrame(step); else resolve();
      }
      requestAnimationFrame(step);
    });
  }

  async function crawlOne(p, index, total, totals) {
    var base = (index / total) * 100;
    var span = 100 / total;
    var bounds = [0, 0.2, 0.38, 0.85, 1];
    function prog(stage, t) { setProgress(base + span * (bounds[stage] + (bounds[stage + 1] - bounds[stage]) * t)); }

    setStat("ls-companies", (index + 1) + " / " + total);
    setStat("ls-current", p.name);
    setStat("ls-ats", "—");
    $("live-title").textContent = "Crawling " + p.name;

    var row = Object.assign({}, p, { status: "Running" });
    state.rows.unshift(row);
    renderTable(true);

    // 1. Career page
    setStage(0, "searching…");
    log('<span class="hl">' + esc(p.domain) + "</span> fetching homepage and sitemap");
    await tween(STAGE_MS[0], function (t) { prog(0, t); });
    log("career page found <span class=\"hl\">" + esc(p.url) + "</span>");
    setMeta(0, "found");

    // 2. ATS
    setStage(1, "fingerprinting…");
    await tween(STAGE_MS[1], function (t) { prog(1, t); });
    setStat("ls-ats", p.ats);
    setMeta(1, p.ats);
    log("platform detected <span class=\"hl\">" + esc(p.ats) + "</span> (confidence 0.9" + (hash(p.domain) % 9) + ")");

    // 3. Jobs
    setStage(2, "0 jobs");
    var pages = Math.max(1, Math.ceil(p.jobs / 50));
    var loggedPages = 0;
    await tween(STAGE_MS[2], function (t) {
      prog(2, t);
      var found = Math.round(p.jobs * t);
      setMeta(2, fmt(found) + " jobs");
      $("ls-jobs").textContent = fmt(totals.jobs + found);
      var pg = Math.min(pages, Math.ceil(t * pages));
      while (t > 0 && loggedPages < pg) { loggedPages++; log("page " + loggedPages + "/" + pages + " parsed"); }
    });
    totals.jobs += p.jobs;

    // 4. Normalize
    setStage(3, "deduplicating…");
    await tween(STAGE_MS[3], function (t) { prog(3, t); });
    setMeta(3, fmt(p.jobs) + " normalized");
    log('<span class="ok">✓</span> ' + fmt(p.jobs) + " jobs normalized for " + esc(p.name));

    Object.assign(row, p);
    renderTable(false);
    select(row, true);
    renderCards(true);
    if (index < total - 1) await wait(350);
  }

  async function run(domains) {
    state.busy = true;
    state.running = 1;
    renderCards(true);
    setRunButton(true, "CRAWLING…");
    $("crawl-error").hidden = true;

    var live = $("live");
    var progress = document.querySelector(".progress");
    live.hidden = false;
    live.classList.remove("is-in"); void live.offsetWidth; live.classList.add("is-in");
    progress.classList.remove("is-done");
    $("log").innerHTML = "";
    $("ls-jobs").textContent = "0";
    var badgeEl = $("live-badge");
    badgeEl.className = "badge badge-running";
    badgeEl.innerHTML = '<span class="pulse"></span>Running';
    setProgress(0);
    setStage(0);
    live.scrollIntoView({ behavior: "smooth", block: "center" });

    log("crawl started for " + domains.length + " compan" + (domains.length === 1 ? "y" : "ies"));
    var totals = { jobs: 0 };
    var profiles = domains.map(function (d) { return profileFor(d); });
    for (var i = 0; i < profiles.length; i++) {
      await crawlOne(profiles[i], i, profiles.length, totals);
    }

    setProgress(100);
    progress.classList.add("is-done");
    setStage(4, fmt(totals.jobs) + " jobs");
    $("live-title").textContent = "Crawl completed";
    setStat("ls-current", profiles.length === 1 ? profiles[0].name : profiles.length + " companies");
    badgeEl.className = "badge badge-completed";
    badgeEl.textContent = "Completed";
    log('<span class="ok">✓ crawl completed</span> · ' + fmt(totals.jobs) + " jobs from " + profiles.length + " compan" + (profiles.length === 1 ? "y" : "ies"));

    state.running = 0;
    state.busy = false;
    renderCards(true);
    setRunButton(true, "DONE ✓");
    toast("Crawl completed · " + fmt(totals.jobs) + " jobs discovered");
    await wait(1400);
    setRunButton(false, "RUN CRAWL");
  }

  var toastTimer;
  function toast(msg) {
    var t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    t.style.animation = "none"; void t.offsetWidth; t.style.animation = "";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 3200);
  }

  // --- wiring --------------------------------------------------------------

  $("crawl").addEventListener("submit", function (e) {
    e.preventDefault();
    if (state.busy) return;
    var parts = $("site").value.split(/[,;\n]+/).map(function (s) { return s.trim(); }).filter(Boolean);
    var domains = [];
    var bad = [];
    parts.forEach(function (p) {
      var d = parseDomain(p);
      if (d) { if (domains.indexOf(d) < 0) domains.push(d); } else bad.push(p);
    });
    var err = $("crawl-error");
    if (!domains.length || bad.length) {
      err.textContent = bad.length ? "That doesn't look like a website: " + bad.join(", ") : "Enter a company website, for example https://example.com";
      err.hidden = false;
      $("site").focus();
      return;
    }
    run(domains.slice(0, 8));
  });

  $("sample-batch").addEventListener("click", function () {
    if (state.busy) return;
    $("site").value = SAMPLE_BATCH;
    $("crawl-error").hidden = true;
    $("site").focus();
  });

  $("d-url").addEventListener("click", function (e) {
    e.preventDefault();
    toast("Demo mode: external links are disabled");
  });

  // Reveal on scroll, and count up the scale numbers when they come into view.
  function countUp(el) {
    var to = parseFloat(el.dataset.count);
    var suffix = el.dataset.suffix || "";
    tween(1400, function (t) {
      el.textContent = fmt(to * (1 - Math.pow(1 - t, 3))) + suffix;
    });
  }
  // Anything whose top has reached the viewport is revealed, including
  // elements that a fast scroll or an anchor jump skipped straight past.
  var pending = Array.prototype.slice.call(document.querySelectorAll(".reveal"));
  var ticking = false;
  function revealVisible() {
    ticking = false;
    var limit = window.innerHeight * 0.92;
    pending = pending.filter(function (el) {
      if (el.getBoundingClientRect().top > limit) return true;
      var siblings = Array.prototype.filter.call(el.parentNode.children, function (c) { return c.classList.contains("reveal"); });
      el.style.transitionDelay = Math.min(siblings.indexOf(el), 6) * 70 + "ms";
      el.classList.add("in");
      el.querySelectorAll("[data-count]").forEach(countUp);
      return false;
    });
    if (!pending.length) {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
    }
  }
  function onScroll() {
    if (!ticking) { ticking = true; requestAnimationFrame(revealVisible); }
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);
  revealVisible();

  // Initial dashboard.
  state.rows = SEED.map(function (s) { return profileFor(s.domain, s); });
  renderTable(false);
  select(state.rows[0]);
  renderCards(false);
})();
