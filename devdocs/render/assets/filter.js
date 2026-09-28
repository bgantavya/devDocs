/* devdocs copies this page filter into every HTML site it writes. Every
   site page loads it, deferred, after pages.js, which lists each page as
   [title, path from the site root, summary]. It adds a search box to the
   masthead that lists the pages whose title and summary hold every word
   typed. Without JavaScript, or without pages.js, nothing is added and the
   site works as it is. Text goes in as text only. */
(function () {
  "use strict";

  const LIMIT = 30;
  const pages = window.devdocsPages;
  const script = document.currentScript;
  const masthead = document.querySelector(".masthead");
  if (!Array.isArray(pages) || !script || !masthead) {
    return;
  }
  const root = new URL("../", script.src);
  const texts = pages.map((page) => (page[0] + " " + page[2]).toLowerCase());

  const box = document.createElement("div");
  box.className = "filter";
  box.setAttribute("role", "search");
  const input = document.createElement("input");
  input.type = "search";
  input.placeholder = "Filter pages";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("aria-label", "Filter pages by title or summary");
  input.setAttribute("aria-controls", "devdocs-filter-results");
  const list = document.createElement("ul");
  list.className = "filter-results";
  list.id = "devdocs-filter-results";
  list.hidden = true;
  const status = document.createElement("p");
  status.className = "visually-hidden";
  status.setAttribute("role", "status");
  box.append(input, list, status);
  masthead.append(box);

  function entry(page) {
    const link = document.createElement("a");
    link.href = new URL(page[1], root).href;
    const title = document.createElement("span");
    title.className = "filter-title";
    title.textContent = page[0];
    link.append(title);
    if (page[2]) {
      const summary = document.createElement("span");
      summary.className = "filter-summary";
      summary.textContent = page[2];
      link.append(summary);
    }
    const item = document.createElement("li");
    item.append(link);
    return item;
  }

  function show() {
    const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    list.textContent = "";
    if (!words.length) {
      list.hidden = true;
      status.textContent = "";
      return;
    }
    const found = pages.filter((page, index) =>
      words.every((word) => texts[index].includes(word)));
    for (const page of found.slice(0, LIMIT)) {
      list.append(entry(page));
    }
    if (!found.length) {
      const empty = document.createElement("li");
      empty.className = "filter-empty";
      empty.textContent = "No matching pages";
      list.append(empty);
    }
    list.hidden = false;
    status.textContent = found.length === 1 ? "1 page" : found.length + " pages";
  }

  input.addEventListener("input", show);
  input.addEventListener("focus", show);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      input.value = "";
      show();
    } else if (event.key === "Enter") {
      const first = list.querySelector("a");
      if (first) {
        window.location.href = first.href;
      }
    }
  });
  document.addEventListener("click", (event) => {
    if (!box.contains(event.target)) {
      list.hidden = true;
    }
  });
})();
