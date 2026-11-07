// Run before stylesheets so a saved appearance is applied before the first paint.
(() => {
  let appearance = "dark";
  try { if (localStorage.getItem("modelmesh-appearance") === "light") appearance = "light"; } catch {}
  const root = document.documentElement;
  root.dataset.theme = appearance;
  root.classList.toggle("dark", appearance === "dark");
  root.style.colorScheme = appearance;
  document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", appearance);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", appearance === "light" ? "#fcfbff" : "#0b0a12");
})();
