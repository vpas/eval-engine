// Proxy /api/* to the FastAPI backend service (server-side, in-cluster). afterFiles so real route
// handlers (e.g. /api/me) take precedence; everything else proxies through with headers intact
// (so oauth2-proxy's X-Forwarded-Email reaches the backend for created_by attribution — the rewrite
// forwards inbound headers verbatim; the backend reads it via auth_email() in eval_engine/api.py).
const BACKEND = process.env.BACKEND_URL || "http://eval-engine-api:8077";
// Inspect log viewer — proxied under /inspect/ (its assets/API are relative, so the prefix is
// stripped here and resolves under /inspect/). Served behind the same OIDC proxy as this app.
const VIEWER = process.env.VIEWER_URL || "http://inspect-view:7575";

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  // keep the trailing slash on /inspect/ (don't strip it) so the viewer's relative URLs (./assets,
  // api/logs) resolve under /inspect/. The header links to "/inspect/" directly — no redirect needed.
  skipTrailingSlashRedirect: true,
  async rewrites() {
    return {
      afterFiles: [
        // our app talks to /be/* (the Inspect viewer owns absolute /api/*, so we ceded it)
        { source: "/be/:path*", destination: `${BACKEND}/:path*` },
        // the Inspect viewer: html+assets under /inspect/, but its data calls are ABSOLUTE /api/*
        { source: "/inspect/:path*", destination: `${VIEWER}/:path*` },
        { source: "/api/:path*", destination: `${VIEWER}/api/:path*` },
      ],
    };
  },
};

export default nextConfig;
