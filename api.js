// Connects the frontend to the SiteGen AI backend. Replaces the claude.ai-only runtime.
(function () {
  const CFG = window.SITEGEN_CONFIG || {};
  const API = (CFG.API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
  const TOK = CFG.APP_TOKEN || "";

  async function api(path, opt = {}) {
    let r;
    try {
      r = await fetch(API + path, { ...opt, headers: { "Content-Type": "application/json", "X-App-Token": TOK } });
    } catch {
      throw Object.assign(new Error("Cannot reach the backend at " + API + ". Is it running, and is this site listed in ALLOWED_ORIGINS?"), { code: "network" });
    }
    let b = null;
    try { b = await r.json(); } catch {}
    if (!r.ok) {
      const d = b && b.detail;
      const msg = typeof d === "string" ? d : r.status === 422 ? "The backend rejected that data (validation error)." : "Request failed (" + r.status + ")";
      throw Object.assign(new Error(msg), { code: r.status });
    }
    return b;
  }

  const sample = { json: async (messages) => (await api("/api/ai/json", { method: "POST", body: JSON.stringify({ prompt: messages[0].content }) })).result };

  function col(name) {
    if (name === "sites") return {
      async get() {
        const b = await api("/api/sites");
        return { docs: b.map((s) => ({ id: s.id, data: () => ({ name: s.name, score: s.score, updated: s.updated * 1000, owner: "local" }) })) };
      },
      doc: (id) => ({
        async set(d) { await api("/api/sites/" + id, { method: "PUT", body: JSON.stringify({ name: d.name || "Untitled site", spec: JSON.parse(d.spec), score: d.score }) }); },
        async get() {
          try {
            const b = await api("/api/sites/" + id);
            return { exists: true, id: b.id, data: () => ({ name: b.name, spec: JSON.stringify(b.spec), score: b.score, updated: b.updated * 1000, owner: "local" }) };
          } catch (e) { if (e.code === 404) return { exists: false }; throw e; }
        },
      }),
    };
    if (name === "marketing") return {
      doc: (id) => ({
        async set(d) { await api("/api/marketing/" + id, { method: "PUT", body: JSON.stringify({ plan: JSON.parse(d.plan || "null"), posts: JSON.parse(d.posts || "[]") }) }); },
        async get() {
          const b = await api("/api/marketing/" + id);
          return b.updated === null ? { exists: false } : { exists: true, id, data: () => ({ plan: JSON.stringify(b.plan), posts: JSON.stringify(b.posts) }) };
        },
      }),
    };
    throw new Error("Unknown collection: " + name);
  }

  const dl = { async save({ filename, data }) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([data], { type: "text/plain" }));
    a.download = filename; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } };

  async function health() { try { return await (await fetch(API + "/health")).json(); } catch { return null; } }

  window.BK = { api, sample, db: { collection: col }, dl, health, API };
})();
