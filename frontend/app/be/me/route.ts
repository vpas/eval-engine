import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

// The OIDC proxy (oauth2-proxy) injects the authenticated identity as a header on the upstream
// request. Surface it so the UI can show who's signed in.
export async function GET(req: NextRequest) {
  const email =
    req.headers.get("x-auth-request-email") ||
    req.headers.get("x-forwarded-email") ||
    req.headers.get("x-auth-request-user") ||
    null;
  return NextResponse.json({ email });
}
