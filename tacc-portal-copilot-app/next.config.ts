import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Produce a self-contained .next/standalone server (server.js + minimal
  // node_modules) so the Docker image can run the frontend without the full
  // dependency tree. See Dockerfile / supervisord.conf.
  output: "standalone",
};

export default nextConfig;
