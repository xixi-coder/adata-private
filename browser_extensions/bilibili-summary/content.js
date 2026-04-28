(() => {
  const ROOT_ID = "bili-summary-root";
  const SUBTITLE_LANGUAGE_PRIORITY = ["zh-CN", "zh-Hans", "zh", "ai-zh"];

  let currentTranscript = "";
  let currentMeta = null;
  let currentSubtitleInfo = null;
  const capturedSubtitleItems = [];

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    if (event.data?.source !== "bili-subtitle-txt") return;
    if (event.data.type === "captured" && event.data.item) {
      capturedSubtitleItems.push(event.data.item);
      setStatus("已捕获播放器字幕资源，可读取", "ok");
    }
    if (event.data.type === "dump-response" && Array.isArray(event.data.items)) {
      for (const item of event.data.items) {
        if (!capturedSubtitleItems.some((old) => old.text === item.text || old.url === item.url)) {
          capturedSubtitleItems.push(item);
        }
      }
    }
  });

  function h(tag, attrs = {}, children = []) {
    const el = document.createElement(tag);
    Object.entries(attrs).forEach(([key, value]) => {
      if (value === undefined || value === null) return;
      if (key === "className") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else {
        el.setAttribute(key, value);
      }
    });
    children.forEach((child) => el.append(child));
    return el;
  }

  function setStatus(text, kind = "idle") {
    const status = document.querySelector(`#${ROOT_ID} [data-role="status"]`);
    if (!status) return;
    status.textContent = text;
    status.dataset.kind = kind;
  }

  function setOutput(text) {
    const output = document.querySelector(`#${ROOT_ID} [data-role="output"]`);
    if (output) output.value = text;
  }

  function extractBalancedJson(source, marker) {
    const markerIndex = source.indexOf(marker);
    if (markerIndex < 0) return null;

    let start = markerIndex + marker.length;
    while (/\s/.test(source[start])) start += 1;
    if (source[start] !== "{") return null;

    let depth = 0;
    let inString = false;
    let quote = "";
    let escaped = false;
    for (let i = start; i < source.length; i += 1) {
      const ch = source[i];
      if (inString) {
        if (escaped) {
          escaped = false;
        } else if (ch === "\\") {
          escaped = true;
        } else if (ch === quote) {
          inString = false;
        }
        continue;
      }

      if (ch === '"' || ch === "'") {
        inString = true;
        quote = ch;
      } else if (ch === "{") {
        depth += 1;
      } else if (ch === "}") {
        depth -= 1;
        if (depth === 0) {
          return source.slice(start, i + 1);
        }
      }
    }
    return null;
  }

  function getInitialState() {
    for (const script of document.scripts) {
      const text = script.textContent || "";
      const raw = extractBalancedJson(text, "window.__INITIAL_STATE__=");
      if (!raw) continue;
      try {
        return JSON.parse(raw);
      } catch {
        return null;
      }
    }
    return null;
  }

  function readVideoIdentityFromUrl() {
    const url = new URL(location.href);
    const pathMatch = url.pathname.match(/\/video\/(BV[0-9A-Za-z]+|av\d+)/);
    return {
      bvid: pathMatch && pathMatch[1].startsWith("BV") ? pathMatch[1] : "",
      aid: pathMatch && pathMatch[1].startsWith("av") ? pathMatch[1].slice(2) : "",
      page: Number(url.searchParams.get("p") || 1),
    };
  }

  async function fetchViewMeta(identity) {
    const query = identity.bvid
      ? `bvid=${encodeURIComponent(identity.bvid)}`
      : `aid=${encodeURIComponent(identity.aid)}`;
    if (!query.includes("=")) return {};

    const resp = await fetch(`https://api.bilibili.com/x/web-interface/view?${query}`, {
      credentials: "include",
    });
    if (!resp.ok) return {};

    const json = await resp.json();
    if (json?.code !== 0 || !json?.data) return {};
    return json.data;
  }

  async function resolveVideoMeta() {
    const state = getInitialState() || {};
    const videoData = state.videoData || state.videoInfo || state.upData?.videoData || {};
    const identity = readVideoIdentityFromUrl();
    const viewData = await fetchViewMeta({
      bvid: videoData.bvid || state.bvid || identity.bvid,
      aid: videoData.aid || state.aid || identity.aid,
    });
    const pages = videoData.pages || state.pages || viewData.pages || [];
    const currentPage = Number(new URL(location.href).searchParams.get("p") || state.p || identity.page || 1);
    const fallbackPage = viewData.cid ? { cid: viewData.cid, page: currentPage } : {};
    const page =
      pages.find((item) => Number(item.page) === currentPage) ||
      pages[currentPage - 1] ||
      pages[0] ||
      fallbackPage;

    const titleNode =
      document.querySelector("h1.video-title") ||
      document.querySelector("[data-title]") ||
      document.querySelector("h1");

    const bvid =
      videoData.bvid ||
      state.bvid ||
      viewData.bvid ||
      identity.bvid;
    const aid =
      videoData.aid ||
      state.aid ||
      viewData.aid ||
      identity.aid;
    const cid =
      page.cid ||
      videoData.cid ||
      state.cid ||
      viewData.cid ||
      new URL(location.href).searchParams.get("cid") ||
      "";
    const title =
      titleNode?.getAttribute("title") ||
      titleNode?.textContent?.trim() ||
      videoData.title ||
      viewData.title ||
      document.title.replace("_哔哩哔哩_bilibili", "").trim();
    const description =
      document.querySelector('meta[name="description"]')?.content ||
      videoData.desc ||
      viewData.desc ||
      "";

    if (!bvid && !aid) throw new Error("没有识别到视频 BV/AV 号");
    if (!cid) throw new Error("没有识别到当前分 P 的 cid");

    return {
      title,
      description,
      bvid,
      aid,
      cid: String(cid),
      page: currentPage,
      url: location.href,
    };
  }

  function normalizeSubtitleUrl(url) {
    if (!url) return "";
    if (url.startsWith("//")) return `https:${url}`;
    if (url.startsWith("http://")) return url.replace("http://", "https://");
    return url;
  }

  function chooseSubtitle(subtitles) {
    if (!Array.isArray(subtitles) || subtitles.length === 0) return null;
    for (const lang of SUBTITLE_LANGUAGE_PRIORITY) {
      const found = subtitles.find((item) => item.lan === lang || item.lan_doc === lang);
      if (found) return found;
    }
    return subtitles[0];
  }

  function formatTime(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const h2 = String(Math.floor(total / 3600)).padStart(2, "0");
    const m2 = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
    const s2 = String(total % 60).padStart(2, "0");
    return `${h2}:${m2}:${s2}`;
  }

  function transcriptFromSubtitleJson(json) {
    const body = Array.isArray(json?.body) ? json.body : [];
    const lines = [];
    let previous = "";
    for (const item of body) {
      const content = String(item.content || "").replace(/\s+/g, " ").trim();
      if (!content || content === previous) continue;
      previous = content;
      lines.push(`[${formatTime(item.from)}] ${content}`);
    }
    return lines.join("\n");
  }

  async function fetchSubtitleJson(url) {
    const resp = await fetch(url, { credentials: "include" });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    const text = await resp.text();
    const trimmed = text.trim();
    if (trimmed.startsWith("<")) {
      throw new Error("字幕地址返回了 HTML，不是 JSON");
    }
    return JSON.parse(trimmed);
  }

  function buildVisibleSubtitleResult() {
    const visibleSubtitle = collectVisibleSubtitleText();
    if (!visibleSubtitle) return null;
    return {
      source: "当前屏幕字幕",
      detail: "DOM visible caption text",
      language: "当前屏幕字幕",
      transcript: `[${formatTime(document.querySelector("video")?.currentTime || 0)}] ${visibleSubtitle}`,
    };
  }

  async function fetchFallbackSubtitleResult() {
    await requestCapturedSubtitleDump();
    const capturedSubtitle = transcriptFromCapturedItems();
    if (capturedSubtitle) return capturedSubtitle;
    const loadedSubtitle = await fetchLoadedSubtitleTranscript();
    if (loadedSubtitle) return loadedSubtitle;
    return buildVisibleSubtitleResult();
  }

  function normalizeCandidateUrl(rawUrl) {
    let url = String(rawUrl || "")
      .replace(/\\u002F/g, "/")
      .replace(/\\\//g, "/")
      .replace(/&amp;/g, "&")
      .trim();
    if (!url) return "";
    if (url.startsWith("//")) url = `https:${url}`;
    if (url.startsWith("http://")) url = url.replace("http://", "https://");
    return url;
  }

  function collectLoadedSubtitleUrls() {
    const urls = new Set();
    const add = (value) => {
      const url = normalizeCandidateUrl(value);
      if (!url) return;
      if (/\/bfs\/(?:ai_subtitle|subtitle)\//.test(url) || /subtitle/i.test(url)) {
        urls.add(url);
      }
    };

    for (const entry of performance.getEntriesByType("resource")) {
      add(entry.name);
    }

    const sourceText = [
      ...Array.from(document.scripts, (script) => script.textContent || ""),
      document.documentElement.innerHTML,
    ].join("\n");
    const urlPattern = /(?:https?:)?\/\/[^"'<>\\\s]+(?:ai_subtitle|subtitle)[^"'<>\\\s]*/gi;
    for (const match of sourceText.matchAll(urlPattern)) {
      add(match[0]);
    }

    return Array.from(urls);
  }

  async function fetchLoadedSubtitleTranscript() {
    const candidates = collectLoadedSubtitleUrls();
    for (const url of candidates) {
      try {
        const json = await fetchSubtitleJson(url);
        const transcript = transcriptFromSubtitleJson(json);
        if (transcript) {
          return {
            source: "已加载资源扫描",
            detail: url,
            language: "已加载字幕",
            transcript,
          };
        }
      } catch {
        // Keep trying other candidate subtitle resources.
      }
    }
    return null;
  }

  function transcriptFromCapturedItems() {
    for (let i = capturedSubtitleItems.length - 1; i >= 0; i -= 1) {
      const item = capturedSubtitleItems[i];
      try {
        const json = JSON.parse(item.text);
        const transcript = transcriptFromSubtitleJson(json);
        if (transcript) {
          return {
            source: "播放器请求捕获",
            detail: item.url || "runtime JSON response",
            language: "播放器捕获字幕",
            transcript,
          };
        }
      } catch {
        // Try older captured items.
      }
    }
    return null;
  }

  function requestCapturedSubtitleDump() {
    window.postMessage({ source: "bili-subtitle-txt", type: "dump-request" }, "*");
    return new Promise((resolve) => setTimeout(resolve, 150));
  }

  function collectVisibleSubtitleText() {
    const player =
      document.querySelector("#bilibili-player") ||
      document.querySelector(".bpx-player-container") ||
      document.querySelector(".bpx-player-video-area") ||
      document.querySelector("video")?.parentElement;
    if (!player) return "";

    const playerRect = player.getBoundingClientRect();
    const texts = [];
    const seen = new Set();
    for (const el of player.querySelectorAll("*")) {
      const text = (el.textContent || "").replace(/\s+/g, " ").trim();
      if (text.length < 2 || text.length > 80 || seen.has(text)) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 20 || rect.height < 10) continue;
      const style = getComputedStyle(el);
      if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) continue;
      const fontSize = Number.parseFloat(style.fontSize || "0");
      const isLowerPlayerText = rect.top > playerRect.top + playerRect.height * 0.45;
      const looksLikeCaption =
        isLowerPlayerText &&
        fontSize >= 16 &&
        !/^(自动|倍速|字幕|关闭|字幕设置|中文|发送|\d{1,2}:\d{2})$/.test(text);
      if (!looksLikeCaption) continue;
      seen.add(text);
      texts.push(text);
    }
    return texts.join("\n");
  }

  async function fetchSubtitleTranscript(meta) {
    const query = meta.bvid
      ? `bvid=${encodeURIComponent(meta.bvid)}&cid=${encodeURIComponent(meta.cid)}`
      : `aid=${encodeURIComponent(meta.aid)}&cid=${encodeURIComponent(meta.cid)}`;
    const playerUrl = `https://api.bilibili.com/x/player/v2?${query}`;
    const playerResp = await fetch(playerUrl, { credentials: "include" });
    if (!playerResp.ok) throw new Error(`字幕列表请求失败：HTTP ${playerResp.status}`);

    const playerJson = await playerResp.json();
    const subtitles = playerJson?.data?.subtitle?.subtitles || [];
    const selected = chooseSubtitle(subtitles);
    if (!selected) {
      const fallbackSubtitle = await fetchFallbackSubtitleResult();
      if (fallbackSubtitle) return fallbackSubtitle;
      throw new Error("没有捕获到完整字幕；请刷新页面，打开 AI 字幕，等字幕显示后再点读取");
    }

    const subtitleUrl = normalizeSubtitleUrl(selected.subtitle_url || selected.subtitleUrl);
    let subtitleJson = null;
    let directFetchError = "";
    try {
      subtitleJson = await fetchSubtitleJson(subtitleUrl);
    } catch (err) {
      directFetchError = err?.message || String(err);
    }
    const transcript = subtitleJson ? transcriptFromSubtitleJson(subtitleJson) : "";
    if (!transcript) {
      const fallbackSubtitle = await fetchFallbackSubtitleResult();
      if (fallbackSubtitle) {
        if (directFetchError) {
          fallbackSubtitle.detail = `${fallbackSubtitle.detail || ""}；官方字幕地址读取失败：${directFetchError}`;
        }
        return fallbackSubtitle;
      }
      throw new Error(directFetchError ? `字幕地址读取失败：${directFetchError}` : "字幕文件为空");
    }
    return {
      source: "官方字幕接口",
      detail: subtitleUrl,
      language: selected.lan_doc || selected.lan || "",
      transcript,
    };
  }

  async function loadTranscript() {
    setStatus("正在读取字幕...", "loading");
    setOutput("");
    currentMeta = await resolveVideoMeta();
    const subtitle = await fetchSubtitleTranscript(currentMeta);
    currentSubtitleInfo = subtitle;
    currentTranscript = subtitle.transcript;
    setOutput(buildTextDocument(currentMeta, currentTranscript, currentSubtitleInfo));
    setStatus(`已读取字幕：${subtitle.source || "未知来源"}${subtitle.language ? ` / ${subtitle.language}` : ""}`, "ok");
  }

  function sanitizeFilename(value) {
    return String(value || "")
      .replace(/[\\/:*?"<>|]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 80);
  }

  function formatFilenameTimestamp(date = new Date()) {
    const pad = (value) => String(value).padStart(2, "0");
    return [
      date.getFullYear(),
      pad(date.getMonth() + 1),
      pad(date.getDate()),
      "-",
      pad(date.getHours()),
      pad(date.getMinutes()),
      pad(date.getSeconds()),
    ].join("");
  }

  function buildDownloadFilename(meta) {
    const title = sanitizeFilename(meta?.title || "bilibili-subtitle");
    const ids = [
      sanitizeFilename(meta?.bvid || (meta?.aid ? `av${meta.aid}` : "")),
      meta?.page ? `p${meta.page}` : "",
      meta?.cid ? `cid${meta.cid}` : "",
      formatFilenameTimestamp(),
    ].filter(Boolean);
    return sanitizeFilename([title, ...ids].join("_")) || `bilibili-subtitle_${formatFilenameTimestamp()}`;
  }

  function buildTextDocument(meta, transcript, subtitleInfo = null) {
    const lines = [
      `标题：${meta?.title || ""}`,
      `链接：${meta?.url || location.href}`,
      `BVID：${meta?.bvid || ""}`,
      `AID：${meta?.aid || ""}`,
      `CID：${meta?.cid || ""}`,
      `分P：${meta?.page || 1}`,
      `字幕来源：${subtitleInfo?.source || "未知"}`,
      `字幕语言：${subtitleInfo?.language || ""}`,
    ];
    if (subtitleInfo?.detail) {
      lines.push(`来源详情：${subtitleInfo.detail}`);
    }
    if (meta?.description) {
      lines.push(`简介：${meta.description}`);
    }
    lines.push("", "字幕：", transcript);
    return lines.join("\n");
  }

  async function downloadTranscript() {
    if (!currentTranscript) {
      await loadTranscript();
    }
    const text = buildTextDocument(currentMeta, currentTranscript, currentSubtitleInfo);
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${buildDownloadFilename(currentMeta)}.txt`;
    document.body.append(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setOutput(text);
    setStatus("TXT 已下载", "ok");
  }

  async function copyOutput() {
    const output = document.querySelector(`#${ROOT_ID} [data-role="output"]`);
    const text = output?.value || "";
    if (!text) {
      setStatus("没有可复制内容", "warn");
      return;
    }
    await navigator.clipboard.writeText(text);
    setStatus("已复制", "ok");
  }

  async function render() {
    if (document.getElementById(ROOT_ID)) return;

    const root = h("section", { id: ROOT_ID, className: "bili-summary" });
    const toggle = h("button", {
      className: "bili-summary__toggle",
      type: "button",
      title: "导出 B站字幕 TXT",
      text: "字",
      onClick: () => root.classList.toggle("is-open"),
    });

    const panel = h("div", { className: "bili-summary__panel" }, [
      h("div", { className: "bili-summary__header" }, [
        h("strong", { text: "B站字幕 TXT" }),
        h("button", {
          className: "bili-summary__icon",
          type: "button",
          title: "收起",
          text: "×",
          onClick: () => root.classList.remove("is-open"),
        }),
      ]),
      h("div", { className: "bili-summary__actions" }, [
        h("button", {
          type: "button",
          text: "读取字幕",
          onClick: () => loadTranscript().catch((err) => setStatus(err.message, "error")),
        }),
        h("button", {
          type: "button",
          text: "下载 TXT",
          onClick: () => downloadTranscript().catch((err) => setStatus(err.message, "error")),
        }),
        h("button", {
          type: "button",
          text: "复制",
          onClick: () => copyOutput().catch((err) => setStatus(err.message, "error")),
        }),
      ]),
      h("div", {
        className: "bili-summary__status",
        "data-role": "status",
        "data-kind": "idle",
        text: "待命",
      }),
      h("textarea", {
        className: "bili-summary__output",
        "data-role": "output",
        spellcheck: "false",
        placeholder: "字幕文本会显示在这里",
      }),
    ]);

    root.append(toggle, panel);
    document.documentElement.append(root);
  }

  render().catch((err) => console.warn("[bili-summary]", err));
})();
