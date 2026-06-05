// Proxy /api/* to the FastAPI backend service (server-side, in-cluster). afterFiles so real route
// handlers (e.g. /api/me) take precedence; everything else proxies through with headers intact
// (so oauth2-proxy's X-Auth-Request-Email reaches the backend for created_by attribution).
const BACKEND = process.env.BACKEND_URL || "http://eval-engine-api:8077";
// Inspect log viewer — proxied under /inspect/ (its assets/API are relative, so the prefix is
// stripped here and resolves under /inspect/). Served behind the same OIDC proxy as this app.
const VIEWER = process.env.VIEWER_URL || "http://inspect-view:7575";

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  async redirects() {
    // ensure the trailing slash so the viewer's relative URLs (./assets, api/logs) resolve under /inspect/
    return [{ source: "/inspect", destination: "/inspect/", permanent: false }];
  },
  async rewrites() {
    return {
      afterFiles: [
        { source: "/api/:path*", destination: `${BACKEND}/:path*` },
        { source: "/inspect/:path*", destination: `${VIEWER}/:path*` },
      ],
    };
  },
};

export default nextConfig;
