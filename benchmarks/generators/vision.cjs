// Render the image test items (charts, tables, screenshots) with known answers.
//   NODE_PATH=node_modules node benchmarks/generators/vision.cjs
// Writes benchmarks/items/vision/*.png and benchmarks/items/vision.jsonl.
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");

const out = path.join(__dirname, "..", "items", "vision");
const page = body => `<html><body style="margin:0;width:640px;height:400px;display:grid;place-items:center;background:#fff;font:20px Arial,sans-serif;color:#111">${body}</body></html>`;
const bars = (values, labels = true) => `<svg width="560" height="340">${Object.entries(values).map(([name, value], i) =>
  `<rect x="${40 + i * 130}" y="${300 - value * 6}" width="80" height="${value * 6}" fill="#4f6bd8"/>` +
  `<text x="${80 + i * 130}" y="328" text-anchor="middle">${name}</text>` +
  (labels ? `<text x="${80 + i * 130}" y="${292 - value * 6}" text-anchor="middle">${value}</text>` : "")).join("")}</svg>`;

const items = [
  ["001", bars({ A: 12, B: 27, C: 41, D: 19 }, false), "Which bar is the tallest? Reply with its letter.", "c", []],
  ["002", bars({ P: 31, Q: 27.5, R: 46, S: 12.8 }), "What value is printed above bar S?", "12.8", []],
  ["003", `<table style="border-collapse:collapse;font-size:24px">${[["Item", "Price"], ["Apples", "$3.20"], ["Bread", "$2.75"], ["Coffee", "$8.90"], ["Milk", "$1.99"]]
    .map(row => `<tr>${row.map(cell => `<td style="border:1px solid #333;padding:8px 24px">${cell}</td>`).join("")}</tr>`).join("")}</table>`,
    "What is the price of coffee in this table?", "$8.90", ["8.90", "8.9", "$8.9"]],
  ["004", `<svg width="600" height="360">${[[60, 60], [200, 120], [420, 80], [120, 260], [500, 280]].map(([x, y]) => `<circle cx="${x}" cy="${y}" r="28" fill="#e02020"/>`).join("")}
    ${[[300, 200], [80, 150], [520, 160]].map(([x, y]) => `<rect x="${x}" y="${y}" width="50" height="50" fill="#2050e0"/>`).join("")}
    ${[[330, 60], [260, 300]].map(([x, y]) => `<polygon points="${x},${y - 30} ${x - 30},${y + 25} ${x + 30},${y + 25}" fill="#20a040"/>`).join("")}</svg>`,
    "How many red circles are in this image?", "5", ["five"]],
  ["005", `<svg width="580" height="340"><polyline fill="none" stroke="#c03030" stroke-width="4" points="${[14, 22, 19, 35, 28, 24].map((v, i) => `${50 + i * 95},${300 - v * 7}`).join(" ")}"/>
    ${["Jan", "Feb", "Mar", "Apr", "May", "Jun"].map((m, i) => `<text x="${50 + i * 95}" y="330" text-anchor="middle">${m}</text>`).join("")}</svg>`,
    "In which month is the line at its highest point?", "april", ["apr"]],
  ["006", `<div style="width:460px;border:1px solid #999;border-radius:8px;box-shadow:0 4px 16px #0003"><div style="background:#d33;color:#fff;padding:10px 16px">Upload failed</div>
    <div style="padding:20px 16px">Error E4021: Connection timed out.<br><br><button>Retry</button></div></div>`,
    "What is the error code shown in this dialog?", "e4021", []],
  ["007", `<div style="display:grid;grid-template-columns:150px 150px;gap:12px">${["#7b2cbf", "#f77f00", "#2b9348", "#1d4ed8"].map(c => `<div style="height:150px;background:${c}"></div>`).join("")}</div>`,
    "What color is the top-right square?", "orange", []],
  ["008", `<div style="border:4px solid #111;padding:30px 60px;font-size:56px;letter-spacing:4px">LANTERN</div>`,
    "What word is written in the box?", "lantern", []],
  ["009", `<svg width="560" height="340" viewBox="-170 -170 560 340"><path d="M0,0 L0,-150 A150,150 0 0,1 0,150 Z" fill="#4f6bd8"/>
    <path d="M0,0 L0,150 A150,150 0 0,1 -142.66,-46.35 Z" fill="#e0a020"/><path d="M0,0 L-142.66,-46.35 A150,150 0 0,1 0,-150 Z" fill="#40a060"/>
    <text x="200" y="-60">Rent 50%</text><text x="200" y="-20">Food 30%</text><text x="200" y="20">Other 20%</text></svg>`,
    "What percentage does the chart give for Food?", "30%", ["30", "30 %"]],
  ["010", `<div style="display:flex;align-items:center;gap:18px;font-size:26px">${["Start", "Build", "Ship"].map(s => `<div style="border:3px solid #333;border-radius:10px;padding:16px 26px">${s}</div>`).join("<div>&rarr;</div>")}</div>`,
    "Which step comes right after Build in this diagram?", "ship", []],
];

(async () => {
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch();
  const tab = await browser.newPage({ viewport: { width: 640, height: 400 } });
  const lines = [];
  for (const [n, body, prompt, answer, accept] of items) {
    await tab.setContent(page(body));
    const file = `vision-${n}.png`;
    await tab.screenshot({ path: path.join(out, file) });
    lines.push(JSON.stringify({ id: `vision-fresh-${n}`, category: "vision", source: "generated", license: "original", fresh: true,
      grader: "vision_exact", image: `vision/${file}`, prompt, answer, accept }));
  }
  await browser.close();
  fs.writeFileSync(path.join(__dirname, "..", "items", "vision.jsonl"), lines.join("\n") + "\n");
  console.log(`wrote ${items.length} image items`);
})();
