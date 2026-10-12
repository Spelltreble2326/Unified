// popup.js — reads espn_s2 + SWID and sends them to the local Django app.

const BACKEND_URL = "http://localhost:8000/api/cookies";

const extractBtn = document.getElementById("extract");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const s2El = document.getElementById("s2");
const swidEl = document.getElementById("swid");

function setStatus(message, type = "") {
  statusEl.textContent = message;
  statusEl.className = "status" + (type ? " " + type : "");
}

// chrome.cookies.get returns a single cookie object (or null). Wrapped in a
// promise for async/await. The cookies API can read HttpOnly cookies, which is
// why this works where document.cookie would not.
function getCookie(name) {
  return chrome.cookies.get({ url: "https://www.espn.com", name });
}

extractBtn.addEventListener("click", async () => {
  extractBtn.disabled = true;
  setStatus("Reading cookies…");
  resultEl.classList.add("hidden");

  try {
    const [s2, swid] = await Promise.all([
      getCookie("espn_s2"),
      getCookie("SWID"),
    ]);

    if (!s2 || !swid) {
      setStatus(
        "Cookies not found. Make sure you're logged into ESPN.com in this browser, then try again.",
        "error"
      );
      return;
    }

    // Show the values in the popup.
    s2El.textContent = s2.value;
    swidEl.textContent = swid.value;
    resultEl.classList.remove("hidden");

    // Send to the backend.
    const res = await fetch(BACKEND_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ espn_s2: s2.value, swid: swid.value }),
    });

    if (!res.ok) {
      throw new Error(`Server responded ${res.status}`);
    }

    setStatus("Cookies extracted and sent to the app.", "success");
  } catch (err) {
    setStatus(`Failed to send to backend: ${err.message}`, "error");
  } finally {
    extractBtn.disabled = false;
  }
});
