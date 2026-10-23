import { api, ApiError, whenSignedOut } from "./api.js";
import { clear, h, ROLE_NAMES } from "./dom.js";
import { renderAdmin } from "./admin.js";
import { renderAsk } from "./ask.js";
import { renderDocuments } from "./documents.js";

const root = document.getElementById("app");
let me = null;

const VIEWS = {
  ask: { label: "Ask", allowed: () => me.permissions.includes("ask"), render: renderAsk },
  documents: { label: "Documents", allowed: () => true, render: renderDocuments },
  admin: { label: "Admin", allowed: () => me.permissions.includes("manage_users"), render: renderAdmin },
};

whenSignedOut(() => {
  me = null;
  showLogin("Your session has ended. Please sign in again.");
});

function showLogin(message = "") {
  const error = h("p", { class: "form-error", role: "alert" }, message);
  const email = h("input", { type: "email", name: "email", autocomplete: "username", required: true });
  const password = h("input", { type: "password", name: "password", autocomplete: "current-password", required: true });
  const button = h("button", { class: "btn btn-primary", type: "submit" }, "Sign in");
  const form = h(
    "form",
    {
      class: "login-card",
      onsubmit: async (event) => {
        event.preventDefault();
        button.disabled = true;
        error.textContent = "";
        try {
          me = await api("/api/auth/login", { method: "POST", json: { email: email.value, password: password.value } });
          start();
        } catch (err) {
          error.textContent = err.message;
        } finally {
          button.disabled = false;
        }
      },
    },
    h("div", { class: "brand brand-large" }, h("img", { src: "/assets/favicon.svg", alt: "" }), "Jigsaw Policy Assistant"),
    h("p", { class: "muted" }, "Answers from Jigsaw policy wordings, with the page they come from."),
    h("label", {}, "Work email", email),
    h("label", {}, "Password", password),
    error,
    button,
  );
  clear(root).append(h("main", { class: "login-page" }, form));
  email.focus();
}

function currentView() {
  const name = location.hash.replace("#/", "") || "ask";
  const view = VIEWS[name] && VIEWS[name].allowed() ? name : Object.keys(VIEWS).find((k) => VIEWS[k].allowed());
  return view;
}

function start() {
  const main = h("main", { class: "content" });
  const nav = h("nav", { class: "nav" });
  const header = h(
    "header",
    { class: "topbar" },
    h("a", { class: "brand", href: "#/ask" }, h("img", { src: "/assets/favicon.svg", alt: "" }), "Jigsaw Policy Assistant"),
    nav,
    h(
      "div",
      { class: "user" },
      h("span", {}, me.full_name, h("small", {}, ROLE_NAMES[me.role] || me.role)),
      h(
        "button",
        {
          class: "btn btn-ghost",
          onclick: async () => {
            await api("/api/auth/logout", { method: "POST" }).catch(() => {});
            me = null;
            showLogin();
          },
        },
        "Sign out",
      ),
    ),
  );
  clear(root).append(header, main);

  const route = () => {
    const name = currentView();
    clear(nav).append(
      ...Object.entries(VIEWS)
        .filter(([, v]) => v.allowed())
        .map(([key, v]) => h("a", { href: `#/${key}`, class: key === name ? "active" : "" }, v.label)),
    );
    clear(main);
    VIEWS[name].render(main, me);
  };
  window.onhashchange = route;
  route();
}

(async () => {
  try {
    me = await api("/api/auth/me");
    start();
  } catch (err) {
    showLogin(err instanceof ApiError && err.code === "network" ? err.message : "");
  }
})();
