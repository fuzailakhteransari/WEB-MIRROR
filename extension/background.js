const NATIVE_HOST_NAME = "com.webdesk.remote_host";

chrome.runtime.onMessageExternal.addListener((message, _sender, sendResponse) => {
  chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, message, (response) => {
    if (chrome.runtime.lastError) {
      sendResponse({ ok: false, error: chrome.runtime.lastError.message });
      return;
    }
    sendResponse(response);
  });
  return true; // Keep message channel open for async native response
});
