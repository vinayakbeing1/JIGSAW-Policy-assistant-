// Tiny DOM helpers. Text is always set with textContent, never innerHTML.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "class") el.className = value;
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

export function formatDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

export function docLabel(d) {
  const version = d.title.includes(d.version_label) ? "" : ` ${d.version_label}`;
  return `${d.title}${version} (${d.region})`;
}

export const ROLE_NAMES = {
  admin: "Administrator",
  underwriter: "Underwriter",
  claims_handler: "Claims handler",
  customer_service: "Customer service",
};

export function toast(message, kind = "info") {
  const el = h("div", { class: `toast toast-${kind}`, role: "status" }, message);
  document.body.append(el);
  setTimeout(() => el.remove(), 4000);
}
