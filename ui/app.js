/* ticket-hub SPA — no build step, fetch-based against /api contract. */
(() => {
  "use strict";

  const STATUSES = ["draft", "open", "running", "blocked", "review", "done", "cancelled"];
  const label = (s) => s ? s[0].toUpperCase() + s.slice(1) : s;
  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).error?.message || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  }

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.add("hidden"), 2600);
  }

  // ---------------------------------------------------------------- state
  const state = {
    view: "board",
    tickets: [],
    childCounts: {},
    collapsed: new Set(),   // graph: ticket ids whose children are hidden
    drawerId: null,
    query: "",
  };

  // ---------------------------------------------------------------- nav
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });

  function switchView(view) {
    state.view = view;
    document.querySelectorAll(".nav-item").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === view));
    document.querySelectorAll(".view").forEach((v) =>
      v.classList.toggle("hidden", v.id !== "view-" + view));
    const titles = { board: "Board", graph: "Graph", updates: "Updates" };
    $("#view-title").textContent = titles[view];
    refresh();
  }

  $("#search").addEventListener("input", (e) => {
    state.query = e.target.value.trim().toLowerCase();
    if (state.view === "board") renderBoard();
    if (state.view === "updates") renderUpdates();
  });

  // ---------------------------------------------------------------- board
  async function loadBoard() {
    const data = await api("/board");
    state.columns = data.columns || {};
    state.childCounts = data.child_counts || {};
  }

  function matchesQuery(t) {
    if (!state.query) return true;
    return (t.title + " " + (t.body || "") + " " + t.id + " " + (t.tags || []).join(" "))
      .toLowerCase().includes(state.query);
  }

  function renderBoard() {
    const root = $("#board-columns");
    root.textContent = "";
    for (const status of STATUSES) {
      const col = el("div", "column");
      const head = el("div", "column-head");
      head.append(el("span", `column-dot st-${status}`));
      head.append(el("span", null, status));
      const tickets = (state.columns?.[status] || []).filter(matchesQuery);
      head.append(el("span", "column-count", String(tickets.length)));
      col.append(head);
      const cards = el("div", "column-cards");
      for (const t of tickets) cards.append(ticketCard(t));
      col.append(cards);
      root.append(col);
    }
  }

  function ticketCard(t) {
    const card = el("div", "card");
    card.append(el("div", "card-title", t.title));
    const meta = el("div", "card-meta");
    meta.append(el("span", `status-pill st-${t.status}`, label(t.status)));
    const owner = t.owner || {};
    if (owner.family && owner.family !== "unknown")
      meta.append(el("span", "chip chip-family", owner.family));
    if (t.priority && t.priority !== "medium")
      meta.append(el("span", `prio prio-${t.priority}`, t.priority));
    const kids = state.childCounts[t.id] || 0;
    if (kids) meta.append(el("span", "child-count", `⌄ ${kids}`));
    card.append(meta);
    card.addEventListener("click", () => openDrawer(t.id));
    return card;
  }

  // ---------------------------------------------------------------- graph
  async function loadTickets() {
    state.tickets = await api("/tickets");
  }

  async function childrenOf(id) {
    try { return await api(`/tickets/${id}/lineage`); } catch { return null; }
  }

  async function renderGraph() {
    const svg = $("#graph-svg");
    svg.textContent = "";
    await loadTickets();
    const empty = $("#graph-empty");
    if (!state.tickets.length) { empty.classList.remove("hidden"); return; }
    empty.classList.add("hidden");

    // Build full tree of parent_of relations via each ticket's ancestry.
    const lineages = {};
    for (const t of state.tickets) lineages[t.id] = await childrenOf(t.id);
    const childMap = {};
    for (const t of state.tickets) childMap[t.id] = (lineages[t.id]?.tree?.children) || [];
    const isChild = new Set();
    for (const t of state.tickets)
      for (const c of childMap[t.id]) isChild.add(c.ticket.id);
    const roots = state.tickets.filter((t) => !isChild.has(t.id));

    const NODE_W = 220, NODE_H = 56, GAP_X = 40, GAP_Y = 20;
    const positions = [];
    function layout(node, depth) {
      const id = node.ticket.id;
      const kids = state.collapsed.has(id) ? [] : (childMap[id] || []);
      if (!kids.length) {
        const pos = { node, depth, slot: layout.next++ };
        positions.push(pos);
        return { top: pos.slot, bottom: pos.slot };
      }
      const ranges = kids.map((k) => layout(k, depth + 1));
      const top = ranges[0].top, bottom = ranges[ranges.length - 1].bottom;
      const mid = (top + bottom) / 2;
      positions.push({ node, depth, slot: mid });
      return { top, bottom };
    }
    layout.next = 0;
    const forest = roots.map((t) => ({ ticket: t, children: [] }));
    const ranges = forest.map((r) => layout(r, 0));

    const totalSlots = Math.max(layout.next, 1);
    const maxDepth = Math.max(...positions.map((p) => p.depth), 0);
    const W = (maxDepth + 1) * (NODE_W + GAP_X) + 60;
    const H = totalSlots * (NODE_H + GAP_Y) + 60;
    svg.setAttribute("width", W);
    svg.setAttribute("height", H);
    const xy = (p) => [50 + p.depth * (NODE_W + GAP_X), 40 + p.slot * (NODE_H + GAP_Y)];
    const byId = {};
    positions.forEach((p) => { byId[p.node.ticket.id] = p; });

    const NS = "http://www.w3.org/2000/svg";
    // edges first
    for (const p of positions) {
      const kids = state.collapsed.has(p.node.ticket.id) ? [] : (childMap[p.node.ticket.id] || []);
      for (const k of kids) {
        const q = byId[k.ticket.id];
        if (!q) continue;
        const [x1, y1] = xy(p), [x2, y2] = xy(q);
        const path = document.createElementNS(NS, "path");
        const sx = x1 + NODE_W, sy = y1 + NODE_H / 2, tx = x2, ty = y2 + NODE_H / 2;
        path.setAttribute("d", `M ${sx} ${sy} C ${sx + GAP_X / 2} ${sy}, ${tx - GAP_X / 2} ${ty}, ${tx} ${ty}`);
        path.setAttribute("class", "edge");
        svg.append(path);
      }
    }
    // nodes
    for (const p of positions) {
      const t = p.node.ticket;
      const [x, y] = xy(p);
      const g = document.createElementNS(NS, "g");
      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("x", x); rect.setAttribute("y", y);
      rect.setAttribute("width", NODE_W); rect.setAttribute("height", NODE_H);
      rect.setAttribute("rx", 7);
      rect.setAttribute("class", "node-box");
      const dot = document.createElementNS(NS, "circle");
      dot.setAttribute("cx", x + 14); dot.setAttribute("cy", y + 18); dot.setAttribute("r", 4);
      dot.setAttribute("fill", `var(--st-${t.status.toLowerCase()})`);
      const title = document.createElementNS(NS, "text");
      title.setAttribute("x", x + 26); title.setAttribute("y", y + 22);
      title.setAttribute("class", "node-title");
      title.textContent = t.title.length > 26 ? t.title.slice(0, 25) + "…" : t.title;
      const sub = document.createElementNS(NS, "text");
      sub.setAttribute("x", x + 14); sub.setAttribute("y", y + 41);
      sub.setAttribute("class", "node-sub");
      sub.textContent = t.id;
      g.append(rect, dot, title, sub);
      rect.addEventListener("click", () => openDrawer(t.id));
      const kids = childMap[t.id] || [];
      if (kids.length) {
        const tg = document.createElementNS(NS, "g");
        tg.setAttribute("class", "node-toggle");
        const c = document.createElementNS(NS, "circle");
        c.setAttribute("cx", x + NODE_W); c.setAttribute("cy", y + NODE_H / 2); c.setAttribute("r", 9);
        const label = document.createElementNS(NS, "text");
        label.setAttribute("x", x + NODE_W); label.setAttribute("y", y + NODE_H / 2 + 4);
        label.setAttribute("text-anchor", "middle");
        label.textContent = state.collapsed.has(t.id) ? `+${kids.length}` : "–";
        tg.append(c, label);
        tg.addEventListener("click", (e) => {
          e.stopPropagation();
          state.collapsed.has(t.id) ? state.collapsed.delete(t.id) : state.collapsed.add(t.id);
          renderGraph();
        });
        g.append(tg);
      }
      svg.append(g);
    }
  }

  // ---------------------------------------------------------------- drawer
  async function openDrawer(id) {
    state.drawerId = id;
    const drawer = $("#drawer");
    drawer.classList.remove("hidden");
    $("#drawer-body").textContent = "";
    $("#drawer-id").textContent = id;
    $("#drawer-title").textContent = "…";
    let ticket, lin, bindings, plan;
    try {
      [ticket, lin] = await Promise.all([
        api(`/tickets/${id}`),
        api(`/tickets/${id}/lineage`),
      ]);
      try { bindings = await api(`/tickets/${id}/bindings`); } catch { bindings = []; }
      try { plan = await api(`/tickets/${id}/plan`); } catch { plan = null; }
    } catch (err) {
      $("#drawer-body").append(el("p", "muted", "Failed to load: " + err.message));
      return;
    }
    $("#drawer-title").textContent = ticket.title;
    const body = $("#drawer-body");

    // lineage breadcrumb
    const sec1 = el("div", "drawer-section");
    sec1.append(el("h3", null, "Lineage"));
    const crumbs = el("div", "breadcrumb");
    const chain = [...(lin.ancestry || [])].reverse();
    chain.forEach((a) => {
      const link = el("a", null, a.id);
      link.addEventListener("click", () => openDrawer(a.id));
      crumbs.append(link, el("span", "sep", "›"));
    });
    crumbs.append(el("span", "current", ticket.id));
    sec1.append(crumbs);
    if ((lin.related || []).length) {
      const rel = el("div", "muted");
      rel.textContent = "Related: " + lin.related.map((r) => r.id).join(", ");
      sec1.append(rel);
    }
    body.append(sec1);

    // details
    const sec2 = el("div", "drawer-section");
    sec2.append(el("h3", null, "Details"));
    const kv = el("dl", "kv");
    const add = (k, v) => { kv.append(el("dt", null, k), el("dd", null, v)); };
    add("Status", ticket.status);
    add("Priority", ticket.priority);
    add("Owner", `${ticket.owner?.name || "—"}${ticket.owner?.family ? " · " + ticket.owner.family : ""}`);
    add("Tags", (ticket.tags || []).join(", ") || "—");
    add("Updated", (ticket.updated_at || "").replace("T", " ").slice(0, 16));
    sec2.append(kv);
    if ((ticket.artifacts || []).length) {
      const art = el("div", "muted");
      art.textContent = "Artifacts: " + ticket.artifacts.map((a) => a.name || a).join(", ");
      sec2.append(art);
    }
    body.append(sec2);

    // plan: progress, blocked by Ray, remaining
    if (plan) {
      const secP = el("div", "drawer-section");
      secP.append(el("h3", null, "Plan"));
      const p = plan.progress || {};
      const bar = el("div", "plan-bar");
      const fill = el("div", "plan-bar-fill");
      fill.style.width = (p.percent || 0) + "%";
      bar.append(fill);
      secP.append(bar);
      secP.append(el("div", "plan-pct",
        `${p.percent || 0}% — ${p.done || 0}/${p.total || 0} work items done`));
      if (plan.summary) {
        const sum = el("p", "plan-summary");
        sum.textContent = plan.summary;
        secP.append(sum);
      }
      if ((plan.blocked_by_ray || []).length) {
        const bl = el("div", "plan-blocked");
        bl.append(el("h4", null, `Blocked by Ray (${plan.blocked_by_ray.length})`));
        for (const b of plan.blocked_by_ray.slice(0, 10)) {
          const row = el("div", "plan-item");
          const link = el("a", null, b.title);
          link.addEventListener("click", () => openDrawer(b.id));
          row.append(el("span", "plan-bang", "!"), link,
                     el("span", "muted", " " + b.status));
          bl.append(row);
        }
        secP.append(bl);
      }
      const rem = plan.remaining_items || [];
      if (rem.length) {
        const rm = el("div");
        rm.append(el("h4", null, `Remaining (${rem.length})`));
        for (const r of rem.slice(0, 15)) {
          const row = el("div", "plan-item");
          const link = el("a", null, r.title);
          link.addEventListener("click", () => openDrawer(r.id));
          row.append(el("span", `status-pill st-${r.status}`, r.status), link,
                     r.owner ? el("span", "muted", " · " + r.owner) : el("span"));
          rm.append(row);
        }
        if (rem.length > 15) rm.append(el("div", "muted", `… ${rem.length - 15} more`));
        secP.append(rm);
      }
      if ((plan.remaining_notes || []).length) {
        const rn = el("div");
        rn.append(el("h4", null, "Remaining (noted in plan)"));
        for (const item of plan.remaining_notes.slice(0, 8))
          rn.append(el("div", "plan-item", "· " + item));
        secP.append(rn);
      }
      body.append(secP);
    }

    // bound threads
    const sec3 = el("div", "drawer-section");
    sec3.append(el("h3", null, "Bound threads"));
    if (bindings && bindings.length) {
      for (const b of bindings) {
        const row = el("div", "thread-row");
        row.append(el("span", "chip platform-chip", b.platform));
        row.append(el("span", null, b.room_id));
        if (b.thread_id) row.append(el("span", "muted", "· " + b.thread_id));
        sec3.append(row);
      }
    } else {
      sec3.append(el("div", "muted", "No threads bound."));
    }
    body.append(sec3);

    // event timeline
    const sec4 = el("div", "drawer-section");
    sec4.append(el("h3", null, "Timeline"));
    const list = el("ul", "timeline");
    const events = [...(lin.events || [])].sort((a, b) =>
      (b.ts || "").localeCompare(a.ts || ""));
    if (!events.length) list.append(el("li", "muted", "No events."));
    for (const ev of events) {
      const li = el("li", "t-event");
      li.append(el("span", `t-dot k-${ev.kind}`));
      const head = el("div", "t-kind");
      head.textContent = ev.kind.replace(/_/g, " ");
      const actor = ev.actor?.name;
      if (actor && actor !== "unknown") {
        const a = el("span", "t-actor", " · " + actor);
        head.append(a);
      }
      li.append(head);
      li.append(el("div", "t-text", eventText(ev)));
      li.append(el("div", "t-meta", `${ev.id} · ${(ev.ts || "").replace("T", " ").slice(0, 19)}`));
      list.append(li);
    }
    sec4.append(list);
    body.append(sec4);
  }

  function eventText(ev) {
    const p = ev.payload || {};
    switch (ev.kind) {
      case "status_changed": return `${p.from || "?"} → ${p.to || "?"}`;
      case "artifact": return p.name || "artifact produced";
      case "decision": return p.text || "decision recorded";
      case "approval_receipt": return "approved by " + (p.approver || ev.actor?.name || "?");
      case "thread_message": return `[${p.platform || "chat"}] ${p.text || ""}`;
      case "created": return p.title ? `Created “${p.title}”` : "Ticket created";
      case "updated": return (p.fields || []).length ? "Updated " + p.fields.join(", ") : "Updated";
      case "comment": return p.text || "";
      default: return p.text || JSON.stringify(p);
    }
  }

  $("#drawer-close").addEventListener("click", () => {
    $("#drawer").classList.add("hidden");
    state.drawerId = null;
  });

  // ---------------------------------------------------------------- updates
  async function renderUpdates() {
    await loadTickets();
    const select = $("#updates-project");
    const prev = select.value;
    // projects = root tickets (no ancestry)
    const projects = [];
    for (const t of state.tickets) {
      try {
        const lin = await api(`/tickets/${t.id}/lineage`);
        if (!(lin.ancestry || []).length) projects.push(t);
      } catch {}
    }
    select.textContent = "";
    if (!projects.length) {
      $("#updates-body").textContent = "";
      $("#updates-body").append(el("p", "muted", "No projects yet."));
      return;
    }
    for (const p of projects) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.title;
      select.append(opt);
    }
    if (prev && projects.some((p) => p.id === prev)) select.value = prev;
    await showDigest(select.value);
  }

  async function showDigest(projectId) {
    const body = $("#updates-body");
    body.textContent = "";
    let digest;
    try { digest = await api(`/tickets/${projectId}/digest?format=json`); }
    catch (err) { body.append(el("p", "muted", err.message)); return; }
    const head = el("div", "digest-head");
    head.append(el("h2", null, digest.project_title || digest.project));
    head.append(el("div", "digest-date",
      (digest.updated_at || "").replace("T", " ").slice(0, 16) + " · " + digest.project));
    body.append(head);
    const ul = el("ul", "digest-bullets");
    if (!digest.bullets.length) ul.append(el("li", "muted", "No updates yet."));
    for (const b of digest.bullets) {
      const li = el("li");
      li.append(document.createTextNode(b.text));
      li.append(el("span", "cite", b.event_ids.join(" ")));
      ul.append(li);
    }
    body.append(ul);
    $("#copy-digest-btn").onclick = async () => {
      const res = await fetch(`/tickets/${projectId}/digest?format=md`);
      await navigator.clipboard.writeText(await res.text());
      toast("Digest copied");
    };
  }

  $("#updates-project").addEventListener("change", (e) => showDigest(e.target.value));

  // ---------------------------------------------------------------- new ticket
  $("#new-ticket-btn").addEventListener("click", () => $("#modal").classList.remove("hidden"));
  $("#modal-cancel").addEventListener("click", () => $("#modal").classList.add("hidden"));
  $("#modal").addEventListener("click", (e) => {
    if (e.target === $("#modal")) $("#modal").classList.add("hidden");
  });

  $("#new-ticket-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const ownerName = f.get("owner")?.trim();
    const family = f.get("family")?.trim() || ownerName || "unknown";
    const body = {
      title: f.get("title").trim(),
      body: (f.get("body") || "").trim(),
      priority: f.get("priority"),
      owner: ownerName ? { name: ownerName, family, kind: "agent" } : null,
      parent: f.get("parent")?.trim() || null,
    };
    try {
      const t = await api("/tickets", { method: "POST", body });
      $("#modal").classList.add("hidden");
      e.target.reset();
      toast(`Created ${t.id}`);
      await refresh();
      openDrawer(t.id);
    } catch (err) {
      toast("Error: " + err.message);
    }
  });

  // ---------------------------------------------------------------- boot
  async function refresh() {
    try {
      if (state.view === "board") { await loadBoard(); renderBoard(); }
      else if (state.view === "graph") { await renderGraph(); }
      else if (state.view === "updates") { await renderUpdates(); }
    } catch (err) {
      toast("Load failed: " + err.message);
    }
  }

  refresh();
  setInterval(() => { if (!document.hidden && state.view === "board") refresh(); }, 15000);
})();
