const readerTab = Number(new URL(location.href).searchParams.get("tab"));
const form = document.getElementById("connect");
const button = document.getElementById("button");
const status = document.getElementById("status");
const reader = document.getElementById("reader");
const port = document.getElementById("port");
port.value = localStorage.getItem("bwPort") || "19225";

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

async function request(endpoint, path, payload) {
  const response = await fetch(endpoint + path, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
  return result;
}

function failed(error) {
  status.textContent = error.message === "Failed to fetch" ?
    "请先运行 python -m mmdl --source bookwalker --bw-serve，并确认服务端口。" : error.message;
  button.disabled = false;
}

async function start() {
  const endpoint = "http://127.0.0.1:" + Number(port.value);
  localStorage.setItem("bwPort", port.value);
  button.disabled = true;
  status.textContent = "正在启动当前卷的下载…";
  try {
    const session = await snapshot();
    if (session.error) throw new Error(session.error);
    reader.textContent = session.title;
    const job = await request(endpoint, "/download", session);
    let renewed = Date.now();

    async function poll(current) {
      status.textContent = current.message;
      if (current.state === "done" || current.state === "error") {
        button.disabled = false;
        return;
      }
      setTimeout(async () => {
        try {
          let session = null;
          if (current.state === "downloading" && Date.now() - renewed >= 20000) {
            session = await snapshot();
            if (session.error) throw new Error(session.error);
            renewed = Date.now();
          }
          await poll(await request(endpoint, "/jobs/" + current.job, {session}));
        } catch (error) {
          failed(error);
        }
      }, 2000);
    }
    await poll(job);
  } catch (error) {
    failed(error);
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  start();
});
