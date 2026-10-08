import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output -> small runtime image for docker-compose.
  output: "standalone",
  // Parent-directory lockfiles must not change the standalone server's layout.
  outputFileTracingRoot: __dirname,
};

export default nextConfig;
