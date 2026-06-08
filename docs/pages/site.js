// Eval Engine docs — shared chrome (topbar + sidebar), injected so the nav lives in one place.
// No build step, no fetch: works on GitHub Pages and from file:// alike.

const NAV = [
  { group: "Start here", items: [
    ["index.html",        "Overview"],
    ["architecture.html", "Architecture & components"],
  ]},
  { group: "How it works", items: [
    ["execution.html",   "Execution & orchestration"],
    ["storage.html",     "Storage choices"],
    ["scale.html",       "Handling the load"],
    ["resilience.html",  "Resilience & HA"],
  ]},
  { group: "Foundations", items: [
    ["inspect.html",      "Integration with Inspect"],
    ["extensibility.html","Extensibility, sandbox & ops"],
  ]},
];

// Sequential order for the prev/next footer.
const ORDER = NAV.flatMap(g => g.items);

function currentFile() {
  const p = location.pathname.split("/").pop();
  return p && p.endsWith(".html") ? p : "index.html";
}

function buildChrome() {
  const here = currentFile();

  const top = document.createElement("header");
  top.className = "topbar";
  top.innerHTML = `
    <button class="menu-btn" aria-label="Toggle navigation">☰ Menu</button>
    <a class="brand" href="index.html" style="text-decoration:none">
      <span class="dot"></span>
      <span>Eval&nbsp;Engine <small>· system documentation</small></span>
    </a>
    <span class="repo">distributed LLM-evaluation platform</span>`;

  const shell = document.createElement("div");
  shell.className = "shell";

  const side = document.createElement("nav");
  side.className = "sidebar";
  side.innerHTML = NAV.map(g => `
    <h4>${g.group}</h4>
    ${g.items.map(([href, label]) =>
      `<a href="${href}" class="${href === here ? "active" : ""}">${label}</a>`).join("")}
  `).join("");

  const main = document.createElement("main");
  main.className = "content";
  main.innerHTML = document.body.innerHTML;

  // prev / next
  const idx = ORDER.findIndex(([h]) => h === here);
  const prev = idx > 0 ? ORDER[idx - 1] : null;
  const next = idx >= 0 && idx < ORDER.length - 1 ? ORDER[idx + 1] : null;
  if (prev || next) {
    const pn = document.createElement("div");
    pn.className = "pagenav";
    pn.innerHTML =
      (prev ? `<a href="${prev[0]}"><span class="dir">← previous</span><br><span class="ttl">${prev[1]}</span></a>` : "<span></span>") +
      (next ? `<a class="next" href="${next[0]}"><span class="dir">next →</span><br><span class="ttl">${next[1]}</span></a>` : "");
    main.appendChild(pn);
  }

  document.body.innerHTML = "";
  document.body.appendChild(top);
  shell.appendChild(side);
  shell.appendChild(main);
  document.body.appendChild(shell);

  top.querySelector(".menu-btn").addEventListener("click", () => side.classList.toggle("open"));
}

document.addEventListener("DOMContentLoaded", buildChrome);
