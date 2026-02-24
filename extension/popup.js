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
    label.textContent = "Connected to MyBot";
  } else {
    dot.className = "dot off";
    label.textContent = "Waiting for MyBot…";
  }
});
