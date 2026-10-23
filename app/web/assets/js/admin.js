import { api } from "./api.js";
import { clear, formatDate, h, ROLE_NAMES, toast } from "./dom.js";

export async function renderAdmin(main, me) {
  const statsBox = h("div", { class: "stats" });
  const weekly = h("tbody");
  const users = h("tbody");
  const audit = h("tbody");

  main.append(
    h("div", { class: "page-head" }, h("h1", {}, "Admin"), h("p", { class: "muted" }, "Usage over the last 30 days, staff accounts and the audit log.")),
    statsBox,
    h("div", { class: "card table-wrap" }, h("h2", {}, "Questions by week"),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Week starting"), h("th", {}, "Questions"), h("th", {}, "Active users"))), weekly)),
    h("div", { class: "card table-wrap" }, h("h2", {}, "Users"), newUserForm(),
      h("table", {}, h("thead", {}, h("tr", {}, ...["Name", "Email", "Role", "Last sign-in", "Status"].map((t) => h("th", {}, t)))), users)),
    h("div", { class: "card table-wrap" }, h("h2", {}, "Audit log"),
      h("table", {}, h("thead", {}, h("tr", {}, ...["When", "User", "Action", "Target"].map((t) => h("th", {}, t)))), audit)),
  );

  async function loadStats() {
    const s = await api("/api/admin/stats?days=30");
    const a = s.answers;
    const pct = (n) => (a.questions ? `${Math.round((100 * n) / a.questions)}%` : "—");
    const tile = (label, value, hint) => h("div", { class: "stat" }, h("span", {}, label), h("strong", {}, value), hint ? h("small", {}, hint) : null);
    clear(statsBox).append(
      tile("Questions", a.questions),
      tile("Active users", a.active_users),
      tile("Answered", pct(a.answered), `${a.abstained} not answerable, ${a.clarifications} clarifications`),
      tile("Errors", a.errors),
      tile("Response time (p50 / p95)", a.p50_latency_ms ? `${(a.p50_latency_ms / 1000).toFixed(1)}s / ${(a.p95_latency_ms / 1000).toFixed(1)}s` : "—"),
      tile("Model cost", `$${Number(a.cost_usd).toFixed(2)}`, "estimated"),
      tile("Feedback", `${s.feedback.helpful} 👍  ${s.feedback.not_helpful} 👎`),
    );
    clear(weekly).append(...s.weekly.map((w) => h("tr", {}, h("td", {}, formatDate(w.week)), h("td", {}, w.questions), h("td", {}, w.active_users))));
  }

  async function loadUsers() {
    const list = (await api("/api/admin/users")).users;
    clear(users).append(
      ...list.map((u) => {
        const role = h("select", { "aria-label": `Role for ${u.full_name}`, disabled: u.id === me.id },
          ...Object.entries(ROLE_NAMES).map(([v, l]) => h("option", { value: v, selected: v === u.role }, l)));
        role.onchange = () => update(u.id, { role: role.value });
        return h(
          "tr",
          {},
          h("td", {}, u.full_name),
          h("td", {}, u.email),
          h("td", {}, role),
          h("td", {}, u.last_login_at ? formatDate(u.last_login_at) : "never"),
          h("td", {}, u.id === me.id ? "you" : h("button", { class: "btn btn-ghost", onclick: () => update(u.id, { is_active: !u.is_active }) }, u.is_active ? "Disable" : "Enable")),
        );
      }),
    );
  }

  async function update(id, change) {
    try {
      await api(`/api/admin/users/${id}`, { method: "PATCH", json: change });
      toast("User updated. Their sessions were signed out.");
    } catch (err) {
      toast(err.message, "error");
    }
    loadUsers();
  }

  function newUserForm() {
    const error = h("p", { class: "form-error", role: "alert" });
    const form = h(
      "form",
      {
        class: "inline-form",
        onsubmit: async (e) => {
          e.preventDefault();
          const data = Object.fromEntries(new FormData(form));
          error.textContent = "";
          try {
            const res = await api("/api/admin/users", { method: "POST", json: data });
            form.reset();
            alert(`Account created for ${res.user.email}.\nTemporary password (shown once): ${res.temporary_password}`);
            loadUsers();
          } catch (err) {
            error.textContent = err.message;
          }
        },
      },
      h("input", { name: "full_name", placeholder: "Full name", required: true }),
      h("input", { name: "email", type: "email", placeholder: "name@jigsaw-insurance.com", required: true }),
      h("select", { name: "role" }, ...Object.entries(ROLE_NAMES).map(([v, l]) => h("option", { value: v, selected: v === "customer_service" }, l))),
      h("button", { class: "btn btn-secondary", type: "submit" }, "Add user"),
      error,
    );
    return form;
  }

  async function loadAudit() {
    const { events } = await api("/api/admin/audit?limit=50");
    clear(audit).append(
      ...events.map((e) =>
        h("tr", {}, h("td", {}, new Date(e.at).toLocaleString("en-GB")), h("td", {}, e.user || "system"), h("td", {}, e.action), h("td", {}, e.details?.title || e.target_id || "")),
      ),
    );
  }

  await Promise.all([loadStats(), loadUsers(), loadAudit()]).catch((err) => toast(err.message, "error"));
}
