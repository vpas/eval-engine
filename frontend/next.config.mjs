// Proxy /api/* to the FastAPI backend service (server-side, in-cluster). afterFiles so real route
// handlers (e.g. /api/me) take precedence; everything else proxies through with headers intact
// (so oauth2-proxy's X-Auth-Request-Email reaches the backend for created_by attribution).
const BACKEND = process.env.BACKEND_URL || "http://eval-engine-api:8077";

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  async rewrites() {
    return {
      afterFiles: [{ source: "/api/:path*", destination: `${BACKEND}/:path*` }],
    };
  },
};

export default nextConfig;
