import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // next dev が AGENTS.md を自動で書き換えないようにする（hootl のレビュー役が読む規約を Next.js の更新で変えないため）
  agentRules: false,
};

export default nextConfig;
