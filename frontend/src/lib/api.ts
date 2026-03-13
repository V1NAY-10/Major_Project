export const getApiUrl = () => {
  // Check if we have a manually configured API URL
  if (process.env.NEXT_PUBLIC_API_URL) {
    return process.env.NEXT_PUBLIC_API_URL;
  }

  // If in a browser, use the current hostname to derive the backend URL
  if (typeof window !== "undefined") {
    const hostname = window.location.hostname;
    return `http://${hostname}:8000`;
  }

  // Fallback to localhost for server-side rendering or as a last resort
  return "http://127.0.0.1:8000";
};

export const API_URL = getApiUrl();
