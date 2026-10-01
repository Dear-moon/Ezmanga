const readerTab = Number(new URL(location.href).searchParams.get("tab"));
const form = document.getElementById("connect");
const button = document.getElementById("button");
const status = document.getElementById("status");
const reader = document.getElementById("reader");

async function readerSession() {
  const address = new URL(location.href);
  const viewers = ["viewer.bookwalker.jp", "viewer-trial.bookwalker.jp", "viewer-df.bookwalker.jp"];
  if (address.protocol !== "https:" || !viewers.includes(address.hostname) || !address.searchParams.has("cid")) {
    return {readerUrl: location.href, error: "请先在已登录浏览器中打开 BW 漫画阅读器"};
  }
  const session = {readerUrl: location.href, title: document.title, userAgent: navigator.userAgent, cr: null};
  if (address.hostname === "viewer-trial.bookwalker.jp") return session;
  try {
    const path = address.hostname === "viewer-df.bookwalker.jp" ? "/browserWebApi4/04/getLoader" : "/browserWebApi/03/getLoader";
    const response = await fetch(path, {credentials: "include"});
    if (!response.ok) throw new Error("loader");
    const text = await response.text();
    const name = text.match(/^(\w+)=function\(\)\{[\s\S]*?\};/m);
    if (!name) throw new Error("loader function");
    // Execute the website's loader as its own script without extension-side eval.
    const script = document.createElement("script");
    script.src = new URL(path, address.origin).href;
    try {
      await new Promise((resolve, reject) => {
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
      });
      session.cr = window[name[1]]();
    } finally {
      script.remove();
    }
    return session;
  } catch {
    return {readerUrl: location.href, error: "无法取得 BW 阅读授权，请重新打开阅读器后连接"};
  }
}

async function snapshot() {
  const [{result}] = await chrome.scripting.executeScript({
    target: {tabId: readerTab}, world: "MAIN", func: readerSession,
  });
  if (result.error) return result;
  const address = new URL(result.readerUrl);
  const path = address.hostname === "viewer-trial.bookwalker.jp" ? "/trial-page/c" :
    address.hostname === "viewer-df.bookwalker.jp" ? "/browserWebApi4/c" : "/browserWebApi/c";
  const stores = await chrome.cookies.getAllCookieStores();
  const storeId = stores.find((store) => store.tabIds.includes(readerTab)).id;
  const urls = [result.readerUrl, address.origin + path];
  const cookies = new Map();
  for (const url of urls) {
    for (const cookie of await chrome.cookies.getAll({url, storeId})) {
      const key = JSON.stringify([cookie.domain, cookie.path, cookie.name]);
      cookies.set(key, {
        name: cookie.name, value: cookie.value, domain: cookie.domain,
        path: cookie.path, secure: cookie.secure,
        expires: cookie.session ? -1 : cookie.expirationDate,
      });
    }
  }
  return {...result, cookies: [...cookies.values()]};
}

async function pair(endpoint, token) {
  const response = await fetch(endpoint + "/pair", {cache: "no-store"});
  if (!response.ok) throw new Error("下载器尚未启动或连接端口不正确");
  const {proof} = await response.json();
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", encoder.encode(token), {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const signed = await crypto.subtle.sign("HMAC", key, encoder.encode("ezmanga-bookwalker"));
  const expected = [...new Uint8Array(signed)].map((value) => value.toString(16).padStart(2, "0")).join("");
  if (proof !== expected) throw new Error("配对码不正确，请使用本次下载显示的配对码");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const endpoint = "http://127.0.0.1:" + Number(document.getElementById("port").value);
  const token = document.getElementById("code").value.trim();
  button.disabled = true;
  status.textContent = "正在连接…";
  try {
    await pair(endpoint, token);
  } catch (error) {
    status.textContent = error.message;
    button.disabled = false;
    return;
  }

  async function send() {
    try {
      const session = await snapshot();
      const response = await fetch(endpoint + "/session", {
        method: "POST", headers: {"Content-Type": "application/json", "Authorization": "Bearer " + token},
        body: JSON.stringify(session),
      });
      if (!response.ok) {
        const {error} = await response.json();
        throw new Error(error);
      }
      if (session.error) throw new Error(session.error);
      reader.textContent = session.title;
      status.textContent = "已连接，正在自动更新会话。下载完成后可以关闭本页。";
      setTimeout(send, 20000);
    } catch (error) {
      status.textContent = error.message === "Failed to fetch" ? "下载器已退出或连接中断，请重新运行命令并配对。" : error.message;
      button.disabled = false;
    }
  }
  await send();
});
