// Prisma CLI の設定（Prisma 7 は .env を自動で読まないので dotenv で読む。設定済みの環境変数は上書きしない）
import "dotenv/config";
import { defineConfig } from "prisma/config";

export default defineConfig({
  schema: "prisma/schema.prisma",
  datasource: {
    // env("DATABASE_URL") だと未設定のとき generate まで失敗するので process.env から読む
    url: process.env.DATABASE_URL,
  },
});
