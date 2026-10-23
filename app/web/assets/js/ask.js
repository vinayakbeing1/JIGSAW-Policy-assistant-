import { api } from "./api.js";
import { clear, docLabel, formatDate, h, toast } from "./dom.js";

const REGION_NAMES = { UK: "United Kingdom", IE: "Republic of Ireland" };

export async function renderAsk(main) {
  const state = {
    catalog: [],
    conversations: [],
    conversationId: null,
    selection: { product_code: null, region: null, document_id: null, incident_date: null, ignore_date: false },
    busy: false,
  };

  const historyList = h("ul", { class: "history" });
  const messages = h("div", { class: "messages", "aria-live": "polite" });
  const drawer = h("aside", { class: "drawer", hidden: true });

  const product = h("select", { "aria-label": "Product" });
  const region = h("select", { "aria-label": "Region" });
  const version = h("select", { "aria-label": "Policy version" });
  const incident = h("input", { type: "date", "aria-label": "Incident date" });
  const input = h("textarea", { rows: 2, maxlength: 2000, placeholder: "Ask about cover, limits, excesses, exclusions or conditions…" });
  const send = h("button", { class: "btn btn-primary", type: "submit" }, "Ask");

  main.append(
    h(
      "div",
      { class: "ask-layout" },
      h(
        "section",
        { class: "sidebar" },
        h("button", { class: "btn btn-secondary btn-block", onclick: () => openConversation(null) }, "New conversation"),
        historyList,
      ),
      h(
        "section",
        { class: "chat" },
        h(
          "div",
          { class: "scope-bar" },
          h("label", {}, "Product", product),
          h("label", {}, "Region", region),
          h("label", {}, "Version", version),
          h("label", {}, "Incident date", incident),
        ),
        messages,
        h("form", { class: "composer", onsubmit: (e) => { e.preventDefault(); ask(input.value); } }, input, send),
      ),
      drawer,
    ),
  );
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ask(input.value);
    }
  });

  // --- Scope selectors ---------------------------------------------------------------------------
  function option(value, label, selected) {
    return h("option", { value: value ?? "", selected: (value ?? "") === (selected ?? "") }, label);
  }

  function renderSelectors() {
    const sel = state.selection;
    clear(product).append(option(null, "Any product", sel.product_code), ...state.catalog.map((p) => option(p.code, p.name, sel.product_code)));
    const current = state.catalog.find((p) => p.code === sel.product_code);
    const wordings = current ? current.wordings : [];
    const regions = [...new Set(wordings.map((w) => w.region))];
    clear(region).append(option(null, "Any region", sel.region), ...regions.map((r) => option(r, REGION_NAMES[r] || r, sel.region)));
    region.disabled = regions.length < 2;
    const versions = wordings.filter((w) => !sel.region || w.region === sel.region);
    clear(version).append(
      option(null, "Automatic (by incident date)", sel.document_id),
      ...versions.map((w) =>
        option(w.id, `${w.version_label} (${w.region}) from ${formatDate(w.effective_from)}`, sel.document_id),
      ),
    );
    version.disabled = !current;
    incident.value = sel.incident_date || "";
  }

  product.onchange = () => {
    Object.assign(state.selection, { product_code: product.value || null, region: null, document_id: null, ignore_date: false });
    renderSelectors();
  };
  region.onchange = () => {
    Object.assign(state.selection, { region: region.value || null, document_id: null });
    renderSelectors();
  };
  version.onchange = () => {
    state.selection.document_id = version.value || null;
    state.selection.ignore_date = false;
  };
  incident.onchange = () => {
    state.selection.incident_date = incident.value || null;
    state.selection.ignore_date = false;
  };

  // --- Conversations -------------------------------------------------------------------------------
  async function loadConversations() {
    state.conversations = (await api("/api/conversations")).conversations;
    clear(historyList).append(
      ...state.conversations.map((c) =>
        h(
          "li",
          { class: c.id === state.conversationId ? "active" : "" },
          h("button", { class: "history-item", onclick: () => openConversation(c.id) }, c.title),
          h("button", { class: "icon-btn", title: "Delete conversation", "aria-label": "Delete conversation", onclick: () => removeConversation(c.id) }, "×"),
        ),
      ),
    );
  }

  async function openConversation(id) {
    state.conversationId = id;
    drawer.hidden = true;
    clear(messages);
    if (!id) {
      messages.append(emptyState());
    } else {
      const conv = await api(`/api/conversations/${id}`);
      for (const m of conv.messages) messages.append(m.role === "user" ? userBubble(m.content) : answerCard(m));
      const last = [...conv.messages].reverse().find((m) => m.selection);
      if (last) {
        state.selection = { ...state.selection, ...last.selection, ignore_date: false };
        renderSelectors();
      }
    }
    await loadConversations();
    messages.scrollTop = messages.scrollHeight;
    input.focus();
  }

  async function removeConversation(id) {
    if (!confirm("Delete this conversation?")) return;
    await api(`/api/conversations/${id}`, { method: "DELETE" });
    if (state.conversationId === id) await openConversation(null);
    else await loadConversations();
  }

  // --- Asking --------------------------------------------------------------------------------------
  let lastQuestion = "";

  async function ask(text, { echo = true } = {}) {
    const question = text.trim();
    if (!question || state.busy) return;
    state.busy = true;
    send.disabled = true;
    lastQuestion = question;
    messages.querySelector(".empty")?.remove();
    if (echo) messages.append(userBubble(question));
    input.value = "";
    const pending = h("div", { class: "answer pending" }, h("span", { class: "spinner" }), "Searching the policy documents…");
    messages.append(pending);
    messages.scrollTop = messages.scrollHeight;
    try {
      if (!state.conversationId) {
        state.conversationId = (await api("/api/conversations", { method: "POST", json: {} })).id;
      }
      const { message } = await api(`/api/conversations/${state.conversationId}/messages`, {
        method: "POST",
        json: { question, selection: state.selection },
      });
      pending.replaceWith(answerCard(message));
      loadConversations();
    } catch (err) {
      pending.replaceWith(h("div", { class: "answer answer-error" }, err.message));
    } finally {
      state.busy = false;
      send.disabled = false;
      messages.scrollTop = messages.scrollHeight;
    }
  }

  function userBubble(text) {
    return h("div", { class: "question" }, text);
  }

  function emptyState() {
    return h(
      "div",
      { class: "empty" },
      h("h2", {}, "Ask about a Jigsaw policy"),
      h("p", {}, "Answers come only from the policy wording, endorsements and guides you have access to, with the page they are on."),
      h(
        "div",
        { class: "examples" },
        ...[
          "What is the escape of water excess on home insurance?",
          "Is a courtesy car provided if my car is written off?",
          "What is the maximum trip length on an annual multi-trip travel policy?",
          "A pipe burst on 10 June 2025. What was the escape of water excess?",
        ].map((q) => h("button", { class: "chip", onclick: () => ask(q) }, q)),
      ),
    );
  }

  function answerCard(m) {
    const a = m.answer || {};
    if (m.status === "clarification") {
      return h(
        "div",
        { class: "answer answer-clarify" },
        h("p", {}, a.clarification.question),
        h(
          "div",
          { class: "options" },
          ...a.clarification.options.map((o) =>
            h(
              "button",
              {
                class: "chip",
                onclick: () => {
                  state.selection = { ...state.selection, ...o.selection };
                  renderSelectors();
                  ask(lastQuestion || m.content, { echo: false });
                },
              },
              o.label,
            ),
          ),
        ),
      );
    }
    if (m.status === "error") {
      return h("div", { class: "answer answer-error" }, a.message || m.content);
    }
    const card = h("div", { class: `answer ${m.status === "abstained" ? "answer-abstain" : ""}` });
    if (m.status === "abstained") {
      card.append(h("p", { class: "summary" }, a.message));
      if (a.missing_information) card.append(h("p", { class: "muted" }, a.missing_information));
    } else {
      card.append(
        h("p", { class: "summary" }, a.summary),
        h(
          "ul",
          { class: "claims" },
          ...a.claims.map((c) =>
            h(
              "li",
              {},
              c.text,
              " ",
              ...c.source_ids.map((sid) => h("button", { class: "cite", onclick: () => openCitation(m.id, sid) }, sid)),
              c.quote ? h("blockquote", {}, `“${c.quote}”`) : null,
            ),
          ),
        ),
        h(
          "div",
          { class: "sources" },
          h("strong", {}, "Sources"),
          ...Object.entries(a.sources).map(([sid, s]) =>
            h(
              "button",
              { class: "source", onclick: () => openCitation(m.id, sid) },
              h("span", { class: "cite" }, sid),
              `${docLabel(s)} · ${s.section || "—"} · p. ${s.page_start}${s.page_end !== s.page_start ? `–${s.page_end}` : ""}`,
            ),
          ),
        ),
      );
    }
    for (const note of a.notes || []) card.append(h("p", { class: "note" }, note));
    if (a.documents?.length) {
      card.append(h("p", { class: "searched" }, "Documents used: ", a.documents.map(docLabel).join("; ")));
    }
    if (a.disclaimer) card.append(h("p", { class: "disclaimer" }, a.disclaimer));
    card.append(feedback(m));
    return card;
  }

  function feedback(m) {
    const wrap = h("div", { class: "feedback" });
    const rate = async (rating) => {
      await api(`/api/messages/${m.id}/feedback`, { method: "PUT", json: { rating } });
      m.rating = rating;
      wrap.replaceWith(feedback(m));
      toast("Thanks for the feedback.");
    };
    wrap.append(
      h("span", { class: "muted" }, "Was this helpful?"),
      h("button", { class: `icon-btn ${m.rating === 1 ? "on" : ""}`, "aria-label": "Helpful", onclick: () => rate(1) }, "👍"),
      h("button", { class: `icon-btn ${m.rating === -1 ? "on" : ""}`, "aria-label": "Not helpful", onclick: () => rate(-1) }, "👎"),
    );
    return wrap;
  }

  async function openCitation(messageId, sourceId) {
    drawer.hidden = false;
    clear(drawer).append(h("p", { class: "muted" }, "Loading…"));
    try {
      const c = await api(`/api/messages/${messageId}/sources/${sourceId}`);
      const text = c.page_text;
      const body = c.highlight
        ? [text.slice(0, c.highlight.start), h("mark", {}, text.slice(c.highlight.start, c.highlight.end)), text.slice(c.highlight.end)]
        : [text];
      clear(drawer).append(
        h("div", { class: "drawer-head" }, h("strong", {}, docLabel(c.document)),
          h("button", { class: "icon-btn", "aria-label": "Close", onclick: () => (drawer.hidden = true) }, "×")),
        h("p", { class: "muted" }, `${c.section || ""} · page ${c.page_number} of ${c.page_count}${c.ocr ? " · scanned page (OCR)" : ""}`),
        h("pre", { class: "page-text" }, ...body),
        h("a", { class: "btn btn-secondary", href: `/api/documents/${c.document.id}/file#page=${c.page_number}`, target: "_blank", rel: "noopener" }, "Open PDF at this page"),
      );
      drawer.querySelector("mark")?.scrollIntoView({ block: "center" });
    } catch (err) {
      clear(drawer).append(h("p", { class: "form-error" }, err.message), h("button", { class: "btn btn-ghost", onclick: () => (drawer.hidden = true) }, "Close"));
    }
  }

  try {
    state.catalog = (await api("/api/catalog")).products;
  } catch (err) {
    toast(err.message, "error");
  }
  renderSelectors();
  await openConversation(null);
}
