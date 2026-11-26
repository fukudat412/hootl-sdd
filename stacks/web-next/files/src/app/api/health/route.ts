// 生存確認（起動確認ゲートが叩く）。消さない
export function GET() {
  return Response.json({ ok: true });
}
