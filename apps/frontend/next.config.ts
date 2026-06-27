import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output -> small runtime image for docker-compose.
  output: "standalone",
};

export default nextConfig;
