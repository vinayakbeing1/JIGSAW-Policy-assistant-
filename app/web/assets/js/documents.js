import { api } from "./api.js";
import { clear, formatDate, h, ROLE_NAMES, toast } from "./dom.js";

const KIND_NAMES = { wording: "Policy wording", endorsement: "Endorsement", guide: "Internal guide" };

export async function renderDocuments(main, me) {
  const isAdmin = me.permissions.includes("manage_documents");
  const table = h("tbody");
  let timer = null;

  main.append(
    h("div", { class: "page-head" }, h("h1", {}, "Documents"),
      h("p", { class: "muted" }, isAdmin ? "Upload, monitor and remove the documents the assistant answers from." : "The documents the assistant answers from for your role.")),
    isAdmin ? await uploadForm() : null,
    h(
      "div",
      { class: "card table-wrap" },
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, ...["Document", "Product", "Version", "Region", "Effective", "Status", ""].map((t) => h("th", {}, t)))),
        table,
      ),
    ),
  );

  async function load() {
    const { documents } = await api("/api/documents");
    clear(table).append(
      ...documents.map((d) =>
        h(
          "tr",
          {},
          h("td", {}, h("strong", {}, d.title), h("small", { class: "block muted" }, `${KIND_NAMES[d.kind]}${d.reference ? ` · ${d.reference}` : ""}`)),
          h("td", {}, d.product_name),
          h("td", {}, d.version_label),
          h("td", {}, d.region),
          h("td", {}, `${formatDate(d.effective_from)}${d.effective_to ? ` – ${formatDate(d.effective_to)}` : " onwards"}`),
          h(
            "td",
            {},
            h("span", { class: `badge badge-${d.status}` }, d.status),
            d.ocr_pages ? h("small", { class: "block muted" }, `${d.ocr_pages} scanned page(s)`) : null,
            d.error_message ? h("small", { class: "block form-error" }, d.error_message) : null,
          ),
          h(
            "td",
            { class: "actions" },
            d.status === "ready" ? h("a", { href: `/api/documents/${d.id}/file`, target: "_blank", rel: "noopener" }, "PDF") : null,
            isAdmin && d.status === "failed" ? h("button", { class: "btn btn-ghost", onclick: () => act(`/api/documents/${d.id}/retry`, "POST", "Queued for ingestion again.") }, "Retry") : null,
            isAdmin ? h("button", { class: "btn btn-ghost danger", onclick: () => confirm(`Delete "${d.title}"? It will no longer be used for answers.`) && act(`/api/documents/${d.id}`, "DELETE", "Document deleted.") }, "Delete") : null,
          ),
        ),
      ),
    );
    clearTimeout(timer);
    if (documents.some((d) => d.status === "queued" || d.status === "processing")) {
      timer = setTimeout(() => main.isConnected && load(), 3000);
    }
  }

  async function act(path, method, message) {
    try {
      await api(path, { method });
      toast(message);
      load();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function uploadForm() {
    const { products } = await api("/api/products");
    const { documents } = await api("/api/documents");
    const wordings = documents.filter((d) => d.kind === "wording");
    const kind = h("select", { name: "kind" }, ...Object.entries(KIND_NAMES).map(([v, l]) => h("option", { value: v }, l)));
    const appliesTo = h("select", { name: "applies_to_id" }, h("option", { value: "" }, "—"),
      ...wordings.map((w) => h("option", { value: w.id }, `${w.title} ${w.version_label} (${w.region})`)));
    const appliesLabel = h("label", { hidden: true }, "Endorsement applies to", appliesTo);
    kind.onchange = () => (appliesLabel.hidden = kind.value !== "endorsement");
    const roles = Object.entries(ROLE_NAMES).filter(([r]) => r !== "admin")
      .map(([r, label]) => h("label", { class: "check" }, h("input", { type: "checkbox", value: r, checked: true }), label));
    const error = h("p", { class: "form-error", role: "alert" });
    const button = h("button", { class: "btn btn-primary", type: "submit" }, "Upload");
    const form = h(
      "form",
      {
        class: "card upload",
        onsubmit: async (e) => {
          e.preventDefault();
          const data = new FormData(form);
          if (!data.get("applies_to_id")) data.delete("applies_to_id");
          if (!data.get("effective_to")) data.delete("effective_to");
          data.set("allowed_roles", ["admin", ...roles.map((l) => l.firstChild).filter((c) => c.checked).map((c) => c.value)].join(","));
          button.disabled = true;
          error.textContent = "";
          try {
            await api("/api/documents", { method: "POST", form: data });
            form.reset();
            appliesLabel.hidden = true;
            toast("Uploaded. Indexing has started.");
            load();
          } catch (err) {
            error.textContent = err.message;
          } finally {
            button.disabled = false;
          }
        },
      },
      h("h2", {}, "Upload a document"),
      h(
        "div",
        { class: "grid" },
        h("label", {}, "PDF file", h("input", { type: "file", name: "file", accept: "application/pdf", required: true })),
        h("label", {}, "Product", h("select", { name: "product_code" }, ...products.map((p) => h("option", { value: p.code }, p.name)))),
        h("label", {}, "Type", kind),
        appliesLabel,
        h("label", {}, "Title", h("input", { name: "title", required: true, minlength: 3, placeholder: "Jigsaw Home Insurance Policy Wording" })),
        h("label", {}, "Version", h("input", { name: "version_label", required: true, placeholder: "2027.1" })),
        h("label", {}, "Region", h("select", { name: "region" }, h("option", { value: "UK" }, "United Kingdom"), h("option", { value: "IE" }, "Republic of Ireland"))),
        h("label", {}, "Reference", h("input", { name: "reference", placeholder: "JIG-HOME-PW-2027.1-UK" })),
        h("label", {}, "Effective from", h("input", { type: "date", name: "effective_from", required: true })),
        h("label", {}, "Effective to (optional)", h("input", { type: "date", name: "effective_to" })),
      ),
      h("fieldset", {}, h("legend", {}, "Who can use it"), ...roles),
      error,
      button,
    );
    return form;
  }

  await load();
}
