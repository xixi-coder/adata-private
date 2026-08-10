(() => {
  const REQUEST_EVENT = "yt-subtitle-txt-request";
  const RESPONSE_EVENT = "yt-subtitle-txt-response";
  const CAPTION_EVENT = "yt-subtitle-txt-caption";
  const capturedCaptions = [];

  function isCaptionUrl(value) {
    return String(value || "").includes("/api/timedtext");
  }

  function captureCaption(url, body) {
    if (!isCaptionUrl(url) || !body) return;
    const text = typeof body === "string" ? body : JSON.stringify(body);
    if (!text || capturedCaptions.some((item) => item.url === String(url) && item.text === text)) return;
    const item = { url: String(url), text };
    capturedCaptions.push(item);
    if (capturedCaptions.length > 12) capturedCaptions.shift();
    document.dispatchEvent(new CustomEvent(CAPTION_EVENT, { detail: JSON.stringify(item) }));
  }

  const originalFetch = window.fetch;
  window.fetch = async function patchedFetch(...args) {
    const response = await originalFetch.apply(this, args);
    const url = typeof args[0] === "string" ? args[0] : args[0]?.url;
    if (isCaptionUrl(url)) {
      response.clone().text().then((text) => captureCaption(url, text)).catch(() => {});
    }
    return response;
  };

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function patchedOpen(method, url, ...rest) {
    this.__ytSubtitleTxtUrl = String(url || "");
    return originalOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function patchedSend(...args) {
    if (isCaptionUrl(this.__ytSubtitleTxtUrl)) {
      this.addEventListener("load", () => {
        try {
          const body = this.responseType === "json" ? this.response : this.responseText;
          captureCaption(this.__ytSubtitleTxtUrl, body);
        } catch {
          // Some response types do not expose responseText.
        }
      }, { once: true });
    }
    return originalSend.apply(this, args);
  };

  function getPlayerResponse() {
    const moviePlayer = document.querySelector("#movie_player");
    const candidates = [
      window.ytInitialPlayerResponse,
      typeof moviePlayer?.getPlayerResponse === "function" ? moviePlayer.getPlayerResponse() : null,
      document.querySelector("ytd-player")?.playerResponse,
    ];
    return candidates.find((item) => item?.videoDetails || item?.captions) || null;
  }

  function reply() {
    let detail = "";
    try {
      const response = getPlayerResponse();
      detail = JSON.stringify({ playerResponse: response, capturedCaptions });
    } catch {
      detail = "";
    }
    document.dispatchEvent(new CustomEvent(RESPONSE_EVENT, { detail }));
  }

  document.addEventListener(REQUEST_EVENT, reply);
  window.addEventListener("yt-navigate-finish", () => setTimeout(reply, 250));
})();
