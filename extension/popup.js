chrome.runtime.sendMessage({ type: "get_status" }, (resp) => {
  const dot = document.getElementById("dot");
  const label = document.getElementById("label");

  if (chrome.runtime.lastError || !resp) {
    dot.className = "dot off";
    label.textContent = "Extension error";
    return;
  }

  if (resp.connected) {
    dot.className = "dot on";
    label.textContent = "Connected to AI Browser";
  } else {
    dot.className = "dot off";
    label.textContent = "Waiting for dock app…";
  }
});
