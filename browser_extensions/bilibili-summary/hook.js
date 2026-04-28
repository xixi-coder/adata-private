(() => {
  const STORE_KEY = "__BILI_SUBTITLE_TXT_CAPTURED__";
  const EVENT_NAME = "bili-subtitle-txt-captured";

  function getStore() {
    if (!Array.isArray(window[STORE_KEY])) {
      Object.defineProperty(window, STORE_KEY, {
        value: [],
        configurable: true,
        writable: true,
      });
    }
    return window[STORE_KEY];
  }

  function looksLikeSubtitleUrl(url) {
    return /(?:ai[_-]?subtitle|subtitle|\/bfs\/ai_subtitle\/|\/bfs\/subtitle\/)/i.test(String(url || ""));
  }

  function looksLikeSubtitleJson(text) {
    if (!text || text.length < 20) return false;
    if (!/"body"\s*:\s*\[/.test(text)) return false;
    return /"content"\s*:/.test(text) || /"from"\s*:/.test(text);
  }

  function remember(url, text) {
    if (!looksLikeSubtitleJson(text)) return;
    const store = getStore();
    if (store.some((item) => item.url === url || item.text === text)) return;
    const item = {
      url: String(url || ""),
      text,
      capturedAt: Date.now(),
    };
    store.push(item);
    window.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: item }));
    window.postMessage({ source: "bili-subtitle-txt", type: "captured", item }, "*");
  }

  function inspectResponse(url, response) {
    if (!response || typeof response.clone !== "function") return;
    const contentType = response.headers?.get?.("content-type") || "";
    if (!looksLikeSubtitleUrl(url) && !/json|text/i.test(contentType)) return;
    response
      .clone()
      .text()
      .then((text) => remember(url, text))
      .catch(() => {});
  }

  const nativeFetch = window.fetch;
  if (typeof nativeFetch === "function") {
    window.fetch = function patchedFetch(input, init) {
      const url = typeof input === "string" ? input : input?.url || "";
      return nativeFetch.apply(this, arguments).then((response) => {
        inspectResponse(url, response);
        return response;
      });
    };
  }

  const NativeXHR = window.XMLHttpRequest;
  if (typeof NativeXHR === "function") {
    const nativeOpen = NativeXHR.prototype.open;
    const nativeSend = NativeXHR.prototype.send;
    NativeXHR.prototype.open = function patchedOpen(method, url) {
      this.__biliSubtitleUrl = url;
      return nativeOpen.apply(this, arguments);
    };
    NativeXHR.prototype.send = function patchedSend() {
      this.addEventListener("load", () => {
        const url = this.__biliSubtitleUrl || "";
        try {
          const text = typeof this.responseText === "string" ? this.responseText : "";
          if (!looksLikeSubtitleUrl(url) && !looksLikeSubtitleJson(text)) return;
          remember(url, text);
        } catch {
          // Some response types do not expose responseText.
        }
      });
      return nativeSend.apply(this, arguments);
    };
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    if (event.data?.source !== "bili-subtitle-txt" || event.data?.type !== "dump-request") return;
    window.postMessage(
      {
        source: "bili-subtitle-txt",
        type: "dump-response",
        items: getStore(),
      },
      "*",
    );
  });
})();
