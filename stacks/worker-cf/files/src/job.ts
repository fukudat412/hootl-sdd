/** 定期実行の本体（ひな形の見本。最初の機能を作ったら置き換えてよい） */
export type JobResult = { ok: boolean; summary: string };

export async function runJob({ now }: { now: Date }): Promise<JobResult> {
  return { ok: true, summary: `${now.toISOString()} に実行しました` };
}
