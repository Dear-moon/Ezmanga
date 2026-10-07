const readerTab = Number(new URL(location.href).searchParams.get("tab"));
const form = document.getElementById("connect");
const button = document.getElementById("button");
const status = document.getElementById("status");
const reader = document.getElementById("reader");
const port = document.getElementById("port");
const settings = document.getElementById("settings");
const source = document.getElementById("source");
const title = document.getElementById("title");
const chapters = document.getElementById("chapters");
const lang = document.getElementById("lang");
const quality = document.getElementById("quality");
const hint = document.getElementById("hint");
port.value = localStorage.getItem("servicePort") || "19225";

function selectionChanged() {
  chapters.disabled = source.value === "bookwalker" || source.value === "kobo";
  quality.disabled = source.value !== "mangamillion";
  chapters.placeholder = source.value === "tongli" ? "留空下载当前卷；可填 8 或 1-8" : "全部；可填 8 或 1-20";
  hint.textContent = {
    bookwalker: "请使用当前已打开的 BW 日本版漫画阅读器链接。",
    bilibili: "支持漫画详情或阅读器链接；阅读器链接默认只下载当前话。",
    tongli: "支持作品 ID、官网书籍或阅读器链接；留空范围默认下载当前卷。",
    pixivcomic: "作品链接下载可读连载；单话阅读器下载当前话；商店链接下载已购或免费卷。",
    lightnovel: "支持漫画 ID 或 https://www.lightnovel.app/manga/<id> 链接。",
    mangamillion: "支持作品链接或 original_title_id 数字 ID，使用当前浏览器设备授权。",
    kobo: "填写 content-id；在 auth.kobobooks.com/ActivateOnWeb 完成扫码登录后打开扩展。ACSM 仍使用本地 ADE 授权。",
  }[source.value] || "选择来源并填写要下载的作品。";
}

function setBusy(busy) {
  settings.disabled = busy;
  button.disabled = busy;
}

async function initialize() {
  const tab = await chrome.tabs.get(readerTab);
  reader.textContent = tab.title;
  const address = new URL(tab.url);
  source.value = {
    "viewer.bookwalker.jp": "bookwalker",
    "viewer-trial.bookwalker.jp": "bookwalker",
    "viewer-df.bookwalker.jp": "bookwalker",
    "manga.bilibili.com": "bilibili",
    "ebook.tongli.com.tw": "tongli",
    "comic.pixiv.net": "pixivcomic",
    "comic-store-viewer.pixiv.net": "pixivcomic",
    "www.lightnovel.app": "lightnovel",
    "lightnovel.app": "lightnovel",
    "mangamillion.shueisha.co.jp": "mangamillion",
    "www.kobo.com": "kobo",
    "kobo.com": "kobo",
    "auth.kobobooks.com": "kobo",
  }[address.hostname] || "";
  title.value = source.value === "kobo" ? "" : tab.url;
  selectionChanged();
}

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

async function siteSession(name) {
  const address = new URL(location.href);
  const sites = {
    bilibili: ["manga.bilibili.com"], tongli: ["ebook.tongli.com.tw"],
    pixivcomic: ["comic.pixiv.net", "comic-store-viewer.pixiv.net"],
    lightnovel: ["www.lightnovel.app", "lightnovel.app"],
    mangamillion: ["mangamillion.shueisha.co.jp"], kobo: ["auth.kobobooks.com"],
  };
  if (address.protocol !== "https:" || !sites[name].includes(address.hostname)) {
    return {error: "请在所选来源的已登录页面打开扩展"};
  }
  const session = {readerUrl: location.href, userAgent: navigator.userAgent};
  if (name === "tongli") {
    const user = window.firebase.auth().currentUser;
    if (!user) return {error: "请先在东立官网登录"};
    session.token = await user.getIdToken();
  } else if (name === "lightnovel") {
    session.token = await new Promise((resolve, reject) => {
      const request = indexedDB.open("LightNovelShelf");
      request.onupgradeneeded = () => {
        request.transaction.abort();
        reject(new Error("请先在轻书架登录"));
      };
      request.onerror = () => reject(new Error("无法读取轻书架登录态"));
      request.onsuccess = () => {
        const db = request.result;
        try {
          const read = db.transaction("USER_AUTHENTICATION").objectStore("USER_AUTHENTICATION").get("RefreshToken");
          read.onsuccess = () => {db.close(); resolve(read.result);};
          read.onerror = () => {db.close(); reject(read.error);};
        } catch (error) {
          db.close();
          reject(error);
        }
      };
    });
  } else if (name === "mangamillion") {
    session.token = localStorage.getItem("ACCESS_TOKEN");
  } else if (name === "kobo") {
    session.userKey = address.searchParams.get("userkey") ||
      (document.body.innerText.match(/userkey\s*:\s*([A-Za-z0-9._-]+)/i) || [])[1];
    if (!session.userKey) {
      const workflow = document.getElementById("activateOnWeb");
      if (!workflow) return {error: "请先打开 Kobo ActivateOnWeb 激活页面"};
      const token = document.querySelector('input[name="__RequestVerificationToken"]').value;
      const response = await fetch(workflow.dataset.pollEndpoint, {
        method: "POST", credentials: "include",
        body: new URLSearchParams({workflowid: workflow.dataset.workflowId, __RequestVerificationToken: token}),
      });
      if (!response.ok) throw new Error("无法读取 Kobo 激活结果");
      const activation = await response.json();
      if (activation.Status !== "Complete") return {error: "请先完成 Kobo ActivateOnWeb 页面上的扫码登录，再开始下载"};
      session.userKey = new URL(activation.RedirectUrl).searchParams.get("userkey");
    }
    if (!session.userKey) return {error: "Kobo 激活结果未包含 userkey，当前设备协议需要进一步实测"};
  }
  if (["tongli", "lightnovel", "mangamillion"].includes(name) && !session.token) {
    return {error: "未取得网页授权，请先登录或打开漫画阅读器"};
  }
  return session;
}

async function snapshot(name) {
  const [{result}] = await chrome.scripting.executeScript({
    target: {tabId: readerTab}, world: "MAIN",
    func: name === "bookwalker" ? readerSession : siteSession, args: [name],
  });
  if (result.error) return result;
  if (!["bookwalker", "bilibili", "pixivcomic"].includes(name)) return result;
  const address = new URL(result.readerUrl);
  const path = address.hostname === "viewer-trial.bookwalker.jp" ? "/trial-page/c" :
    address.hostname === "viewer-df.bookwalker.jp" ? "/browserWebApi4/c" : "/browserWebApi/c";
  const stores = await chrome.cookies.getAllCookieStores();
  const storeId = stores.find((store) => store.tabIds.includes(readerTab)).id;
  const urls = name === "bilibili" ? [result.readerUrl, "https://api.bilibili.com/"] :
    name === "pixivcomic" ? [result.readerUrl, "https://comic.pixiv.net/", "https://comic-store-viewer.pixiv.net/api/c"] :
    [result.readerUrl, address.origin + path];
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
    "请先运行 python -m mmdl --serve，并确认服务端口。" : error.message;
  setBusy(false);
}

async function start() {
  const endpoint = "http://127.0.0.1:" + Number(port.value);
  localStorage.setItem("servicePort", port.value);
  setBusy(true);
  status.textContent = "正在启动下载…";
  try {
    const name = source.value;
    const needsSession = !(name === "kobo" && title.value.toLowerCase().endsWith(".acsm"));
    const session = needsSession ? await snapshot(name) : null;
    if (session && session.error) throw new Error(session.error);
    const formats = ["cbz", "zip", "epub"].filter((format) => document.getElementById(format).checked);
    const job = await request(endpoint, "/download", {
      source: source.value, title: title.value, session,
      options: {chapters: chapters.disabled ? "" : chapters.value, lang: lang.value,
        quality: quality.disabled ? "" : quality.value, formats},
    });
    let renewed = Date.now();

    async function poll(current) {
      status.textContent = current.message;
      if (current.state === "done" || current.state === "error") {
        setBusy(false);
        return;
      }
      setTimeout(async () => {
        try {
          let session = null;
          if (["bookwalker", "tongli", "lightnovel", "mangamillion", "pixivcomic"].includes(name) &&
              current.state === "downloading" && Date.now() - renewed >= 20000) {
            session = await snapshot(name);
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
source.addEventListener("change", selectionChanged);
initialize().catch(failed);
