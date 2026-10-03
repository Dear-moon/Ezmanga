chrome.action.onClicked.addListener((tab) => {
  chrome.tabs.create({url: chrome.runtime.getURL("bridge.html") + "?tab=" + tab.id});
});
