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
