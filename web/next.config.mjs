import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,

  // Pinned to this directory. Without it Next walks up looking for a lockfile, finds the one in
  // the home directory, and traces build output from there — which on Vercel means shipping the
  // wrong file set.
  outputFileTracingRoot: dirname(fileURLToPath(import.meta.url)),
};

export default nextConfig;
