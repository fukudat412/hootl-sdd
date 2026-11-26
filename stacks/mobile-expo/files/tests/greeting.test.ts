import { describe, expect, it } from "@jest/globals";
import { greeting } from "@/lib/greeting";

describe("greeting（ひな形の見本）", () => {
  it("アプリ名を含む", () => {
    expect(greeting("テスト")).toContain("テスト");
  });
});
