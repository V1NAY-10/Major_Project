/**
 * Returns the backend API base URL.
 * Always call this function at the point of use (inside event handlers or
 * useEffect) rather than at module scope, so that `window` is available.
 */
export const getApiUrl = (): string => {
  // Env var takes priority (set NEXT_PUBLIC_API_URL in .env.local to override)
  if (process.env.NEXT_PUBLIC_API_URL) {
    return process.env.NEXT_PUBLIC_API_URL;
  }

  // Browser: derive from current hostname so it works on any machine/IP
  if (typeof window !== "undefined") {
    const hostname = window.location.hostname;
    // Force IPv4 for localhost to avoid IPv6 "connection refused" issues
    if (hostname === "localhost" || hostname === "127.0.0.1") {
      return "http://127.0.0.1:8000";
    }
    return `http://${hostname}:8000`;
  }

  // SSR fallback — network requests from the server should not happen for
  // client-only data like sessions. Return a placeholder that will be
  // overwritten once the component hydrates in the browser.
  return "http://127.0.0.1:8000";
};

/**
 * Module-level constant — safe to use inside "use client" components.
 * Do NOT use this in Server Components or at the top level of shared modules.
 */
export const API_URL = getApiUrl();

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

/** fetch against the backend, with a readable error when it is not running. */
export async function backendFetch(path: string, init: RequestInit = {}): Promise<Response> {
  try {
    return await fetch(`${API_URL}${path}`, init);
  } catch {
    throw new ApiError("Cannot reach the backend. Is it running on port 8000?", 0);
  }
}

async function errorFrom(res: Response): Promise<ApiError> {
  const data = await res.json().catch(() => ({}));
  const detail = typeof data?.detail === "string" ? data.detail : `Request failed (${res.status})`;
  return new ApiError(detail, res.status);
}

/** fetch + JSON with the backend's `{detail}` error shape turned into an ApiError. */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await backendFetch(path, init);
  if (!res.ok) throw await errorFrom(res);
  return (await res.json().catch(() => ({}))) as T;
}

/** Binary download (meshes, exports). */
export async function apiBlob(path: string): Promise<{ blob: Blob; filename: string | null }> {
  const res = await backendFetch(path);
  if (!res.ok) throw await errorFrom(res);
  const disposition = res.headers.get("Content-Disposition") ?? "";
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
  return { blob: await res.blob(), filename: match ? decodeURIComponent(match[1]) : null };
}

export const postJson = <T>(path: string, body?: unknown) =>
  apiFetch<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
