// Browser -> Next.js (/api/*) -> Python backend. CORS দরকার হয় না।
const RADAR_API = process.env.RADAR_API ?? "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${RADAR_API}/api/:path*` }];
  },
};

export default nextConfig;
