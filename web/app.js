(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const COLORS = {
    person: "#818cf8",
    module: "#fdba74",
    file: "#94a3b8",
    orphan_file: "#fb7185",
    candidate: "#86efac"
  };

  let network = null;
  let nodeData = null;
  let edgeData = null;
  let lastAnswer = "";
  let handoffPlans = 0;
  let activeFilter = "all";
  let toastTimeout = null;
  let activeGraph = localStorage.getItem("handoffgraph.graph") || null;
  let conversation = [];
  let lastQuestion = "";
  let contextVersion = 0;

  function toast(message, isError = false) {
    const el = $("toast");
    el.textContent = message;
    el.classList.toggle("error", isError);
    el.classList.add("show");
    window.clearTimeout(toastTimeout);
    toastTimeout = window.setTimeout(() => el.classList.remove("show"), 3000);
  }

  function setConnection(online, label) {
    $("api-indicator").classList.toggle("online", online);
    $("api-indicator").classList.toggle("offline", !online);
    $("api-status").textContent = label;
  }

  async function loadMeta() {
    try {
      let query = activeGraph ? `?graph_name=${encodeURIComponent(activeGraph)}` : "";
      let response = await fetch(`/api/meta${query}`, { headers: { Accept: "application/json" } });
      if (response.status === 404 && activeGraph) {
        activeGraph = null;
        localStorage.removeItem("handoffgraph.graph");
        query = "";
        response = await fetch("/api/meta", { headers: { Accept: "application/json" } });
      }
      if (!response.ok) throw new Error(`API returned ${response.status}`);
      const meta = await response.json();
      const repo = meta.repo || "Repository";
      activeGraph = meta.graph || null;
      if (activeGraph) localStorage.setItem("handoffgraph.graph", activeGraph);
      $("repo-name").textContent = repo;
      $("repo-name").title = repo;
      $("repo-subtitle").textContent = meta.graph ? `Graph · ${meta.graph}` : "Evidence-backed analysis";
      setConnection(true, "API connected");
    } catch (error) {
      setConnection(false, "API unavailable");
      $("repo-subtitle").textContent = "Start the backend to connect";
    }
  }

  function safeText(value) {
    return value == null ? "" : String(value);
  }

  function addChatMessage(role, content, isError = false) {
    const message = document.createElement("div");
    message.className = `chat-message ${role}-message${isError ? " chat-error" : ""}`;

    const label = document.createElement("strong");
    label.textContent = role === "user" ? "You" : "HandoffGraph";

    const text = document.createElement("p");
    text.textContent = safeText(content);
    message.append(label, text);
    $("chat-history").append(message);
    $("chat-history").scrollTop = $("chat-history").scrollHeight;
    return message;
  }

  function renderAnswer(answer) {
    lastAnswer = safeText(answer);
    addChatMessage("assistant", lastAnswer || "The agent returned an empty answer.");
    $("answer-actions").hidden = !lastAnswer;
  }

  function renderTrace(trace) {
    const body = $("trace-content");
    body.replaceChildren();
    const calls = Array.isArray(trace) ? trace : [];
    $("trace-count").textContent = `${calls.length} call${calls.length === 1 ? "" : "s"}`;

    if (!calls.length) {
      const empty = document.createElement("p");
      empty.className = "trace-empty";
      empty.textContent = "No graph tool calls were returned for this response.";
      body.append(empty);
      return;
    }

    calls.forEach((call, index) => {
      const details = document.createElement("details");
      details.className = "trace-call";
      const summary = document.createElement("summary");
      const number = document.createElement("span");
      number.className = "trace-number";
      number.textContent = String(index + 1).padStart(2, "0");
      const tool = document.createElement("span");
      tool.className = "trace-tool";
      tool.textContent = safeText(call && call.tool) || "Graph tool";
      summary.append(number, tool);
      details.append(summary);

      const args = document.createElement("div");
      args.className = "trace-args";
      args.textContent = `Arguments\n${JSON.stringify((call && call.args) || {}, null, 2)}`;
      details.append(args);

      const cypherItems = Array.isArray(call && call.cypher) ? call.cypher : [];
      if (cypherItems.length) {
        cypherItems.forEach((query) => {
          const pre = document.createElement("pre");
          pre.className = "trace-cypher";
          pre.textContent = safeText(query);
          details.append(pre);
        });
      } else {
        const noQuery = document.createElement("div");
        noQuery.className = "trace-args";
        noQuery.textContent = "No Cypher query was attached to this tool call.";
        details.append(noQuery);
      }
      body.append(details);
    });
  }

  function normalizeNode(node) {
    const group = safeText(node.group || node.type || "file");
    const isModule = group === "module";
    const label = safeText(node.label || node.name || node.id || "Node");
    return {
      ...node,
      id: node.id,
      label,
      title: `${label}\nType: ${group}`,
      shape: isModule ? "box" : (group === "file" || group === "orphan_file" ? "diamond" : "dot"),
      color: {
        background: COLORS[group] || "#94a3b8",
        border: COLORS[group] || "#94a3b8",
        highlight: { background: "#e0e7ff", border: "#c7d2fe" },
        hover: { background: COLORS[group] || "#94a3b8", border: "#e0e7ff" }
      },
      font: { color: "#edf2ff", size: 13, face: "DM Sans", strokeWidth: 0 },
      borderWidth: 1.5,
      margin: isModule ? 10 : 7
    };
  }

  function draw(viz) {
    const rawNodes = viz && Array.isArray(viz.nodes) ? viz.nodes : [];
    const rawEdges = viz && Array.isArray(viz.edges) ? viz.edges : [];
    const validNodes = rawNodes.filter((node) => node && node.id != null);
    const validIds = new Set(validNodes.map((node) => String(node.id)));
    const validEdges = rawEdges.filter((edge) =>
      edge && edge.from != null && edge.to != null &&
      validIds.has(String(edge.from)) && validIds.has(String(edge.to))
    );

    $("graph-empty").style.display = validNodes.length ? "none" : "flex";
    $("graph-summary").textContent = `${validNodes.length} nodes · ${validEdges.length} relationships`;
    $("risk-count").textContent = String(validNodes.filter((node) => safeText(node.group || node.type) === "module").length);
    $("people-count").textContent = String(new Set(validNodes
      .filter((node) => ["person", "candidate"].includes(safeText(node.group || node.type)))
      .map((node) => String(node.id))).size);
    $("gap-count").textContent = String(validNodes.filter((node) => safeText(node.group || node.type) === "orphan_file").length);

    if (!window.vis || !window.vis.Network || !window.vis.DataSet) {
      $("graph-empty").style.display = "flex";
      $("graph-empty").querySelector("h3").textContent = "Graph library unavailable";
      $("graph-empty").querySelector("p").textContent = "Check your internet connection or the vis-network CDN.";
      toast("Graph visualization library did not load.", true);
      return;
    }

    const nodes = validNodes.map(normalizeNode);
    const edges = validEdges.map((edge, index) => ({
      id: edge.id != null ? edge.id : `edge-${index}-${String(edge.from)}-${String(edge.to)}`,
      from: edge.from,
      to: edge.to,
      label: safeText(edge.label),
      arrows: "to",
      color: { color: "#3b4968", highlight: "#a5b4fc", hover: "#7c8db3" },
      font: { color: "#91a0bc", size: 10, face: "DM Mono", strokeWidth: 0, align: "middle" },
      smooth: { type: "dynamic", roundness: 0.35 }
    }));

    if (!network) {
      nodeData = new vis.DataSet(nodes);
      edgeData = new vis.DataSet(edges);
      network = new vis.Network($("graph"), { nodes: nodeData, edges: edgeData }, {
        autoResize: true,
        interaction: { hover: true, tooltipDelay: 180, navigationButtons: false, keyboard: { enabled: true } },
        physics: { enabled: true, stabilization: { iterations: 120 }, barnesHut: { gravitationalConstant: -4500, springLength: 135, springConstant: 0.035 } },
        nodes: { scaling: { min: 12, max: 25 }, shadow: { enabled: true, color: "#02061755", size: 8, x: 0, y: 3 } },
        edges: { width: 1.1, selectionWidth: 2, hoverWidth: 1.7 },
        layout: { improvedLayout: true }
      });

      network.on("selectNode", (event) => {
        if (!event.nodes || !event.nodes.length) return;
        const selected = nodeData.get(event.nodes[0]);
        if (!selected) return;
        $("node-details").hidden = false;
        $("node-title").textContent = selected.label || "Unnamed node";
        $("node-description").textContent =
          selected.title || selected.group || "No additional details available.";
      });
      network.on("stabilizationIterationsDone", () => network.setOptions({ physics: { enabled: false } }));
    } else {
      nodeData.clear();
      edgeData.clear();
      nodeData.add(nodes);
      edgeData.add(edges);
      network.setOptions({ physics: { enabled: true } });
      network.stabilize(80);
    }
    applyGraphFilter();
  }

  function applyGraphFilter() {
    if (!nodeData || !edgeData) return;
    const visibleNodes = nodeData.get().filter((node) => {
      if (activeFilter === "all") return true;
      const group = safeText(node.group || node.type);
      if (activeFilter === "person") return group === "person";
      if (activeFilter === "successor") return group === "candidate";
      if (activeFilter === "file") return group === "file" || group === "orphan_file";
      return group === activeFilter;
    });
    const visibleIds = new Set(visibleNodes.map((node) => String(node.id)));
    nodeData.update(nodeData.get().map((node) => ({ id: node.id, hidden: !visibleIds.has(String(node.id)) })));
    edgeData.update(edgeData.get().map((edge) => ({
      id: edge.id,
      hidden: !visibleIds.has(String(edge.from)) || !visibleIds.has(String(edge.to))
    })));
  }

  function clearGraph() {
    if (nodeData) nodeData.clear();
    if (edgeData) edgeData.clear();
    $("graph-empty").style.display = "flex";
    $("graph-empty").querySelector("h3").textContent = "Your repository map will appear here";
    $("graph-empty").querySelector("p").textContent = "Run an analysis to explore real contributor relationships.";
    $("graph-summary").textContent = "Waiting for graph data";
    $("risk-count").textContent = "—";
    $("people-count").textContent = "—";
    $("gap-count").textContent = "—";
    $("node-details").hidden = true;
    if (network) network.unselectAll();
  }

  function clearConversation(message) {
    contextVersion += 1;
    conversation.length = 0;
    lastAnswer = "";
    lastQuestion = "";
    $("chat-history").replaceChildren();
    const placeholder = document.createElement("p");
    placeholder.className = "answer-placeholder";
    placeholder.textContent = message;
    $("chat-history").append(placeholder);
    $("question").value = "";
    $("answer-actions").hidden = true;
  }

  async function loadRepository(event) {
    event.preventDefault();
    const input = $("repo-url");
    const status = $("repo-status");
    const button = $("load-repo");
    const repoUrl = input.value.trim();
    if (!repoUrl) {
      status.textContent = "Enter a public GitHub repository URL.";
      return;
    }

    button.disabled = true;
    status.textContent = "Cloning and building the repository graph…";
    try {
      const response = await fetch("/api/repositories", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ repo_url: repoUrl })
      });
      let data;
      try {
        data = await response.json();
      } catch {
        throw new Error(`The server returned an unreadable response (${response.status}).`);
      }
      if (!response.ok) {
        throw new Error(data.detail || "Could not load repository.");
      }

      activeGraph = data.graph_name;
      localStorage.setItem("handoffgraph.graph", activeGraph);
      $("repo-name").textContent = data.repo;
      $("repo-name").title = data.repo;
      $("repo-subtitle").textContent = "Graph ready";
      status.textContent = `Loaded ${data.repo} (${data.commits} commits).`;
      clearConversation("New conversation. Ask about the selected repository.");
      clearGraph();
      renderTrace([]);
      $("trace-panel").hidden = true;
      $("trace-toggle").setAttribute("aria-expanded", "false");
      $("trace-toggle").textContent = "Show evidence ↓";
      handoffPlans = 0;
      $("handoff-count").textContent = "0";
      setConnection(true, "API connected");
    } catch (error) {
      status.textContent = error.message || "Repository loading failed.";
    } finally {
      button.disabled = false;
    }
  }

  async function ask(questionOverride) {
    if ($("go").disabled) return;
    const question = safeText(questionOverride || $("question").value).trim();
    if (!question) {
      $("question").focus();
      return;
    }

    const requestContext = contextVersion;
    lastQuestion = question;
    addChatMessage("user", question);
    $("question").value = "";
    $("go").disabled = true;
    $("go").querySelector("span").textContent = "Analyzing…";
    const loading = addChatMessage("assistant", "Following graph relationships and gathering evidence…");
    loading.classList.add("chat-pending");
    $("answer-actions").hidden = true;

    try {
      const response = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          question,
          open_issues: $("open-issues").checked,
          graph_name: activeGraph,
          history: conversation.slice(-12)
        })
      });

      let data;
      try {
        data = await response.json();
      } catch {
        throw new Error(`The server returned an unreadable response (${response.status}).`);
      }
      if (!response.ok) {
        throw new Error(data.detail || data.error || `Request failed with status ${response.status}.`);
      }

      if (requestContext !== contextVersion) {
        loading.remove();
        return;
      }
      loading.remove();
      renderAnswer(data.answer);
      conversation.push(
        { role: "user", content: question },
        { role: "assistant", content: safeText(data.answer) }
      );
      renderTrace(data.trace);
      draw(data.viz || {});
      const handoffCount = Array.isArray(data.trace)
        ? data.trace.filter((call) => safeText(call && call.tool).includes("plan_handoff")).length
        : 0;
      handoffPlans += handoffCount;
      $("handoff-count").textContent = String(handoffPlans);
      $("trace-panel").hidden = true;
      $("trace-toggle").setAttribute("aria-expanded", "false");
      $("trace-toggle").textContent = "Show evidence ↓";
      setConnection(true, "API connected");
    } catch (error) {
      loading.remove();
      if (requestContext !== contextVersion) return;
      addChatMessage(
        "assistant",
        `${error.message || "An unexpected error occurred."}\n\nCheck that FastAPI and FalkorDB are running, then try again.`,
        true
      );
      setConnection(false, "Request failed");
      toast("Analysis request failed. See the message for details.", true);
    } finally {
      $("go").disabled = false;
      $("go").querySelector("span").textContent = "Analyze";
    }
  }

  function downloadMarkdown() {
    if (!lastAnswer) return;
    const blob = new Blob([`# HandoffGraph analysis\n\nQuestion: ${lastQuestion}\n\n${lastAnswer}\n`], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "handoffgraph-analysis.md";
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast("Markdown file downloaded.");
  }

  async function copyAnswer() {
    if (!lastAnswer) return;
    try {
      await navigator.clipboard.writeText(lastAnswer);
      toast("Answer copied to clipboard.");
    } catch {
      toast("Clipboard access was blocked by the browser.", true);
    }
  }

  function init() {
    $("repo-switcher").addEventListener("submit", loadRepository);
    $("new-chat").addEventListener("click", () => {
      clearConversation("New conversation. Ask about the selected repository.");
      $("question").focus();
    });

    $("ask-form").addEventListener("submit", (event) => {
      event.preventDefault();
      ask();
    });

    $("question").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        ask();
      }
    });

    document.querySelectorAll(".prompt-chip").forEach((button) => {
      button.addEventListener("click", () => {
        $("question").value = button.dataset.prompt || "";
        $("question").focus();
      });
    });

    $("refresh-btn").addEventListener("click", async () => {
      $("api-status").textContent = "Refreshing…";
      await loadMeta();
    });
    $("fit-graph").addEventListener("click", () => {
      if (network) network.fit({ animation: { duration: 350, easingFunction: "easeInOutQuad" } });
      else toast("Ask a question to load graph data.");
    });
    $("graph-filters").addEventListener("click", (event) => {
      const button = event.target.closest("[data-filter]");
      if (!button) return;
      activeFilter = button.dataset.filter;
      $("graph-filters").querySelectorAll("[data-filter]").forEach((filterButton) => {
        const selected = filterButton === button;
        filterButton.classList.toggle("active", selected);
        filterButton.setAttribute("aria-pressed", String(selected));
      });
      applyGraphFilter();
      if (network) network.fit({ animation: { duration: 300, easingFunction: "easeInOutQuad" } });
    });
    $("trace-toggle").addEventListener("click", () => {
      const expanded = $("trace-toggle").getAttribute("aria-expanded") === "true";
      $("trace-toggle").setAttribute("aria-expanded", String(!expanded));
      $("trace-panel").hidden = expanded;
      $("trace-toggle").textContent = expanded ? "Show evidence ↓" : "Hide evidence ↑";
    });
    $("close-node-details").addEventListener("click", () => {
      $("node-details").hidden = true;
      if (network) network.unselectAll();
    });
    $("copy-answer").addEventListener("click", copyAnswer);
    $("download-answer").addEventListener("click", downloadMarkdown);
    loadMeta();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
