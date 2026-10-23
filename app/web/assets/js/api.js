// JSON API client: session cookie + CSRF header, readable errors.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let onSignedOut = () => {};
export function whenSignedOut(fn) {
  onSignedOut = fn;
}

function csrfToken() {
  const match = document.cookie.match(/(?:^|; )jigsaw_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export async function api(path, { method = "GET", json, form } = {}) {
  const headers = {};
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form;
  }
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  let response;
  try {
    response = await fetch(path, { method, headers, body, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "network", "Can't reach the server. Check your connection and try again.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail || {};
    if (response.status === 401 && !path.startsWith("/api/auth/login")) onSignedOut();
    throw new ApiError(response.status, detail.code || "error", detail.message || `Request failed (${response.status}).`);
  }
  return data;
}
