import { describe, expect, it } from "vitest";
import { app } from "../src/index";
import { runJob } from "../src/job";

describe("ひな形の見本", () => {
  it("/api/health が ok を返す", async () => {
    const res = await app.request("/api/health");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });
  });

  it("定期実行は渡した時刻で動く", async () => {
    const result = await runJob({ now: new Date("2026-01-02T03:04:00Z") });
    expect(result.summary).toContain("2026-01-02T03:04");
  });
});
