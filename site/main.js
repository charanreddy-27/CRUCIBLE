/* CRUCIBLE site — theme, reveal-on-scroll, the arena, and the weakest link.
   Vanilla JS, no dependencies. Everything degrades: without JS the arena
   shows its static final round and the dials simply don't react. */
(function () {
  "use strict";

  var root = document.documentElement;
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── theme ─────────────────────────────────────────────────────────── */
  var toggle = document.getElementById("themeToggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var cur = root.getAttribute("data-theme");
      if (!cur) cur = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
      var next = cur === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("crucible-theme", next); } catch (e) {}
    });
  }

  /* ── reveal on scroll ──────────────────────────────────────────────── */
  var reveals = document.querySelectorAll(".reveal");
  if (reduceMotion || !("IntersectionObserver" in window)) {
    reveals.forEach(function (el) { el.classList.add("in"); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
      });
    }, { threshold: 0.1, rootMargin: "0px 0px -6% 0px" });
    reveals.forEach(function (el) { io.observe(el); });
  }

  /* ── the arena: a real --demo tournament, replayed ─────────────────── */
  var arena = document.getElementById("arena");
  var data = window.CRUCIBLE_DEMO;
  if (arena && data && data.rounds && data.rounds.length) {
    var el = function (id) { return document.getElementById(id); };
    var roundLabel = el("arenaRound"), dots = el("arenaDots"), phase = el("arenaPhase");
    var lanes = el("arenaLanes"), tags = el("arenaTags"), cost = el("arenaCost");
    var meter = el("arenaMeter"), verdict = el("arenaVerdict"), replay = el("arenaReplay");
    var timers = [];
    var total = data.rounds.length;

    dots.innerHTML = "";
    tags.innerHTML = "";
    data.rounds.forEach(function (r) {
      dots.appendChild(document.createElement("i"));
      var t = document.createElement("span");
      t.className = "tag";
      t.textContent = "round-" + r.n;
      tags.appendChild(t);
    });

    function at(ms, fn) { timers.push(setTimeout(fn, reduceMotion ? 0 : ms)); }
    function money(v) { return "$" + v.toFixed(2); }
    function shortAngle(a) { return a === "straight read" ? "reads the idea straight" : a.replace(/^Bias toward /, "→ "); }

    function setCost(spent) {
      cost.textContent = money(spent) + " of " + money(data.budget);
      meter.style.width = Math.min(100, (spent / data.budget) * 100).toFixed(1) + "%";
    }

    function buildLanes(r) {
      lanes.innerHTML = "";
      return r.cands.map(function (c) {
        var lane = document.createElement("div");
        lane.className = "lane";
        lane.innerHTML =
          '<span class="cid"></span><div class="work"><div class="angle"></div>' +
          '<div class="track"><i></i></div></div><span class="gate">—</span><span class="score">—</span>';
        lane.querySelector(".cid").textContent = "c" + c.id;
        lane.querySelector(".angle").textContent = shortAngle(c.angle);
        lanes.appendChild(lane);
        return { lane: lane, c: c };
      });
    }

    function playRound(i, spentBefore, done) {
      var r = data.rounds[i];
      roundLabel.textContent = "Round " + r.n + " / " + total;
      Array.prototype.forEach.call(dots.children, function (d, k) {
        d.className = k < i ? "done" : k === i ? "now" : "";
      });
      phase.textContent = "forking " + r.cands.length + " worktrees from trunk";
      var rows = buildLanes(r);

      at(500, function () {
        phase.textContent = "building in parallel";
        rows.forEach(function (row, k) {
          var bar = row.lane.querySelector(".track i");
          bar.style.transition = "width " + (reduceMotion ? 0 : 1.1 + k * 0.35) + "s cubic-bezier(.4,0,.2,1)";
          bar.style.width = "100%";
        });
      });
      at(2300, function () {
        phase.textContent = "gates: pytest + ruff, run by the controller";
        rows.forEach(function (row) {
          var g = row.lane.querySelector(".gate");
          var green = row.c.status === "green";
          g.textContent = (green ? "✓ " : "✗ ") + row.c.gates;
          g.className = "gate " + (green ? "ok" : "bad");
          if (!green) row.lane.classList.add("red");
        });
      });
      at(3100, function () {
        var survivors = rows.filter(function (row) { return row.c.score !== null; });
        phase.textContent = "critic: " + survivors.length + " survivor" + (survivors.length === 1 ? "" : "s") + ", read-only, no memory";
        rows.forEach(function (row) {
          var s = row.lane.querySelector(".score");
          if (row.c.score === null) {
            s.textContent = "—";
            s.title = "gates red — not reviewed";
          } else {
            s.innerHTML = "<b>" + row.c.score + "</b>/10";
          }
        });
      });
      at(4300, function () {
        rows.forEach(function (row) { if (row.c.id === r.winner) row.lane.classList.add("win"); });
        var w = r.cands.filter(function (c) { return c.id === r.winner; })[0];
        phase.textContent = "c" + r.winner + " wins at min " + w.score + " → fast-forwarded onto trunk";
        tags.children[i].className = "tag on";
        setCost(spentBefore + r.cost);
      });
      at(5600, function () { done(spentBefore + r.cost); });
    }

    function finish() {
      Array.prototype.forEach.call(dots.children, function (d) { d.className = "done"; });
      phase.textContent = "stop rule: pass — every dimension ≥ " + data.threshold + ", nothing left to fix";
      verdict.className = "verdict pass";
      verdict.textContent = "PASS · best round " + data.bestRound;
      var best = document.createElement("span");
      best.className = "tag on";
      best.textContent = "best";
      tags.appendChild(best);
    }

    function start() {
      timers.forEach(clearTimeout);
      timers = [];
      Array.prototype.forEach.call(tags.querySelectorAll(".tag"), function (t, k) {
        if (k >= total) t.remove(); else t.className = "tag";
      });
      verdict.className = "verdict run";
      verdict.textContent = "running";
      setCost(0);
      var i = 0;
      var next = function (spent) {
        if (i >= total) { finish(); return; }
        playRound(i++, spent, next);
      };
      next(0);
    }

    if (replay) replay.addEventListener("click", start);
    start();
  }

  /* ── the weakest link ──────────────────────────────────────────────── */
  var dialsEl = document.getElementById("dials");
  if (dialsEl) {
    var THRESHOLD = 8;
    var inputs = Array.prototype.slice.call(dialsEl.querySelectorAll("input[type=range]"));
    var big = document.getElementById("minScore");
    var tag = document.getElementById("minVerdict");
    var why = document.getElementById("minWhy");
    var mean = document.getElementById("meanScore");

    function label(input) { return input.getAttribute("data-label"); }

    function update() {
      var values = inputs.map(function (i) { return Number(i.value); });
      var low = Math.min.apply(null, values);
      var avg = values.reduce(function (a, b) { return a + b; }, 0) / values.length;
      inputs.forEach(function (input, k) {
        var dial = input.closest(".dial");
        dial.querySelector("output").textContent = values[k];
        dial.className = "dial" + (values[k] === low ? " low" : values[k] >= THRESHOLD ? " ok" : "");
      });
      big.innerHTML = low + "<small>/10</small>";
      mean.textContent = avg.toFixed(1);
      var pass = low >= THRESHOLD;
      tag.className = "tag " + (pass ? "pass" : "revise");
      tag.textContent = pass ? "PASS" : "REVISE";
      var weakest = inputs.filter(function (i, k) { return values[k] === low; }).map(label);
      why.textContent = pass
        ? "Every dimension clears " + THRESHOLD + ". With an empty must_fix list, the controller stops: pass."
        : "Weakest: " + weakest.join(" and ") + " at " + low + ". An average would call this " +
          avg.toFixed(1) + "/10 — CRUCIBLE calls it " + low + ", and goes another round.";
    }

    inputs.forEach(function (i) { i.addEventListener("input", update); });
    Array.prototype.forEach.call(document.querySelectorAll("[data-preset]"), function (btn) {
      btn.addEventListener("click", function () {
        btn.getAttribute("data-preset").split(",").forEach(function (v, k) { inputs[k].value = v; });
        update();
      });
    });
    update();
  }
})();
